import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostart import MemoryRegistry, build_autostart_command, disable_autostart, enable_autostart, is_autostart_enabled, quote_win_arg, sync_autostart
from single_instance import acquire_single_instance, mutex_name


class AutostartTests(unittest.TestCase):
    def test_quote_keeps_korean_and_spaces(self):
        path = r"C:\Users\tkd83\Desktop\이메일자동수집파일\MAIL_MONSTER_PRO.exe"
        q = quote_win_arg(path)
        self.assertTrue(q.startswith('"') and q.endswith('"'))
        self.assertIn("이메일자동수집파일", q)

    def test_command_includes_recovery_flag(self):
        cmd = build_autostart_command()
        self.assertIn("--resume", cmd)
        self.assertNotIn("os.getcwd", cmd)
        self.assertIn(quote_win_arg(sys.executable).strip('"')[:2], cmd.replace('"', ""))

    def test_sync_enable_disable(self):
        mem = MemoryRegistry()
        sync_autostart(True, backend=mem, base_dir=".")
        self.assertTrue(is_autostart_enabled(mem))
        self.assertIn("--resume", mem.get("MAIL_MONSTER_PRO"))
        sync_autostart(False, backend=mem)
        self.assertFalse(is_autostart_enabled(mem))


class SingleInstanceTests(unittest.TestCase):
    def test_file_lock_second_fails(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            os.environ["MAILMONSTER_SKIP_MUTEX"] = ""
            os.environ["MAILMONSTER_LOCK_DIR"] = td.name
            os.environ["MAILMONSTER_MUTEX_NAME"] = "test-mm-lock"
            # 강제 파일 잠금 경로
            from single_instance import _acquire_file_lock

            ok1 = _acquire_file_lock("test-mm-lock")
            ok2 = _acquire_file_lock("test-mm-lock")
            self.assertTrue(ok1)
            self.assertFalse(ok2)
        finally:
            os.environ.pop("MAILMONSTER_LOCK_DIR", None)
            os.environ.pop("MAILMONSTER_MUTEX_NAME", None)
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
