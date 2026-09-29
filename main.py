import sys
from data_migrate import prepare_user_data
from login import LoginApp
from main_ui import ModernMailSender
from single_instance import acquire_single_instance, show_already_running_message
from autostart import is_recovery_argv
from ui_dialogs import consume_pending_launch

AUTOSTART_RECOVERY = is_recovery_argv(sys.argv)


def initialize_user_data():
    """단일 인스턴스 잠금 다음에만 저장 위치를 정한다."""
    if not acquire_single_instance():
        return "already-running"
    prepare_user_data()
    return "ready"


def launch_main_app(user_name, grade, remaining, login_user_id=""):
    app = ModernMailSender(
        user_name=user_name,
        grade=grade,
        remaining=remaining,
        login_user_id=login_user_id,
        autostart_recovery=AUTOSTART_RECOVERY,
    )
    app.mainloop()


def run_ui_self_test() -> int:
    """패키징된 EXE에서 다계정 화면·템플릿 복원을 실제 창으로 확인한다. 로그인·SMTP는 호출하지 않는다."""
    import json
    import tempfile
    import time
    from pathlib import Path

    from app_paths import DATA_DIR_ENV

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        os_mod = __import__("os")
        os_mod.environ[DATA_DIR_ENV] = folder
        root = Path(folder)
        keys = [f"네이버_{n}" for n in range(1, 13)]
        config = {
            key: {"id": f"user{n}@example.com", "pw": "secret", "smtp": "smtp.example.com", "port": "465"}
            for n, key in enumerate(keys, 1)
        }
        config["__account_order__"] = keys
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (root / "templates.json").write_text(
            json.dumps({"복원": {"id": "tpl-self", "title": "복원제목", "body": "복원본문", "sender": "보낸이", "files": [], "imgs": {}}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (root / "recipients.json").write_text("{}", encoding="utf-8")
        (root / "user_profiles.json").write_text("{}", encoding="utf-8")
        app = ModernMailSender(user_name="selftest", grade="유료", remaining="1", login_user_id="selftest")
        app.withdraw()
        app.campaign_store.set_last_template_id("selftest", "네이버_12", "tpl-self")
        app.campaign_store.import_account_recipients(
            "selftest",
            "네이버_12",
            [{"업체명": "c", "이메일": f"e{i}@ex.com"} for i in range(120)],
        )
        started = time.perf_counter()
        app._switch_profile("네이버_12")
        app.update()
        elapsed = time.perf_counter() - started
        title = app._shared_widgets["title"].get()
        body = app._shared_widgets["body"].get("1.0", "end-1c")
        shown = len(app._shared_widgets["tree"].get_children())
        total = app.campaign_store.count_account_recipients("selftest", "네이버_12")
        frames = len(app.profile_frames)
        app.destroy()
        if frames != 1 or title != "복원제목" or "복원본문" not in body or shown != 100 or total != 120 or elapsed >= 1:
            return 1
        return 0


def run_storage_self_test() -> int:
    """임시 데이터 폴더에서 JSON·SQLite 저장만 확인한다. SMTP·시트·HKCU는 호출하지 않는다."""
    import os
    from pathlib import Path

    from app_paths import DATA_DIR_ENV
    from campaign_store import CampaignStore
    from data_migrate import chosen_data_dir, prepare_user_data, reset_prepare_cache
    from db_access import connect
    from json_atomic import atomic_write_json, read_json_object

    phase = "write"
    if "--storage-self-test-verify" in sys.argv:
        phase = "verify"
    elif "--storage-self-test-fallback" in sys.argv:
        phase = "fallback"
    elif "--storage-self-test-nosend" in sys.argv:
        phase = "nosend"

    if phase == "fallback":
        if os.environ.get("MAILMONSTER_SELFTEST") != "1":
            return 9
        local = os.environ.get("LOCALAPPDATA") or ""
        if not local:
            return 8
        os.environ.pop(DATA_DIR_ENV, None)
        reset_prepare_cache()
        prepare_user_data(force=True)
        chosen = chosen_data_dir()
        if not os.path.normcase(os.path.abspath(chosen)).startswith(os.path.normcase(os.path.abspath(local))):
            return 2
        target = Path(chosen)
        atomic_write_json(
            str(target / "recipients.json"),
            {"네이버_1": {"row_count": 1}, "메일플러그_1": {"row_count": 2}},
            kind="수신처 목록",
        )
        db_path = str(target / "sent_history.db")
        store = CampaignStore(db_path)
        del store
        con = connect(db_path, kind="발송 기록")
        con.execute("CREATE TABLE IF NOT EXISTS selftest_probe(note TEXT)")
        con.execute("INSERT INTO selftest_probe(note) VALUES ('kept')")
        con.commit()
        con.close()
        again = CampaignStore(db_path)
        del again
        con = connect(db_path, kind="발송 기록")
        row = con.execute("SELECT note FROM selftest_probe").fetchone()
        con.close()
        if not row or row[0] != "kept":
            return 7
        return 0

    if phase == "nosend":
        from datetime import datetime

        from business_hours import KST, BusinessHours
        from campaign_runtime import CampaignRunner
        from campaign_store import ITEM_NEEDS_REVIEW, ITEM_SENT, JOB_RUNNING
        from json_atomic import StorageWriteError

        data_dir = os.environ.get(DATA_DIR_ENV) or ""
        if not data_dir:
            return 3
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        store = CampaignStore(str(root / "sent_history.db"))
        when = datetime(2026, 9, 16, 10, 0, 0, tzinfo=KST)
        job = store.create_job(
            login_user_id="selftest",
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="보낸이",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "secret"},
            interval_label="즉시",
            prevent_dup=False,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=[{"업체명": "회사", "이메일": "a@ex.com"}],
            status=JOB_RUNNING,
            now=when,
        )
        calls = []
        original = store.mark_item

        def wrapped(item_id, status, *args, **kwargs):
            if status == ITEM_SENT:
                raise StorageWriteError(
                    "발송 기록",
                    str(root),
                    preserved=True,
                    category="readonly",
                    db_path=str(root / "sent_history.db"),
                    smtp_phase="after",
                    auto_resend_blocked=True,
                )
            return original(item_id, status, *args, **kwargs)

        store.mark_item = wrapped
        runner = CampaignRunner(
            store,
            BusinessHours(extra_dates=set(), now_fn=lambda: when),
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=lambda payload, job, item: calls.append(1) or (True, ""),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: when,
            sleep_fn=lambda _s: None,
            owner="selftest",
            worker_id=job.get("worker_id"),
            max_retries=1,
        )
        runner.run(job["job_id"], wait_off_hours=False)
        if len(calls) != 1 or runner.smtp_calls != 1:
            return 11
        from db_access import load_recovery_journal, reset_write_block

        if not load_recovery_journal():
            return 12
        reset_write_block()
        again = CampaignStore(str(root / "sent_history.db"))
        calls.clear()
        runner2 = CampaignRunner(
            again,
            BusinessHours(extra_dates=set(), now_fn=lambda: when),
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=lambda payload, job, item: calls.append(1) or (True, ""),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: when,
            sleep_fn=lambda _s: None,
            owner="selftest-2",
            max_retries=1,
        )
        runner2.run(job["job_id"], wait_off_hours=False)
        item_status = ""
        for row in again.list_items_by_status(job["job_id"], ITEM_NEEDS_REVIEW):
            item_status = row["status"]
        if calls or runner2.smtp_calls or item_status != ITEM_NEEDS_REVIEW:
            return 13
        if not (root / "sent_history.db").is_file():
            return 14
        return 0

    data = os.environ.get(DATA_DIR_ENV) or ""
    if not data:
        return 3
    root = Path(data)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "recipients.json"
    db_path = root / "sent_history.db"
    if phase == "write":
        atomic_write_json(
            str(path),
            {
                "네이버_1": {"rows": [], "row_count": 1},
                "메일플러그_1": {"rows": [], "row_count": 2},
            },
            kind="수신처 목록",
        )
        store = CampaignStore(str(db_path))
        del store
        con = connect(str(db_path), kind="발송 기록")
        con.execute("CREATE TABLE IF NOT EXISTS selftest_probe(note TEXT)")
        con.execute("INSERT INTO selftest_probe(note) VALUES ('kept')")
        con.commit()
        con.close()
        return 0
    payload = read_json_object(str(path))
    if payload.get("네이버_1", {}).get("row_count") != 1:
        return 4
    if payload.get("메일플러그_1", {}).get("row_count") != 2:
        return 5
    if not db_path.is_file():
        return 6
    con = connect(str(db_path), kind="발송 기록")
    try:
        row = con.execute("SELECT note FROM selftest_probe").fetchone()
    except Exception:
        return 6
    finally:
        con.close()
    if not row or row[0] != "kept":
        return 6
    return 0


def run_v286_self_test() -> int:
    """임시 폴더에서 v2.8.6 저장 위치·복구·중복 스킵을 확인한다. SMTP·시트·HKCU는 호출하지 않는다."""
    import os
    import threading
    from datetime import datetime
    from pathlib import Path
    from unittest.mock import patch

    if os.environ.get("MAILMONSTER_SELFTEST") != "1":
        return 9
    local = os.environ.get("LOCALAPPDATA") or ""
    if "mm286" not in os.path.normcase(local):
        return 8
    os.environ["MAILMONSTER_SCAN_CLOUD"] = "0"
    os.environ.pop("MAILMONSTER_DATA_DIR", None)

    from app_paths import install_dir, is_frozen, storage_block_reason
    from business_hours import KST, BusinessHours
    from campaign_attention import maybe_release_attention
    from campaign_runtime import CampaignRunner
    from campaign_store import (
        ITEM_NEEDS_REVIEW,
        ITEM_PENDING,
        JOB_RUNNING,
        CampaignStore,
    )
    from data_migrate import chosen_data_dir, prepare_user_data, reset_prepare_cache
    from db_access import load_recovery_journal, reset_write_block
    from json_atomic import StorageWriteError
    from ui_dialogs import UiDialogManager

    if "OneDrive" not in (storage_block_reason(r"C:\Users\dkdk6\OneDrive\Desktop\CI비") or ""):
        return 22
    if storage_block_reason(r"\\server\share\db") is None:
        return 23
    korean = Path(local) / "한글 폴더"
    korean.mkdir(parents=True, exist_ok=True)
    probe = korean / "검사 파일.txt"
    probe.write_text("ok", encoding="utf-8")
    if probe.read_text(encoding="utf-8") != "ok":
        return 24

    when = datetime(2026, 9, 16, 10, 0, 0, tzinfo=KST)
    if is_frozen():
        src = Path(install_dir())
    else:
        raw_source = os.environ.get("MAILMONSTER_SELFTEST_SOURCE") or ""
        if not raw_source:
            return 7
        src = Path(raw_source)
    src.mkdir(parents=True, exist_ok=True)
    store = CampaignStore(str(src / "sent_history.db"))
    job = store.create_job(
        login_user_id="selftest",
        task_key="네이버_1",
        provider="네이버",
        account_idx=1,
        subject="제목",
        body="본문",
        sender_name="보낸이",
        smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "secret"},
        interval_label="즉시",
        prevent_dup=False,
        apply_public_filter=False,
        template_name="T",
        attachments={"files": [], "imgs": {}},
        recipients=[
            {"업체명": "보존", "이메일": "keep@ex.com"},
            {"업체명": "다음", "이메일": "next@ex.com"},
        ],
        status=JOB_RUNNING,
        now=when,
    )
    job_id = job["job_id"]
    del store
    reset_prepare_cache()
    with patch("data_migrate.install_dir", return_value=str(src)):
        prepare_user_data(force=True)
        chosen = chosen_data_dir()
    if not os.path.normcase(os.path.abspath(chosen)).startswith(os.path.normcase(os.path.abspath(local))):
        return 25
    if os.path.normcase(os.path.abspath(chosen)) == os.path.normcase(str(src)):
        return 26
    dest_db = str(Path(chosen) / "sent_history.db")
    restored = CampaignStore(dest_db)
    if restored.count_by_status(job_id, ITEM_PENDING) != 2:
        return 27
    if not (src / "sent_history.db").is_file():
        return 28
    restarted = CampaignStore(dest_db)
    if restarted.count_by_status(job_id, ITEM_PENDING) != 2:
        return 29

    class _Win:
        def __init__(self, parent=None):
            self.parent = parent
            self.destroyed = False
            self._exists = True
            self.protocols = {}

        def winfo_exists(self):
            return self._exists and not self.destroyed

        def winfo_toplevel(self):
            return self if self.parent is None else self.parent.winfo_toplevel()

        def state(self):
            return "normal"

        def after(self, _ms, fn):
            fn()

        def lift(self):
            return None

        def focus_force(self):
            return None

        def transient(self, _parent):
            return None

        def wait_visibility(self):
            return None

        def attributes(self, *_a):
            return None

        def protocol(self, name, fn):
            self.protocols[name] = fn

        def grab_set(self):
            return None

        def grab_release(self):
            return None

        def destroy(self):
            self.destroyed = True
            self._exists = False

    parent = _Win()
    made = []

    def factory(_parent, **_kwargs):
        win = _Win(parent=_parent)
        made.append(win)
        return win

    dialogs = UiDialogManager(lambda: parent, toplevel_factory=factory, ui_thread_id=threading.get_ident())
    first = dialogs.open_toplevel("attention_job", title="발송 확인 필요", modal=True)
    second = dialogs.open_toplevel("attention_job", title="발송 확인 필요", modal=True)
    if first is None or second is not first or len(made) != 1:
        return 30
    dialogs.close_toplevel(first, key="attention_job")
    if dialogs.is_open("attention_job"):
        return 31

    fresh = restarted.create_job(
        login_user_id="selftest",
        task_key="메일플러그_1",
        provider="메일플러그",
        account_idx=1,
        subject="제목",
        body="본문",
        sender_name="보낸이",
        smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "secret"},
        interval_label="즉시",
        prevent_dup=False,
        apply_public_filter=False,
        template_name="T",
        attachments={"files": [], "imgs": {}},
        recipients=[{"업체명": "한곳", "이메일": "one@ex.com"}],
        status=JOB_RUNNING,
        now=when,
    )
    calls = []

    def runner_for(target, send_once):
        return CampaignRunner(
            restarted,
            BusinessHours(extra_dates=set(), now_fn=lambda: when),
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=send_once,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: when,
            sleep_fn=lambda _s: None,
            owner="selftest",
            worker_id=target.get("worker_id"),
            max_retries=1,
        )

    blocked = runner_for(fresh, lambda payload, job, item: calls.append(payload["email"]) or (True, ""))
    with patch(
        "campaign_runtime.assert_immediate_write",
        side_effect=StorageWriteError(
            "발송 기록",
            chosen,
            preserved=True,
            category="readonly",
            db_path=dest_db,
            smtp_phase="before",
            auto_resend_blocked=True,
        ),
    ):
        blocked.run(fresh["job_id"], wait_off_hours=False)
    if calls:
        return 32
    journal = load_recovery_journal()
    if not journal or journal[-1].get("smtp_phase") != "before":
        return 33
    if restarted.list_items_by_status(fresh["job_id"], ITEM_NEEDS_REVIEW):
        return 34
    reset_write_block()
    if maybe_release_attention(restarted, fresh["job_id"], send_allowed=True, now=when) != JOB_RUNNING:
        return 35
    resumed = runner_for(fresh, lambda payload, job, item: calls.append(payload["email"]) or (True, ""))
    resumed.run(fresh["job_id"], wait_off_hours=False)
    if calls != ["one@ex.com"]:
        return 36

    rows = [{"업체명": "a", "이메일": "first@ex.com"}]
    rows += [{"업체명": "c", "이메일": f"dup{i}@ex.com"} for i in range(500)]
    rows.append({"업체명": "new", "이메일": "new@ex.com"})
    bulk = restarted.create_job(
        login_user_id="selftest",
        task_key="다음_1",
        provider="다음",
        account_idx=1,
        subject="제목",
        body="본문",
        sender_name="보낸이",
        smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "secret"},
        interval_label="3초",
        prevent_dup=False,
        apply_public_filter=False,
        template_name="T",
        attachments={"files": [], "imgs": {}},
        recipients=rows,
        status=JOB_RUNNING,
        now=when,
    )
    sent = []
    sleeps = []
    mono = {"v": 1000.0}

    def prepare(j, item):
        if str(item.get("email") or "").startswith("dup"):
            return "skipped", "duplicate"
        return "ready", {"email": item.get("email")}

    def sleep_fn(seconds):
        sleeps.append(seconds)
        mono["v"] += float(seconds or 0)

    bulk_runner = CampaignRunner(
        restarted,
        BusinessHours(extra_dates=set(), now_fn=lambda: when),
        prepare_fn=prepare,
        send_once_fn=lambda payload, job, item: sent.append(payload["email"]) or (True, ""),
        is_user_stopped=lambda: False,
        is_cancelled=lambda: False,
        interval_seconds_fn=lambda: 3,
        now_fn=lambda: when,
        sleep_fn=sleep_fn,
        owner="selftest-bulk",
        worker_id=bulk.get("worker_id"),
        max_retries=1,
    )
    with patch("campaign_runtime.time.monotonic", lambda: mono["v"]):
        bulk_runner.run(bulk["job_id"], wait_off_hours=False)
    if sent != ["first@ex.com", "new@ex.com"] or sleeps != [1, 1, 1]:
        return 37
    return 0


if __name__ == "__main__":
    if "--ui-self-test" in sys.argv:
        sys.exit(run_ui_self_test())
    if "--v286-self-test" in sys.argv:
        sys.exit(run_v286_self_test())
    if any(arg.startswith("--storage-self-test") for arg in sys.argv):
        sys.exit(run_storage_self_test())
    try:
        state = initialize_user_data()
    except Exception as exc:
        from data_migrate import StorageUnavailable

        if isinstance(exc, StorageUnavailable):
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, exc.message, "MAIL MONSTER PRO", 0x10)
            sys.exit(1)
        raise
    if state == "already-running":
        show_already_running_message(silent=AUTOSTART_RECOVERY)
        sys.exit(0)
    login_window = LoginApp(launch_main_app, autostart_recovery=AUTOSTART_RECOVERY)
    login_window.mainloop()
    pending = consume_pending_launch(login_window)
    if pending:
        launch_main_app(
            pending.get("user_name") or "사용자",
            pending.get("grade") or "무료권",
            pending.get("remaining") or "0",
            pending.get("login_user_id") or "",
        )
