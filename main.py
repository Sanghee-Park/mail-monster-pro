# main.py 파일은 딱 이 내용만 있어야 합니다!
from login import LoginApp
from main_ui import ModernMailSender

def launch_main_app(user_name, grade, remaining, login_user_id=""):
    app = ModernMailSender(
        user_name=user_name,
        grade=grade,
        remaining=remaining,
        login_user_id=login_user_id,
    )
    app.mainloop()

if __name__ == "__main__":
    login_window = LoginApp(launch_main_app)
    login_window.mainloop()