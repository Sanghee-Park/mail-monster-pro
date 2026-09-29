import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_paths import storage_block_reason
from business_hours import KST, BusinessHours
from campaign_attention import maybe_release_attention, replace_job_attachments
from campaign_runtime import CampaignRunner
from campaign_store import (
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING,
    ITEM_SENT,
    ITEM_SKIPPED,
    JOB_CANCELLED,
    JOB_NEEDS_ATTENTION,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
    CampaignStore,
)
from data_migrate import apply_database_choice, autosend_blocked, prepare_user_data, reset_prepare_cache
from db_access import database_inventory, load_recovery_journal, reset_write_block
from json_atomic import StorageWriteError
from unittest.mock import patch


def kst(hour=10, minute=0):
    return datetime(2026, 9, 16, hour, minute, 0, tzinfo=KST)


class V286Tests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)
        self.old_local = os.environ.get("LOCALAPPDATA")
        self.old_data = os.environ.get("MAILMONSTER_DATA_DIR")
        os.environ["LOCALAPPDATA"] = str(self.root / "Local")
        os.environ["MAILMONSTER_SCAN_CLOUD"] = "0"
        os.environ.pop("MAILMONSTER_DATA_DIR", None)
        reset_write_block()
        reset_prepare_cache()

    def tearDown(self):
        reset_write_block()
        reset_prepare_cache()
        if self.old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self.old_local
        if self.old_data is None:
            os.environ.pop("MAILMONSTER_DATA_DIR", None)
        else:
            os.environ["MAILMONSTER_DATA_DIR"] = self.old_data
        self.td.cleanup()

    def test_onedrive_path_is_rejected(self):
        path = r"C:\Users\dkdk6\OneDrive\Desktop\CI비"
        self.assertIn("OneDrive", storage_block_reason(path) or "")
        self.assertIn("네트워크", storage_block_reason(r"\\server\share\db") or "")

    def test_onedrive_db_migrates_without_deleting_source(self):
        src = self.root / "OneDrive" / "Desktop" / "CI비"
        src.mkdir(parents=True)
        db = src / "sent_history.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sent_log(email TEXT)")
        con.execute("INSERT INTO sent_log(email) VALUES ('keep@ex.com')")
        con.commit()
        con.close()
        (src / "recipients.json").write_text('{"네이버_1":{"rows":[{"이메일":"keep@ex.com"}]}}', encoding="utf-8")
        before = (src / "recipients.json").read_text(encoding="utf-8")
        with patch("data_migrate.install_dir", return_value=str(src)):
            report = prepare_user_data(force=True)
        dest = Path(os.environ["LOCALAPPDATA"]) / "MAIL_MONSTER_PRO"
        self.assertTrue((dest / "sent_history.db").is_file())
        self.assertEqual((src / "recipients.json").read_text(encoding="utf-8"), before)
        con = sqlite3.connect(dest / "sent_history.db")
        email = con.execute("SELECT email FROM sent_log").fetchone()[0]
        con.close()
        self.assertEqual(email, "keep@ex.com")
        self.assertTrue(src.joinpath("sent_history.db").is_file())
        self.assertFalse(report.block_autosend)

    def test_different_dbs_are_not_overwritten(self):
        src = self.root / "exe"
        src.mkdir()
        dest_root = Path(os.environ["LOCALAPPDATA"]) / "MAIL_MONSTER_PRO"
        dest_root.mkdir(parents=True)
        con = sqlite3.connect(src / "sent_history.db")
        con.execute("CREATE TABLE sent_log(email TEXT)")
        con.execute("INSERT INTO sent_log(email) VALUES ('old@ex.com')")
        con.commit()
        con.close()
        con = sqlite3.connect(dest_root / "sent_history.db")
        con.execute("CREATE TABLE sent_log(email TEXT)")
        con.execute("INSERT INTO sent_log(email) VALUES ('new@ex.com')")
        con.commit()
        con.close()
        with patch("data_migrate.install_dir", return_value=str(src)):
            prepare_user_data(force=True)
        con = sqlite3.connect(dest_root / "sent_history.db")
        emails = [row[0] for row in con.execute("SELECT email FROM sent_log")]
        con.close()
        self.assertCountEqual(emails, ["new@ex.com", "old@ex.com"])
        self.assertTrue((src / "sent_history.db").is_file())
        from data_migrate import last_migration_report

        report = last_migration_report()
        self.assertFalse(report.block_autosend)
        self.assertFalse(report.needs_choice)
        self.assertIn("sent_log", report.detail_note)
        self.assertTrue((dest_root / "mm-backup").is_dir())
        again = prepare_user_data(force=True)
        self.assertFalse(again.needs_choice)
        self.assertFalse(again.block_autosend)

    def _store(self):
        return CampaignStore(str(self.root / "sent_history.db"))

    def _job(self, store, rows, task_key="네이버_1"):
        return store.create_job(
            login_user_id="alice",
            task_key=task_key,
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
            now=kst(),
        )

    def _runner(self, store, job, send_once, interval=60, clock=None):
        clock = clock or {"v": kst()}
        sleeps = []

        def sleep_fn(seconds):
            sleeps.append(seconds)
            clock["v"] = clock["v"] + timedelta(seconds=int(seconds or 0))

        runner = CampaignRunner(
            store,
            BusinessHours(extra_dates=set(), now_fn=lambda: clock["v"]),
            prepare_fn=lambda j, i: ("ready", {"email": i["email"], "body_hash": "same" if i["email"].startswith("dup") else i["email"]}),
            send_once_fn=send_once,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: interval,
            now_fn=lambda: clock["v"],
            sleep_fn=sleep_fn,
            monotonic_fn=lambda: clock["v"].timestamp(),
            owner="owner",
            worker_id=job.get("worker_id"),
            max_retries=1,
        )
        runner.sleeps = sleeps
        runner.clock = clock
        return runner

    def test_duplicate_skips_do_not_sleep_interval(self):
        store = self._store()
        rows = [{"업체명": "a", "이메일": "first@ex.com"}]
        rows += [{"업체명": "c", "이메일": f"dup{i}@ex.com"} for i in range(4)]
        rows.append({"업체명": "new", "이메일": "new@ex.com"})
        job = self._job(store, rows)
        sent = []

        def send_once(payload, job, item):
            sent.append(payload["email"])
            return True, ""

        runner = self._runner(store, job, send_once, interval=60)

        def prepare(j, item):
            email = item["email"]
            if email.startswith("dup"):
                return "skipped", "duplicate"
            return "ready", {"email": email}

        runner.prepare_fn = prepare
        runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(sent, ["first@ex.com", "new@ex.com"])
        self.assertEqual(runner.sleeps, [1] * 60)
        self.assertEqual(len(store.list_items_by_status(job["job_id"], ITEM_SKIPPED)), 4)

    def test_empty_review_releases_attention(self):
        store = self._store()
        job = self._job(store, [{"업체명": "c", "이메일": "a@ex.com"}])
        store.set_needs_attention(job["job_id"], "저장 오류", attention_code="storage_before_smtp")
        status = maybe_release_attention(store, job["job_id"], send_allowed=True, now=kst())
        self.assertEqual(status, JOB_RUNNING)
        self.assertEqual(store.count_by_status(job["job_id"], ITEM_PENDING), 1)

    def test_before_smtp_restart_stays_pending(self):
        os.environ["MAILMONSTER_DATA_DIR"] = str(self.root)
        store = self._store()
        job = self._job(store, [{"업체명": "c", "이메일": "a@ex.com"}])
        calls = []

        def send_once(payload, job, item):
            calls.append(1)
            return True, ""

        runner = self._runner(store, job, send_once, interval=0)
        with patch("campaign_runtime.assert_immediate_write", side_effect=StorageWriteError(
            "발송 기록", str(self.root), preserved=True, category="readonly",
            db_path=str(self.root / "sent_history.db"), smtp_phase="before", auto_resend_blocked=True,
        )):
            runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(calls, [])
        self.assertEqual(store.list_items_by_status(job["job_id"], ITEM_PENDING)[0]["status"], ITEM_PENDING)
        self.assertEqual(load_recovery_journal()[0]["smtp_phase"], "before")
        reset_write_block()
        again = CampaignStore(str(self.root / "sent_history.db"))
        self.assertEqual(again.list_items_by_status(job["job_id"], ITEM_NEEDS_REVIEW), [])
        self.assertEqual(len(again.list_items_by_status(job["job_id"], ITEM_PENDING)), 1)

    def test_active_campaign_rows_survive_backup_migration(self):
        src = self.root / "OneDrive" / "Desktop" / "CI비"
        src.mkdir(parents=True)
        store = CampaignStore(str(src / "sent_history.db"))
        job = self._job(store, [{"업체명": "보존", "이메일": "keep@ex.com"}, {"업체명": "다음", "이메일": "next@ex.com"}])
        con = sqlite3.connect(store.db_path)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                reason TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        con.execute("INSERT INTO blacklist(email, reason, added_at) VALUES ('bad@ex.com', 't', datetime('now'))")
        con.commit()
        con.close()
        before = database_inventory(store.db_path)
        with patch("data_migrate.install_dir", return_value=str(src)):
            report = prepare_user_data(force=True)
        dest = Path(os.environ["LOCALAPPDATA"]) / "MAIL_MONSTER_PRO" / "sent_history.db"
        after = database_inventory(str(dest))
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(before["pending"], after["pending"])
        self.assertFalse(report.block_autosend)
        restored = CampaignStore(str(dest))
        self.assertEqual(restored.get_job(job["job_id"])["status"], JOB_RUNNING)
        self.assertEqual(restored.count_by_status(job["job_id"], ITEM_PENDING), 2)
        self.assertTrue((src / "sent_history.db").is_file())

    def test_file_turns_readonly_before_smtp(self):
        store = self._store()
        job = self._job(store, [{"업체명": "c", "이메일": "a@ex.com"}])
        calls = []

        def send_once(payload, job, item):
            calls.append(1)
            return True, ""

        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFileAttributesW.argtypes = (ctypes.c_wchar_p,)
        kernel.GetFileAttributesW.restype = ctypes.c_uint32
        kernel.SetFileAttributesW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        kernel.SetFileAttributesW.restype = ctypes.c_int
        attrs = kernel.GetFileAttributesW(store.db_path)
        kernel.SetFileAttributesW(store.db_path, attrs | 0x1)
        runner = self._runner(store, job, send_once, interval=60)
        try:
            with patch("json_atomic.clear_readonly", return_value=False), patch(
                "db_access.clear_readonly", return_value=False
            ):
                result = runner.run(job["job_id"], wait_off_hours=False)
        finally:
            kernel.SetFileAttributesW(store.db_path, attrs & ~0x1)
        self.assertEqual(calls, [])
        self.assertEqual(runner.smtp_calls, 0)
        self.assertEqual(result, JOB_NEEDS_ATTENTION)
        self.assertEqual(store.list_items_by_status(job["job_id"], ITEM_NEEDS_REVIEW), [])
        self.assertEqual(store.list_items_by_status(job["job_id"], ITEM_SENT), [])
        self.assertEqual(load_recovery_journal()[0]["smtp_phase"], "before")

    def test_five_hundred_duplicates_do_not_wait_each(self):
        store = self._store()
        rows = [{"업체명": "a", "이메일": "first@ex.com"}]
        rows += [{"업체명": "c", "이메일": f"dup{i}@ex.com"} for i in range(500)]
        rows.append({"업체명": "new", "이메일": "new@ex.com"})
        job = self._job(store, rows)
        sent = []

        def send_once(payload, job, item):
            sent.append(payload["email"])
            return True, ""

        runner = self._runner(store, job, send_once, interval=3)

        def prepare(j, item):
            if str(item["email"]).startswith("dup"):
                return "skipped", "duplicate"
            return "ready", {"email": item["email"]}

        runner.prepare_fn = prepare
        progress = []
        runner.on_progress = lambda stats: progress.append(1)
        mono = {"v": 1000.0}
        base_sleep = runner.sleep_fn

        def sleep_fn(seconds):
            base_sleep(seconds)
            mono["v"] += float(seconds or 0)

        runner.sleep_fn = sleep_fn
        with patch("campaign_runtime.time.monotonic", lambda: mono["v"]):
            runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(sent, ["first@ex.com", "new@ex.com"])
        self.assertEqual(runner.sleeps, [1, 1, 1])
        self.assertEqual(len(store.list_items_by_status(job["job_id"], ITEM_SKIPPED)), 500)
        self.assertLess(len(progress), 500)

    def test_blacklist_and_public_filter_do_not_wait(self):
        store = self._store()
        rows = [
            {"업체명": "a", "이메일": "first@ex.com"},
            {"업체명": "b", "이메일": "blocked@ex.com"},
            {"업체명": "p", "이메일": "public@ex.com"},
            {"업체명": "n", "이메일": "new@ex.com"},
        ]
        job = self._job(store, rows)
        con = sqlite3.connect(store.db_path)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                reason TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "INSERT INTO blacklist(email, reason, added_at) VALUES ('blocked@ex.com', 't', datetime('now'))"
        )
        con.commit()
        con.close()
        sent = []

        def send_once(payload, job, item):
            sent.append(payload["email"])
            return True, ""

        runner = self._runner(store, job, send_once, interval=60)

        def prepare(j, item):
            if item["email"] == "public@ex.com":
                return "skipped", "public_filter"
            return "ready", {"email": item["email"]}

        runner.prepare_fn = prepare
        runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(sent, ["first@ex.com", "new@ex.com"])
        self.assertEqual(runner.sleeps, [1] * 60)

    def test_skip_loop_pauses_at_18(self):
        clock = {"v": kst(17, 59)}
        store = self._store()
        rows = [{"업체명": "c", "이메일": f"s{i}@ex.com"} for i in range(4)]
        job = self._job(store, rows)
        runner = self._runner(store, job, lambda p, j, i: (True, ""), interval=60, clock=clock)

        def prepare(j, item):
            clock["v"] = kst(18, 0)
            return "skipped", "duplicate"

        runner.prepare_fn = prepare
        result = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(result, JOB_SCHEDULED_PAUSE)
        self.assertEqual(runner.smtp_calls, 0)
        self.assertEqual(runner.sleeps, [])
        self.assertGreater(store.count_by_status(job["job_id"], ITEM_PENDING), 0)

    def test_skip_loop_honors_stop_and_cancel(self):
        store = self._store()
        flags = {"stop": False, "cancel": False}
        job = self._job(store, [{"업체명": "c", "이메일": f"s{i}@ex.com"} for i in range(3)])
        runner = self._runner(store, job, lambda p, j, i: (True, ""), interval=60)
        runner.is_user_stopped = lambda: flags["stop"]

        def prepare(j, item):
            flags["stop"] = True
            return "skipped", "duplicate"

        runner.prepare_fn = prepare
        self.assertEqual(runner.run(job["job_id"], wait_off_hours=False), JOB_USER_STOPPED)
        self.assertEqual(runner.smtp_calls, 0)

        flags = {"stop": False, "cancel": False}
        job2 = self._job(store, [{"업체명": "c", "이메일": f"c{i}@ex.com"} for i in range(3)], task_key="메일플러그_1")
        runner2 = self._runner(store, job2, lambda p, j, i: (True, ""), interval=60)
        runner2.is_cancelled = lambda: flags["cancel"]

        def prepare_cancel(j, item):
            flags["cancel"] = True
            return "skipped", "duplicate"

        runner2.prepare_fn = prepare_cancel
        self.assertEqual(runner2.run(job2["job_id"], wait_off_hours=False), JOB_CANCELLED)

    def test_rebind_attachment_then_release(self):
        store = self._store()
        missing = self.root / "없는 파일.pdf"
        job = self._job(store, [{"업체명": "c", "이메일": "a@ex.com"}])
        store.update_attachments(job["job_id"], {"files": [str(missing)], "imgs": {}})
        store.set_needs_attention(job["job_id"], "첨부 없음", attention_code="missing_attachment")
        self.assertEqual(
            maybe_release_attention(store, job["job_id"], send_allowed=True, now=kst()),
            JOB_NEEDS_ATTENTION,
        )
        ready = self.root / "안내.pdf"
        ready.write_bytes(b"pdf")
        _, still = replace_job_attachments(store, job["job_id"], files=[str(ready)])
        self.assertEqual(still, [])
        self.assertEqual(
            maybe_release_attention(store, job["job_id"], send_allowed=True, now=kst()),
            JOB_RUNNING,
        )

    def test_storage_block_then_only_recovered_accounts_resume(self):
        store = self._store()
        first = self._job(store, [{"업체명": "a", "이메일": "a@ex.com"}])
        second = self._job(
            store,
            [{"업체명": "b", "이메일": "b@ex.com"}],
            task_key="메일플러그_1",
        )
        calls = []

        def send_once(payload, job, item):
            calls.append(payload["email"])
            return True, ""

        runner_a = self._runner(store, first, send_once, interval=0)
        with patch(
            "campaign_runtime.assert_immediate_write",
            side_effect=StorageWriteError(
                "발송 기록",
                str(self.root),
                preserved=True,
                category="readonly",
                db_path=store.db_path,
                smtp_phase="before",
                auto_resend_blocked=True,
            ),
        ):
            runner_a.run(first["job_id"], wait_off_hours=False)
        runner_b = self._runner(store, second, send_once, interval=0)
        runner_b.run(second["job_id"], wait_off_hours=False)
        self.assertEqual(calls, [])
        self.assertEqual(store.count_by_status(second["job_id"], ITEM_PENDING), 1)
        reset_write_block()
        for job in (first, second):
            self.assertEqual(
                maybe_release_attention(store, job["job_id"], send_allowed=True, now=kst()),
                JOB_RUNNING,
            )
        runner_b2 = self._runner(store, second, send_once, interval=0)
        runner_b2.run(second["job_id"], wait_off_hours=False)
        runner_a2 = self._runner(store, first, send_once, interval=0)
        runner_a2.run(first["job_id"], wait_off_hours=False)
        self.assertEqual(calls, ["b@ex.com", "a@ex.com"])
        self.assertEqual(store.list_items_by_status(first["job_id"], ITEM_NEEDS_REVIEW), [])

    def _history_table(self, path):
        con = sqlite3.connect(path)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT,
                email TEXT,
                normalized_email TEXT,
                account_id TEXT,
                content_hash TEXT,
                message_id TEXT,
                sent_at TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                reason TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        return con

    def test_active_campaign_keeps_history_and_blocks_duplicates(self):
        src = self.root / "OneDrive" / "Desktop" / "CI비"
        src.mkdir(parents=True)
        store = CampaignStore(str(src / "sent_history.db"))
        job = self._job(
            store,
            [
                {"업체명": "기존", "이메일": "old@ex.com"},
                {"업체명": "신규", "이메일": "next@ex.com"},
            ],
        )
        con = self._history_table(store.db_path)
        con.execute(
            "INSERT INTO sent_log(email, normalized_email, account_id, content_hash, message_id, sent_at) VALUES (?,?,?,?,?,?)",
            ("shared-copy@ex.com", "shared-copy@ex.com", "alice", "other", "<dup@mail>", "2026-09-01 10:00:00"),
        )
        con.execute(
            "INSERT INTO sent_log(email, normalized_email, account_id, content_hash, message_id, sent_at) VALUES (?,?,?,?,?,?)",
            ("old@ex.com", "old@ex.com", "alice", "body", "<old@mail>", "2026-09-02 10:00:00"),
        )
        con.execute("INSERT INTO blacklist(email, reason, added_at) VALUES ('bad2@ex.com','t','2026-09-01')")
        con.commit()
        con.close()
        dest_root = Path(os.environ["LOCALAPPDATA"]) / "MAIL_MONSTER_PRO"
        dest_root.mkdir(parents=True, exist_ok=True)
        con = self._history_table(dest_root / "sent_history.db")
        con.execute(
            "INSERT INTO sent_log(email, normalized_email, account_id, content_hash, message_id, sent_at) VALUES (?,?,?,?,?,?)",
            ("history@ex.com", "history@ex.com", "alice", "hist", "<hist@mail>", "2026-08-01 10:00:00"),
        )
        con.execute(
            "INSERT INTO sent_log(email, normalized_email, account_id, content_hash, message_id, sent_at) VALUES (?,?,?,?,?,?)",
            ("shared@ex.com", "shared@ex.com", "alice", "same", "<dup@mail>", "2026-08-02 10:00:00"),
        )
        con.execute("INSERT INTO blacklist(email, reason, added_at) VALUES ('bad1@ex.com','t','2026-08-01')")
        con.execute("INSERT INTO blacklist(email, reason, added_at) VALUES ('bad2@ex.com','t','2026-08-02')")
        con.commit()
        con.close()
        with patch("data_migrate.install_dir", return_value=str(src)):
            report = prepare_user_data(force=True)
        self.assertFalse(report.needs_choice)
        self.assertFalse(report.block_autosend)
        self.assertTrue((src / "sent_history.db").is_file())
        merged = CampaignStore(str(dest_root / "sent_history.db"))
        self.assertEqual(merged.count_by_status(job["job_id"], ITEM_PENDING), 2)
        con = sqlite3.connect(dest_root / "sent_history.db")
        dup = con.execute("SELECT COUNT(*) FROM sent_log WHERE message_id=?", ("<dup@mail>",)).fetchone()[0]
        emails = {row[0] for row in con.execute("SELECT email FROM sent_log")}
        blocked = {row[0].lower() for row in con.execute("SELECT email FROM blacklist")}
        con.close()
        self.assertEqual(dup, 1)
        self.assertIn("history@ex.com", emails)
        self.assertIn("old@ex.com", emails)
        self.assertEqual(blocked, {"bad1@ex.com", "bad2@ex.com"})
        sent = []

        def send_once(payload, job, item):
            sent.append(payload["email"])
            return True, ""

        runner = self._runner(merged, merged.get_job(job["job_id"]), send_once, interval=0)

        def prepare(j, item):
            email = item["email"]
            return "ready", {"email": email, "body_hash": "body" if email == "old@ex.com" else "fresh"}

        runner.prepare_fn = prepare
        runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(sent, ["next@ex.com"])
        self.assertEqual(merged.list_items_by_status(job["job_id"], ITEM_SKIPPED)[0]["email"], "old@ex.com")
        with patch("data_migrate.install_dir", return_value=str(src)):
            again = prepare_user_data(force=True)
        self.assertFalse(again.needs_choice)
        self.assertFalse(again.block_autosend)

    def test_conflicting_campaigns_wait_for_choice(self):
        src = self.root / "exe"
        src.mkdir()
        left = CampaignStore(str(src / "sent_history.db"))
        source_job = self._job(left, [{"업체명": "기존위치", "이메일": "from-source@ex.com"}])
        dest_root = Path(os.environ["LOCALAPPDATA"]) / "MAIL_MONSTER_PRO"
        dest_root.mkdir(parents=True, exist_ok=True)
        right = CampaignStore(str(dest_root / "sent_history.db"))
        self._job(right, [{"업체명": "사용자폴더", "이메일": "from-local@ex.com"}])
        with patch("data_migrate.install_dir", return_value=str(src)):
            report = prepare_user_data(force=True)
        self.assertTrue(report.needs_choice)
        self.assertTrue(report.block_autosend)
        self.assertIn("경로:", report.detail_note)
        self.assertIn("sent_log:", report.detail_note)
        self.assertIn("활성 캠페인:", report.detail_note)
        self.assertTrue(autosend_blocked())
        calls = []
        local_job = right.list_jobs_for_user("alice")[0]
        runner = self._runner(
            right,
            local_job,
            lambda payload, job, item: calls.append(1) or (True, ""),
            interval=0,
        )
        self.assertEqual(runner.run(local_job["job_id"], wait_off_hours=False), "storage_choice")
        self.assertEqual(calls, [])
        self.assertTrue((src / "sent_history.db").is_file())
        apply_database_choice("secondary")
        self.assertFalse(autosend_blocked())
        merged = CampaignStore(str(dest_root / "sent_history.db"))
        pending = merged.list_items_by_status(source_job["job_id"], ITEM_PENDING)
        self.assertEqual([row["email"] for row in pending], ["from-source@ex.com"])
        resumed = self._runner(
            merged,
            merged.get_job(source_job["job_id"]),
            lambda payload, job, item: calls.append(payload["email"]) or (True, ""),
            interval=0,
        )
        resumed.run(source_job["job_id"], wait_off_hours=False)
        self.assertEqual(calls, ["from-source@ex.com"])
        with patch("data_migrate.install_dir", return_value=str(src)):
            again = prepare_user_data(force=True)
        self.assertFalse(again.needs_choice)
        self.assertFalse(again.block_autosend)


if __name__ == "__main__":
    unittest.main()
