import sys
from data_migrate import prepare_user_data
from login import LoginApp
from main_ui import ModernMailSender
from single_instance import acquire_single_instance, show_already_running_message
from autostart import is_recovery_argv

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


if __name__ == "__main__":
    prepare_user_data()
    if not acquire_single_instance():
        show_already_running_message(silent=AUTOSTART_RECOVERY)
        sys.exit(0)
    login_window = LoginApp(launch_main_app, autostart_recovery=AUTOSTART_RECOVERY)
    login_window.mainloop()
