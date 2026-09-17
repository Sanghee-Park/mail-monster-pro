"""발송 작업·수신자 대기열 SQLite 영속 저장.

기존 sent_log / blacklist 테이블은 삭제하지 않고 CREATE TABLE IF NOT EXISTS 및
필요 시 ADD COLUMN 만 사용한다.

SMTP 비밀번호·토큰은 campaign_jobs 에 저장하지 않는다. 계정 식별자(task_key)와
비민감 설정만 스냅샷한다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

from business_hours import KST, as_kst
from smtp_credentials import public_smtp_snapshot, snapshot_contains_secrets

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SCHEDULED_PAUSE = "scheduled_pause"
JOB_USER_STOPPED = "user_stopped"
JOB_COMPLETED = "completed"
JOB_CANCELLED = "cancelled"
JOB_NEEDS_ATTENTION = "needs_attention"

ITEM_PENDING = "pending"
ITEM_SENDING = "sending"
ITEM_SENT = "sent"
ITEM_SKIPPED = "skipped"
ITEM_FAILED = "failed"
ITEM_NEEDS_REVIEW = "needs_review"

# 새 캠페인 생성·계정 삭제를 막는 상태 (사용자 확인 필요 포함)
ACTIVE_JOB_STATUSES = (JOB_QUEUED, JOB_RUNNING, JOB_SCHEDULED_PAUSE, JOB_NEEDS_ATTENTION)
# 자동복구로 재개하는 상태. needs_attention 은 사용자 확인 전까지 재개하지 않음.
RESUME_JOB_STATUSES = (JOB_QUEUED, JOB_RUNNING, JOB_SCHEDULED_PAUSE)
TERMINAL_ITEM_STATUSES = (ITEM_SENT, ITEM_SKIPPED, ITEM_FAILED, ITEM_NEEDS_REVIEW)
AUTO_SEND_BLOCK_ITEM_STATUSES = (ITEM_SENT, ITEM_SKIPPED, ITEM_FAILED, ITEM_NEEDS_REVIEW)


class DuplicateActiveCampaignError(Exception):
    def __init__(self, existing_job: Optional[dict] = None):
        self.existing_job = existing_job or {}
        jid = self.existing_job.get("job_id") or ""
        st = self.existing_job.get("status") or ""
        super().__init__(f"이미 활성 캠페인이 있습니다 ({st} {jid})")


def campaign_message_id(job_id: str, item_id: int, seq: int = 0) -> str:
    return f"<{job_id}.{int(item_id)}.{int(seq)}@mail-monster.pro>"


def _now_iso(dt: Optional[datetime] = None) -> str:
    return as_kst(dt).isoformat(timespec="seconds")


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(text: Optional[str], default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def ensure_campaign_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_jobs (
            job_id TEXT PRIMARY KEY,
            login_user_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            next_resume_at TEXT,
            subject TEXT,
            body TEXT,
            sender_name TEXT,
            task_key TEXT,
            provider TEXT,
            account_idx INTEGER,
            smtp_config_json TEXT,
            interval_label TEXT,
            prevent_dup INTEGER DEFAULT 1,
            apply_public_filter INTEGER DEFAULT 0,
            template_name TEXT,
            attachments_json TEXT,
            cid_json TEXT,
            total_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            runner_id TEXT,
            lease_until TEXT,
            attention_reason TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            recipient_json TEXT,
            email TEXT,
            company TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            error_message TEXT,
            processed_at TEXT,
            message_id TEXT,
            UNIQUE(job_id, seq)
        )
        """
    )
    job_cols = {c[1] for c in con.execute("PRAGMA table_info(campaign_jobs)").fetchall()}
    extras = {
        "sender_name": "TEXT",
        "task_key": "TEXT",
        "provider": "TEXT",
        "account_idx": "INTEGER",
        "smtp_config_json": "TEXT",
        "interval_label": "TEXT",
        "prevent_dup": "INTEGER DEFAULT 1",
        "apply_public_filter": "INTEGER DEFAULT 0",
        "template_name": "TEXT",
        "attachments_json": "TEXT",
        "cid_json": "TEXT",
        "total_count": "INTEGER DEFAULT 0",
        "success_count": "INTEGER DEFAULT 0",
        "skipped_count": "INTEGER DEFAULT 0",
        "failed_count": "INTEGER DEFAULT 0",
        "runner_id": "TEXT",
        "lease_until": "TEXT",
        "next_resume_at": "TEXT",
        "login_user_id": "TEXT NOT NULL DEFAULT ''",
        "attention_reason": "TEXT",
    }
    for name, decl in extras.items():
        if name not in job_cols:
            con.execute(f"ALTER TABLE campaign_jobs ADD COLUMN {name} {decl}")

    q_cols = {c[1] for c in con.execute("PRAGMA table_info(campaign_queue)").fetchall()}
    q_extras = {
        "recipient_json": "TEXT",
        "email": "TEXT",
        "company": "TEXT",
        "attempts": "INTEGER DEFAULT 0",
        "error_message": "TEXT",
        "processed_at": "TEXT",
        "status": "TEXT",
        "seq": "INTEGER",
        "job_id": "TEXT",
        "message_id": "TEXT",
    }
    for name, decl in q_extras.items():
        if name not in q_cols:
            con.execute(f"ALTER TABLE campaign_queue ADD COLUMN {name} {decl}")

    sent_cols = {c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()}
    if sent_cols and "message_id" not in sent_cols:
        try:
            con.execute("ALTER TABLE sent_log ADD COLUMN message_id TEXT")
        except sqlite3.OperationalError:
            pass

    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_jobs_user_status ON campaign_jobs(login_user_id, status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_jobs_status ON campaign_jobs(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_queue_job_status ON campaign_queue(job_id, status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_queue_job_seq ON campaign_queue(job_id, seq)")


def _scrub_smtp_json_row(raw: Optional[str], task_key: str = "") -> Optional[str]:
    cfg = _json_loads(raw, {}) or {}
    if not isinstance(cfg, dict):
        return _json_dumps(public_smtp_snapshot({}, task_key))
    if not snapshot_contains_secrets(cfg) and "task_key" in cfg:
        return None
    cleaned = public_smtp_snapshot(cfg, task_key or str(cfg.get("task_key") or ""))
    return _json_dumps(cleaned)


class CampaignStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        with self._connect() as con:
            ensure_campaign_schema(con)
            con.commit()
        self.scrub_stored_smtp_secrets()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def scrub_stored_smtp_secrets(self) -> int:
        """기존 캠페인 스냅샷에 남아 있을 수 있는 비밀번호를 제거한다. 값은 로그하지 않는다."""
        n = 0
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute("SELECT job_id, task_key, smtp_config_json FROM campaign_jobs").fetchall()
                for row in rows:
                    new_json = _scrub_smtp_json_row(row["smtp_config_json"], row["task_key"] or "")
                    if new_json is None:
                        continue
                    if new_json != (row["smtp_config_json"] or ""):
                        con.execute(
                            "UPDATE campaign_jobs SET smtp_config_json=? WHERE job_id=?",
                            (new_json, row["job_id"]),
                        )
                        n += 1
                con.commit()
            finally:
                con.close()
        return n

    def create_job(
        self,
        *,
        login_user_id: str,
        task_key: str,
        provider: str,
        account_idx: int,
        subject: str,
        body: str,
        sender_name: str,
        smtp_config: dict,
        interval_label: str,
        prevent_dup: bool,
        apply_public_filter: bool,
        template_name: str,
        attachments: dict,
        recipients: Iterable[dict],
        status: str = JOB_QUEUED,
        next_resume_at: Optional[str] = None,
        now: Optional[datetime] = None,
        job_id: Optional[str] = None,
        exclusive: bool = True,
    ) -> dict:
        rows = list(recipients)
        jid = job_id or uuid.uuid4().hex
        ts = _now_iso(now)
        files = list((attachments or {}).get("files") or [])
        imgs = dict((attachments or {}).get("imgs") or {})
        snapshot = public_smtp_snapshot(smtp_config or {}, task_key or "")
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                if exclusive:
                    existing = con.execute(
                        f"""
                        SELECT * FROM campaign_jobs
                        WHERE login_user_id=? AND status IN ({",".join("?" * len(ACTIVE_JOB_STATUSES))})
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (login_user_id or "", *ACTIVE_JOB_STATUSES),
                    ).fetchone()
                    if existing:
                        con.rollback()
                        raise DuplicateActiveCampaignError(dict(existing))
                con.execute(
                    """
                    INSERT INTO campaign_jobs(
                        job_id, login_user_id, status, created_at, updated_at, next_resume_at,
                        subject, body, sender_name, task_key, provider, account_idx,
                        smtp_config_json, interval_label, prevent_dup, apply_public_filter,
                        template_name, attachments_json, cid_json, total_count,
                        success_count, skipped_count, failed_count, attention_reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        jid,
                        login_user_id or "",
                        status,
                        ts,
                        ts,
                        next_resume_at,
                        subject or "",
                        body or "",
                        sender_name or "",
                        task_key or "",
                        provider or "",
                        int(account_idx or 0),
                        _json_dumps(snapshot),
                        interval_label or "",
                        1 if prevent_dup else 0,
                        1 if apply_public_filter else 0,
                        template_name or "",
                        _json_dumps({"files": files, "imgs": imgs}),
                        _json_dumps(imgs),
                        len(rows),
                        0,
                        0,
                        0,
                        None,
                    ),
                )
                for seq, row in enumerate(rows, 1):
                    row = row if isinstance(row, dict) else {}
                    email = str(row.get("이메일") or row.get("email") or "").strip()
                    company = str(row.get("업체명") or row.get("comp") or "")
                    con.execute(
                        """
                        INSERT INTO campaign_queue(job_id, seq, recipient_json, email, company, status, attempts)
                        VALUES (?,?,?,?,?,?,0)
                        """,
                        (jid, seq, _json_dumps(row), email, company, ITEM_PENDING),
                    )
                con.commit()
            finally:
                con.close()
        return self.get_job(jid)

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM campaign_jobs WHERE job_id=?", (job_id,)).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def list_jobs_for_user(self, login_user_id: str, statuses: Optional[Iterable[str]] = None) -> List[dict]:
        uid = login_user_id or ""
        with self._lock:
            con = self._connect()
            try:
                if statuses:
                    st = list(statuses)
                    placeholders = ",".join("?" * len(st))
                    rows = con.execute(
                        f"SELECT * FROM campaign_jobs WHERE login_user_id=? AND status IN ({placeholders}) ORDER BY created_at",
                        [uid, *st],
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT * FROM campaign_jobs WHERE login_user_id=? ORDER BY created_at",
                        (uid,),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def list_active_jobs(self, login_user_id: Optional[str] = None) -> List[dict]:
        with self._lock:
            con = self._connect()
            try:
                if login_user_id is None:
                    rows = con.execute(
                        f"SELECT * FROM campaign_jobs WHERE status IN ({','.join('?' * len(ACTIVE_JOB_STATUSES))}) ORDER BY created_at",
                        ACTIVE_JOB_STATUSES,
                    ).fetchall()
                else:
                    rows = con.execute(
                        f"SELECT * FROM campaign_jobs WHERE login_user_id=? AND status IN ({','.join('?' * len(ACTIVE_JOB_STATUSES))}) ORDER BY created_at",
                        (login_user_id or "", *ACTIVE_JOB_STATUSES),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def list_resumable_jobs(self, login_user_id: str) -> List[dict]:
        return self.list_jobs_for_user(login_user_id, RESUME_JOB_STATUSES)

    def list_needs_attention_jobs(self, login_user_id: str) -> List[dict]:
        return self.list_jobs_for_user(login_user_id, (JOB_NEEDS_ATTENTION,))

    def has_active_job_for_user(self, login_user_id: str, task_key: Optional[str] = None) -> bool:
        with self._lock:
            con = self._connect()
            try:
                if task_key:
                    row = con.execute(
                        f"SELECT 1 FROM campaign_jobs WHERE login_user_id=? AND task_key=? AND status IN ({','.join('?' * len(ACTIVE_JOB_STATUSES))}) LIMIT 1",
                        (login_user_id or "", task_key, *ACTIVE_JOB_STATUSES),
                    ).fetchone()
                else:
                    row = con.execute(
                        f"SELECT 1 FROM campaign_jobs WHERE login_user_id=? AND status IN ({','.join('?' * len(ACTIVE_JOB_STATUSES))}) LIMIT 1",
                        (login_user_id or "", *ACTIVE_JOB_STATUSES),
                    ).fetchone()
                return row is not None
            finally:
                con.close()

    def find_resumable_job(self, login_user_id: str, task_key: str) -> Optional[dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    f"""
                    SELECT * FROM campaign_jobs
                    WHERE login_user_id=? AND task_key=? AND status IN ({",".join("?" * len(RESUME_JOB_STATUSES))})
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (login_user_id or "", task_key, *RESUME_JOB_STATUSES),
                ).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def find_blocking_job(self, login_user_id: str) -> Optional[dict]:
        jobs = self.list_active_jobs(login_user_id)
        return jobs[0] if jobs else None

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = fields.get("updated_at") or _now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [job_id]
        with self._lock:
            con = self._connect()
            try:
                con.execute(f"UPDATE campaign_jobs SET {cols} WHERE job_id=?", vals)
                con.commit()
            finally:
                con.close()

    def set_status(
        self,
        job_id: str,
        status: str,
        *,
        next_resume_at: Optional[str] = None,
        now: Optional[datetime] = None,
        clear_runner: bool = False,
        attention_reason: Optional[str] = None,
    ) -> None:
        fields: Dict[str, Any] = {"status": status, "updated_at": _now_iso(now)}
        if next_resume_at is not None:
            fields["next_resume_at"] = next_resume_at
        elif status != JOB_SCHEDULED_PAUSE:
            fields["next_resume_at"] = None
        if attention_reason is not None:
            fields["attention_reason"] = attention_reason
        elif status not in (JOB_NEEDS_ATTENTION,):
            fields["attention_reason"] = None
        if clear_runner or status in (
            JOB_COMPLETED,
            JOB_CANCELLED,
            JOB_USER_STOPPED,
            JOB_SCHEDULED_PAUSE,
            JOB_NEEDS_ATTENTION,
        ):
            fields["runner_id"] = None
            fields["lease_until"] = None
        self.update_job(job_id, **fields)

    def set_needs_attention(self, job_id: str, reason: str, *, now: Optional[datetime] = None) -> None:
        self.set_status(
            job_id,
            JOB_NEEDS_ATTENTION,
            now=now,
            clear_runner=True,
            attention_reason=reason or "사용자 확인이 필요합니다.",
        )

    def update_attachments(self, job_id: str, attachments: dict) -> None:
        files = list((attachments or {}).get("files") or [])
        imgs = dict((attachments or {}).get("imgs") or {})
        self.update_job(
            job_id,
            attachments_json=_json_dumps({"files": files, "imgs": imgs}),
            cid_json=_json_dumps(imgs),
        )

    def refresh_counts(self, job_id: str) -> dict:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT status, COUNT(*) AS n FROM campaign_queue WHERE job_id=? GROUP BY status",
                    (job_id,),
                ).fetchall()
                counts = {r["status"]: r["n"] for r in rows}
                total = sum(counts.values())
                success = counts.get(ITEM_SENT, 0)
                skipped = counts.get(ITEM_SKIPPED, 0)
                failed = counts.get(ITEM_FAILED, 0)
                con.execute(
                    """
                    UPDATE campaign_jobs
                    SET total_count=?, success_count=?, skipped_count=?, failed_count=?, updated_at=?
                    WHERE job_id=?
                    """,
                    (total, success, skipped, failed, _now_iso(), job_id),
                )
                con.commit()
            finally:
                con.close()
        job = self.get_job(job_id) or {}
        job["pending_count"] = self.count_by_status(job_id, ITEM_PENDING)
        job["sending_count"] = self.count_by_status(job_id, ITEM_SENDING)
        job["needs_review_count"] = self.count_by_status(job_id, ITEM_NEEDS_REVIEW)
        return job

    def count_by_status(self, job_id: str, status: str) -> int:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT COUNT(*) AS n FROM campaign_queue WHERE job_id=? AND status=?",
                    (job_id, status),
                ).fetchone()
                return int(row["n"] if row else 0)
            finally:
                con.close()

    def remaining_count(self, job_id: str) -> int:
        """자동 발송 대상(pending). sending/needs_review 는 포함하지 않는다."""
        return self.count_by_status(job_id, ITEM_PENDING)

    def unfinished_count(self, job_id: str) -> int:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT COUNT(*) AS n FROM campaign_queue WHERE job_id=? AND status IN (?,?,?)",
                    (job_id, ITEM_PENDING, ITEM_SENDING, ITEM_NEEDS_REVIEW),
                ).fetchone()
                return int(row["n"] if row else 0)
            finally:
                con.close()

    def next_pending(self, job_id: str) -> Optional[dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    """
                    SELECT * FROM campaign_queue
                    WHERE job_id=? AND status=?
                    ORDER BY seq ASC LIMIT 1
                    """,
                    (job_id, ITEM_PENDING),
                ).fetchone()
                if not row:
                    return None
                item = dict(row)
                item["recipient"] = _json_loads(item.get("recipient_json"), {})
                return item
            finally:
                con.close()

    def claim_next_pending(self, job_id: str, *, message_id: Optional[str] = None) -> Optional[dict]:
        """pending → sending 을 한 트랜잭션에서 수행해 이중 발송을 막는다."""
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    """
                    SELECT * FROM campaign_queue
                    WHERE job_id=? AND status=?
                    ORDER BY seq ASC LIMIT 1
                    """,
                    (job_id, ITEM_PENDING),
                ).fetchone()
                if not row:
                    con.commit()
                    return None
                item = dict(row)
                mid = message_id or campaign_message_id(job_id, int(item["id"]), int(item.get("seq") or 0))
                cur = con.execute(
                    """
                    UPDATE campaign_queue
                    SET status=?, message_id=?, processed_at=?
                    WHERE id=? AND status=?
                    """,
                    (ITEM_SENDING, mid, _now_iso(), item["id"], ITEM_PENDING),
                )
                if not cur.rowcount:
                    con.rollback()
                    return None
                con.commit()
                item["status"] = ITEM_SENDING
                item["message_id"] = mid
                item["recipient"] = _json_loads(item.get("recipient_json"), {})
                return item
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    pass
                return None
            finally:
                con.close()

    def get_item(self, item_id: int) -> Optional[dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM campaign_queue WHERE id=?", (item_id,)).fetchone()
                if not row:
                    return None
                item = dict(row)
                item["recipient"] = _json_loads(item.get("recipient_json"), {})
                return item
            finally:
                con.close()

    def list_items_by_status(self, job_id: str, status: str) -> List[dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT * FROM campaign_queue WHERE job_id=? AND status=? ORDER BY seq",
                    (job_id, status),
                ).fetchall()
                out = []
                for row in rows:
                    item = dict(row)
                    item["recipient"] = _json_loads(item.get("recipient_json"), {})
                    out.append(item)
                return out
            finally:
                con.close()

    def reset_interrupted_sending(self, job_id: Optional[str] = None) -> int:
        """하위호환: SMTP를 한 번도 시도하지 않은 sending 만 pending 으로 되돌린다."""
        result = self.reconcile_interrupted_sending(job_id)
        return int(result.get("reset") or 0)

    def reconcile_interrupted_sending(
        self,
        job_id: Optional[str] = None,
        sent_lookup: Optional[Callable[[dict], bool]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> dict:
        """sending 상태로 남은 항목: sent_log 확인 시 sent, 미확인이면 needs_review.
        attempts=0 이고 message_id 만 예약된 채 SMTP 전 종료면 pending 복귀.
        """
        if job_id:
            job = self.get_job(job_id) or {}
            rid = (job.get("runner_id") or "").strip()
            lease_until = job.get("lease_until") or ""
            if rid and lease_until:
                try:
                    if as_kst(datetime.fromisoformat(lease_until)) > as_kst(now):
                        return {"reset": 0, "sent": 0, "review": 0, "skipped_live": 1}
                except Exception:
                    pass
        stats = {"reset": 0, "sent": 0, "review": 0}
        with self._lock:
            con = self._connect()
            try:
                if job_id:
                    rows = con.execute(
                        "SELECT * FROM campaign_queue WHERE job_id=? AND status=?",
                        (job_id, ITEM_SENDING),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT * FROM campaign_queue WHERE status=?",
                        (ITEM_SENDING,),
                    ).fetchall()
                touched_jobs = set()
                for row in rows:
                    item = dict(row)
                    item["recipient"] = _json_loads(item.get("recipient_json"), {})
                    jid = item.get("job_id")
                    touched_jobs.add(jid)
                    attempts = int(item.get("attempts") or 0)
                    mid = str(item.get("message_id") or "").strip()
                    found = False
                    if sent_lookup:
                        try:
                            found = bool(sent_lookup(item))
                        except Exception:
                            found = False
                    if found:
                        con.execute(
                            "UPDATE campaign_queue SET status=?, error_message=?, processed_at=? WHERE id=?",
                            (ITEM_SENT, "복구: sent_log에서 성공 확인", _now_iso(now), item["id"]),
                        )
                        stats["sent"] += 1
                        continue
                    if attempts <= 0 and not mid:
                        con.execute(
                            "UPDATE campaign_queue SET status=? WHERE id=? AND status=?",
                            (ITEM_PENDING, item["id"], ITEM_SENDING),
                        )
                        stats["reset"] += 1
                        continue
                    con.execute(
                        """
                        UPDATE campaign_queue
                        SET status=?, error_message=?, processed_at=?
                        WHERE id=?
                        """,
                        (
                            ITEM_NEEDS_REVIEW,
                            "SMTP 접수 여부가 확인되지 않아 자동 재발송하지 않습니다. 수신함·발송 로그를 확인한 뒤 처리하세요.",
                            _now_iso(now),
                            item["id"],
                        ),
                    )
                    stats["review"] += 1
                con.commit()
            finally:
                con.close()
        if job_id:
            touched = [job_id]
        else:
            touched = list(touched_jobs) if stats["review"] or stats["sent"] or stats["reset"] else []
        for jid in touched:
            if not jid:
                continue
            self.refresh_counts(jid)
            if self.count_by_status(jid, ITEM_NEEDS_REVIEW) > 0:
                job = self.get_job(jid) or {}
                if job.get("status") in RESUME_JOB_STATUSES or job.get("status") == JOB_RUNNING:
                    n = self.count_by_status(jid, ITEM_NEEDS_REVIEW)
                    self.set_needs_attention(
                        jid,
                        f"발송 결과를 확정할 수 없는 수신자가 {n}건 있습니다. 중복 발송을 막기 위해 자동 재발송하지 않습니다.",
                        now=now,
                    )
        return stats

    def mark_item(
        self,
        item_id: int,
        status: str,
        *,
        error_message: Optional[str] = None,
        inc_attempts: bool = False,
        now: Optional[datetime] = None,
        message_id: Optional[str] = None,
        reset_attempts: bool = False,
    ) -> None:
        with self._lock:
            con = self._connect()
            try:
                sets = ["status=?", "error_message=?", "processed_at=?"]
                vals: List[Any] = [status, error_message, _now_iso(now)]
                if message_id is not None:
                    sets.append("message_id=?")
                    vals.append(message_id)
                if inc_attempts:
                    sets.append("attempts=attempts+1")
                elif reset_attempts:
                    sets.append("attempts=0")
                vals.append(item_id)
                con.execute(
                    f"UPDATE campaign_queue SET {', '.join(sets)} WHERE id=?",
                    vals,
                )
                con.commit()
            finally:
                con.close()

    def try_claim_job(
        self,
        job_id: str,
        owner: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: int = 180,
        allowed_statuses: Iterable[str] = RESUME_JOB_STATUSES,
    ) -> bool:
        now_dt = as_kst(now)
        lease = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        now_s = now_dt.isoformat(timespec="seconds")
        allowed = list(allowed_statuses)
        placeholders = ",".join("?" * len(allowed))
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT runner_id, lease_until, status FROM campaign_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if not row:
                    con.rollback()
                    return False
                if row["status"] not in allowed:
                    con.rollback()
                    return False
                rid = (row["runner_id"] or "").strip()
                lease_until = row["lease_until"] or ""
                expired = True
                if rid and lease_until:
                    try:
                        expired = as_kst(datetime.fromisoformat(lease_until)) <= now_dt
                    except Exception:
                        expired = True
                if rid and rid != owner and not expired:
                    con.rollback()
                    return False
                con.execute(
                    f"""
                    UPDATE campaign_jobs
                    SET runner_id=?, lease_until=?, updated_at=?
                    WHERE job_id=? AND status IN ({placeholders})
                    """,
                    [owner, lease, now_s, job_id, *allowed],
                )
                con.commit()
                return True
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    pass
                return False
            finally:
                con.close()

    def has_live_lease(self, job_id: str, *, now: Optional[datetime] = None, owner: Optional[str] = None) -> bool:
        """다른 실행자가 유효한 lease를 갖고 있으면 True. 자기 owner이면 False."""
        now_dt = as_kst(now)
        job = self.get_job(job_id) or {}
        rid = (job.get("runner_id") or "").strip()
        if not rid:
            return False
        if owner and rid == owner:
            return False
        lease_until = job.get("lease_until") or ""
        if not lease_until:
            return False
        try:
            return as_kst(datetime.fromisoformat(lease_until)) > now_dt
        except Exception:
            return False

    def heartbeat(self, job_id: str, owner: str, *, now: Optional[datetime] = None, lease_seconds: int = 180) -> None:
        now_dt = as_kst(now)
        lease = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE campaign_jobs SET lease_until=?, updated_at=? WHERE job_id=? AND runner_id=?",
                    (lease, _now_iso(now_dt), job_id, owner),
                )
                con.commit()
            finally:
                con.close()

    def release_job(self, job_id: str, owner: Optional[str] = None) -> None:
        with self._lock:
            con = self._connect()
            try:
                if owner:
                    con.execute(
                        "UPDATE campaign_jobs SET runner_id=NULL, lease_until=NULL, updated_at=? WHERE job_id=? AND runner_id=?",
                        (_now_iso(), job_id, owner),
                    )
                else:
                    con.execute(
                        "UPDATE campaign_jobs SET runner_id=NULL, lease_until=NULL, updated_at=? WHERE job_id=?",
                        (_now_iso(), job_id),
                    )
                con.commit()
            finally:
                con.close()

    def job_snapshot_attachments(self, job: dict) -> dict:
        data = _json_loads(job.get("attachments_json"), {}) or {}
        files = list(data.get("files") or [])
        imgs = data.get("imgs") if isinstance(data.get("imgs"), dict) else _json_loads(job.get("cid_json"), {}) or {}
        if not isinstance(imgs, dict):
            imgs = {}
        return {"files": files, "imgs": imgs}

    def job_smtp_config(self, job: dict) -> dict:
        cfg = _json_loads(job.get("smtp_config_json"), {}) or {}
        return cfg if isinstance(cfg, dict) else {}

    def any_active_on_pc(self) -> bool:
        return bool(self.list_active_jobs())

    def stats_dict(self, job_id: str) -> dict:
        job = self.refresh_counts(job_id) or {}
        remaining = int(job.get("pending_count") or 0)
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "next_resume_at": job.get("next_resume_at"),
            "attention_reason": job.get("attention_reason") or "",
            "total": int(job.get("total_count") or 0),
            "success": int(job.get("success_count") or 0),
            "skipped": int(job.get("skipped_count") or 0),
            "failed": int(job.get("failed_count") or 0),
            "remaining": remaining,
            "needs_review": int(job.get("needs_review_count") or 0),
        }
