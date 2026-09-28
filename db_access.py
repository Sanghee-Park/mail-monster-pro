"""프로덕션 SQLite 연결의 유일한 입구.

잠금·busy 만 짧게 재시도한다. 읽기 전용, 권한, I/O 오류는 같은 작업을
반복하지 않고 StorageWriteError 로 올린다. 실패해도 DB 파일을 지우거나
경로를 바꾸지 않는다.
"""
from __future__ import annotations

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


def assert_immediate_write(path: str, *, kind: str = "발송 기록") -> None:
    """SMTP 직전에 쓰기 잠금을 한 번 잡고 바로 되돌린다."""
    if writes_blocked():
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


def backup_database(src: str, dest: str) -> None:
    """SQLite backup API 로 커밋된 상태만 복사한다. -wal/-shm 은 복사하지 않는다."""
    dest_dir = os.path.dirname(dest) or "."
    os.makedirs(dest_dir, exist_ok=True)
    tmp = dest + ".mm_backup_tmp"
    src_con = sqlite3.connect(os.path.abspath(src))
    dst_con = sqlite3.connect(tmp)
    try:
        src_con.backup(dst_con)
        dst_con.commit()
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
        if str(row.get("item_id")) == str(item_id) and row.get("status") != "restored":
            return True
        if str(row.get("item_id")) == str(item_id) and row.get("smtp_phase") in ("after", "uncertain", "before"):
            return True
    return False
