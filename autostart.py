"""Windows 시작 시 현재 사용자 Run 키로 프로그램 자동 실행 (관리자 권한 불필요)."""
from __future__ import annotations

import os
import sys
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MAIL_MONSTER_PRO"
AUTOSTART_ARG = "--autostart-recovery"
RESUME_ARG = "--resume"
RECOVERY_ARGS = (AUTOSTART_ARG, RESUME_ARG)


def is_recovery_argv(argv=None) -> bool:
    args = list(argv if argv is not None else sys.argv)
    return any(a in RECOVERY_ARGS for a in args)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def quote_win_arg(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def build_autostart_command(base_dir: Optional[str] = None) -> str:
    """경로에 한글·공백이 있어도 동작하도록 모든 경로를 따옴표로 감싼다."""
    if is_frozen():
        exe = sys.executable
        return f"{quote_win_arg(exe)} {RESUME_ARG}"
    python = sys.executable
    script_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(script_dir, "main.py")
    return f"{quote_win_arg(python)} {quote_win_arg(script)} {RESUME_ARG}"


class RegistryBackend:
    def get(self, name: str) -> Optional[str]:
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError


class MemoryRegistry(RegistryBackend):
    def __init__(self):
        self.values = {}

    def get(self, name: str) -> Optional[str]:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class WinRegBackend(RegistryBackend):
    def get(self, name: str) -> Optional[str]:
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def set(self, name: str, value: str) -> None:
        if sys.platform != "win32":
            return
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        if sys.platform != "win32":
            return
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return
        except OSError:
            return


def is_autostart_enabled(backend: Optional[RegistryBackend] = None) -> bool:
    b = backend or WinRegBackend()
    val = b.get(VALUE_NAME)
    return bool(val)


def enable_autostart(command: Optional[str] = None, backend: Optional[RegistryBackend] = None, base_dir: Optional[str] = None) -> str:
    b = backend or WinRegBackend()
    cmd = command or build_autostart_command(base_dir)
    b.set(VALUE_NAME, cmd)
    return cmd


def disable_autostart(backend: Optional[RegistryBackend] = None) -> None:
    b = backend or WinRegBackend()
    b.delete(VALUE_NAME)


def sync_autostart(should_enable: bool, backend: Optional[RegistryBackend] = None, base_dir: Optional[str] = None) -> bool:
    """활성 작업(실행/예약대기)이 있을 때만 Run 키를 유지한다."""
    b = backend or WinRegBackend()
    if should_enable:
        enable_autostart(backend=b, base_dir=base_dir)
        return True
    disable_autostart(backend=b)
    return False
