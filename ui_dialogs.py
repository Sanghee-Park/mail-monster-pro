"""파일 선택창·CTkToplevel 을 Tk 메인 스레드에서만 열고, 살아 있는 메인 창을 parent 로 쓴다.

네이티브 filedialog 는 provider 를 주입해 테스트한다. worker 는 창을 직접 만들지 않고
request_on_ui() 로만 요청하며, 완료를 기다리지 않는다.
"""
from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable, Optional

logger = logging.getLogger("mail_monster.ui_dialogs")


def is_widget_alive(widget) -> bool:
    if widget is None:
        return False
    if getattr(widget, "_closing", False):
        return False
    try:
        exists = getattr(widget, "winfo_exists", None)
        if callable(exists) and not exists():
            return False
    except Exception:
        return False
    return True


def widget_is_withdrawn(widget) -> bool:
    try:
        st = str(widget.state())
        return st in ("withdrawn", "iconic")
    except Exception:
        return False


def is_usable_parent(widget) -> bool:
    if not is_widget_alive(widget):
        return False
    if widget_is_withdrawn(widget):
        return False
    return True


def live_ui_parent(candidate, fallbacks=()) -> Optional[Any]:
    """숨겨진 LoginApp·파괴된 root 가 아니라 현재 보이는 메인 창을 고른다."""
    seen = []
    for w in (candidate, *tuple(fallbacks or ())):
        if w is None or w in seen:
            continue
        seen.append(w)
        try:
            top = w.winfo_toplevel() if callable(getattr(w, "winfo_toplevel", None)) else w
        except Exception:
            top = w
        if top not in seen:
            seen.append(top)
        if is_usable_parent(top):
            return top
        if is_usable_parent(w):
            return w
    try:
        import tkinter as tk

        root = getattr(tk, "_default_root", None)
        if is_usable_parent(root):
            return root
    except Exception:
        pass
    return None


class TkFileDialogProvider:
    """실제 tkinter.filedialog. 테스트에서는 FakeDialogProvider 로 대체한다."""

    def askopenfilename(self, **kwargs):
        from tkinter import filedialog

        return filedialog.askopenfilename(**kwargs)

    def askopenfilenames(self, **kwargs):
        from tkinter import filedialog

        return filedialog.askopenfilenames(**kwargs)

    def askdirectory(self, **kwargs):
        from tkinter import filedialog

        return filedialog.askdirectory(**kwargs)

    def asksaveasfilename(self, **kwargs):
        from tkinter import filedialog

        return filedialog.asksaveasfilename(**kwargs)

    def askstring(self, title, prompt, **kwargs):
        from tkinter import simpledialog

        return simpledialog.askstring(title, prompt, **kwargs)


class FakeDialogProvider:
    """단위 테스트용. 실제 OS 파일창·시트를 열지 않는다."""

    def __init__(self):
        self.calls = []
        self.thread_ids = []
        self.openfilename_result = ""
        self.openfilenames_result = ()
        self.directory_result = ""
        self.saveas_result = ""
        self.string_result = None
        self.raise_on = None
        self.block_fn = None

    def _record(self, name, kwargs):
        self.calls.append((name, dict(kwargs)))
        self.thread_ids.append(threading.get_ident())
        if self.block_fn:
            self.block_fn()
        if self.raise_on == name:
            raise RuntimeError("dialog provider test error")

    def askopenfilename(self, **kwargs):
        self._record("askopenfilename", kwargs)
        return self.openfilename_result

    def askopenfilenames(self, **kwargs):
        self._record("askopenfilenames", kwargs)
        return self.openfilenames_result

    def askdirectory(self, **kwargs):
        self._record("askdirectory", kwargs)
        return self.directory_result

    def asksaveasfilename(self, **kwargs):
        self._record("asksaveasfilename", kwargs)
        return self.saveas_result

    def askstring(self, title, prompt, **kwargs):
        self._record("askstring", {"title": title, "prompt": prompt, **kwargs})
        return self.string_result


