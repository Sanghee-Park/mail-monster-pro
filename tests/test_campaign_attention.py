import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_hours import KST, BusinessHours
from campaign_attachments import missing_attachment_paths
from campaign_attention import (
    ACTION_CANCEL_JOB,
    ACTION_MARK_SENT,
    ACTION_RESEND,
    ACTION_SKIP,
    ATTENTION_BUTTON_LABELS,
    ATTENTION_REVIEW_ACTIONS,
    RESEND_WARNING,
    ReviewActionError,
    cancel_campaign,
    list_review_items,
    maybe_release_attention,
    replace_job_attachments,
    resolve_review_item,
)
from campaign_runtime import CampaignRunner
from campaign_store import (
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING,
    ITEM_SENT,
    ITEM_SKIPPED,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_NEEDS_ATTENTION,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    CampaignStore,
)


def kst(y, m, d, hh=10, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST)


class CampaignAttentionTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.td.name) / "sent_history.db")
        self.store = CampaignStore(self.db)
        self.clock = kst(2026, 9, 16, 10, 0)
        self.hours = BusinessHours(extra_dates=set(), now_fn=lambda: self.clock)

    def tearDown(self):
        self.td.cleanup()

    def _job(self, n=2, extra=None):
        rows = [{"업체명": f"c{i}", "이메일": f"a{i}@ex.com"} for i in range(1, n + 1)]
        kw = dict(
            login_user_id="alice",
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "pw"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=JOB_RUNNING,
            now=self.clock,
        )
        if extra:
            kw.update(extra)
        return self.store.create_job(**kw)

    def _mark_reviews(self, job, n=None):
        items = []
        for it in self.store.list_items_by_status(job["job_id"], ITEM_PENDING):
            if n is not None and len(items) >= n:
                break
            self.store.mark_item(it["id"], ITEM_NEEDS_REVIEW, error_message="결과 불확실")
            items.append(self.store.get_item(it["id"]))
        self.store.set_needs_attention(job["job_id"], "확인 필요")
        return items

    def _release(self, job_id, send_allowed=True):
        nxt = self.hours.next_send_window_start().isoformat(timespec="seconds")
        return maybe_release_attention(
            self.store,
            job_id,
            send_allowed=send_allowed,
            next_resume_at=nxt,
            now=self.clock,
        )

    def test_ui_contract_labels_and_no_auto_resend(self):
        src = (Path(__file__).resolve().parents[1] / "main_ui.py").read_text(encoding="utf-8")
        for action, label in ATTENTION_BUTTON_LABELS.items():
            self.assertIn(label, src, label)
        for label in ("발송 완료로 처리", "다시 발송", "건너뛰기", "캠페인 취소", "첨부파일 다시 지정"):
            self.assertIn(label, src)
        self.assertIn("RESEND_WARNING", src)
        self.assertIn("중복 발송 경고", src)
        self.assertIn("누락된 파일 경로", src)
        self.assertEqual(
            set(ATTENTION_REVIEW_ACTIONS),
            {ACTION_MARK_SENT, ACTION_RESEND, ACTION_SKIP},
        )
        self.assertNotIn("자동 재발송", "".join(ATTENTION_BUTTON_LABELS.values()))

    def test_mark_sent_does_not_resend(self):
        job = self._job(n=2)
        a, b = self._mark_reviews(job)
        sent = []
        resolve_review_item(self.store, a["id"], ACTION_MARK_SENT, now=self.clock)
        self.assertEqual(self.store.get_item(a["id"])["status"], ITEM_SENT)
        self.assertEqual(self.store.get_item(b["id"])["status"], ITEM_NEEDS_REVIEW)
        st = self._release(job["job_id"], send_allowed=True)
        self.assertEqual(st, JOB_NEEDS_ATTENTION)
        runner = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=lambda p, j, i: (sent.append(p["email"]) or True, ""),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=lambda s: None,
            owner="rev",
        )
        self.assertEqual(runner.run(job["job_id"], wait_off_hours=False), JOB_NEEDS_ATTENTION)
        self.assertEqual(sent, [])

    def test_resend_requires_confirm_and_only_that_item(self):
        job = self._job(n=2)
        a, b = self._mark_reviews(job)
        with self.assertRaises(ReviewActionError):
            resolve_review_item(self.store, a["id"], ACTION_RESEND, resend_confirmed=False)
        self.assertEqual(self.store.get_item(a["id"])["status"], ITEM_NEEDS_REVIEW)
        resolve_review_item(self.store, a["id"], ACTION_RESEND, resend_confirmed=True, now=self.clock)
        self.assertEqual(self.store.get_item(a["id"])["status"], ITEM_PENDING)
        self.assertEqual(self.store.get_item(a["id"])["attempts"], 0)
        self.assertEqual(self.store.get_item(b["id"])["status"], ITEM_NEEDS_REVIEW)
        self.assertEqual(self._release(job["job_id"]), JOB_NEEDS_ATTENTION)

    def test_skip_requires_note(self):
        job = self._job(n=1)
        (item,) = self._mark_reviews(job)
        with self.assertRaises(ReviewActionError):
            resolve_review_item(self.store, item["id"], ACTION_SKIP, note="  ")
        resolve_review_item(self.store, item["id"], ACTION_SKIP, note="수신 거부 확인", now=self.clock)
        self.assertEqual(self.store.get_item(item["id"])["status"], ITEM_SKIPPED)
        self.assertIn("수신 거부 확인", self.store.get_item(item["id"])["error_message"])

    def test_cancel_campaign_button_action(self):
        job = self._job(n=1)
        self._mark_reviews(job)
        self.assertEqual(cancel_campaign(self.store, job["job_id"], now=self.clock), JOB_CANCELLED)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_CANCELLED)
        self.assertEqual(self._release(job["job_id"]), JOB_CANCELLED)
        self.assertEqual(ACTION_CANCEL_JOB, "cancel_job")

    def test_all_reviews_resolved_business_hours_running(self):
        job = self._job(n=2)
        a, b = self._mark_reviews(job)
        resolve_review_item(self.store, a["id"], ACTION_MARK_SENT, now=self.clock)
        resolve_review_item(self.store, b["id"], ACTION_RESEND, resend_confirmed=True, now=self.clock)
        self.assertEqual(list_review_items(self.store, job["job_id"]), [])
        st = self._release(job["job_id"], send_allowed=True)
        self.assertEqual(st, JOB_RUNNING)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_RUNNING)

    def test_all_reviews_resolved_off_hours_scheduled_pause(self):
        self.clock = kst(2026, 9, 16, 19, 0)
        job = self._job(n=2)
        a, b = self._mark_reviews(job)
        resolve_review_item(self.store, a["id"], ACTION_SKIP, note="중복", now=self.clock)
        resolve_review_item(self.store, b["id"], ACTION_RESEND, resend_confirmed=True, now=self.clock)
        st = self._release(job["job_id"], send_allowed=False)
        self.assertEqual(st, JOB_SCHEDULED_PAUSE)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_SCHEDULED_PAUSE)

    def test_unresolved_review_blocks_resume(self):
        job = self._job(n=1)
        self._mark_reviews(job)
        self.assertEqual(self._release(job["job_id"], send_allowed=True), JOB_NEEDS_ATTENTION)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_NEEDS_ATTENTION)

    def test_all_resolved_without_pending_completes(self):
        job = self._job(n=1)
        (item,) = self._mark_reviews(job)
        resolve_review_item(self.store, item["id"], ACTION_MARK_SENT, now=self.clock)
        self.assertEqual(self._release(job["job_id"], send_allowed=True), JOB_COMPLETED)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_COMPLETED)

    def test_missing_attachment_shows_path_and_blocks_send(self):
        missing = str(Path(self.td.name) / "gone.pdf")
        job = self._job(extra={"attachments": {"files": [missing], "imgs": {}}})
        self.assertEqual(self._release(job["job_id"]), JOB_NEEDS_ATTENTION)
        reason = self.store.get_job(job["job_id"])["attention_reason"]
        self.assertIn(missing, reason)
        sent = []
        runner = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=lambda p, j, i: (sent.append("x") or True, ""),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=lambda s: None,
            owner="att",
        )
        self.assertEqual(runner.run(job["job_id"], wait_off_hours=False), JOB_NEEDS_ATTENTION)
        self.assertEqual(sent, [])

    def test_rebind_attachments_updates_snapshot_and_resumes_only_if_present(self):
        missing = str(Path(self.td.name) / "gone.pdf")
        real = Path(self.td.name) / "ok.pdf"
        real.write_bytes(b"%PDF")
        job = self._job(extra={"attachments": {"files": [missing], "imgs": {"logo": missing}}})
        attach, still = replace_job_attachments(self.store, job["job_id"], files=[str(real)])
        self.assertEqual(attach["files"], [str(real)])
        self.assertTrue(still)
        self.assertEqual(self._release(job["job_id"]), JOB_NEEDS_ATTENTION)
        attach, still = replace_job_attachments(
            self.store, job["job_id"], files=[str(real)], imgs={"logo": str(real)}
        )
        self.assertFalse(missing_attachment_paths(attach))
        live = self.store.job_snapshot_attachments(self.store.get_job(job["job_id"]))
        self.assertEqual(live["files"], [str(real)])
        self.assertEqual(live["imgs"]["logo"], str(real))
        self.assertEqual(self._release(job["job_id"], send_allowed=True), JOB_RUNNING)

    def test_cannot_resume_without_files_even_if_reviews_cleared(self):
        missing = str(Path(self.td.name) / "gone.pdf")
        job = self._job(n=1, extra={"attachments": {"files": [missing], "imgs": {}}})
        (item,) = self._mark_reviews(job)
        resolve_review_item(self.store, item["id"], ACTION_MARK_SENT, now=self.clock)
        self.assertEqual(self._release(job["job_id"], send_allowed=True), JOB_NEEDS_ATTENTION)
        self.assertIn(missing, self.store.get_job(job["job_id"])["attention_reason"])


if __name__ == "__main__":
    unittest.main()
