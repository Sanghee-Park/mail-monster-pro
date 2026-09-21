import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_hours import KST, BusinessHours
from campaign_runtime import CampaignRunner
from campaign_store import JOB_RUNNING, CampaignStore
from recipient_import import parse_recipient_excel_files, start_excel_import
from ui_dialogs import (
    FakeDialogProvider,
    UiDialogManager,
    consume_pending_launch,
    is_usable_parent,
    live_ui_parent,
    widget_is_withdrawn,
)


def kst(y, m, d, hh=10, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST)


class FakeWidget:
    def __init__(self, exists=True, state="normal", parent=None):
        self._exists = exists
        self._state = state
        self._closing = False
        self.parent = parent
        self._grab = None
        self.queue = []
        self.lifts = 0
        self.focuses = 0
        self.destroyed = False
        self.protocols = {}
        self.attrs = []
        self.transients = []
        self.titles = []
        self.geoms = []
        self.created_on = threading.get_ident()

    def winfo_exists(self):
        return bool(self._exists) and not self.destroyed

    def winfo_toplevel(self):
        return self.parent.winfo_toplevel() if self.parent is not None else self

    def state(self):
        return self._state

    def after(self, ms, fn):
        self.queue.append(("after", ms, fn))
        return len(self.queue)

    def pump(self):
        pending = list(self.queue)
        self.queue.clear()
        for _kind, _ms, fn in pending:
            fn()

    def lift(self):
        self.lifts += 1

    def focus_force(self):
        self.focuses += 1

    def grab_current(self):
        return self._grab

    def grab_set(self):
        self._grab = self
        top = self.winfo_toplevel()
        if top is not self:
            top._grab = self

    def grab_release(self):
        self._grab = None
        top = self.winfo_toplevel()
        if getattr(top, "_grab", None) is self:
            top._grab = None

    def destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroyed = True
        self._exists = False

    def transient(self, parent):
        self.transients.append(parent)

    def wait_visibility(self):
        return None

    def attributes(self, *args):
        self.attrs.append(args)

    def title(self, t):
        self.titles.append(t)

    def geometry(self, g):
        self.geoms.append(g)

    def minsize(self, *a):
        return None

    def protocol(self, name, fn):
        self.protocols[name] = fn