class UiDialogManager:
    def __init__(
        self,
        root_getter: Callable[[], Any],
        *,
        provider=None,
        toplevel_factory=None,
        ui_thread_id: Optional[int] = None,
        show_error: Optional[Callable[[str, str], None]] = None,
        after_ms: int = 0,
    ):
        self._root_getter = root_getter
        self.provider = provider or TkFileDialogProvider()
        self.toplevel_factory = toplevel_factory
        self.ui_thread_id = int(ui_thread_id if ui_thread_id is not None else threading.get_ident())
        self.show_error = show_error
        self.after_ms = int(after_ms or 0)
        self._open = {}
        self._flag_lock = threading.Lock()
        self.last_error = ""
        self.created_thread_ids = []

    def is_ui_thread(self) -> bool:
        return threading.get_ident() == self.ui_thread_id

    def live_parent(self):
        try:
            candidate = self._root_getter()
        except Exception:
            candidate = None
        return live_ui_parent(candidate)

    def request_on_ui(self, fn: Callable[[], Any]) -> None:
        """worker 는 이 경로만 사용한다. 완료를 기다리지 않는다."""
        if not callable(fn):
            return
        if self.is_ui_thread():
            self._run_safe(fn)
            return
        root = None
        try:
            root = self._root_getter()
        except Exception:
            root = None
        if root is None or not callable(getattr(root, "after", None)):
            logger.error("UI 스레드에 창 요청을 전달할 root.after 가 없습니다.")
            return
        try:
            root.after(self.after_ms, lambda: self._run_safe(fn))
        except Exception:
            logger.exception("request_on_ui after 실패")

    def _run_safe(self, fn) -> None:
        try:
            fn()
        except Exception:
            self._handle_error("창을 여는 중 오류가 발생했습니다.")

    def _handle_error(self, user_msg: str) -> None:
        tb = traceback.format_exc()
        self.last_error = tb
        logger.error("%s\n%s", user_msg, tb)
        parent = self.live_parent()
        if callable(self.show_error):
            try:
                self.show_error(user_msg, tb)
                return
            except Exception:
                logger.exception("show_error 실패")
        try:
            from tkinter import messagebox

            messagebox.showerror("오류", user_msg, parent=parent if is_usable_parent(parent) else None)
        except Exception:
            logger.exception("오류창 표시 실패")

    def _reject_non_ui(self, cancel_value):
        logger.error(
            "dialog 는 Tk 메인 스레드에서만 열 수 있습니다.\n%s",
            "".join(traceback.format_stack(limit=12)),
        )
        self.last_error = "not_ui_thread"
        return cancel_value

    def _adopt_default_root(self, parent) -> None:
        if parent is None:
            return
        try:
            import tkinter as tk

            tk._default_root = parent
        except Exception:
            pass

    def register_open(self, key: str, win) -> None:
        with self._flag_lock:
            self._open[key] = win

    def _native(self, key: str, cancel_value, method_name: str, kwargs: dict):
        if not self.is_ui_thread():
            return self._reject_non_ui(cancel_value)
        kwargs = dict(kwargs)
        requested = kwargs.pop("parent", None)
        parent = requested if is_usable_parent(requested) else self.live_parent()
        if parent is None:
            self._handle_error("메인 창을 찾을 수 없어 파일 선택창을 열 수 없습니다.")
            return cancel_value
        self._adopt_default_root(parent)
        kwargs["parent"] = parent
        with self._flag_lock:
            if self._open.get(key):
                existing = self._open.get(key)
                if existing not in (True, "native") and callable(getattr(existing, "lift", None)):
                    try:
                        existing.lift()
                    except Exception:
                        pass
                return cancel_value
            self._open[key] = "native"
        self.created_thread_ids.append(threading.get_ident())
        try:
            fn = getattr(self.provider, method_name)
            return fn(**kwargs)
        except Exception:
            self._handle_error("파일 선택창을 열 수 없습니다.")
            return cancel_value
        finally:
            with self._flag_lock:
                if self._open.get(key) == "native":
                    self._open.pop(key, None)

    def askopenfilename(self, *, key="open_file", **kwargs):
        return self._native(key, "", "askopenfilename", kwargs)

    def askopenfilenames(self, *, key="open_files", **kwargs):
        result = self._native(key, (), "askopenfilenames", kwargs)
        if result is None:
            return ()
        if isinstance(result, str):
            return (result,) if result else ()
        return tuple(result)

    def askdirectory(self, *, key="open_dir", **kwargs):
        return self._native(key, "", "askdirectory", kwargs)

    def asksaveasfilename(self, *, key="save_file", **kwargs):
        return self._native(key, "", "asksaveasfilename", kwargs)

    def askstring(self, title: str, prompt: str, *, key="ask_string", **kwargs):
        if not self.is_ui_thread():
            return self._reject_non_ui(None)
        kwargs = dict(kwargs)
        requested = kwargs.pop("parent", None)
        parent = requested if is_usable_parent(requested) else self.live_parent()
        if parent is not None:
            kwargs["parent"] = parent
        with self._flag_lock:
            self._open[key] = "native"
        try:
            return self.provider.askstring(title, prompt, **kwargs)
        except Exception:
            self._handle_error("입력 창을 열 수 없습니다.")
            return None
        finally:
            with self._flag_lock:
                self._open.pop(key, None)

    def is_open(self, key: str) -> bool:
        with self._flag_lock:
            return bool(self._open.get(key))

    def get_open(self, key: str):
        with self._flag_lock:
            return self._open.get(key)

    def open_toplevel(
        self,
        key: str,
        *,
        title: str,
        geometry: Optional[str] = None,
        minsize=None,
        modal: bool = False,
        factory=None,
        **kwargs,
    ):
        if not self.is_ui_thread():
            self._reject_non_ui(None)
            return None
        parent = self.live_parent()
        if parent is None:
            self._handle_error("메인 창을 찾을 수 없어 창을 열 수 없습니다.")
            return None
        existing = self.get_open(key)
        if existing not in (None, True, "native") and is_widget_alive(existing):
            self._lift(existing)
            return existing
        make = factory or self.toplevel_factory
        if make is None:
            import customtkinter as ctk

            make = ctk.CTkToplevel
        self.created_thread_ids.append(threading.get_ident())
        try:
            win = make(parent, **kwargs)
        except Exception:
            self._handle_error("창을 열 수 없습니다.")
            return None
        with self._flag_lock:
            self._open[key] = win
        try:
            if title:
                win.title(title)
            if geometry:
                win.geometry(geometry)
            if minsize:
                win.minsize(*minsize)
        except Exception:
            pass
        self.reveal_toplevel(win, modal=modal, key=key)
        return win

    def reveal_toplevel(self, win, *, modal: bool = False, key: Optional[str] = None) -> None:
        parent = self.live_parent()
        reveal_window(win, parent, modal=modal, brief_topmost_ms=250)
        self._bind_close(win, key, modal)

    def _lift(self, win) -> None:
        try:
            win.lift()
        except Exception:
            pass
        try:
            win.focus_force()
        except Exception:
            pass

    def _bind_close(self, win, key: Optional[str], modal: bool) -> None:
        prev = getattr(win, "_ui_dialog_close_bound", False)
        if prev:
            return
        try:
            win._ui_dialog_close_bound = True
        except Exception:
            pass

        def _on_close():
            self.close_toplevel(win, key=key)

        try:
            win.protocol("WM_DELETE_WINDOW", _on_close)
        except Exception:
            pass

    def close_toplevel(self, win=None, *, key: Optional[str] = None) -> None:
        if win is None and key:
            win = self.get_open(key)
        parent = self.live_parent()
        try:
            if win is not None and is_widget_alive(win):
                try:
                    win.grab_release()
                except Exception:
                    pass
                try:
                    win.destroy()
                except Exception:
                    pass
        finally:
            with self._flag_lock:
                if key:
                    cur = self._open.get(key)
                    if cur is win or cur in (True, "native") or not is_widget_alive(cur):
                        self._open.pop(key, None)
                for k, v in list(self._open.items()):
                    if v is win or not is_widget_alive(v):
                        self._open.pop(k, None)
            try:
                if parent is not None and is_usable_parent(parent):
                    parent.focus_force()
                    if callable(getattr(parent, "lift", None)):
                        parent.lift()
            except Exception:
                pass

    def grab_current(self):
        parent = self.live_parent()
        if parent is None:
            return None
        try:
            return parent.grab_current()
        except Exception:
            return None


