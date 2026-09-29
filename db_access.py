"""프로덕션 SQLite 연결의 유일한 입구.

잠금·busy 만 짧게 재시도한다. 읽기 전용, 권한, I/O 오류는 같은 작업을
반복하지 않고 StorageWriteError 로 올린다. 실패해도 DB 파일을 지우거나
경로를 바꾸지 않는다.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from typing import Callable, Optional

from json_atomic import StorageWriteError, atomic_write_json, clear_readonly, has_readonly_attribute, read_json_object

_LOCK_ATTEMPTS = 5
_WRITE_BLOCKED = False
_JOURNAL_NAME = "send_recovery_journal.json"


def classify_sqlite_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "readonly" in text or "read-only" in text:
        return "readonly"
    if "database is locked" in text or "locked" in text:
        return "locked"
    if "busy" in text:
        return "busy"
    if "unable to open" in text or "permission" in text or "access is denied" in text or "denied" in text:
        return "permission"
    if "disk i/o" in text or "i/o error" in text:
        return "io"
    return "other"


def writes_blocked() -> bool:
    return _WRITE_BLOCKED


def block_writes() -> None:
    global _WRITE_BLOCKED
    _WRITE_BLOCKED = True


def reset_write_block() -> None:
    global _WRITE_BLOCKED
    _WRITE_BLOCKED = False


def _reason_for(category: str) -> str:
    return {
        "readonly": "데이터베이스가 읽기 전용입니다.",
        "locked": "데이터베이스가 다른 작업에 잠겨 있습니다.",
        "busy": "데이터베이스가 사용 중입니다.",
        "permission": "데이터베이스를 열 권한이 없습니다.",
        "io": "데이터베이스 입출력에 실패했습니다.",
    }.get(category, "데이터베이스 쓰기에 실패했습니다.")


def storage_error_from_sqlite(
    exc: BaseException,
    path: str,
    *,
    kind: str = "발송 기록",
    smtp_phase: str = "",
) -> StorageWriteError:
    category = classify_sqlite_error(exc)
    folder = os.path.dirname(os.path.abspath(path)) or "."
    return StorageWriteError(
        kind,
        folder,
        preserved=True,
        retryable=category in ("locked", "busy"),
        relaunch=category in ("readonly", "permission", "io"),
        reason=_reason_for(category),
        category=category,
        db_path=os.path.abspath(path),
        smtp_phase=smtp_phase,
        auto_resend_blocked=True,
    )


def format_storage_log(exc: StorageWriteError, *, operation: str) -> str:
    phase = exc.smtp_phase or "unknown"
    phase_text = {"before": "SMTP 호출 전", "after": "SMTP 호출 후", "uncertain": "SMTP 결과 불명"}.get(phase, phase)
    preserved = "예" if exc.preserved else "확인 필요"
    blocked = "예" if exc.auto_resend_blocked else "아니오"
    return (
        f"❌ {operation} 실패 | 분류: {exc.category or 'io'}"
        f" | DB: {exc.db_path or exc.folder}"
        f" | 시점: {phase_text}"
        f" | 기존 데이터 보존: {preserved}"
        f" | 자동 재발송 차단: {blocked}"
    )


class ManagedConnection:
    def __init__(self, raw: sqlite3.Connection, path: str, kind: str):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "path", os.path.abspath(path))
        object.__setattr__(self, "kind", kind)

    @property
    def raw(self) -> sqlite3.Connection:
        return self._raw

    def _fail(self, exc: sqlite3.OperationalError) -> None:
        try:
            self._raw.rollback()
        except Exception:
            pass
        raise storage_error_from_sqlite(exc, self.path, kind=self.kind) from exc

    def _run(self, fn: Callable):
        delay = 0.05
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                category = classify_sqlite_error(exc)
                if category == "other":
                    raise
                if category in ("locked", "busy") and attempt < _LOCK_ATTEMPTS - 1:
                    time.sleep(delay)
                    delay = min(0.4, delay * 2)
                    continue
                self._fail(exc)
        raise StorageWriteError(self.kind, os.path.dirname(self.path), preserved=True, category="io", db_path=self.path)

    def execute(self, sql, parameters=()):
        return self._run(lambda: self._raw.execute(sql, parameters))

    def executemany(self, sql, seq):
        return self._run(lambda: self._raw.executemany(sql, seq))

    def executescript(self, sql):
        return self._run(lambda: self._raw.executescript(sql))

    def commit(self):
        return self._run(self._raw.commit)

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        return self._raw.close()

    def __del__(self):
        try:
            self._raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                self._raw.rollback()
            except Exception:
                pass
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        if name in {"_raw", "path", "kind"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._raw, name, value)


def connect(path: str, *, kind: str = "발송 기록", _readonly_retried: bool = False) -> ManagedConnection:
    """절대경로 DB 를 연다. 시작 때 정한 경로를 여기서 바꾸지 않는다."""
    path = os.path.abspath(path)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    if os.path.isfile(path) and has_readonly_attribute(path):
        clear_readonly(path)
    delay = 0.05
    raw = None
    for attempt in range(_LOCK_ATTEMPTS):
        try:
            raw = sqlite3.connect(path, timeout=30)
            break
        except sqlite3.OperationalError as exc:
            category = classify_sqlite_error(exc)
            if category in ("locked", "busy") and attempt < _LOCK_ATTEMPTS - 1:
                time.sleep(delay)
                delay = min(0.4, delay * 2)
                continue
            if category == "readonly" and not _readonly_retried and os.path.isfile(path) and clear_readonly(path):
                return connect(path, kind=kind, _readonly_retried=True)
            raise storage_error_from_sqlite(exc, path, kind=kind) from exc
    if raw is None:
        raise storage_error_from_sqlite(sqlite3.OperationalError("unable to open database file"), path, kind=kind)
    try:
        raw.execute("PRAGMA busy_timeout=30000")
    except sqlite3.OperationalError as exc:
        raw.close()
        raise storage_error_from_sqlite(exc, path, kind=kind) from exc
    try:
        raw.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        category = classify_sqlite_error(exc)
        raw.close()
        if category == "readonly" and not _readonly_retried and clear_readonly(path):
            return connect(path, kind=kind, _readonly_retried=True)
        if category in ("readonly", "permission", "io"):
            raise storage_error_from_sqlite(exc, path, kind=kind) from exc
        raw = sqlite3.connect(path, timeout=30)
        raw.execute("PRAGMA busy_timeout=30000")
    return ManagedConnection(raw, path, kind)


def assert_immediate_write(path: str, *, kind: str = "발송 기록", recovery: bool = False) -> None:
    """SMTP 직전 또는 복구 버튼에서 SQLite 쓰기 잠금을 확인한다."""
    if writes_blocked() and not recovery:
        raise StorageWriteError(
            kind,
            os.path.dirname(os.path.abspath(path)) or ".",
            preserved=True,
            retryable=False,
            relaunch=True,
            reason="이전 저장 실패로 자동발송이 중단된 상태입니다.",
            category="readonly",
            db_path=os.path.abspath(path),
            smtp_phase="before",
            auto_resend_blocked=True,
        )
    con = connect(path, kind=kind)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
    finally:
        try:
            con.close()
        except Exception:
            pass
    if recovery:
        reset_write_block()


_INVENTORY_TABLES = ("sent_log", "campaign_jobs", "campaign_workers", "campaign_queue", "blacklist")


def _table_names(con: sqlite3.Connection) -> set:
    return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _quick_check_ok(con: sqlite3.Connection) -> bool:
    row = con.execute("PRAGMA quick_check").fetchone()
    return bool(row) and str(row[0]).lower() == "ok"


def _table_counts(con: sqlite3.Connection) -> dict:
    names = _table_names(con)
    counts = {}
    for table in _INVENTORY_TABLES:
        if table not in names:
            counts[table] = 0
            continue
        counts[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


def database_inventory(path: str) -> dict:
    """행 수와 내용 지문. 안내 문구에는 이메일 원문을 넣지 않는다."""
    con = sqlite3.connect(os.path.abspath(path))
    try:
        ok = _quick_check_ok(con)
        names = _table_names(con)
        counts = _table_counts(con)
        pending = 0
        active = []
        if "campaign_queue" in names:
            pending = int(
                con.execute("SELECT COUNT(*) FROM campaign_queue WHERE status=?", ("pending",)).fetchone()[0]
            )
        if "campaign_jobs" in names:
            active = [
                (str(row[0]), str(row[1]))
                for row in con.execute(
                    """
                    SELECT job_id, status FROM campaign_jobs
                    WHERE status IN ('queued','running','scheduled_pause','needs_attention','user_stopped','storage_blocked')
                    ORDER BY job_id
                    """
                )
            ]
        signatures = {}
        for table in _INVENTORY_TABLES:
            if table not in names:
                signatures[table] = ""
                continue
            cols = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
            if not cols:
                signatures[table] = ""
                continue
            quoted = ", ".join(cols)
            rows = con.execute(f"SELECT {quoted} FROM {table} ORDER BY {quoted}").fetchall()
            blob = repr(tuple(tuple("" if col is None else str(col) for col in row) for row in rows)).encode("utf-8")
            signatures[table] = hashlib.sha256(blob).hexdigest()
        return {"ok": ok, "counts": counts, "pending": pending, "active": active, "signatures": signatures}
    finally:
        con.close()


_ACTIVE_JOB_STATUSES = (
    "queued",
    "running",
    "scheduled_pause",
    "needs_attention",
    "user_stopped",
    "storage_blocked",
)
_QUEUE_STATUSES = ("pending", "sending", "needs_review")


def database_profile(path: str) -> dict:
    """충돌 화면에 보여줄 건수. 이메일 원문은 넣지 않는다."""
    abs_path = os.path.abspath(path)
    info = {
        "path": abs_path,
        "mtime": "",
        "sent_log": 0,
        "blacklist": 0,
        "active_campaigns": 0,
        "pending": 0,
        "sending": 0,
        "needs_review": 0,
        "active_jobs": [],
        "ok": False,
    }
    if not os.path.isfile(abs_path):
        return info
    try:
        info["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(abs_path)))
    except OSError:
        info["mtime"] = ""
    con = sqlite3.connect(abs_path)
    try:
        info["ok"] = _quick_check_ok(con)
        names = _table_names(con)
        if "sent_log" in names:
            info["sent_log"] = int(con.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0])
        if "blacklist" in names:
            info["blacklist"] = int(con.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0])
        if "campaign_queue" in names:
            for status in _QUEUE_STATUSES:
                info[status] = int(
                    con.execute("SELECT COUNT(*) FROM campaign_queue WHERE status=?", (status,)).fetchone()[0]
                )
        if "campaign_jobs" in names:
            marks = ",".join("?" * len(_ACTIVE_JOB_STATUSES))
            rows = con.execute(
                f"""
                SELECT job_id, COALESCE(login_user_id,''), COALESCE(task_key,''), status
                FROM campaign_jobs
                WHERE status IN ({marks})
                ORDER BY task_key, job_id
                """,
                _ACTIVE_JOB_STATUSES,
            ).fetchall()
            info["active_jobs"] = [
                {"job_id": str(row[0]), "login_user_id": str(row[1]), "task_key": str(row[2]), "status": str(row[3])}
                for row in rows
            ]
            info["active_campaigns"] = len(info["active_jobs"])
        return info
    finally:
        con.close()


def format_database_profile(label: str, info: dict) -> str:
    return (
        f"{label}\n"
        f"경로: {info.get('path') or ''}\n"
        f"마지막 변경 시각: {info.get('mtime') or '-'}\n"
        f"sent_log: {int(info.get('sent_log') or 0)}건\n"
        f"blacklist: {int(info.get('blacklist') or 0)}건\n"
        f"활성 캠페인: {int(info.get('active_campaigns') or 0)}건\n"
        f"pending: {int(info.get('pending') or 0)}건, "
        f"sending: {int(info.get('sending') or 0)}건, "
        f"needs_review: {int(info.get('needs_review') or 0)}건"
    )


def conflicting_task_keys(left: dict, right: dict) -> list:
    """같은 task_key 에 서로 다른 활성 캠페인이 있으면 그 키를 반환한다."""
    by_left = {}
    for job in left.get("active_jobs") or []:
        key = str(job.get("task_key") or "")
        if key:
            by_left.setdefault(key, set()).add(str(job.get("job_id") or ""))
    found = []
    for job in right.get("active_jobs") or []:
        key = str(job.get("task_key") or "")
        if not key or key not in by_left:
            continue
        ids = by_left[key]
        if str(job.get("job_id") or "") not in ids or len(ids) > 1:
            found.append(key)
    return sorted(set(found))


def _sent_identity(row: dict) -> tuple:
    message_id = str(row.get("message_id") or "").strip()
    if message_id:
        return ("message_id", message_id)
    email = str(row.get("normalized_email") or row.get("email") or "").strip().lower()
    return (
        "composite",
        str(row.get("account_id") or "").strip().lower(),
        str(row.get("task_key") or ""),
        email,
        str(row.get("content_hash") or "").strip().lower(),
        str(row.get("template_name") or "").strip().lower(),
        str(row.get("sent_at") or ""),
        str(row.get("subject") or ""),
    )


def _table_columns(con: sqlite3.Connection, table: str) -> list:
    if table not in _table_names(con):
        return []
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def _ensure_table_from(dst: sqlite3.Connection, src: sqlite3.Connection, table: str) -> None:
    if table in _table_names(dst):
        return
    row = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if row and row[0]:
        dst.execute(row[0])


def _ensure_columns(con: sqlite3.Connection, table: str, columns: list) -> None:
    have = set(_table_columns(con, table))
    if not have:
        return
    for name in columns:
        if name in have or name == "id":
            continue
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")


def _select_dicts(con: sqlite3.Connection, table: str) -> list:
    cols = _table_columns(con, table)
    if not cols:
        return []
    quoted = ", ".join(cols)
    return [dict(zip(cols, row)) for row in con.execute(f"SELECT {quoted} FROM {table}")]


def _insert_dict(con: sqlite3.Connection, table: str, row: dict, columns: list) -> None:
    usable = [name for name in columns if name in row and name != "id"]
    if not usable:
        return
    marks = ", ".join("?" * len(usable))
    names = ", ".join(usable)
    con.execute(f"INSERT INTO {table}({names}) VALUES ({marks})", [row.get(name) for name in usable])


def merge_user_databases(base_path: str, other_path: str, dest_path: str) -> dict:
    """캠페인 기준 DB에 상대 sent_log·blacklist 합집합을 넣고 dest 로 원자 교체한다."""
    tmp = dest_path + ".mm_merge_tmp"
    for extra in (tmp, tmp + "-wal", tmp + "-shm"):
        try:
            if os.path.isfile(extra):
                os.remove(extra)
        except OSError:
            pass
    backup_database(base_path, tmp)
    base = sqlite3.connect(tmp)
    other = sqlite3.connect(os.path.abspath(other_path))
    try:
        base.execute("PRAGMA foreign_keys=OFF")
        sent_keys = set()
        for row in _select_dicts(base, "sent_log"):
            sent_keys.add(_sent_identity(row))
        other_sent = _select_dicts(other, "sent_log")
        if other_sent:
            _ensure_table_from(base, other, "sent_log")
            _ensure_columns(base, "sent_log", list(other_sent[0].keys()))
            dest_cols = _table_columns(base, "sent_log")
            for row in other_sent:
                key = _sent_identity(row)
                if key in sent_keys:
                    continue
                _insert_dict(base, "sent_log", row, dest_cols)
                sent_keys.add(key)
        seen_email = set()
        for row in _select_dicts(base, "blacklist"):
            seen_email.add(str(row.get("email") or "").strip().lower())
        other_black = _select_dicts(other, "blacklist")
        if other_black:
            _ensure_table_from(base, other, "blacklist")
            _ensure_columns(base, "blacklist", list(other_black[0].keys()))
            dest_cols = _table_columns(base, "blacklist")
            for row in other_black:
                email = str(row.get("email") or "").strip().lower()
                if not email or email in seen_email:
                    continue
                _insert_dict(base, "blacklist", row, dest_cols)
                seen_email.add(email)
        base_tasks = {str(job.get("task_key") or "") for job in database_profile(base_path).get("active_jobs") or []}
        other_jobs = [
            job
            for job in _select_dicts(other, "campaign_jobs")
            if str(job.get("status") or "") in _ACTIVE_JOB_STATUSES
            and str(job.get("task_key") or "")
            and str(job.get("task_key") or "") not in base_tasks
        ]
        copied_jobs = []
        if other_jobs:
            _ensure_table_from(base, other, "campaign_jobs")
            _ensure_columns(base, "campaign_jobs", list(other_jobs[0].keys()))
            job_cols = _table_columns(base, "campaign_jobs")
            known_jobs = {str(row.get("job_id") or "") for row in _select_dicts(base, "campaign_jobs")}
            for job in other_jobs:
                if str(job.get("job_id") or "") in known_jobs:
                    continue
                _insert_dict(base, "campaign_jobs", job, job_cols)
                copied_jobs.append(str(job.get("job_id") or ""))
            if copied_jobs:
                for table in ("campaign_workers", "campaign_queue"):
                    rows = [
                        row
                        for row in _select_dicts(other, table)
                        if str(row.get("job_id") or "") in copied_jobs
                    ]
                    if not rows:
                        continue
                    _ensure_table_from(base, other, table)
                    _ensure_columns(base, table, list(rows[0].keys()))
                    cols = _table_columns(base, table)
                    for row in rows:
                        _insert_dict(base, table, row, cols)
        for row in _select_dicts(other, "send_reservations"):
            _ensure_table_from(base, other, "send_reservations")
            break
        if "send_reservations" in _table_names(base) and "send_reservations" in _table_names(other):
            have = {
                (
                    str(row.get("login_user_id") or "").strip().lower(),
                    str(row.get("normalized_email") or "").strip().lower(),
                    str(row.get("content_hash") or "").strip().lower(),
                )
                for row in _select_dicts(base, "send_reservations")
            }
            rows = _select_dicts(other, "send_reservations")
            if rows:
                _ensure_columns(base, "send_reservations", list(rows[0].keys()))
                cols = _table_columns(base, "send_reservations")
                for row in rows:
                    key = (
                        str(row.get("login_user_id") or "").strip().lower(),
                        str(row.get("normalized_email") or "").strip().lower(),
                        str(row.get("content_hash") or "").strip().lower(),
                    )
                    if key in have:
                        continue
                    _insert_dict(base, "send_reservations", row, cols)
                    have.add(key)
        base.commit()
        if not _quick_check_ok(base):
            raise sqlite3.DatabaseError("통합 데이터베이스 무결성 검사에 실패했습니다.")
        merged_now = database_profile(tmp)
        base_now = database_profile(base_path)
        base_ids = {str(job.get("job_id") or "") for job in base_now.get("active_jobs") or []}
        merged_ids = {str(job.get("job_id") or "") for job in merged_now.get("active_jobs") or []}
        if not base_ids <= merged_ids:
            raise sqlite3.DatabaseError("통합 데이터베이스에 활성 캠페인이 빠졌습니다.")
        if int(merged_now.get("sent_log") or 0) < len(sent_keys) or int(merged_now.get("blacklist") or 0) < len(seen_email):
            raise sqlite3.DatabaseError("통합 데이터베이스 행 수가 예상과 다릅니다.")
        if int(merged_now.get("pending") or 0) < int(base_now.get("pending") or 0):
            raise sqlite3.DatabaseError("통합 데이터베이스의 대기 수신자가 줄었습니다.")
    except Exception:
        try:
            base.close()
        except Exception:
            pass
        try:
            other.close()
        except Exception:
            pass
        for extra in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                if os.path.isfile(extra):
                    os.remove(extra)
            except OSError:
                pass
        raise
    base.close()
    other.close()
    try:
        live = sqlite3.connect(os.path.abspath(dest_path))
        try:
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            live.close()
    except sqlite3.Error:
        pass
    for extra in (dest_path + "-wal", dest_path + "-shm"):
        try:
            if os.path.isfile(extra):
                os.remove(extra)
        except OSError:
            pass
    os.replace(tmp, dest_path)
    clear_readonly(dest_path)
    for extra in (tmp + "-wal", tmp + "-shm"):
        try:
            if os.path.isfile(extra):
                os.remove(extra)
        except OSError:
            pass
    return database_profile(dest_path)


def backup_database(src: str, dest: str) -> None:
    """SQLite backup API 로 커밋된 상태만 복사한다. -wal/-shm 은 복사하지 않는다."""
    dest_dir = os.path.dirname(dest) or "."
    os.makedirs(dest_dir, exist_ok=True)
    tmp = dest + ".mm_backup_tmp"
    src_con = sqlite3.connect(os.path.abspath(src))
    dst_con = sqlite3.connect(tmp)
    try:
        if not _quick_check_ok(src_con):
            raise sqlite3.DatabaseError("원본 데이터베이스 무결성 검사에 실패했습니다.")
        src_con.backup(dst_con)
        dst_con.commit()
        if not _quick_check_ok(dst_con):
            raise sqlite3.DatabaseError("복사한 데이터베이스 무결성 검사에 실패했습니다.")
        if _table_counts(src_con) != _table_counts(dst_con):
            raise sqlite3.DatabaseError("복사한 데이터베이스 행 수가 원본과 다릅니다.")
    except Exception:
        try:
            dst_con.close()
        except Exception:
            pass
        try:
            src_con.close()
        except Exception:
            pass
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    dst_con.close()
    src_con.close()
    os.replace(tmp, dest)
    clear_readonly(dest)


def recovery_journal_path() -> str:
    override = (os.environ.get("MAILMONSTER_DATA_DIR") or "").strip()
    if override:
        folder = os.path.abspath(override)
    else:
        local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(local, "MAIL_MONSTER_PRO")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, _JOURNAL_NAME)


def append_recovery_journal(entry: dict) -> None:
    allowed = ("job_id", "item_id", "task_key", "message_id", "at", "status", "smtp_phase")
    safe = {key: entry.get(key) for key in allowed}
    path = recovery_journal_path()
    data = read_json_object(path)
    rows = list(data.get("entries") or [])
    rows.append(safe)
    atomic_write_json(path, {"entries": rows}, indent=2, ensure_ascii=False, kind="복구 저널")


def load_recovery_journal() -> list:
    data = read_json_object(recovery_journal_path())
    rows = data.get("entries") or []
    return [row for row in rows if isinstance(row, dict)]


def mark_journal_applied(item_id) -> None:
    path = recovery_journal_path()
    data = read_json_object(path)
    rows = []
    for row in data.get("entries") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("item_id")) == str(item_id):
            copied = dict(row)
            copied["status"] = "restored"
            rows.append(copied)
        else:
            rows.append(row)
    atomic_write_json(path, {"entries": rows}, indent=2, ensure_ascii=False, kind="복구 저널")


def journal_blocks_item(item_id) -> bool:
    for row in load_recovery_journal():
        if str(row.get("item_id")) != str(item_id):
            continue
        if row.get("smtp_phase") == "before":
            return False
        return True
    return False
