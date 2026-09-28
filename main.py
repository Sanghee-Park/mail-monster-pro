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


if __name__ == "__main__":
    if "--ui-self-test" in sys.argv:
        sys.exit(run_ui_self_test())
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