def reveal_window(win, parent=None, *, modal: bool = False, brief_topmost_ms: int = 250) -> None:
    """CTkToplevel 표시 순서: transient → wait_visibility → lift → 짧은 topmost → 필요 시 grab → focus."""
    try:
        if parent is not None:
            win.transient(parent)
    except Exception:
        pass
    try:
        win.wait_visibility()
    except Exception:
        pass
    try:
        win.lift()
    except Exception:
        pass
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    def _clear_topmost(w=win):
        try:
            if is_widget_alive(w):
                w.attributes("-topmost", False)
        except Exception:
            pass

    delay = max(0, int(brief_topmost_ms or 0))
    try:
        if delay and callable(getattr(win, "after", None)):
            win.after(delay, _clear_topmost)
        else:
            _clear_topmost()
    except Exception:
        _clear_topmost()
    if modal:
        try:
            win.grab_set()
        except Exception:
            logger.exception("grab_set 실패")
    try:
        win.focus_force()
    except Exception:
        pass


def consume_pending_launch(login_window):
    """로그인 mainloop 종료 후 숨겨진 LoginApp 을 제거한 뒤 메인 실행 인자를 반환한다."""
    pending = getattr(login_window, "pending_launch", None) if login_window is not None else None
    try:
        if login_window is not None:
            login_window.destroy()
    except Exception:
        pass
    return pending
