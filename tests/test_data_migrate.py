import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_paths import DATA_DIR_ENV
from campaign_store import CampaignStore, ITEM_SENT, JOB_RUNNING
from data_migrate import (
    MIGRATABLE_FILES,
    copy_file_atomic,
    format_migration_user_message,
    migrate_legacy_data_files,
    prepare_user_data,
    reset_prepare_cache,
)


SECRET = "super-secret-smtp-password-do-not-log"


def kst_job_now():
    from datetime import datetime
    from business_hours import KST

    return datetime(2026, 9, 16, 10, 0, 0, tzinfo=KST)


class DataMigrateTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)
        self.src = self.root / "ProgramFiles" / "MAIL MONSTER PRO"
        self.dest = self.root / "LocalAppData" / "MAIL_MONSTER_PRO"
        self.src.mkdir(parents=True)
        self.dest.mkdir(parents=True)
        reset_prepare_cache()
        self._old_data = os.environ.get(DATA_DIR_ENV)
        os.environ[DATA_DIR_ENV] = str(self.dest)

    def tearDown(self):
        reset_prepare_cache()
        if self._old_data is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = self._old_data
        self.td.cleanup()

    def _seed_legacy(self, *, with_db=True):
        names = {
            "login_settings.json": '{"uid":"user","pw":"%s"}' % SECRET,
            "config.json": '{"네이버_1":{"id":"a","pw":"%s"}}' % SECRET,
            "recipients.json": "[]",
            "templates.json": "{}",
            "user_profiles.json": "{}",
            "extra_holidays.json": '{"dates":[]}',
        }
        for name, text in names.items():
            (self.src / name).write_text(text, encoding="utf-8")
        if with_db:
            db = str(self.src / "sent_history.db")
            store = CampaignStore(db)
            job = store.create_job(
                login_user_id="alice",
                task_key="네이버_1",
                provider="네이버",
                account_idx=1,
                subject="제목",
                body="본문",
                sender_name="홍길동",
                smtp_config={"smtp": "localhost", "port": 465, "id": "id", "pw": SECRET},
                interval_label="1분",
                prevent_dup=True,
                apply_public_filter=False,
                template_name="T",
                attachments={"files": [], "imgs": {}},
                recipients=[{"업체명": "c1", "이메일": "a1@ex.com"}],
                status=JOB_RUNNING,
                now=kst_job_now(),
            )
            item = store.next_pending(job["job_id"])
            store.mark_item(item["id"], ITEM_SENT, error_message="ok")
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE IF NOT EXISTS sent_log (id INTEGER PRIMARY KEY, email TEXT, message_id TEXT)"
            )
            con.execute(
                "INSERT INTO sent_log(email, message_id) VALUES (?, ?)",
                ("a1@ex.com", "<legacy.1@mail-monster.pro>"),
            )
            con.commit()
            con.close()
            self.job_id = job["job_id"]
        return names

    def test_readonly_install_copies_when_localappdata_empty(self):
        self._seed_legacy()
        report = migrate_legacy_data_files(str(self.src), str(self.dest), source_writable=False)
        self.assertFalse(report.used_portable)
        self.assertIn("sent_history.db", report.migrated)
        for name in MIGRATABLE_FILES:
            self.assertTrue((self.dest / name).is_file(), name)
            self.assertTrue((self.src / name).is_file(), name)
            self.assertEqual(
                (self.src / name).read_bytes(),
                (self.dest / name).read_bytes(),
            )
        self.assertFalse(report.conflicts)
        self.assertFalse(report.failed)

    def test_existing_localappdata_not_overwritten(self):
        self._seed_legacy()
        (self.dest / "config.json").write_text('{"keep":"DEST_ONLY"}', encoding="utf-8")
        (self.dest / "sent_history.db").write_bytes(b"DESTDB")
        src_cfg = (self.src / "config.json").read_text(encoding="utf-8")
        report = migrate_legacy_data_files(str(self.src), str(self.dest), source_writable=False)
        self.assertIn("config.json", report.skipped_existing)
        self.assertIn("sent_history.db", report.skipped_existing)
        self.assertIn("config.json", report.conflicts)
        self.assertEqual((self.dest / "config.json").read_text(encoding="utf-8"), '{"keep":"DEST_ONLY"}')
        self.assertEqual((self.dest / "sent_history.db").read_bytes(), b"DESTDB")
        self.assertEqual((self.src / "config.json").read_text(encoding="utf-8"), src_cfg)
        msg = format_migration_user_message(report)
        self.assertIn("사용자 폴더 파일을 사용", msg)
        self.assertNotIn(SECRET, msg)
        self.assertNotIn("DEST_ONLY", msg)

    def test_inherited_db_can_query_campaign_and_sent_log(self):
        self._seed_legacy()
        report = migrate_legacy_data_files(str(self.src), str(self.dest), source_writable=False)
        self.assertIn("sent_history.db", report.migrated)
        dest_db = str(self.dest / "sent_history.db")
        store = CampaignStore(dest_db)
        job = store.get_job(self.job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["subject"], "제목")
        items = store.list_items_by_status(self.job_id, ITEM_SENT)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["email"], "a1@ex.com")
        con = sqlite3.connect(dest_db)
        row = con.execute("SELECT email FROM sent_log").fetchone()
        con.close()
        self.assertEqual(row[0], "a1@ex.com")

    def test_partial_copy_failure_preserves_source(self):
        names = self._seed_legacy()
        src_cfg = (self.src / "config.json").read_text(encoding="utf-8")
        src_db = (self.src / "sent_history.db").read_bytes()

        def flaky(src, dest):
            if os.path.basename(src) == "config.json":
                raise OSError("simulated-copy-fail")
            copy_file_atomic(src, dest)

        report = migrate_legacy_data_files(
            str(self.src),
            str(self.dest),
            source_writable=False,
            copy_fn=flaky,
        )
        self.assertTrue(any(n == "config.json" for n, _ in report.failed))
        self.assertIn("sent_history.db", report.migrated)
        self.assertEqual((self.src / "config.json").read_text(encoding="utf-8"), src_cfg)
        self.assertEqual((self.src / "sent_history.db").read_bytes(), src_db)
        self.assertFalse((self.dest / "config.json").exists())
        self.assertTrue((self.dest / "sent_history.db").is_file())
        for name in names:
            self.assertTrue((self.src / name).is_file())
        msg = format_migration_user_message(report)
        self.assertIn(str(self.src), msg)
        self.assertIn("원본은 삭제되지 않았습니다", msg)
        self.assertNotIn(SECRET, msg)
        self.assertNotIn("simulated-copy-fail", msg)

    def test_writable_portable_does_not_move(self):
        self._seed_legacy()
        report = migrate_legacy_data_files(str(self.src), str(self.dest), source_writable=True)
        self.assertTrue(report.used_portable)
        self.assertEqual(report.migrated, [])
        self.assertFalse((self.dest / "sent_history.db").exists())
        self.assertTrue((self.src / "sent_history.db").is_file())
        self.assertEqual(format_migration_user_message(report), "")

    def test_prepare_uses_env_dest_when_install_readonly(self):
        self._seed_legacy()
        reset_prepare_cache()
        with patch("data_migrate.install_dir", return_value=str(self.src)), patch(
            "data_migrate._dir_is_writable", return_value=False
        ):
            report = prepare_user_data(force=True)
        self.assertFalse(report.used_portable)
        self.assertIn("sent_history.db", report.migrated)
        self.assertTrue((self.dest / "login_settings.json").is_file())

    def test_atomic_copy_does_not_leave_tmp(self):
        src = self.src / "templates.json"
        src.write_text("{}", encoding="utf-8")
        dest = self.dest / "templates.json"
        copy_file_atomic(str(src), str(dest))
        self.assertTrue(dest.is_file())
        self.assertFalse((self.dest / "templates.json.mm_migrating").exists())


if __name__ == "__main__":
    unittest.main()
