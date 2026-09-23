"""프로세스 전역 잠금으로 JSON을 원자적으로 저장한다.

Windows에서 대상 파일이 잠시 잠기면 짧은 횟수만 재시도한다.
저장에 실패해도 기존 파일은 교체하지 않는다.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import threading
import time
from typing import Callable

JSON_LOCK = threading.RLock()
_TRANSIENT_WINERRORS = {5, 32, 33}
_REPLACE_ATTEMPTS = 6


class StorageWriteError(OSError):
    def __init__(
        self,
        kind: str,
        folder: str,
        *,
        preserved: bool = True,
        retryable: bool = True,
        relaunch: bool = False,
        reason: str = "",
    ):
        self.kind = kind
        self.folder = folder
        self.preserved = preserved
        self.retryable = retryable
        self.relaunch = relaunch
        self.reason = reason
        lines = [
            f"{kind} 저장에 실패했습니다.",
            f"데이터 폴더: {folder}",
            "기존 데이터는 덮어쓰지 않았습니다." if preserved else "저장이 끝나기 전에 중단되었습니다.",
        ]
        if reason:
            lines.append(f"원인: {reason}")
        if retryable:
            lines.append("같은 작업을 다시 시도할 수 있습니다.")
        if relaunch:
            lines.append("계속 실패하면 프로그램을 완전히 종료한 뒤 다시 실행하세요.")
        super().__init__("\n".join(lines))


def _safe_reason(exc: BaseException) -> str:
    win = getattr(exc, "winerror", None)
    if win in (5, 32, 33):
        return "파일 접근이 거부되었거나 다른 프로세스가 파일을 사용 중입니다."
    text = str(exc).lower()
    if "readonly" in text or "read-only" in text:
        return "데이터베이스가 읽기 전용입니다."
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "파일 접근이 거부되었습니다."
    return "파일을 교체하지 못했습니다."


def is_transient_lock_error(exc: BaseException) -> bool:
    win = getattr(exc, "winerror", None)
    if win in _TRANSIENT_WINERRORS:
        return True
    if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY):
        return True
    return False


def clear_readonly(path: str) -> bool:
    if not path or not os.path.exists(path):
        return True
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            attrs = kernel.GetFileAttributesW(str(path))
            if attrs != 0xFFFFFFFF and attrs & 0x1:
                kernel.SetFileAttributesW(str(path), attrs & ~0x1)
        except Exception:
            pass
    return file_allows_write(path)


def file_allows_write(path: str) -> bool:
    if not os.path.isfile(path):
        return True
    try:
        with open(path, "r+b") as handle:
            handle.read(0)
        return True
    except OSError:
        return False


def replace_with_retry(src: str, dst: str, attempts: int = _REPLACE_ATTEMPTS) -> None:
    if os.path.isfile(dst):
        clear_readonly(dst)
    delay = 0.05
    last: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            if not is_transient_lock_error(exc) or attempt >= attempts - 1:
                raise
            time.sleep(delay)
            delay = min(0.4, delay * 2)
            if os.path.isfile(dst):
                clear_readonly(dst)
    if last:
        raise last


def atomic_write_json(path: str, data, *, indent: int = 2, ensure_ascii: bool = False, kind: str = "설정 파일") -> None:
    with JSON_LOCK:
        _atomic_write_json_unlocked(path, data, indent=indent, ensure_ascii=ensure_ascii, kind=kind)


def _atomic_write_json_unlocked(path: str, data, *, indent: int = 2, ensure_ascii: bool = False, kind: str = "설정 파일") -> None:
    path = os.path.abspath(path)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    payload = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix="mm_json_", suffix=".tmp", dir=folder)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, path)
        replaced = True
    except OSError as exc:
        if isinstance(exc, StorageWriteError):
            raise
        relaunch = is_transient_lock_error(exc)
        raise StorageWriteError(
            kind,
            folder,
            preserved=os.path.isfile(path),
            retryable=True,
            relaunch=relaunch,
            reason=_safe_reason(exc),
        ) from exc
    finally:
        if not replaced:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def read_json_object(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def update_json_object(
    path: str,
    mutator: Callable[[dict], None],
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    kind: str = "설정 파일",
) -> None:
    with JSON_LOCK:
        data = read_json_object(path)
        mutator(data)
        _atomic_write_json_unlocked(path, data, indent=indent, ensure_ascii=ensure_ascii, kind=kind)
