"""Tk/CustomTkinter 위젯은 생성한 메인 스레드에서만 갱신한다."""
from __future__ import annotations


def schedule_on_ui(root, fn) -> None:
    if root is None or not callable(fn):
        return
    if getattr(root, "_closing", False):
        return
    try:
        exists = getattr(root, "winfo_exists", None)
        if callable(exists) and not exists():
            return
        root.after(0, lambda: _run_ui(root, fn))
    except Exception:
        return


def _run_ui(root, fn) -> None:
    if getattr(root, "_closing", False):
        return
    try:
        exists = getattr(root, "winfo_exists", None)
        if callable(exists) and not exists():
            return
        fn()
    except Exception:
        return
