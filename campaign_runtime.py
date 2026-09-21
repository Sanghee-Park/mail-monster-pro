"""캠페인 발송 루프 (SMTP/UI와 분리, 테스트에서 mock 가능)."""
from __future__ import annotations

import time
import uuid
from typing import Callable, Optional, Tuple

from business_hours import BusinessHours, as_kst
from campaign_attachments import format_missing_files_reason, missing_attachment_paths
from campaign_store import (
    ITEM_FAILED,
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING,
    ITEM_SENDING,
    ITEM_SENT,
    ITEM_SKIPPED,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_NEEDS_ATTENTION,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
    CampaignStore,
)

PrepareResult = Tuple[str, object]
# prepare -> ("skipped", reason) | ("ready", payload) | ("error", message) | ("halt", message)
SendOnce = Callable[..., Tuple[bool, str]]
GateStop = Callable[[], bool]


def evaluate_send_gate(
    *,
    job: dict,
    item: dict,
    hours: BusinessHours,
    is_user_stopped: GateStop,
    is_cancelled: GateStop,
    now=None,
    require_running: bool = True,
    worker: Optional[dict] = None,
) -> Optional[str]:
    """발송/재시도/간격 대기 직전 공통 검사. None이면 진행, 문자열이면 중단 사유."""
    if is_cancelled():
        return JOB_CANCELLED
    if (job or {}).get("status") == JOB_CANCELLED:
        return JOB_CANCELLED
    if is_user_stopped():
        return JOB_USER_STOPPED
    wstatus = (worker or {}).get("status") if worker else None
    if wstatus == JOB_USER_STOPPED:
        return JOB_USER_STOPPED
    if wstatus == JOB_NEEDS_ATTENTION:
        return JOB_NEEDS_ATTENTION
    if require_running:
        if worker is not None:
            if wstatus not in (JOB_RUNNING, JOB_QUEUED, JOB_SCHEDULED_PAUSE):
                return "not_running" if not wstatus else wstatus
        elif (job or {}).get("status") != JOB_RUNNING:
            return "not_running"
    st = (item or {}).get("status")
    if st in (ITEM_SENT, ITEM_SKIPPED, ITEM_FAILED, ITEM_NEEDS_REVIEW):
        return "already_processed"
    if not hours.is_send_allowed(now):
        return JOB_SCHEDULED_PAUSE
    return None


