"""Tk/CustomTkinter 위젯은 생성한 메인 스레드에서만 갱신한다."""
from __future__ import annotations

import logging
import traceback

logger = logging.getLogger("mail_monster.ui")


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
        logger.exception("schedule_on_ui after 실패")
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
        tb = traceback.format_exc()
        logger.error("UI callback 예외\n%s", tb)
        try:
            setattr(root, "_last_ui_error", tb)
        except Exception:
            pass
