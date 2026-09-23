import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_paths import DATA_DIR_ENV, resolve_state_files
from campaign_store import CampaignStore
from data_migrate import (
    chosen_data_dir,
    copy_file_atomic,
    directory_supports_replace_and_wal,
    prepare_user_data,
    reset_prepare_cache,
)
from json_atomic import (
    StorageWriteError,
    atomic_write_json,
    file_allows_write,
    read_json_object,
    update_json_object,
)
from main_ui import ModernMailSender


SECRET = "smtp-password-must-not-appear"


def _deny(src, dst):
    err = PermissionError(13, "Access is denied", src)
    err.winerror = 5
    raise err


class JsonStorageTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_replace_succeeds_after_transient_locks(self):
        folder = self.root / "새 폴더 (3)"
        folder.mkdir()
        path = folder / "recipients.json"
        path.write_text('{"old": true}', encoding="utf-8")
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                _deny(src, dst)
            return real(src, dst)

        with patch("json_atomic.os.replace", side_effect=flaky), patch("json_atomic.time.sleep"):
            atomic_write_json(str(path), {"네이버_1": {"row_count": 1}}, kind="수신처 목록")
        self.assertGreaterEqual(calls["n"], 3)
        self.assertEqual(read_json_object(str(path))["네이버_1"]["row_count"], 1)
        self.assertEqual(list(folder.glob("mm_json_*.tmp")), [])

    def test_permanent_replace_failure_keeps_original_json(self):
        folder = self.root / "새 폴더 (3)"
        folder.mkdir()
        path = folder / "recipients.json"
        original = '{"네이버_1": {"rows": [{"이메일": "keep@ex.com"}]}}'
        path.write_text(original, encoding="utf-8")
        calls = {"n": 0}
        success = False

        def always(src, dst):
            calls["n"] += 1
            _deny(src, dst)

        with patch("json_atomic.os.replace", side_effect=always), patch("json_atomic.time.sleep"):
            with self.assertRaises(StorageWriteError) as caught:
                atomic_write_json(str(path), {"wiped": True}, kind="수신처 목록")
                success = True
        self.assertFalse(success)
        self.assertEqual(calls["n"], 6)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(folder.glob("mm_json_*.tmp")), [])
        text = str(caught.exception)
        self.assertIn("수신처 목록", text)
        self.assertIn("데이터 폴더", text)
        self.assertIn(str(folder), text)
        self.assertIn("덮어쓰지 않았습니다", text)
        self.assertIn("다시 시도", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("keep@ex.com", text)

    def test_non_lock_error_is_not_retried(self):
        path = self.root / "recipients.json"
        path.write_text("{}", encoding="utf-8")
        calls = {"n": 0}

        def once(src, dst):
            calls["n"] += 1
            raise OSError(28, "No space left on device")

        with patch("json_atomic.os.replace", side_effect=once), patch("json_atomic.time.sleep"):
            with self.assertRaises(StorageWriteError):
                atomic_write_json(str(path), {"x": 1}, kind="수신처 목록")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "{}")

    def test_concurrent_updates_keep_every_account(self):
        path = self.root / "새 폴더 (3)" / "recipients.json"
        path.parent.mkdir()
        path.write_text("{}", encoding="utf-8")
        errors = []

        def worker(index):
            key = f"계정_{index}"
            try:
                def mutate(data, account=key, n=index):
                    data[account] = {"row_count": n, "rows": []}

                update_json_object(str(path), mutate, kind="수신처 목록")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        payload = read_json_object(str(path))
        self.assertEqual(set(payload), {f"계정_{i}" for i in range(12)})
        self.assertEqual(payload["계정_7"]["row_count"], 7)

    def test_failed_recipient_save_is_not_marked_success(self):
        db = self.root / "sent_history.db"
        path = self.root / "recipients.json"
        original = '{"메일플러그_1": {"rows": [{"이메일": "old@ex.com"}]}}'
        path.write_text(original, encoding="utf-8")
        store = CampaignStore(str(db))
        host = SimpleNamespace(
            campaign_store=store,
            login_user_id="alice",
            recipients_file=str(path),
        )
        success = False
        with patch("json_atomic.os.replace", side_effect=_deny), patch("json_atomic.time.sleep"):
            with self.assertRaises(StorageWriteError):
                ModernMailSender.save_recipients_rows(
                    host,
                    "메일플러그_1",
                    [{"업체명": "회사", "이메일": "new@ex.com"}],
                )
                success = True
        self.assertFalse(success)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(store.count_account_recipients("alice", "메일플러그_1"), 1)


class SqliteStorageTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)
        self._old_data = os.environ.get(DATA_DIR_ENV)
        self._old_local = os.environ.get("LOCALAPPDATA")
        reset_prepare_cache()

    def tearDown(self):
        reset_prepare_cache()
        if self._old_data is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = self._old_data
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        self.td.cleanup()

    def _seed_legacy_db(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "recipients.json").write_text(
            '{"네이버_1": {"rows": [{"이메일": "keep@ex.com"}]}}',
            encoding="utf-8",
        )
        (folder / "templates.json").write_text('{"안내": {"title": "제목"}}', encoding="utf-8")
        (folder / "config.json").write_text('{"네이버_1": {"pw": "%s"}}' % SECRET, encoding="utf-8")
        db = folder / "sent_history.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sent_log(email TEXT, message_id TEXT)")
        con.execute(
            "INSERT INTO sent_log(email, message_id) VALUES (?, ?)",
            ("keep@ex.com", "<legacy@mail-monster.pro>"),
        )
        con.commit()
        con.close()

    def test_readonly_database_is_cleared_and_history_kept(self):
        db = self.root / "sent_history.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sent_log(email TEXT)")
        con.execute("INSERT INTO sent_log(email) VALUES ('keep@ex.com')")
        con.commit()
        con.close()
        os.chmod(db, stat.S_IREAD)
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(db), 0x1)
        self.assertFalse(file_allows_write(str(db)))
        store = CampaignStore(str(db))
        con = sqlite3.connect(str(db))
        row = con.execute("SELECT email FROM sent_log").fetchone()
        con.close()
        del store
        self.assertEqual(row[0], "keep@ex.com")
        self.assertTrue(file_allows_write(str(db)))
        self.assertGreater(db.stat().st_size, 0)

    def test_unwritable_install_moves_to_localappdata_without_deleting_source(self):
        src = self.root / "Program Files" / "MAIL MONSTER PRO"
        local = self.root / "Local"
        self._seed_legacy_db(src)
        src_db = (src / "sent_history.db").read_bytes()
        os.chmod(src / "sent_history.db", stat.S_IREAD)
        os.environ.pop(DATA_DIR_ENV, None)
        os.environ["LOCALAPPDATA"] = str(local)
        reset_prepare_cache()
        from json_atomic import clear_readonly as real_clear
        from json_atomic import file_allows_write as real_allows

        def fake_clear(path):
            if os.path.basename(path) == "sent_history.db" and os.path.normcase(str(path)).startswith(os.path.normcase(str(src))):
                return False
            return real_clear(path)

        def fake_allows(path):
            if os.path.basename(path) == "sent_history.db" and os.path.normcase(str(path)).startswith(os.path.normcase(str(src))):
                return False
            return real_allows(path)

        with patch("data_migrate.install_dir", return_value=str(src)), patch(
            "data_migrate.clear_readonly", side_effect=fake_clear
        ), patch("data_migrate.file_allows_write", side_effect=fake_allows):
            report = prepare_user_data(force=True)
            chosen = chosen_data_dir()
        dest = Path(chosen)
        self.assertFalse(report.used_portable)
        self.assertTrue(os.path.normcase(str(dest)).startswith(os.path.normcase(str(local))))
        self.assertEqual((src / "sent_history.db").read_bytes(), src_db)
        self.assertTrue((src / "recipients.json").is_file())
        self.assertTrue((src / "templates.json").is_file())
        self.assertIn("keep@ex.com", (dest / "recipients.json").read_text(encoding="utf-8"))
        self.assertIn("제목", (dest / "templates.json").read_text(encoding="utf-8"))
        self.assertTrue((dest / "mm-backup").is_dir())
        con = sqlite3.connect(dest / "sent_history.db")
        email = con.execute("SELECT email FROM sent_log").fetchone()[0]
        con.close()
        self.assertEqual(email, "keep@ex.com")

    def test_existing_destination_is_not_overwritten(self):
        src = self.root / "install"
        local = self.root / "Local"
        self._seed_legacy_db(src)
        dest_root = local / "MAIL_MONSTER_PRO"
        dest_root.mkdir(parents=True)
        (dest_root / "templates.json").write_text('{"keep":"DEST"}', encoding="utf-8")
        (dest_root / "sent_history.db").write_bytes(b"DESTDB")
        os.environ.pop(DATA_DIR_ENV, None)
        os.environ["LOCALAPPDATA"] = str(local)
        reset_prepare_cache()
        from json_atomic import clear_readonly as real_clear
        from json_atomic import file_allows_write as real_allows

        def fake_clear(path):
            if os.path.normcase(str(path)).startswith(os.path.normcase(str(src))):
                return False
            return real_clear(path)

        def fake_allows(path):
            if os.path.normcase(str(path)).startswith(os.path.normcase(str(src))):
                return False
            return real_allows(path)

        with patch("data_migrate.install_dir", return_value=str(src)), patch(
            "data_migrate.clear_readonly", side_effect=fake_clear
        ), patch("data_migrate.file_allows_write", side_effect=fake_allows):
            report = prepare_user_data(force=True)
        self.assertIn("templates.json", report.conflicts)
        self.assertIn("sent_history.db", report.conflicts)
        self.assertEqual((dest_root / "templates.json").read_text(encoding="utf-8"), '{"keep":"DEST"}')
        self.assertEqual((dest_root / "sent_history.db").read_bytes(), b"DESTDB")
        self.assertTrue((src / "templates.json").is_file())
        self.assertIn("제목", (src / "templates.json").read_text(encoding="utf-8"))

    def test_wal_sidecar_unavailable_uses_localappdata(self):
        src = self.root / "install"
        local = self.root / "Local"
        self._seed_legacy_db(src)
        src_db = (src / "sent_history.db").read_bytes()
        os.environ.pop(DATA_DIR_ENV, None)
        os.environ["LOCALAPPDATA"] = str(local)
        reset_prepare_cache()
        real_isfile = os.path.isfile

        def hide_wal(path):
            text = os.path.normcase(str(path))
            if text.startswith(os.path.normcase(str(src))) and (text.endswith("-wal") or text.endswith("-shm")):
                return False
            return real_isfile(path)

        with patch("data_migrate.install_dir", return_value=str(src)), patch(
            "data_migrate.os.path.isfile", side_effect=hide_wal
        ):
            self.assertFalse(directory_supports_replace_and_wal(str(src)))
            report = prepare_user_data(force=True)
            chosen = chosen_data_dir()
        self.assertFalse(report.used_portable)
        self.assertTrue(os.path.normcase(chosen).startswith(os.path.normcase(str(local))))
        self.assertEqual((src / "sent_history.db").read_bytes(), src_db)
        con = sqlite3.connect(Path(chosen) / "sent_history.db")
        moved = con.execute("SELECT email FROM sent_log").fetchone()[0]
        con.close()
        self.assertEqual(moved, "keep@ex.com")
        paths = {
            name: os.path.dirname(path)
            for name, path in {
                "db": str(Path(chosen) / "sent_history.db"),
                "json": str(Path(chosen) / "recipients.json"),
            }.items()
        }
        self.assertEqual(len(set(os.path.normcase(p) for p in paths.values())), 1)

    def test_same_db_from_another_working_directory(self):
        data = self.root / "data"
        other = self.root / "other cwd"
        data.mkdir()
        other.mkdir()
        os.environ[DATA_DIR_ENV] = str(data)
        reset_prepare_cache()
        old = os.getcwd()
        try:
            os.chdir(other)
            first = resolve_state_files()["sent_history.db"]
            store = CampaignStore(first)
            con = sqlite3.connect(first)
            con.execute("CREATE TABLE cwd_probe(note TEXT)")
            con.execute("INSERT INTO cwd_probe(note) VALUES ('same')")
            con.commit()
            con.close()
            os.chdir(self.root)
            second = resolve_state_files()["sent_history.db"]
            self.assertEqual(os.path.normcase(first), os.path.normcase(second))
            con = sqlite3.connect(second)
            note = con.execute("SELECT note FROM cwd_probe").fetchone()[0]
            con.close()
            del store
            self.assertEqual(note, "same")
            self.assertFalse((other / "sent_history.db").exists())
        finally:
            os.chdir(old)

    def test_copied_readonly_db_can_be_written(self):
        src = self.root / "src.db"
        dest = self.root / "dest.db"
        con = sqlite3.connect(src)
        con.execute("CREATE TABLE sent_log(email TEXT)")
        con.execute("INSERT INTO sent_log(email) VALUES ('keep@ex.com')")
        con.commit()
        con.close()
        os.chmod(src, stat.S_IREAD)
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(src), 0x1)
        copy_file_atomic(str(src), str(dest))
        self.assertEqual(src.read_bytes(), dest.read_bytes())
        self.assertTrue(file_allows_write(str(dest)))
        store = CampaignStore(str(dest))
        con = sqlite3.connect(dest)
        con.execute("INSERT INTO sent_log(email) VALUES ('next@ex.com')")
        con.commit()
        count = con.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0]
        con.close()
        del store
        self.assertEqual(count, 2)

    def test_concurrent_account_recipient_saves(self):
        db = self.root / "sent_history.db"
        path = self.root / "recipients.json"
        path.write_text("{}", encoding="utf-8")
        store = CampaignStore(str(db))
        host = SimpleNamespace(
            campaign_store=store,
            login_user_id="alice",
            recipients_file=str(path),
        )
        errors = []

        def worker(index):
            key = "네이버_1" if index % 2 == 0 else "메일플러그_1"
            email = f"user{index}@ex.com"
            try:
                ModernMailSender.save_recipients_rows(
                    host,
                    key,
                    [{"업체명": key, "이메일": email}],
                )
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        payload = read_json_object(str(path))
        self.assertIn("네이버_1", payload)
        self.assertIn("메일플러그_1", payload)
        self.assertEqual(store.count_account_recipients("alice", "네이버_1"), 1)
        self.assertEqual(store.count_account_recipients("alice", "메일플러그_1"), 1)
        self.assertNotEqual(
            store.list_account_recipients("alice", "네이버_1")[0]["이메일"],
            store.list_account_recipients("alice", "메일플러그_1")[0]["이메일"],
        )


if __name__ == "__main__":
    unittest.main()