class CampaignRunner:
    def __init__(
        self,
        store: CampaignStore,
        hours: BusinessHours,
        *,
        prepare_fn: Callable[[dict, dict], PrepareResult],
        send_once_fn: SendOnce,
        is_user_stopped: GateStop,
        is_cancelled: GateStop,
        interval_seconds_fn: Callable[[], int],
        now_fn=None,
        sleep_fn=None,
        on_log=None,
        on_progress=None,
        max_retries: int = 3,
        wait_poll_seconds: int = 1,
        owner: Optional[str] = None,
        sent_lookup_fn=None,
        attachments_from_job=None,
        worker_id: Optional[str] = None,
    ):
        self.store = store
        self.hours = hours
        self.prepare_fn = prepare_fn
        self.send_once_fn = send_once_fn
        self.is_user_stopped = is_user_stopped
        self.is_cancelled = is_cancelled
        self.interval_seconds_fn = interval_seconds_fn
        self.now_fn = now_fn or hours.now
        self.sleep_fn = sleep_fn or time.sleep
        self.on_log = on_log or (lambda m: None)
        self.on_progress = on_progress or (lambda stats: None)
        self.max_retries = max_retries
        self.wait_poll_seconds = wait_poll_seconds
        self.owner = owner or uuid.uuid4().hex
        self.smtp_calls = 0
        self.sent_lookup_fn = sent_lookup_fn
        self.attachments_from_job = attachments_from_job or (lambda job: store.job_snapshot_attachments(job))
        self.worker_id = worker_id

    def now(self):
        return as_kst(self.now_fn())

    def _worker(self) -> Optional[dict]:
        if not self.worker_id:
            return None
        return self.store.get_worker(self.worker_id)

    def _progress(self, job_id: str) -> None:
        self.on_progress(self.store.stats_dict(job_id, worker_id=self.worker_id))

    def _gate(self, job_id: str, item: dict) -> Optional[str]:
        job = self.store.get_job(job_id) or {}
        live = self.store.get_item(item["id"]) if item.get("id") else item
        return evaluate_send_gate(
            job=job,
            item=live or item,
            hours=self.hours,
            is_user_stopped=self.is_user_stopped,
            is_cancelled=self.is_cancelled,
            now=self.now(),
            worker=self._worker(),
        )

    def _pause_scheduled(self, job_id: str) -> str:
        nxt = self.hours.next_send_window_start(self.now())
        iso = nxt.isoformat(timespec="seconds")
        self.store.pause_workers_scheduled(job_id, next_resume_at=iso, now=self.now())
        self.on_log(self.hours.format_resume_text(nxt))
        self._progress(job_id)
        return JOB_SCHEDULED_PAUSE

    def _apply_terminal_user(self, job_id: str, status: str) -> str:
        self.store.reconcile_interrupted_sending(job_id, self.sent_lookup_fn, now=self.now())
        if status == JOB_CANCELLED:
            self.store.cancel_campaign_workers(job_id, now=self.now())
        elif self.worker_id:
            self.store.set_worker_status(self.worker_id, status, now=self.now(), clear_runner=True)
        else:
            self.store.set_status(job_id, status, now=self.now(), clear_runner=True)
        self._progress(job_id)
        return status

    def _halt_worker(self, job_id: str, reason: str) -> str:
        if self.worker_id:
            self.store.set_needs_attention_worker(self.worker_id, reason, now=self.now())
        else:
            self.store.set_needs_attention(job_id, reason, now=self.now())
        self.on_log(reason)
        self._progress(job_id)
        return JOB_NEEDS_ATTENTION

    def _halt_campaign(self, job_id: str, reason: str) -> str:
        self.store.set_needs_attention(job_id, reason, now=self.now())
        for w in self.store.list_workers(job_id):
            if w.get("status") in (JOB_RUNNING, JOB_QUEUED, JOB_SCHEDULED_PAUSE):
                self.store.set_worker_status(
                    w["worker_id"],
                    JOB_NEEDS_ATTENTION,
                    now=self.now(),
                    clear_runner=True,
                    attention_reason=reason,
                    sync_job=False,
                )
        self.on_log(reason)
        self._progress(job_id)
        return JOB_NEEDS_ATTENTION

    def wait_for_send_window(self, job_id: str) -> str:
        """scheduled_pause 유지. 업무시간이 되면 running으로 전환.
        사용자 정지/취소 시 해당 상태 반환.
        """
        while True:
            if self.is_cancelled():
                return self._apply_terminal_user(job_id, JOB_CANCELLED)
            if self.is_user_stopped():
                return self._apply_terminal_user(job_id, JOB_USER_STOPPED)
            job = self.store.get_job(job_id) or {}
            st = job.get("status")
            worker = self._worker() or {}
            wst = worker.get("status")
            if st in (JOB_CANCELLED, JOB_COMPLETED):
                return st
            if wst in (JOB_USER_STOPPED, JOB_NEEDS_ATTENTION, JOB_COMPLETED, JOB_CANCELLED):
                return wst
            if self.hours.is_send_allowed(self.now()):
                self.store.resume_workers_for_window(job_id, now=self.now())
                self._progress(job_id)
                return JOB_RUNNING
            if st != JOB_SCHEDULED_PAUSE:
                self._pause_scheduled(job_id)
            self.sleep_fn(self.wait_poll_seconds)

    def _wait_interval(self, job_id: str) -> Optional[str]:
        total = max(0, int(self.interval_seconds_fn() or 0))
        for _ in range(total):
            job = self.store.get_job(job_id) or {}
            dummy = {"id": None, "status": ITEM_PENDING}
            reason = evaluate_send_gate(
                job=job,
                item=dummy,
                hours=self.hours,
                is_user_stopped=self.is_user_stopped,
                is_cancelled=self.is_cancelled,
                now=self.now(),
                worker=self._worker(),
            )
            if reason == JOB_USER_STOPPED:
                return self._apply_terminal_user(job_id, JOB_USER_STOPPED)
            if reason == JOB_CANCELLED:
                return self._apply_terminal_user(job_id, JOB_CANCELLED)
            if reason == JOB_SCHEDULED_PAUSE:
                return self._pause_scheduled(job_id)
            if reason == JOB_NEEDS_ATTENTION:
                return JOB_NEEDS_ATTENTION
            self._heartbeat(job_id)
            self.sleep_fn(1)
        return None

    def _heartbeat(self, job_id: str) -> None:
        if self.worker_id:
            self.store.heartbeat_worker(self.worker_id, self.owner, now=self.now())
        else:
            self.store.heartbeat(job_id, self.owner, now=self.now())

    def _complete_if_done(self, job_id: str) -> bool:
        pending = self.store.count_by_status(job_id, ITEM_PENDING)
        sending = self.store.count_by_status(job_id, ITEM_SENDING)
        review = self.store.count_by_status(job_id, ITEM_NEEDS_REVIEW)
        if pending > 0 or sending > 0:
            return False
        self.store.refresh_counts(job_id)
        if review > 0:
            self._halt_campaign(
                job_id,
                f"발송 결과를 확정할 수 없는 수신자가 {review}건 있어 자동 재발송하지 않습니다.",
            )
            return True
        self.store.set_status(job_id, JOB_COMPLETED, now=self.now(), clear_runner=True)
        for w in self.store.list_workers(job_id):
            if w.get("status") not in (JOB_CANCELLED, JOB_USER_STOPPED, JOB_NEEDS_ATTENTION):
                self.store.update_worker(w["worker_id"], status=JOB_COMPLETED, runner_id=None, lease_until=None)
        self.on_log("🏁 모든 작업 종료")
        self._progress(job_id)
        return True

    def _check_attachments(self, job_id: str) -> Optional[str]:
        job = self.store.get_job(job_id) or {}
        attach = self.attachments_from_job(job)
        missing = missing_attachment_paths(attach)
        if missing:
            return self._halt_campaign(job_id, format_missing_files_reason(missing))
        return None

    def _bump(self, kind: str) -> None:
        self.store.bump_worker_result(self.worker_id, kind)

    def process_one(self, job_id: str, item: dict) -> Optional[str]:
        """한 수신자 처리.
        반환: None(계속), 'did_smtp', 또는 작업 상태 문자열.
        """
        reason = self._gate(job_id, item)
        if reason == JOB_USER_STOPPED:
            if (item.get("status") == ITEM_SENDING) and int(item.get("attempts") or 0) <= 0:
                self.store.mark_item(item["id"], ITEM_PENDING)
            return self._apply_terminal_user(job_id, JOB_USER_STOPPED)
        if reason == JOB_CANCELLED:
            if (item.get("status") == ITEM_SENDING) and int(item.get("attempts") or 0) <= 0:
                self.store.mark_item(item["id"], ITEM_PENDING)
            return self._apply_terminal_user(job_id, JOB_CANCELLED)
        if reason == JOB_SCHEDULED_PAUSE:
            if (item.get("status") == ITEM_SENDING) and int(item.get("attempts") or 0) <= 0:
                self.store.mark_item(item["id"], ITEM_PENDING)
            return self._pause_scheduled(job_id)
        if reason == JOB_NEEDS_ATTENTION:
            return JOB_NEEDS_ATTENTION
        if reason == "already_processed":
            return None
        if reason == "not_running":
            return job_get_status_safe(self.store, job_id, self.worker_id)

        if (item.get("status") or ITEM_PENDING) != ITEM_SENDING:
            self.store.mark_item(item["id"], ITEM_SENDING, message_id=item.get("message_id"))
        halt_att = self._check_attachments(job_id)
        if halt_att:
            self.store.mark_item(item["id"], ITEM_PENDING, error_message="첨부파일 확인 필요")
            return halt_att

        job = self.store.get_job(job_id) or {}
        try:
            kind, payload = self.prepare_fn(job, item)
        except Exception as e:
            self.store.mark_item(item["id"], ITEM_FAILED, error_message=str(e))
            self._bump("failed")
            self.store.refresh_counts(job_id)
            self._progress(job_id)
            return None

        if kind == "skipped":
            skip_reason = str(payload or "")
            self.store.mark_item(
                item["id"],
                ITEM_SKIPPED,
                error_message=skip_reason,
                skip_reason=skip_reason,
            )
            self._bump("skipped")
            self.store.refresh_counts(job_id)
            self._progress(job_id)
            return None
        if kind == "halt":
            self.store.mark_item(item["id"], ITEM_PENDING, error_message=str(payload or ""))
            return self._halt_worker(job_id, str(payload or "사용자 확인이 필요합니다."))
        if kind == "error":
            self.store.mark_item(item["id"], ITEM_FAILED, error_message=str(payload or ""))
            self._bump("failed")
            self.store.refresh_counts(job_id)
            self._progress(job_id)
            return None

        if isinstance(payload, dict) and payload.get("msg") is not None and item.get("message_id"):
            try:
                payload["msg"]["Message-ID"] = item["message_id"]
            except Exception:
                pass

        # queue 생성 후 새로 추가된 blacklist도 실제 SMTP 연결 직전에 다시 차단한다.
        send_email = (
            (payload or {}).get("email")
            if isinstance(payload, dict)
            else item.get("email")
        ) or item.get("email")
        if self.store.is_blacklisted(send_email):
            self.store.mark_item(
                item["id"],
                ITEM_SKIPPED,
                error_message="blacklist",
                skip_reason="blacklist",
                now=self.now(),
            )
            self._bump("skipped")
            self.store.refresh_counts(job_id)
            self._progress(job_id)
            return None

        reservation_id = ""
        if bool(job.get("prevent_dup")) and isinstance(payload, dict):
            reservation = self.store.reserve_send(
                login_user_id=job.get("login_user_id") or "",
                email=send_email or "",
                content_hash=payload.get("body_hash") or payload.get("content_hash") or "",
                job_id=job_id,
                item_id=int(item["id"]),
                task_key=job.get("task_key") or "",
                message_id=item.get("message_id") or "",
                now=self.now(),
            )
            if not reservation.get("acquired"):
                self.store.mark_item(
                    item["id"],
                    ITEM_SKIPPED,
                    error_message="duplicate",
                    skip_reason="duplicate",
                    now=self.now(),
                )
                self._bump("skipped")
                self.store.refresh_counts(job_id)
                self._progress(job_id)
                return None
            reservation_id = reservation.get("reservation_id") or ""

        attempts_done = int(item.get("attempts") or 0)
        last_err = ""
        while attempts_done < self.max_retries:
            live = self.store.get_item(item["id"]) or item
            reason = self._gate(job_id, live)
            if reason == JOB_USER_STOPPED:
                if attempts_done <= 0:
                    self.store.mark_item(item["id"], ITEM_PENDING, error_message=last_err)
                return self._apply_terminal_user(job_id, JOB_USER_STOPPED)
            if reason == JOB_CANCELLED:
                if attempts_done <= 0:
                    self.store.mark_item(item["id"], ITEM_PENDING, error_message=last_err)
                return self._apply_terminal_user(job_id, JOB_CANCELLED)
            if reason == JOB_SCHEDULED_PAUSE:
                if attempts_done <= 0:
                    self.store.mark_item(item["id"], ITEM_PENDING, error_message=last_err)
                return self._pause_scheduled(job_id)
            if reason == JOB_NEEDS_ATTENTION:
                return JOB_NEEDS_ATTENTION
            if reason == "already_processed":
                return None

            if self.store.is_blacklisted(send_email):
                self.store.mark_item(
                    item["id"],
                    ITEM_SKIPPED,
                    error_message="blacklist",
                    skip_reason="blacklist",
                    now=self.now(),
                )
                self.store.set_reservation_status(
                    reservation_id,
                    "released",
                    error_message="blacklist",
                    now=self.now(),
                )
                self._bump("skipped")
                self.store.refresh_counts(job_id)
                self._progress(job_id)
                return None

            self.smtp_calls += 1
            ok, err = self.send_once_fn(payload, job, live)
            attempts_done += 1
            self.store.mark_item(
                item["id"],
                ITEM_SENDING,
                error_message=err if not ok else "",
                inc_attempts=True,
                now=self.now(),
                message_id=item.get("message_id"),
            )
            if ok:
                self.store.mark_item(item["id"], ITEM_SENT, now=self.now(), message_id=item.get("message_id"))
                self.store.set_reservation_status(reservation_id, "sent", now=self.now())
                self._bump("sent")
                self.store.refresh_counts(job_id)
                self._progress(job_id)
                return "did_smtp"
            last_err = err or ""
            low = last_err.lower()
            if "535" in last_err or "authentication" in low or "자격증명" in last_err:
                self.store.mark_item(item["id"], ITEM_PENDING, error_message=last_err)
                self.store.set_reservation_status(
                    reservation_id,
                    "released",
                    error_message=last_err,
                    now=self.now(),
                )
                return self._halt_worker(
                    job_id,
                    "SMTP 자격증명을 확인할 수 없어 발송을 중단했습니다. 계정 설정에서 비밀번호를 확인하세요.",
                )
            if attempts_done < self.max_retries:
                for _ in range(min(2 ** attempts_done, 8)):
                    reason = self._gate(job_id, self.store.get_item(item["id"]) or live)
                    if reason in (JOB_USER_STOPPED, JOB_CANCELLED, JOB_SCHEDULED_PAUSE):
                        if attempts_done <= 0:
                            self.store.mark_item(item["id"], ITEM_PENDING, error_message=last_err)
                        if reason == JOB_SCHEDULED_PAUSE:
                            return self._pause_scheduled(job_id)
                        return self._apply_terminal_user(job_id, reason)
                    self.sleep_fn(1)

        low = (last_err or "").lower()
        ambiguous = any(
            token in low
            for token in (
                "timeout",
                "timed out",
                "connection reset",
                "server disconnected",
                "broken pipe",
                "connection aborted",
                "remote end closed",
            )
        )
        if ambiguous:
            self.store.mark_item(
                item["id"],
                ITEM_NEEDS_REVIEW,
                error_message="SMTP 접수 여부가 불명확하여 자동 재발송하지 않습니다.",
                now=self.now(),
            )
            self.store.set_reservation_status(
                reservation_id,
                "review",
                error_message=last_err,
                now=self.now(),
            )
            return self._halt_worker(
                job_id,
                "SMTP 접수 여부가 불명확한 항목이 있어 자동 재발송하지 않습니다.",
            )
        self.store.mark_item(item["id"], ITEM_FAILED, error_message=last_err, now=self.now())
        self.store.set_reservation_status(
            reservation_id,
            "failed",
            error_message=last_err,
            now=self.now(),
        )
        self._bump("failed")
        self.store.refresh_counts(job_id)
        self._progress(job_id)
        return "did_smtp"

    def _claim_or_lock(self, job_id: str) -> bool:
        if self.worker_id:
            if not self.store.try_claim_worker(self.worker_id, self.owner, now=self.now()):
                self.on_log("다른 실행 인스턴스가 이미 이 계정을 발송 중입니다.")
                return False
            return True
        if not self.store.try_claim_job(job_id, self.owner, now=self.now()):
            self.on_log("다른 실행 인스턴스가 이미 이 작업을 발송 중입니다.")
            return False
        return True

    def _release(self, job_id: str) -> None:
        if self.worker_id:
            self.store.release_worker(self.worker_id, self.owner)
        else:
            self.store.release_job(job_id, self.owner)

    def run(self, job_id: str, *, wait_off_hours: bool = True) -> str:
        job = self.store.get_job(job_id)
        if not job:
            return "missing"
        worker = self._worker()
        if worker and worker.get("status") in (JOB_USER_STOPPED, JOB_CANCELLED, JOB_COMPLETED):
            return worker["status"]
        if job.get("status") in (JOB_USER_STOPPED, JOB_CANCELLED, JOB_COMPLETED):
            return job["status"]
        if self.worker_id:
            if self.store.has_live_worker_lease(self.worker_id, now=self.now(), owner=self.owner):
                self.on_log("다른 실행 인스턴스가 이미 이 계정을 발송 중입니다.")
                return "locked"
        elif self.store.has_live_lease(job_id, now=self.now(), owner=self.owner):
            self.on_log("다른 실행 인스턴스가 이미 이 작업을 발송 중입니다.")
            return "locked"

        rec = self.store.reconcile_interrupted_sending(job_id, self.sent_lookup_fn, now=self.now())
        job = self.store.get_job(job_id) or job
        worker = self._worker() or worker
        if worker and worker.get("status") == JOB_NEEDS_ATTENTION:
            self.on_log(worker.get("attention_reason") or "사용자 확인이 필요한 항목이 있어 자동 재발송하지 않습니다.")
            self._progress(job_id)
            return JOB_NEEDS_ATTENTION
        if rec.get("review"):
            return JOB_NEEDS_ATTENTION
        if job.get("status") == JOB_NEEDS_ATTENTION:
            pending = self.store.count_by_status(job_id, ITEM_PENDING)
            if pending <= 0 or not self.worker_id:
                self.on_log(job.get("attention_reason") or "사용자 확인이 필요한 항목이 있어 자동 재발송하지 않습니다.")
                self._progress(job_id)
                return JOB_NEEDS_ATTENTION

        halt_att = self._check_attachments(job_id)
        if halt_att:
            return halt_att

        if not self.hours.is_send_allowed(self.now()):
            self._pause_scheduled(job_id)
            if not wait_off_hours:
                return JOB_SCHEDULED_PAUSE
            resumed = self.wait_for_send_window(job_id)
            if resumed != JOB_RUNNING:
                return resumed
        else:
            if self.worker_id:
                wst = (self._worker() or {}).get("status")
                if wst in (JOB_QUEUED, JOB_SCHEDULED_PAUSE, JOB_RUNNING, None):
                    self.store.set_worker_status(self.worker_id, JOB_RUNNING, next_resume_at=None, now=self.now())
            if job.get("status") != JOB_RUNNING:
                self.store.set_status(job_id, JOB_RUNNING, next_resume_at=None, now=self.now())

        if not self._claim_or_lock(job_id):
            return "locked"

        need_wait_before_smtp = False
        try:
            while True:
                if self.is_cancelled():
                    return self._apply_terminal_user(job_id, JOB_CANCELLED)
                if self.is_user_stopped():
                    return self._apply_terminal_user(job_id, JOB_USER_STOPPED)
                job = self.store.get_job(job_id) or {}
                worker = self._worker()
                if worker and worker.get("status") in (JOB_USER_STOPPED, JOB_NEEDS_ATTENTION, JOB_CANCELLED, JOB_COMPLETED):
                    return worker["status"]
                if job.get("status") == JOB_CANCELLED:
                    return JOB_CANCELLED
                if job.get("status") == JOB_NEEDS_ATTENTION and self.store.count_by_status(job_id, ITEM_PENDING) <= 0:
                    return JOB_NEEDS_ATTENTION
                if job.get("status") != JOB_RUNNING:
                    if job.get("status") == JOB_SCHEDULED_PAUSE:
                        if not wait_off_hours:
                            return JOB_SCHEDULED_PAUSE
                        resumed = self.wait_for_send_window(job_id)
                        if resumed != JOB_RUNNING:
                            return resumed
                        if not self._claim_or_lock(job_id):
                            return "locked"
                        continue
                    if self.worker_id and job.get("status") in (JOB_QUEUED, JOB_NEEDS_ATTENTION):
                        pass
                    else:
                        return job.get("status") or "not_running"

                if not self.hours.is_send_allowed(self.now()):
                    self._pause_scheduled(job_id)
                    if not wait_off_hours:
                        return JOB_SCHEDULED_PAUSE
                    resumed = self.wait_for_send_window(job_id)
                    if resumed != JOB_RUNNING:
                        return resumed
                    if not self._claim_or_lock(job_id):
                        return "locked"
                    continue

                halt_att = self._check_attachments(job_id)
                if halt_att:
                    return halt_att

                item = self.store.claim_next_pending(
                    job_id,
                    worker_id=self.worker_id,
                    task_key=(worker or {}).get("task_key") if worker else None,
                )
                if item is None:
                    if self._complete_if_done(job_id):
                        return job_get_status_safe(self.store, job_id, self.worker_id)
                    self.sleep_fn(self.wait_poll_seconds)
                    continue

                remaining_before = self.store.remaining_count(job_id) + 1

                if need_wait_before_smtp:
                    paused = self._wait_interval(job_id)
                    if paused:
                        if int(item.get("attempts") or 0) <= 0:
                            self.store.mark_item(item["id"], ITEM_PENDING)
                        if paused == JOB_SCHEDULED_PAUSE and wait_off_hours:
                            resumed = self.wait_for_send_window(job_id)
                            if resumed != JOB_RUNNING:
                                return resumed
                            if not self._claim_or_lock(job_id):
                                return "locked"
                            need_wait_before_smtp = True
                            continue
                        return paused

                result = self.process_one(job_id, item)
                if result in (JOB_USER_STOPPED, JOB_CANCELLED, JOB_NEEDS_ATTENTION):
                    return result
                if result == JOB_SCHEDULED_PAUSE:
                    if not wait_off_hours:
                        return JOB_SCHEDULED_PAUSE
                    resumed = self.wait_for_send_window(job_id)
                    if resumed != JOB_RUNNING:
                        return resumed
                    if not self._claim_or_lock(job_id):
                        return "locked"
                    continue
                if result == "did_smtp":
                    need_wait_before_smtp = True
                if remaining_before <= 1 or self.store.remaining_count(job_id) == 0:
                    if self._complete_if_done(job_id):
                        return job_get_status_safe(self.store, job_id, self.worker_id)
                self._heartbeat(job_id)
        finally:
            self._release(job_id)


def job_get_status_safe(store: CampaignStore, job_id: str, worker_id: Optional[str] = None) -> str:
    if worker_id:
        worker = store.get_worker(worker_id) or {}
        if worker.get("status"):
            return worker["status"]
    job = store.get_job(job_id) or {}
    return job.get("status") or "missing"
