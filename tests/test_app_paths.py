import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_paths import DATA_DIR_ENV, extra_holidays_example_path, extra_holidays_user_path, find_existing_file, resolve_state_files, writable_file
from autostart import RESUME_ARG, is_recovery_argv


class AppPathTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data = Path(self.td.name) / "data"
        self.data.mkdir()
        self.cwd = Path(self.td.name) / "unrelated_cwd"
        self.cwd.mkdir()
        os.environ[DATA_DIR_ENV] = str(self.data)

    def tearDown(self):
        os.environ.pop(DATA_DIR_ENV, None)
        self.td.cleanup()

    def test_resume_from_foreign_cwd_finds_db_and_settings(self):
        for name in (
            "sent_history.db",
            "login_settings.json",
            "config.json",
            "recipients.json",
            "templates.json",
            "user_profiles.json",
            "extra_holidays.json",
        ):
            (self.data / name).write_text("{}", encoding="utf-8")
        old = os.getcwd()
        try:
            os.chdir(self.cwd)
            self.assertFalse((Path(os.getcwd()) / "sent_history.db").exists())
            paths = resolve_state_files()
            self.assertEqual(Path(paths["sent_history.db"]), self.data / "sent_history.db")
            self.assertEqual(Path(paths["login_settings.json"]), self.data / "login_settings.json")
            self.assertEqual(Path(paths["config.json"]), self.data / "config.json")
            self.assertEqual(Path(find_existing_file("sent_history.db")), self.data / "sent_history.db")
            self.assertTrue(os.path.isabs(writable_file("sent_history.db")))
            self.assertNotEqual(os.path.abspath(os.getcwd()), os.path.dirname(paths["sent_history.db"]))
        finally:
            os.chdir(old)

    def test_resume_argv_alias(self):
        self.assertTrue(is_recovery_argv(["main.py", "--resume"]))
        self.assertTrue(is_recovery_argv(["main.py", "--autostart-recovery"]))
        self.assertFalse(is_recovery_argv(["main.py"]))
        self.assertEqual(RESUME_ARG, "--resume")

    def test_extra_holidays_example_is_not_user_file(self):
        example = extra_holidays_example_path()
        user = extra_holidays_user_path()
        self.assertTrue(example.endswith("extra_holidays.example.json"))
        self.assertTrue(user.endswith("extra_holidays.json"))
        self.assertNotEqual(os.path.normcase(example), os.path.normcase(user))

    def test_no_getcwd_relative_lookup(self):
        roots = Path(__file__).resolve().parents[1]
        skip = {".venv", "tests", "build", "dist"}
        offenders = []
        for py in roots.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            if "= os.getcwd()" in text or "os.getcwd()" in text and py.name != "app_paths.py":
                if "os.getcwd()" in text:
                    offenders.append(py.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