class UiDialogTests(unittest.TestCase):
    def setUp(self):
        self.main = FakeWidget()
        self.provider = FakeDialogProvider()
        self.errors = []
        self.wins = []

        def factory(parent, **kwargs):
            win = FakeWidget(parent=parent)
            self.wins.append(win)
            return win

        self.mgr = UiDialogManager(
            lambda: self.main,
            provider=self.provider,
            toplevel_factory=factory,
            ui_thread_id=threading.get_ident(),
            show_error=lambda msg, tb: self.errors.append((msg, tb)),
        )

    def test_excel_callback_runs_on_ui_thread(self):
        self.provider.openfilenames_result = ("a.xlsx",)
        paths = self.mgr.askopenfilenames(key="excel_recipients", filetypes=[("Excel Files", "*.xlsx")])
        self.assertEqual(paths, ("a.xlsx",))
        self.assertEqual(self.provider.thread_ids[-1], threading.get_ident())
        self.assertEqual(self.mgr.created_thread_ids[-1], threading.get_ident())
        self.assertIs(self.provider.calls[-1][1]["parent"], self.main)

    def test_folder_callback_runs_on_ui_thread(self):
        self.provider.directory_result = r"C:\tmp"
        path = self.mgr.askdirectory(key="pick_folder", title="폴더 선택")
        self.assertEqual(path, r"C:\tmp")
        self.assertEqual(self.provider.thread_ids[-1], threading.get_ident())
        self.assertIs(self.provider.calls[-1][1]["parent"], self.main)

    def test_template_window_created_on_ui_thread(self):
        win = self.mgr.open_toplevel("template_library", title="템플릿", geometry="320x420")
        self.assertIsNotNone(win)
        self.assertEqual(win.created_on, threading.get_ident())
        self.assertEqual(self.mgr.created_thread_ids[-1], threading.get_ident())
        self.assertIn(self.main, win.transients)

    def test_live_main_window_is_parent(self):
        self.provider.openfilename_result = "x.png"
        self.mgr.askopenfilename(key="attach_cid_file")
        self.assertIs(self.provider.calls[-1][1]["parent"], self.main)
        self.assertTrue(is_usable_parent(self.main))

    def test_destroyed_login_is_not_parent(self):
        login = FakeWidget(exists=False, state="withdrawn")
        self.assertFalse(is_usable_parent(login))
        parent = live_ui_parent(login, (self.main,))
        self.assertIs(parent, self.main)
        import tkinter as tk

        old = getattr(tk, "_default_root", None)
        try:
            tk._default_root = login
            self.provider.openfilenames_result = ("a.xlsx",)
            self.mgr.askopenfilenames(key="excel_recipients")
            self.assertIs(self.provider.calls[-1][1]["parent"], self.main)
            self.assertIsNot(self.provider.calls[-1][1]["parent"], login)
        finally:
            tk._default_root = old

    def test_withdrawn_login_not_usable(self):
        login = FakeWidget(state="withdrawn")
        self.assertTrue(widget_is_withdrawn(login))
        self.assertFalse(is_usable_parent(login))

    def test_excel_dialog_while_account_a_worker_runs(self):
        stop = threading.Event()
        ticks = []

        def worker():
            while not stop.is_set():
                ticks.append(1)
                time.sleep(0.01)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.03)
        before = len(ticks)
        self.provider.openfilenames_result = ("a.xlsx",)
        self.mgr.askopenfilenames(key="excel_recipients")
        time.sleep(0.05)
        self.assertGreater(len(ticks), before)
        stop.set()
        t.join(2)

    def test_template_dialog_while_two_workers_run(self):
        stop = threading.Event()
        progress = {"a": 0, "b": 0}

        def run(name):
            while not stop.wait(0.01):
                progress[name] += 1

        t1 = threading.Thread(target=run, args=("a",), daemon=True)
        t2 = threading.Thread(target=run, args=("b",), daemon=True)
        t1.start()
        t2.start()
        time.sleep(0.03)
        before = dict(progress)
        win = self.mgr.open_toplevel("template_library", title="템플릿")
        self.assertIsNotNone(win)
        time.sleep(0.05)
        self.assertGreater(progress["a"], before["a"])
        self.assertGreater(progress["b"], before["b"])
        stop.set()
        t1.join(2)
        t2.join(2)

    def test_cancel_then_reopen_file_dialog(self):
        self.provider.openfilenames_result = ()
        first = self.mgr.askopenfilenames(key="excel_recipients")
        self.assertEqual(first, ())
        self.assertFalse(self.mgr.is_open("excel_recipients"))
        self.provider.openfilenames_result = ("b.xlsx",)
        second = self.mgr.askopenfilenames(key="excel_recipients")
        self.assertEqual(second, ("b.xlsx",))

    def test_close_template_then_reopen(self):
        w1 = self.mgr.open_toplevel("template_library", title="템플릿")
        self.mgr.close_toplevel(w1, key="template_library")
        self.assertTrue(w1.destroyed)
        self.assertFalse(self.mgr.is_open("template_library"))
        w2 = self.mgr.open_toplevel("template_library", title="템플릿")
        self.assertIsNot(w2, w1)
        self.assertEqual(len(self.wins), 2)

    def test_dialog_exception_resets_flag(self):
        self.provider.raise_on = "askopenfilenames"
        result = self.mgr.askopenfilenames(key="excel_recipients")
        self.assertEqual(result, ())
        self.assertFalse(self.mgr.is_open("excel_recipients"))
        self.assertTrue(self.errors)
        self.provider.raise_on = None
        self.provider.openfilenames_result = ("ok.xlsx",)
        again = self.mgr.askopenfilenames(key="excel_recipients")
        self.assertEqual(again, ("ok.xlsx",))

    def test_grab_current_cleared_after_modal_close(self):
        win = self.mgr.open_toplevel("attention_1", title="발송 확인 필요", modal=True)
        self.assertIs(self.main.grab_current(), win)
        self.mgr.close_toplevel(win, key="attention_1")
        self.assertIsNone(self.mgr.grab_current())
        self.assertIsNone(self.main.grab_current())

    def test_duplicate_click_reuses_window(self):
        w1 = self.mgr.open_toplevel("template_library", title="템플릿")
        lifts = w1.lifts
        w2 = self.mgr.open_toplevel("template_library", title="템플릿")
        self.assertIs(w1, w2)
        self.assertEqual(len(self.wins), 1)
        self.assertGreater(w2.lifts, lifts)

    def test_nested_native_dialog_does_not_open_second(self):
        nested = []

        def block():
            nested.append(self.mgr.askopenfilenames(key="excel_recipients"))

        self.provider.block_fn = block
        self.provider.openfilenames_result = ("a.xlsx",)
        first = self.mgr.askopenfilenames(key="excel_recipients")
        self.assertEqual(first, ("a.xlsx",))
        self.assertEqual(nested, [()])
        self.assertEqual(len(self.provider.calls), 1)

    def test_excel_parse_does_not_block_ui_queue(self):
        started = threading.Event()
        gate = threading.Event()
        ticks = []
        applied = []

        def parse(paths):
            started.set()
            self.assertTrue(gate.wait(2))
            return {
                "rows": [{"업체명": "A", "이메일": "a@ex.com"}],
                "headers": ["업체명", "이메일"],
                "loaded_files": 1,
                "failed_files": [],
            }

        def apply(result):
            applied.append(result)

        self.main.after(0, lambda: ticks.append("tick"))
        self.provider.openfilenames_result = ("slow.csv",)
        info = start_excel_import(
            dialogs=self.mgr,
            parse_fn=parse,
            apply_on_ui=lambda r: self.main.after(0, lambda: apply(r)),
            key="excel_recipients",
        )
        self.assertTrue(info["started"])
        self.assertTrue(started.wait(1))
        self.main.pump()
        self.assertEqual(ticks, ["tick"])
        self.assertEqual(applied, [])
        gate.set()
        deadline = time.time() + 2
        while time.time() < deadline and not applied:
            time.sleep(0.01)
            self.main.pump()
        self.assertEqual(len(applied), 1)

    def test_dialog_does_not_wait_for_ui_from_worker(self):
        ran = []

        def slow():
            time.sleep(0.2)
            ran.append("ui")

        returned = []

        def worker():
            self.mgr.request_on_ui(slow)
            returned.append(time.time())

        t = threading.Thread(target=worker)
        t.start()
        t.join(1)
        self.assertTrue(returned)
        self.assertEqual(ran, [])
        self.main.pump()
        self.assertEqual(ran, ["ui"])

    def test_worker_cannot_open_native_dialog_directly(self):
        result = {"v": "unset"}

        def worker():
            result["v"] = self.mgr.askopenfilenames(key="excel_recipients")
            result["err"] = self.mgr.last_error

        t = threading.Thread(target=worker)
        t.start()
        t.join(2)
        self.assertEqual(result["v"], ())
        self.assertEqual(result["err"], "not_ui_thread")
        self.assertEqual(self.provider.calls, [])

    def test_consume_pending_launch_destroys_login(self):
        login = FakeWidget(state="withdrawn")
        login.pending_launch = {"user_name": "홍길동", "grade": "유료권", "remaining": "10", "login_user_id": "u1"}
        pending = consume_pending_launch(login)
        self.assertTrue(login.destroyed)
        self.assertEqual(pending["login_user_id"], "u1")

    def test_brief_topmost_is_queued_not_permanent(self):
        win = self.mgr.open_toplevel("template_library", title="템플릿")
        self.assertIn(("-topmost", True), win.attrs)
        win.pump()
        self.assertIn(("-topmost", False), win.attrs)


