import sys
from data_migrate import prepare_user_data
from login import LoginApp
from main_ui import ModernMailSender
from single_instance import acquire_single_instance, show_already_running_message
from autostart import is_recovery_argv
from ui_dialogs import consume_pending_launch

AUTOSTART_RECOVERY = is_recovery_argv(sys.argv)


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


if __name__ == "__main__":
    if "--ui-self-test" in sys.argv:
        sys.exit(run_ui_self_test())
    prepare_user_data()
    if not acquire_single_instance():
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
