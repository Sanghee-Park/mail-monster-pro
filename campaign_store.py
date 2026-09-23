"""발송 작업·수신자 대기열 SQLite 영속 저장.

기존 sent_log / blacklist 테이블은 삭제하지 않고 CREATE TABLE IF NOT EXISTS 및
필요 시 ADD COLUMN 만 사용한다.

SMTP 비밀번호·토큰은 campaign_jobs 에 저장하지 않는다. 계정 식별자(task_key)와
비민감 설정만 스냅샷한다.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

from business_hours import KST, as_kst
from json_atomic import StorageWriteError, clear_readonly
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
ITEM_CANCELLED = "cancelled"

# 새 캠페인 생성·계정 삭제를 막는 상태 (사용자 확인 필요 포함)
ACTIVE_JOB_STATUSES = (
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_NEEDS_ATTENTION,
)
BLOCKING_JOB_STATUSES = (*ACTIVE_JOB_STATUSES, JOB_USER_STOPPED)
# 자동복구로 재개하는 상태. needs_attention 은 사용자 확인 전까지 재개하지 않음.
RESUME_JOB_STATUSES = (JOB_QUEUED, JOB_RUNNING, JOB_SCHEDULED_PAUSE)
TERMINAL_ITEM_STATUSES = (ITEM_SENT, ITEM_SKIPPED, ITEM_FAILED, ITEM_NEEDS_REVIEW, ITEM_CANCELLED)
AUTO_SEND_BLOCK_ITEM_STATUSES = TERMINAL_ITEM_STATUSES


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


_EMAIL_RE = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")


def normalize_email(value: Any) -> str:
    text = str(value or "").strip().lower()
    matches = _EMAIL_RE.findall(text)
    return (matches[0] if matches else text).strip().lower()


def _blacklist_match(email: str, token: str) -> bool:
    email = normalize_email(email)
    token = str(token or "").strip().lower()
    if not email or not token or "@" not in email:
        return False
    if token.startswith("@"):
        domain = token[1:]
        return email.endswith("@" + domain) or email.endswith("." + domain)
    if token.startswith("*."):
        domain = token[2:]
        return email.endswith("@" + domain) or email.endswith("." + domain)
    if "@" not in token and "." in token:
        return email.endswith("@" + token) or email.endswith("." + token)
    return email == token


def recipient_fingerprint(row: dict) -> str:
    """개인정보 원문을 로그에 쓰지 않고 recipient set을 비교할 때 사용하는 해시."""
    email = normalize_email((row or {}).get("이메일") or (row or {}).get("email"))
    return hashlib.sha256(email.encode("utf-8")).hexdigest() if email else ""


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
        "generation": "INTEGER NOT NULL DEFAULT 1",
        "legacy_pool": "INTEGER NOT NULL DEFAULT 0",
        "migration_state": "TEXT",
        "active_scope": "TEXT",
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
        "claimed_by_worker_id": "TEXT",
        "claimed_by_task_key": "TEXT",
        "normalized_email": "TEXT",
        "content_hash": "TEXT",
        "skip_reason": "TEXT",
        "reservation_id": "TEXT",
        "owner_task_key": "TEXT",
    }
    for name, decl in q_extras.items():
        if name not in q_cols:
            con.execute(f"ALTER TABLE campaign_queue ADD COLUMN {name} {decl}")
    con.execute(
        """
        UPDATE campaign_queue
        SET normalized_email=LOWER(TRIM(email))
        WHERE TRIM(COALESCE(normalized_email,''))=''
          AND TRIM(COALESCE(email,''))!=''
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_workers (
            worker_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            login_user_id TEXT NOT NULL DEFAULT '',
            task_key TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            next_resume_at TEXT,
            provider TEXT,
            account_idx INTEGER,
            interval_label TEXT,
            smtp_config_json TEXT,
            runner_id TEXT,
            lease_until TEXT,
            attention_reason TEXT,
            success_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0
        )
        """
    )
    w_cols = {c[1] for c in con.execute("PRAGMA table_info(campaign_workers)").fetchall()}
    w_extras = {
        "login_user_id": "TEXT NOT NULL DEFAULT ''",
        "task_key": "TEXT",
        "status": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "next_resume_at": "TEXT",
        "provider": "TEXT",
        "account_idx": "INTEGER",
        "interval_label": "TEXT",
        "smtp_config_json": "TEXT",
        "runner_id": "TEXT",
        "lease_until": "TEXT",
        "attention_reason": "TEXT",
        "success_count": "INTEGER DEFAULT 0",
        "skipped_count": "INTEGER DEFAULT 0",
        "failed_count": "INTEGER DEFAULT 0",
    }
    for name, decl in w_extras.items():
        if name not in w_cols:
            con.execute(f"ALTER TABLE campaign_workers ADD COLUMN {name} {decl}")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS recipient_batches (
            batch_id TEXT PRIMARY KEY,
            login_user_id TEXT NOT NULL DEFAULT '',
            task_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source_label TEXT,
            source_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS account_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login_user_id TEXT NOT NULL DEFAULT '',
            task_key TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            normalized_email TEXT NOT NULL,
            recipient_json TEXT NOT NULL,
            company TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(login_user_id, task_key, normalized_email)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS account_template_prefs (
            login_user_id TEXT NOT NULL,
            task_key TEXT NOT NULL,
            template_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (login_user_id, task_key)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS recipient_state_migrations (
            login_user_id TEXT PRIMARY KEY,
            migrated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS send_reservations (
            reservation_id TEXT PRIMARY KEY,
            login_user_id TEXT NOT NULL DEFAULT '',
            normalized_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            job_id TEXT,
            item_id INTEGER,
            task_key TEXT,
            message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_message TEXT,
            UNIQUE(login_user_id, normalized_email, content_hash)
        )
        """
    )
    con.execute(
        """
        INSERT OR IGNORE INTO campaign_workers(
            worker_id, job_id, login_user_id, task_key, status, created_at, updated_at,
            next_resume_at, provider, account_idx, interval_label, smtp_config_json,
            runner_id, lease_until, attention_reason, success_count, skipped_count, failed_count
        )
        SELECT
            job_id || ':w0', job_id, login_user_id, task_key, status, created_at, updated_at,
            next_resume_at, provider, account_idx, interval_label, smtp_config_json,
            runner_id, lease_until, attention_reason, success_count, skipped_count, failed_count
        FROM campaign_jobs
        WHERE task_key IS NOT NULL AND task_key != ''
          AND NOT EXISTS (
            SELECT 1 FROM campaign_workers w WHERE w.job_id = campaign_jobs.job_id
          )
        """
    )

    sent_cols = {c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()}
    if sent_cols and "message_id" not in sent_cols:
        try:
            con.execute("ALTER TABLE sent_log ADD COLUMN message_id TEXT")
        except sqlite3.OperationalError:
            pass
    if sent_cols and "normalized_email" not in sent_cols:
        try:
            con.execute("ALTER TABLE sent_log ADD COLUMN normalized_email TEXT")
        except sqlite3.OperationalError:
            pass
    sent_cols = {c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()}
    if sent_cols and "normalized_email" in sent_cols:
        con.execute(
            """
            UPDATE sent_log
            SET normalized_email=LOWER(TRIM(email))
            WHERE TRIM(COALESCE(normalized_email,''))=''
            """
        )

    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_jobs_user_status ON campaign_jobs(login_user_id, status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_jobs_status ON campaign_jobs(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_queue_job_status ON campaign_queue(job_id, status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_queue_job_seq ON campaign_queue(job_id, seq)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_queue_job_email ON campaign_queue(job_id, normalized_email)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_workers_job ON campaign_workers(job_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_campaign_workers_user_task ON campaign_workers(login_user_id, task_key, status)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_recipients_task ON account_recipients(login_user_id, task_key)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_send_reservation_lookup ON send_reservations(login_user_id, normalized_email, content_hash)"
    )
    try:
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_queue_unique_job_email
            ON campaign_queue(job_id, normalized_email)
            WHERE normalized_email IS NOT NULL AND normalized_email != ''
            """
        )
    except sqlite3.IntegrityError:
        # 기존 legacy 공용 큐에 중복이 있을 수 있다. 자동 삭제하지 않고 review 대상으로 둔다.
        pass
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_queue_unique_owned_email
        ON campaign_queue(job_id, owner_task_key, normalized_email)
        WHERE owner_task_key IS NOT NULL AND owner_task_key != ''
          AND normalized_email IS NOT NULL AND normalized_email != ''
        """
    )
    try:
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_jobs_active_user_task
            ON campaign_jobs(login_user_id, task_key)
            WHERE status IN ('queued','running','scheduled_pause','user_stopped','needs_attention')
              AND COALESCE(legacy_pool,0)=0
            """
        )
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        pass
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_jobs_active_scope
        ON campaign_jobs(active_scope)
        WHERE active_scope IS NOT NULL AND active_scope != ''
        """
    )
    try:
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_workers_active_user_task
            ON campaign_workers(login_user_id, task_key)
            WHERE status IN ('queued','running','scheduled_pause','needs_attention')
            """
        )
    except sqlite3.OperationalError:
        pass

    # 기존 sent_log를 전역 중복 reservation으로 안전하게 승계한다.
    if sent_cols and {"account_id", "normalized_email", "content_hash"} <= sent_cols:
        con.execute(
            """
            INSERT OR IGNORE INTO send_reservations(
                reservation_id, login_user_id, normalized_email, content_hash, status,
                job_id, item_id, task_key, message_id, created_at, updated_at, error_message
            )
            SELECT
                'history:' || id,
                LOWER(TRIM(COALESCE(account_id,''))),
                LOWER(TRIM(normalized_email)),
                LOWER(TRIM(content_hash)),
                'sent',
                NULL, NULL, task_key, message_id,
                COALESCE(sent_at,''), COALESCE(sent_at,''), 'sent_log migration'
            FROM sent_log
            WHERE TRIM(COALESCE(account_id,''))!=''
              AND TRIM(COALESCE(normalized_email,''))!=''
              AND TRIM(COALESCE(content_hash,''))!=''
            """
        )

    # v2.8.1 공용 sender-pool: 여러 task_key가 하나의 job을 공유하면 미배정 pending
    # 수신자를 안전하게 원계정으로 복원할 수 없으므로 자동 발송을 차단한다.
    legacy_rows = con.execute(
        """
        SELECT j.job_id, COUNT(DISTINCT w.task_key) AS task_count
        FROM campaign_jobs j
        JOIN campaign_workers w ON w.job_id=j.job_id
        GROUP BY j.job_id
        HAVING task_count > 1
        """
    ).fetchall()
    for legacy in legacy_rows:
        jid = legacy[0]
        reason = (
            "v2.8.1 공용 sender-pool 대기열은 수신자의 원래 SMTP 계정을 확정할 수 없습니다. "
            "잘못된 발송을 막기 위해 중단했습니다. 계정별 Excel을 다시 지정한 뒤 새 작업을 시작하세요."
        )
        con.execute(
            """
            UPDATE campaign_jobs
            SET legacy_pool=1, migration_state='needs_review', status=?,
                attention_reason=?, runner_id=NULL, lease_until=NULL, updated_at=?
            WHERE job_id=? AND status NOT IN (?,?)
            """,
            (JOB_NEEDS_ATTENTION, reason, _now_iso(), jid, JOB_COMPLETED, JOB_CANCELLED),
        )
        con.execute(
            """
            UPDATE campaign_workers
            SET status=?, attention_reason=?, runner_id=NULL, lease_until=NULL, updated_at=?
            WHERE job_id=? AND status NOT IN (?,?)
            """,
            (JOB_NEEDS_ATTENTION, reason, _now_iso(), jid, JOB_COMPLETED, JOB_CANCELLED),
        )


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
        existed = os.path.isfile(db_path)
        folder = os.path.dirname(os.path.abspath(db_path)) or "."
        con = self._connect()
        try:
            ensure_campaign_schema(con)
            con.commit()
        except sqlite3.OperationalError as exc:
            self._discard_new_db(existed)
            self._raise_storage_error(exc, folder)
            raise
        finally:
            con.close()
        try:
            self.scrub_stored_smtp_secrets()
        except sqlite3.OperationalError as exc:
            self._raise_storage_error(exc, folder)
            raise

    def _connect(self) -> sqlite3.Connection:
        folder = os.path.dirname(os.path.abspath(self.db_path)) or "."
        last: Optional[sqlite3.OperationalError] = None
        for attempt in range(2):
            try:
                con = sqlite3.connect(self.db_path, timeout=30)
            except sqlite3.OperationalError as exc:
                last = exc
                if attempt == 0 and self._retry_after_readonly(exc):
                    continue
                if "unable to open" in str(exc).lower():
                    parent = os.path.dirname(self.db_path) or "."
                    names = []
                    try:
                        names = os.listdir(parent)[:8]
                    except OSError:
                        names = ["listdir-failed"]
                    raise sqlite3.OperationalError(
                        f"{exc}; exists={os.path.isfile(self.db_path)}; dir={os.path.isdir(parent)}; names={names}"
                    ) from exc
                self._raise_storage_error(exc, folder)
                raise
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=30000")
            try:
                con.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if not self._is_readonly_error(exc):
                    return con
                con.close()
                last = exc
                if attempt == 0 and self._retry_after_readonly(exc):
                    continue
                self._raise_storage_error(exc, folder)
                raise
            return con
        if last:
            raise last
        raise sqlite3.OperationalError("unable to open database file")

    @staticmethod
    def _is_readonly_error(exc: sqlite3.OperationalError) -> bool:
        text = str(exc).lower()
        return "readonly" in text or "read-only" in text

    def _retry_after_readonly(self, exc: sqlite3.OperationalError) -> bool:
        if not self._is_readonly_error(exc):
            return False
        return bool(os.path.isfile(self.db_path) and clear_readonly(self.db_path))

    def _discard_new_db(self, existed: bool) -> None:
        if existed or not os.path.isfile(self.db_path):
            return
        for extra in (self.db_path, self.db_path + "-wal", self.db_path + "-shm"):
            try:
                if os.path.isfile(extra):
                    clear_readonly(extra)
                    os.remove(extra)
            except OSError:
                pass

    @staticmethod
    def _raise_storage_error(exc: sqlite3.OperationalError, folder: str) -> None:
        text = str(exc).lower()
        if "readonly" in text or "read-only" in text:
            raise StorageWriteError(
                "발송 기록",
                folder,
                preserved=True,
                retryable=True,
                relaunch=True,
                reason="데이터베이스가 읽기 전용이거나 보조 파일을 만들 수 없습니다.",
            ) from exc

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
                wrows = con.execute("SELECT worker_id, task_key, smtp_config_json FROM campaign_workers").fetchall()
                for row in wrows:
                    new_json = _scrub_smtp_json_row(row["smtp_config_json"], row["task_key"] or "")
                    if new_json is None:
                        continue
                    if new_json != (row["smtp_config_json"] or ""):
                        con.execute(
                            "UPDATE campaign_workers SET smtp_config_json=? WHERE worker_id=?",
                            (new_json, row["worker_id"]),
                        )
                        n += 1
                con.commit()
            finally:
                con.close()
        return n

    def import_account_recipients(
        self,
        login_user_id: str,
        task_key: str,
        rows: Iterable[dict],
        *,
        source_label: str = "",
        now: Optional[datetime] = None,
    ) -> dict:
        """계정별 현재 recipient set에 merge한다. 정규화 이메일은 계정 안에서 유일하다."""
        uid = login_user_id or ""
        tkey = task_key or ""
        batch_id = uuid.uuid4().hex
        ts = _now_iso(now)
        inserted = 0
        updated = 0
        duplicates = 0
        invalid = 0
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                source_rows = list(rows or [])
                con.execute(
                    """
                    INSERT INTO recipient_batches(
                        batch_id, login_user_id, task_key, created_at, source_label, source_count
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (batch_id, uid, tkey, ts, source_label or "", len(source_rows)),
                )
                seen = set()
                for raw in source_rows:
                    row = raw if isinstance(raw, dict) else {}
                    email = normalize_email(row.get("이메일") or row.get("email"))
                    if not email:
                        invalid += 1
                        continue
                    if email in seen:
                        duplicates += 1
                        continue
                    seen.add(email)
                    exists = con.execute(
                        """
                        SELECT id FROM account_recipients
                        WHERE login_user_id=? AND task_key=? AND normalized_email=?
                        """,
                        (uid, tkey, email),
                    ).fetchone()
                    company = str(row.get("업체명") or row.get("comp") or "")
                    if exists:
                        con.execute(
                            """
                            UPDATE account_recipients
                            SET recipient_json=?, company=?
                            WHERE id=?
                            """,
                            (_json_dumps(row), company, exists["id"]),
                        )
                        updated += 1
                    else:
                        con.execute(
                            """
                            INSERT INTO account_recipients(
                                login_user_id, task_key, batch_id, normalized_email,
                                recipient_json, company, created_at
                            ) VALUES (?,?,?,?,?,?,?)
                            """,
                            (uid, tkey, batch_id, email, _json_dumps(row), company, ts),
                        )
                        inserted += 1
                con.commit()
                total = int(
                    con.execute(
                        """
                        SELECT COUNT(*) FROM account_recipients
                        WHERE login_user_id=? AND task_key=?
                        """,
                        (uid, tkey),
                    ).fetchone()[0]
                )
            finally:
                con.close()
        return {
            "batch_id": batch_id,
            "inserted": inserted,
            "updated": updated,
            "duplicates": duplicates,
            "invalid": invalid,
            "total": total,
        }

    def replace_account_recipients(
        self,
        login_user_id: str,
        task_key: str,
        rows: Iterable[dict],
        *,
        source_label: str = "ui_replace",
        now: Optional[datetime] = None,
    ) -> dict:
        """사용자가 목록을 삭제/편집했을 때 선택 계정의 현재 set만 교체한다."""
        uid = login_user_id or ""
        tkey = task_key or ""
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "DELETE FROM account_recipients WHERE login_user_id=? AND task_key=?",
                    (uid, tkey),
                )
                con.commit()
            finally:
                con.close()
        return self.import_account_recipients(uid, tkey, rows, source_label=source_label, now=now)

    def list_account_recipients(self, login_user_id: str, task_key: str) -> List[dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    """
                    SELECT recipient_json FROM account_recipients
                    WHERE login_user_id=? AND task_key=?
                    ORDER BY id
                    """,
                    (login_user_id or "", task_key or ""),
                ).fetchall()
                return [
                    data
                    for data in (_json_loads(r["recipient_json"], {}) for r in rows)
                    if isinstance(data, dict)
                ]
            finally:
                con.close()

    def count_account_recipients(self, login_user_id: str, task_key: str) -> int:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    """
                    SELECT COUNT(*) FROM account_recipients
                    WHERE login_user_id=? AND task_key=?
                    """,
                    (login_user_id or "", task_key or ""),
                ).fetchone()
                return int(row[0] if row else 0)
            finally:
                con.close()

    def list_account_recipients_page(
        self,
        login_user_id: str,
        task_key: str,
        offset: int = 0,
        limit: int = 100,
    ) -> List[dict]:
        """화면 페이지 조회. limit는 표시 단위이며 저장 상한이 아니다."""
        safe_limit = max(1, int(limit or 1))
        safe_offset = max(0, int(offset or 0))
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    """
                    SELECT recipient_json FROM account_recipients
                    WHERE login_user_id=? AND task_key=?
                    ORDER BY id
                    LIMIT ? OFFSET ?
                    """,
                    (login_user_id or "", task_key or "", safe_limit, safe_offset),
                ).fetchall()
                return [
                    data
                    for data in (_json_loads(r["recipient_json"], {}) for r in rows)
                    if isinstance(data, dict)
                ]
            finally:
                con.close()

    def recipient_counts_by_task(self, login_user_id: str) -> Dict[str, int]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    """
                    SELECT task_key, COUNT(*) AS n FROM account_recipients
                    WHERE login_user_id=?
                    GROUP BY task_key
                    """,
                    (login_user_id or "",),
                ).fetchall()
                return {str(r["task_key"]): int(r["n"]) for r in rows}
            finally:
                con.close()

    def delete_account_recipients(self, login_user_id: str, task_key: str, emails: Iterable[str]) -> int:
        norms = []
        seen = set()
        for raw in emails or []:
            email = normalize_email(raw)
            if email and email not in seen:
                seen.add(email)
                norms.append(email)
        if not norms:
            return 0
        deleted = 0
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                for start in range(0, len(norms), 400):
                    chunk = norms[start:start + 400]
                    marks = ",".join("?" * len(chunk))
                    cur = con.execute(
                        f"""
                        DELETE FROM account_recipients
                        WHERE login_user_id=? AND task_key=? AND normalized_email IN ({marks})
                        """,
                        (login_user_id or "", task_key or "", *chunk),
                    )
                    deleted += int(cur.rowcount or 0)
                con.commit()
            finally:
                con.close()
        return deleted

    def get_last_template_id(self, login_user_id: str, task_key: str) -> str:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    """
                    SELECT template_id FROM account_template_prefs
                    WHERE login_user_id=? AND task_key=?
                    """,
                    (login_user_id or "", task_key or ""),
                ).fetchone()
                return str(row["template_id"] or "") if row else ""
            finally:
                con.close()

    def set_last_template_id(self, login_user_id: str, task_key: str, template_id: str) -> None:
        template_id = str(template_id or "").strip()
        if not template_id:
            self.clear_last_template_id(login_user_id, task_key)
            return
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    INSERT INTO account_template_prefs(login_user_id, task_key, template_id, updated_at)
                    VALUES (?,?,?,?)
                    ON CONFLICT(login_user_id, task_key) DO UPDATE SET
                        template_id=excluded.template_id,
                        updated_at=excluded.updated_at
                    """,
                    (login_user_id or "", task_key or "", template_id, _now_iso()),
                )
                con.commit()
            finally:
                con.close()

    def clear_last_template_id(self, login_user_id: str, task_key: str) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "DELETE FROM account_template_prefs WHERE login_user_id=? AND task_key=?",
                    (login_user_id or "", task_key or ""),
                )
                con.commit()
            finally:
                con.close()

    def clear_template_preferences(self, template_id: str) -> int:
        template_id = str(template_id or "").strip()
        if not template_id:
            return 0
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    "DELETE FROM account_template_prefs WHERE template_id=?",
                    (template_id,),
                )
                con.commit()
                return int(cur.rowcount or 0)
            finally:
                con.close()

    def migrate_recipient_state(self, login_user_id: str, states: dict) -> dict:
        """recipients.json을 삭제하지 않고 SQLite 계정별 set으로 1회 merge한다."""
        migrated = {}
        if not isinstance(states, dict):
            return migrated
        uid = login_user_id or ""
        with self._lock:
            con = self._connect()
            try:
                done = con.execute(
                    "SELECT 1 FROM recipient_state_migrations WHERE login_user_id=?",
                    (uid,),
                ).fetchone()
            finally:
                con.close()
        if done:
            return migrated
        for task_key, state in states.items():
            if not task_key or str(task_key).startswith("__"):
                continue
            rows = state if isinstance(state, list) else (state or {}).get("rows", [])
            if not isinstance(rows, list) or not rows:
                continue
            existing = self.list_account_recipients(uid, str(task_key))
            if existing:
                migrated[str(task_key)] = {"total": len(existing), "already": True}
                continue
            migrated[str(task_key)] = self.import_account_recipients(
                uid,
                str(task_key),
                rows,
                source_label="recipients.json migration",
            )
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    INSERT OR IGNORE INTO recipient_state_migrations(login_user_id, migrated_at)
                    VALUES (?,?)
                    """,
                    (uid, _now_iso()),
                )
                con.commit()
            finally:
                con.close()
        return migrated

    def recipient_state_migrated(self, login_user_id: str) -> bool:
        with self._lock:
            con = self._connect()
            try:
                return (
                    con.execute(
                        "SELECT 1 FROM recipient_state_migrations WHERE login_user_id=?",
                        (login_user_id or "",),
                    ).fetchone()
                    is not None
                )
            finally:
                con.close()

    def find_latest_job(self, login_user_id: str, task_key: str) -> Optional[dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    """
                    SELECT * FROM campaign_jobs
                    WHERE login_user_id=? AND task_key=? AND COALESCE(legacy_pool,0)=0
                    ORDER BY generation DESC, created_at DESC LIMIT 1
                    """,
                    (login_user_id or "", task_key or ""),
                ).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

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
        raw_rows = list(recipients)
        rows = []
        seen_emails = set()
        for raw in raw_rows:
            row = raw if isinstance(raw, dict) else {}
            email = str(row.get("이메일") or row.get("email") or "").strip()
            normalized = normalize_email(email)
            if normalized:
                if normalized in seen_emails:
                    continue
                seen_emails.add(normalized)
            rows.append((row, email, normalized))
        jid = job_id or uuid.uuid4().hex
        ts = _now_iso(now)
        files = list((attachments or {}).get("files") or [])
        imgs = dict((attachments or {}).get("imgs") or {})
        snapshot = public_smtp_snapshot(smtp_config or {}, task_key or "")
        wid = uuid.uuid4().hex
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                uid = login_user_id or ""
                tkey = task_key or ""
                if exclusive:
                    same = con.execute(
                        """
                        SELECT *
                        FROM campaign_jobs
                        WHERE login_user_id=? AND task_key=?
                          AND status IN (?,?,?,?,?)
                          AND COALESCE(legacy_pool,0)=0
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (
                            uid,
                            tkey,
                            JOB_QUEUED,
                            JOB_RUNNING,
                            JOB_SCHEDULED_PAUSE,
                            JOB_USER_STOPPED,
                            JOB_NEEDS_ATTENTION,
                        ),
                    ).fetchone()
                    if same:
                        con.rollback()
                        raise DuplicateActiveCampaignError(dict(same))
                generation_row = con.execute(
                    """
                    SELECT COALESCE(MAX(generation),0)+1
                    FROM campaign_jobs
                    WHERE login_user_id=? AND task_key=?
                    """,
                    (uid, tkey),
                ).fetchone()
                generation = int(generation_row[0] or 1)
                con.execute(
                    """
                    INSERT INTO campaign_jobs(
                        job_id, login_user_id, status, created_at, updated_at, next_resume_at,
                        subject, body, sender_name, task_key, provider, account_idx,
                        smtp_config_json, interval_label, prevent_dup, apply_public_filter,
                        template_name, attachments_json, cid_json, total_count,
                        success_count, skipped_count, failed_count, attention_reason,
                        generation, legacy_pool, migration_state, active_scope
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        jid,
                        uid,
                        status,
                        ts,
                        ts,
                        next_resume_at,
                        subject or "",
                        body or "",
                        sender_name or "",
                        tkey,
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
                        generation,
                        0,
                        "independent",
                        f"{uid}\x1f{tkey}",
                    ),
                )
                blacklist_tokens = []
                try:
                    blacklist_tokens = [
                        (str(r[0] or ""), str(r[1] or ""))
                        for r in con.execute("SELECT email, reason FROM blacklist").fetchall()
                    ]
                except sqlite3.Error:
                    blacklist_tokens = []
                for seq, (row, email, normalized) in enumerate(rows, 1):
                    company = str(row.get("업체명") or row.get("comp") or "")
                    blacklisted = any(_blacklist_match(normalized, token) for token, _ in blacklist_tokens)
                    item_status = ITEM_SKIPPED if blacklisted else ITEM_PENDING
                    skip_reason = "blacklist" if blacklisted else None
                    con.execute(
                        """
                        INSERT INTO campaign_queue(
                            job_id, seq, recipient_json, email, normalized_email, company,
                            status, attempts, skip_reason, error_message, owner_task_key
                        ) VALUES (?,?,?,?,?,?,?,0,?,?,?)
                        """,
                        (
                            jid,
                            seq,
                            _json_dumps(row),
                            email,
                            normalized,
                            company,
                            item_status,
                            skip_reason,
                            skip_reason,
                            tkey,
                        ),
                    )
                con.execute(
                    """
                    INSERT INTO campaign_workers(
                        worker_id, job_id, login_user_id, task_key, status, created_at, updated_at,
                        next_resume_at, provider, account_idx, interval_label, smtp_config_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        wid,
                        jid,
                        uid,
                        tkey,
                        status,
                        ts,
                        ts,
                        next_resume_at,
                        provider or "",
                        int(account_idx or 0),
                        interval_label or "",
                        _json_dumps(snapshot),
                    ),
                )
                con.commit()
            finally:
                con.close()
        self.refresh_counts(jid)
        job = self.get_job(jid) or {}
        job["worker_id"] = wid
        job["joined_existing"] = False
        return job

    def get_worker(self, worker_id: str) -> Optional[dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM campaign_workers WHERE worker_id=?", (worker_id,)).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def list_workers(self, job_id: str) -> List[dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT * FROM campaign_workers WHERE job_id=? ORDER BY created_at",
                    (job_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def find_active_worker(self, login_user_id: str, task_key: str) -> Optional[dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    f"""
                    SELECT * FROM campaign_workers
                    WHERE login_user_id=? AND task_key=?
                      AND status IN ({",".join("?" * len(ACTIVE_JOB_STATUSES))})
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (login_user_id or "", task_key or "", *ACTIVE_JOB_STATUSES),
                ).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def list_resumable_workers(self, login_user_id: str) -> List[dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    f"""
                    SELECT * FROM campaign_workers
                    WHERE login_user_id=? AND status IN ({",".join("?" * len(RESUME_JOB_STATUSES))})
                    ORDER BY created_at
                    """,
                    (login_user_id or "", *RESUME_JOB_STATUSES),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def list_attention_workers(self, login_user_id: str) -> List[dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    """
                    SELECT * FROM campaign_workers
                    WHERE login_user_id=? AND status=?
                    ORDER BY created_at
                    """,
                    (login_user_id or "", JOB_NEEDS_ATTENTION),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def primary_worker_id(self, job_id: str, task_key: Optional[str] = None) -> Optional[str]:
        workers = self.list_workers(job_id)
        if task_key:
            for w in workers:
                if (w.get("task_key") or "") == task_key:
                    return w.get("worker_id")
        return workers[0]["worker_id"] if workers else None

    def update_worker(self, worker_id: str, **fields) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = fields.get("updated_at") or _now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [worker_id]
        with self._lock:
            con = self._connect()
            try:
                con.execute(f"UPDATE campaign_workers SET {cols} WHERE worker_id=?", vals)
                con.commit()
            finally:
                con.close()

    def set_worker_status(
        self,
        worker_id: str,
        status: str,
        *,
        next_resume_at: Optional[str] = None,
        now: Optional[datetime] = None,
        clear_runner: bool = False,
        attention_reason: Optional[str] = None,
        sync_job: bool = True,
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
        self.update_worker(worker_id, **fields)
        if sync_job:
            worker = self.get_worker(worker_id) or {}
            jid = worker.get("job_id")
            if jid:
                self.sync_job_status_from_workers(jid, now=now)

    def set_needs_attention_worker(self, worker_id: str, reason: str, *, now: Optional[datetime] = None) -> None:
        self.set_worker_status(
            worker_id,
            JOB_NEEDS_ATTENTION,
            now=now,
            clear_runner=True,
            attention_reason=reason or "사용자 확인이 필요합니다.",
        )

    def pause_workers_scheduled(self, job_id: str, *, next_resume_at: str, now: Optional[datetime] = None) -> None:
        for w in self.list_workers(job_id):
            if w.get("status") in (JOB_RUNNING, JOB_QUEUED, JOB_SCHEDULED_PAUSE):
                self.set_worker_status(
                    w["worker_id"],
                    JOB_SCHEDULED_PAUSE,
                    next_resume_at=next_resume_at,
                    now=now,
                    clear_runner=True,
                    sync_job=False,
                )
        self.set_status(job_id, JOB_SCHEDULED_PAUSE, next_resume_at=next_resume_at, now=now, clear_runner=True)

    def resume_workers_for_window(self, job_id: str, *, now: Optional[datetime] = None) -> None:
        for w in self.list_workers(job_id):
            if w.get("status") in (JOB_SCHEDULED_PAUSE, JOB_QUEUED, JOB_RUNNING):
                self.set_worker_status(
                    w["worker_id"], JOB_RUNNING, next_resume_at=None, now=now, sync_job=False
                )
        self.set_status(job_id, JOB_RUNNING, next_resume_at=None, now=now)

    def cancel_campaign_workers(self, job_id: str, *, now: Optional[datetime] = None) -> None:
        for w in self.list_workers(job_id):
            if w.get("status") not in (JOB_COMPLETED, JOB_CANCELLED):
                self.set_worker_status(w["worker_id"], JOB_CANCELLED, now=now, clear_runner=True, sync_job=False)
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    UPDATE campaign_queue
                    SET status=?, skip_reason='cancelled', error_message='cancelled',
                        processed_at=?
                    WHERE job_id=? AND status=?
                    """,
                    (ITEM_CANCELLED, _now_iso(now), job_id, ITEM_PENDING),
                )
                con.commit()
            finally:
                con.close()
        self.set_status(job_id, JOB_CANCELLED, now=now, clear_runner=True)
        self.refresh_counts(job_id)

    def sync_job_status_from_workers(self, job_id: str, *, now: Optional[datetime] = None) -> str:
        job = self.get_job(job_id) or {}
        if job.get("status") in (JOB_CANCELLED, JOB_COMPLETED):
            return job["status"]
        pending = self.count_by_status(job_id, ITEM_PENDING)
        sending = self.count_by_status(job_id, ITEM_SENDING)
        review = self.count_by_status(job_id, ITEM_NEEDS_REVIEW)
        workers = self.list_workers(job_id)
        if pending == 0 and sending == 0:
            if review > 0:
                if job.get("status") != JOB_NEEDS_ATTENTION:
                    self.set_status(
                        job_id,
                        JOB_NEEDS_ATTENTION,
                        now=now,
                        clear_runner=True,
                        attention_reason=f"확인이 필요한 수신자가 {review}건 남아 있습니다.",
                    )
                return JOB_NEEDS_ATTENTION
            self.set_status(job_id, JOB_COMPLETED, now=now, clear_runner=True)
            for w in workers:
                if w.get("status") not in (JOB_CANCELLED, JOB_USER_STOPPED, JOB_NEEDS_ATTENTION):
                    self.update_worker(w["worker_id"], status=JOB_COMPLETED, runner_id=None, lease_until=None)
            return JOB_COMPLETED
        statuses = [w.get("status") for w in workers]
        if any(s == JOB_RUNNING for s in statuses):
            if job.get("status") != JOB_RUNNING:
                self.set_status(job_id, JOB_RUNNING, now=now)
            return JOB_RUNNING
        if any(s == JOB_SCHEDULED_PAUSE for s in statuses):
            nxt = next((w.get("next_resume_at") for w in workers if w.get("next_resume_at")), job.get("next_resume_at"))
            if job.get("status") != JOB_SCHEDULED_PAUSE:
                self.set_status(job_id, JOB_SCHEDULED_PAUSE, next_resume_at=nxt, now=now, clear_runner=True)
            return JOB_SCHEDULED_PAUSE
        if any(s == JOB_QUEUED for s in statuses):
            return JOB_QUEUED
        if any(s == JOB_NEEDS_ATTENTION for s in statuses) and pending > 0:
            runnable = any(s in (JOB_RUNNING, JOB_QUEUED, JOB_SCHEDULED_PAUSE) for s in statuses)
            if not runnable and job.get("status") != JOB_NEEDS_ATTENTION:
                self.set_status(job_id, JOB_NEEDS_ATTENTION, now=now, clear_runner=True)
            return JOB_NEEDS_ATTENTION if not runnable else (job.get("status") or JOB_RUNNING)
        if statuses and all(s in (JOB_USER_STOPPED, JOB_CANCELLED, JOB_COMPLETED) for s in statuses):
            if all(s == JOB_USER_STOPPED for s in statuses) or any(s == JOB_USER_STOPPED for s in statuses):
                self.set_status(job_id, JOB_USER_STOPPED, now=now, clear_runner=True)
                return JOB_USER_STOPPED
        return job.get("status") or JOB_RUNNING

    def worker_stats(self, job_id: str, worker_id: Optional[str] = None, task_key: Optional[str] = None) -> dict:
        job = self.refresh_counts(job_id) or {}
        worker = None
        if worker_id:
            worker = self.get_worker(worker_id)
        elif task_key:
            for w in self.list_workers(job_id):
                if (w.get("task_key") or "") == task_key:
                    worker = w
                    break
        if worker:
            sent = int(worker.get("success_count") or 0)
            skipped = int(worker.get("skipped_count") or 0)
            failed = int(worker.get("failed_count") or 0)
            st = worker.get("status") or job.get("status")
            nxt = worker.get("next_resume_at") or job.get("next_resume_at")
            reason = worker.get("attention_reason") or job.get("attention_reason") or ""
        else:
            sent = int(job.get("success_count") or 0)
            skipped = int(job.get("skipped_count") or 0)
            failed = int(job.get("failed_count") or 0)
            st = job.get("status")
            nxt = job.get("next_resume_at")
            reason = job.get("attention_reason") or ""
        return {
            "job_id": job_id,
            "worker_id": (worker or {}).get("worker_id") or "",
            "task_key": (worker or {}).get("task_key") or job.get("task_key") or "",
            "status": st,
            "next_resume_at": nxt,
            "attention_reason": reason,
            "total": int(job.get("total_count") or 0),
            "success": sent,
            "skipped": skipped,
            "failed": failed,
            "campaign_success": int(job.get("success_count") or 0),
            "campaign_skipped": int(job.get("skipped_count") or 0),
            "campaign_failed": int(job.get("failed_count") or 0),
            "remaining": int(job.get("pending_count") or 0),
            "needs_review": int(job.get("needs_review_count") or 0),
        }

    def bump_worker_result(self, worker_id: Optional[str], kind: str) -> None:
        if not worker_id:
            return
        col = {"sent": "success_count", "skipped": "skipped_count", "failed": "failed_count"}.get(kind)
        if not col:
            return
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    f"UPDATE campaign_workers SET {col}={col}+1, updated_at=? WHERE worker_id=?",
                    (_now_iso(), worker_id),
                )
                con.commit()
            finally:
                con.close()

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
                        f"SELECT 1 FROM campaign_jobs WHERE login_user_id=? AND task_key=? AND status IN ({','.join('?' * len(BLOCKING_JOB_STATUSES))}) LIMIT 1",
                        (login_user_id or "", task_key, *BLOCKING_JOB_STATUSES),
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

    def find_blocking_job(self, login_user_id: str, task_key: Optional[str] = None) -> Optional[dict]:
        uid = login_user_id or ""
        if task_key:
            with self._lock:
                con = self._connect()
                try:
                    row = con.execute(
                        f"""
                        SELECT * FROM campaign_jobs
                        WHERE login_user_id=? AND task_key=?
                          AND status IN ({",".join("?" * len(BLOCKING_JOB_STATUSES))})
                        ORDER BY generation DESC, created_at DESC LIMIT 1
                        """,
                        (uid, task_key, *BLOCKING_JOB_STATUSES),
                    ).fetchone()
                    if not row:
                        return None
                    job = dict(row)
                finally:
                    con.close()
            job["worker_id"] = self.primary_worker_id(job["job_id"], task_key)
            job["worker_status"] = job.get("status")
            return job
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
        if status in (JOB_COMPLETED, JOB_CANCELLED):
            fields["active_scope"] = None
        elif status in BLOCKING_JOB_STATUSES:
            job = self.get_job(job_id) or {}
            if not int(job.get("legacy_pool") or 0):
                fields["active_scope"] = (
                    f"{job.get('login_user_id') or ''}\x1f{job.get('task_key') or ''}"
                )
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

    def claim_next_pending(
        self,
        job_id: str,
        *,
        message_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        task_key: Optional[str] = None,
        retries: int = 8,
    ) -> Optional[dict]:
        """pending → sending 을 BEGIN IMMEDIATE 트랜잭션에서 수행해 이중 발송을 막는다."""
        last_lock = False
        for attempt in range(max(1, int(retries))):
            item, locked = self._claim_next_pending_once(
                job_id, message_id=message_id, worker_id=worker_id, task_key=task_key
            )
            if not locked:
                return item
            last_lock = True
            time.sleep(0.02 * (attempt + 1))
        return None if last_lock else None

    def _claim_next_pending_once(
        self,
        job_id: str,
        *,
        message_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        task_key: Optional[str] = None,
    ) -> tuple:
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    """
                    SELECT * FROM campaign_queue q
                    WHERE q.job_id=? AND q.status=?
                      AND (
                        q.email IS NULL OR TRIM(q.email) = ''
                        OR NOT EXISTS (
                          SELECT 1 FROM campaign_queue o
                          WHERE o.job_id = q.job_id
                            AND LOWER(o.email) = LOWER(q.email)
                            AND o.status IN (?,?,?,?,?)
                        )
                      )
                    ORDER BY q.seq ASC LIMIT 1
                    """,
                    (
                        job_id,
                        ITEM_PENDING,
                        ITEM_SENDING,
                        ITEM_SENT,
                        ITEM_SKIPPED,
                        ITEM_FAILED,
                        ITEM_NEEDS_REVIEW,
                    ),
                ).fetchone()
                if not row:
                    con.commit()
                    return None, False
                item = dict(row)
                mid = message_id or campaign_message_id(job_id, int(item["id"]), int(item.get("seq") or 0))
                cur = con.execute(
                    """
                    UPDATE campaign_queue
                    SET status=?, message_id=?, processed_at=?,
                        claimed_by_worker_id=?, claimed_by_task_key=?
                    WHERE id=? AND status=?
                    """,
                    (
                        ITEM_SENDING,
                        mid,
                        _now_iso(),
                        worker_id or "",
                        task_key or "",
                        item["id"],
                        ITEM_PENDING,
                    ),
                )
                if not cur.rowcount:
                    con.rollback()
                    return None, True
                con.commit()
                item["status"] = ITEM_SENDING
                item["message_id"] = mid
                item["claimed_by_worker_id"] = worker_id or ""
                item["claimed_by_task_key"] = task_key or ""
                item["recipient"] = _json_loads(item.get("recipient_json"), {})
                return item, False
            except sqlite3.OperationalError:
                try:
                    con.rollback()
                except Exception:
                    pass
                return None, True
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    pass
                return None, False
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

    def is_blacklisted(self, email: str) -> bool:
        normalized = normalize_email(email)
        if not normalized:
            return False
        with self._lock:
            con = self._connect()
            try:
                try:
                    rows = con.execute("SELECT email FROM blacklist").fetchall()
                except sqlite3.Error:
                    return False
                return any(_blacklist_match(normalized, row[0]) for row in rows)
            finally:
                con.close()

    def reserve_send(
        self,
        *,
        login_user_id: str,
        email: str,
        content_hash: str,
        job_id: str,
        item_id: int,
        task_key: str,
        message_id: str = "",
        now: Optional[datetime] = None,
    ) -> dict:
        """SMTP 직전 전역 중복 예약. 같은 로그인 사용자·이메일·최종 본문은 하나만 획득한다."""
        uid = str(login_user_id or "").strip().lower()
        normalized = normalize_email(email)
        body_hash = str(content_hash or "").strip().lower()
        if not uid or not normalized or not body_hash:
            return {"acquired": True, "reservation_id": "", "unkeyed": True}
        ts = _now_iso(now)
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                try:
                    sent = con.execute(
                        """
                        SELECT id FROM sent_log
                        WHERE LOWER(TRIM(COALESCE(account_id,'')))=?
                          AND LOWER(TRIM(COALESCE(normalized_email,email,'')))=?
                          AND LOWER(TRIM(COALESCE(content_hash,'')))=?
                        LIMIT 1
                        """,
                        (uid, normalized, body_hash),
                    ).fetchone()
                except sqlite3.Error:
                    sent = None
                if sent:
                    con.commit()
                    return {"acquired": False, "reason": "sent"}
                existing = con.execute(
                    """
                    SELECT * FROM send_reservations
                    WHERE login_user_id=? AND normalized_email=? AND content_hash=?
                    """,
                    (uid, normalized, body_hash),
                ).fetchone()
                if existing:
                    existing = dict(existing)
                    same_item = (
                        str(existing.get("job_id") or "") == str(job_id or "")
                        and int(existing.get("item_id") or 0) == int(item_id or 0)
                    )
                    status = existing.get("status") or ""
                    if same_item and status in ("reserved", "failed"):
                        con.execute(
                            """
                            UPDATE send_reservations
                            SET status='reserved', updated_at=?, message_id=?, error_message=NULL
                            WHERE reservation_id=?
                            """,
                            (ts, message_id or "", existing["reservation_id"]),
                        )
                        con.commit()
                        return {
                            "acquired": True,
                            "reservation_id": existing["reservation_id"],
                            "reused": True,
                        }
                    if status in ("failed", "released"):
                        con.execute(
                            """
                            UPDATE send_reservations
                            SET status='reserved', job_id=?, item_id=?, task_key=?, message_id=?,
                                updated_at=?, error_message=NULL
                            WHERE reservation_id=?
                            """,
                            (
                                job_id,
                                int(item_id),
                                task_key or "",
                                message_id or "",
                                ts,
                                existing["reservation_id"],
                            ),
                        )
                        con.commit()
                        return {
                            "acquired": True,
                            "reservation_id": existing["reservation_id"],
                            "reused": True,
                        }
                    con.commit()
                    return {"acquired": False, "reason": status or "reserved"}
                rid = uuid.uuid4().hex
                con.execute(
                    """
                    INSERT INTO send_reservations(
                        reservation_id, login_user_id, normalized_email, content_hash, status,
                        job_id, item_id, task_key, message_id, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        rid,
                        uid,
                        normalized,
                        body_hash,
                        "reserved",
                        job_id,
                        int(item_id),
                        task_key or "",
                        message_id or "",
                        ts,
                        ts,
                    ),
                )
                con.execute(
                    """
                    UPDATE campaign_queue
                    SET normalized_email=?, content_hash=?, reservation_id=?
                    WHERE id=?
                    """,
                    (normalized, body_hash, rid, int(item_id)),
                )
                con.commit()
                return {"acquired": True, "reservation_id": rid}
            except sqlite3.IntegrityError:
                try:
                    con.rollback()
                except Exception:
                    pass
                return {"acquired": False, "reason": "reserved"}
            finally:
                con.close()

    def set_reservation_status(
        self,
        reservation_id: str,
        status: str,
        *,
        error_message: str = "",
        now: Optional[datetime] = None,
    ) -> None:
        if not reservation_id:
            return
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    UPDATE send_reservations
                    SET status=?, error_message=?, updated_at=?
                    WHERE reservation_id=?
                    """,
                    (status, error_message or None, _now_iso(now), reservation_id),
                )
                con.commit()
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
            live_workers = set()
            for w in self.list_workers(job_id):
                if self._lease_is_live(w, now):
                    live_workers.add(w.get("worker_id"))
        else:
            live_workers = set()
            for job in self.list_active_jobs():
                for w in self.list_workers(job["job_id"]):
                    if self._lease_is_live(w, now):
                        live_workers.add(w.get("worker_id"))
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
                    claimed_w = (item.get("claimed_by_worker_id") or "").strip()
                    if claimed_w and claimed_w in live_workers:
                        continue
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
                        if item.get("reservation_id"):
                            con.execute(
                                """
                                UPDATE send_reservations
                                SET status='sent', updated_at=?
                                WHERE reservation_id=?
                                """,
                                (_now_iso(now), item["reservation_id"]),
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
                    if item.get("reservation_id"):
                        con.execute(
                            """
                            UPDATE send_reservations
                            SET status='review', updated_at=?, error_message=?
                            WHERE reservation_id=?
                            """,
                            (
                                _now_iso(now),
                                "SMTP 접수 여부가 확인되지 않아 자동 재발송하지 않습니다.",
                                item["reservation_id"],
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
        skip_reason: Optional[str] = None,
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
                if skip_reason is not None:
                    sets.append("skip_reason=?")
                    vals.append(skip_reason)
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

    def _lease_is_live(self, row: Optional[dict], now: Optional[datetime] = None) -> bool:
        if not row:
            return False
        rid = (row.get("runner_id") or "").strip()
        lease_until = row.get("lease_until") or ""
        if not rid or not lease_until:
            return False
        try:
            return as_kst(datetime.fromisoformat(lease_until)) > as_kst(now)
        except Exception:
            return False

    def try_claim_worker(
        self,
        worker_id: str,
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
                    "SELECT runner_id, lease_until, status FROM campaign_workers WHERE worker_id=?",
                    (worker_id,),
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
                    UPDATE campaign_workers
                    SET runner_id=?, lease_until=?, updated_at=?
                    WHERE worker_id=? AND status IN ({placeholders})
                    """,
                    [owner, lease, now_s, worker_id, *allowed],
                )
                con.commit()
                return True
            except sqlite3.OperationalError:
                try:
                    con.rollback()
                except Exception:
                    pass
                return False
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    pass
                return False
            finally:
                con.close()

    def has_live_worker_lease(
        self, worker_id: str, *, now: Optional[datetime] = None, owner: Optional[str] = None
    ) -> bool:
        now_dt = as_kst(now)
        worker = self.get_worker(worker_id) or {}
        rid = (worker.get("runner_id") or "").strip()
        if not rid:
            return False
        if owner and rid == owner:
            return False
        return self._lease_is_live(worker, now_dt)

    def heartbeat_worker(
        self, worker_id: str, owner: str, *, now: Optional[datetime] = None, lease_seconds: int = 180
    ) -> None:
        now_dt = as_kst(now)
        lease = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE campaign_workers SET lease_until=?, updated_at=? WHERE worker_id=? AND runner_id=?",
                    (lease, _now_iso(now_dt), worker_id, owner),
                )
                con.commit()
            finally:
                con.close()

    def release_worker(self, worker_id: str, owner: Optional[str] = None) -> None:
        with self._lock:
            con = self._connect()
            try:
                if owner:
                    con.execute(
                        "UPDATE campaign_workers SET runner_id=NULL, lease_until=NULL, updated_at=? WHERE worker_id=? AND runner_id=?",
                        (_now_iso(), worker_id, owner),
                    )
                else:
                    con.execute(
                        "UPDATE campaign_workers SET runner_id=NULL, lease_until=NULL, updated_at=? WHERE worker_id=?",
                        (_now_iso(), worker_id),
                    )
                con.commit()
            finally:
                con.close()

    def worker_smtp_config(self, worker: dict) -> dict:
        cfg = _json_loads((worker or {}).get("smtp_config_json"), {}) or {}
        return cfg if isinstance(cfg, dict) else {}

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

    def stats_dict(self, job_id: str, worker_id: Optional[str] = None, task_key: Optional[str] = None) -> dict:
        if worker_id or task_key:
            return self.worker_stats(job_id, worker_id=worker_id, task_key=task_key)
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