class RecipientImportTests(unittest.TestCase):
    def test_parse_csv_without_blocking_caller(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            path = Path(td.name) / "r.csv"
            path.write_text("업체명,이메일\n가나다,a@ex.com\n", encoding="utf-8")
            parsed = parse_recipient_excel_files([str(path)])
            self.assertEqual(parsed["loaded_files"], 1)
            self.assertEqual(parsed["rows"][0]["이메일"], "a@ex.com")
        finally:
            td.cleanup()

    def test_parse_missing_file_is_failure_not_crash(self):
        parsed = parse_recipient_excel_files([r"C:\missing\nope.xlsx"])
        self.assertEqual(parsed["loaded_files"], 0)
        self.assertTrue(parsed["failed_files"])


class DialogCampaignStressTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.td.name) / "sent_history.db")
        self.store = CampaignStore(self.db)
        self.clock = kst(2026, 9, 16, 10, 0, 0)
        self.hours = BusinessHours(extra_dates=set(), now_fn=lambda: self.clock)

    def tearDown(self):
        self.td.cleanup()

    def test_campaign_workers_continue_while_dialogs_used(self):
        rows = [{"업체명": f"c{i}", "이메일": f"a{i}@ex.com"} for i in range(1, 13)]
        job_a = self.store.create_job(
            login_user_id="alice",
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "secret"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=JOB_RUNNING,
            now=self.clock,
            exclusive=True,
        )
        job_b = self.store.create_job(
            login_user_id="alice",
            task_key="네이버_2",
            provider="네이버",
            account_idx=2,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id2", "pw": "secret"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=JOB_RUNNING,
            now=self.clock,
            exclusive=True,
        )
        sent = []
        lock = threading.Lock()

        def send_fn(payload, job, item):
            with lock:
                sent.append((payload or {}).get("email") or (item or {}).get("email"))
            time.sleep(0.02)
            return True, ""

        def make_runner(wid):
            return CampaignRunner(
                self.store,
                self.hours,
                prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
                send_once_fn=send_fn,
                is_user_stopped=lambda: False,
                is_cancelled=lambda: False,
                interval_seconds_fn=lambda: 0,
                now_fn=lambda: self.clock,
                sleep_fn=lambda s: None,
                max_retries=1,
                wait_poll_seconds=0.01,
                owner=wid,
                worker_id=wid,
            )

        t1 = threading.Thread(target=lambda: make_runner(job_a["worker_id"]).run(job_a["job_id"], wait_off_hours=False), daemon=True)
        t2 = threading.Thread(target=lambda: make_runner(job_b["worker_id"]).run(job_b["job_id"], wait_off_hours=False), daemon=True)
        t1.start()
        t2.start()
        deadline = time.time() + 2
        while time.time() < deadline and len(sent) < 2:
            time.sleep(0.01)
        self.assertGreaterEqual(len(sent), 1)

        main = FakeWidget()
        provider = FakeDialogProvider()
        wins = []

        def factory(parent, **kwargs):
            w = FakeWidget(parent=parent)
            wins.append(w)
            return w

        mgr = UiDialogManager(
            lambda: main,
            provider=provider,
            toplevel_factory=factory,
            ui_thread_id=threading.get_ident(),
            show_error=lambda m, tb: None,
        )
        provider.block_fn = lambda: time.sleep(0.12)
        provider.openfilenames_result = ("a.xlsx",)
        before = len(sent)
        mgr.askopenfilenames(key="excel_recipients")
        mgr.open_toplevel("template_library", title="템플릿")
        time.sleep(1.2)
        self.assertGreater(len(sent), before)
        t1.join(8)
        t2.join(8)


if __name__ == "__main__":
    unittest.main()
