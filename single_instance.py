"""Windows 단일 인스턴스 mutex. 자동실행과 수동실행이 겹치지 않게 한다."""
from __future__ import annotations

import os
import sys
from typing import Optional

DEFAULT_MUTEX_NAME = "Local\\MAIL_MONSTER_PRO.SingleInstance"
_MUTEX_HANDLE = None


def mutex_name() -> str:
    env = os.environ.get("MAILMONSTER_MUTEX_NAME", "").strip()
    if env:
        return env
    return DEFAULT_MUTEX_NAME


def acquire_single_instance(name: Optional[str] = None):
    """이미 실행 중이면 False. 핸들은 프로세스 종료까지 전역으로 유지."""
    global _MUTEX_HANDLE
    if os.environ.get("MAILMONSTER_SKIP_MUTEX", "").strip() in ("1", "true", "yes"):
        return True
    if sys.platform != "win32":
        return _acquire_file_lock(name)
    import ctypes

    mutex = name or mutex_name()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, mutex)
    already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    if not handle:
        return not already
    if already:
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


_LOCK_FILE = None


def _acquire_file_lock(name: Optional[str] = None) -> bool:
    """비 Windows 테스트용 파일 잠금."""
    global _LOCK_FILE
    from pathlib import Path

    safe = (name or mutex_name()).replace("\\", "_").replace("/", "_")
    path = Path(os.environ.get("MAILMONSTER_LOCK_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".instance.lock"))
    if os.environ.get("MAILMONSTER_LOCK_DIR"):
        path = Path(os.environ["MAILMONSTER_LOCK_DIR"]) / f"{safe}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCK_FILE = f
    return True


def show_already_running_message(silent: bool = False) -> None:
    if silent:
        return
    msg = "MAIL MONSTER PRO가 이미 실행 중입니다."
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, "MAIL MONSTER PRO", 0x40)
            return
        except Exception:
            pass
    try:
        from tkinter import messagebox

        messagebox.showinfo("MAIL MONSTER PRO", msg)
    except Exception:
        print(msg)
