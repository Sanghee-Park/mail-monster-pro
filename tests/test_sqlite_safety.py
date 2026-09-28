import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_paths import DATA_DIR_ENV, resolve_state_files
from business_hours import KST, BusinessHours
from campaign_runtime import CampaignRunner
from campaign_store import (
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING,
    ITEM_SENDING,
    ITEM_SENT,
    JOB_RUNNING,
    CampaignStore,
)
from data_migrate import StorageUnavailable, prepare_user_data, reset_prepare_cache
from db_access import (
    ManagedConnection,
    append_recovery_journal,
    connect,
    load_recovery_journal,
    reset_write_block,
)
from json_atomic import StorageWriteError
from main import initialize_user_data


def kst_now():
    return datetime(2026, 9, 16, 10, 0, 0, tzinfo=KST)


def _only_item(store, job_id):
    rows = []
    for status in (ITEM_PENDING, ITEM_SENDING, ITEM_NEEDS_REVIEW, ITEM_SENT):
        rows.extend(store.list_items_by_status(job_id, status))
    return rows[0]


class Raw:
    def __init__(self, stage):
        self.stage = stage
        self.rolled = False

    def execute(self, sql, parameters=()):
        text = str(sql).upper()
        if self.stage == "wal" and "JOURNAL_MODE" in text:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        if self.stage == "begin" and "BEGIN" in text:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        if self.stage == "insert" and text.strip().startswith("INSERT"):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        if self.stage == "update" and text.strip().startswith("UPDATE"):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        if self.stage == "delete" and text.strip().startswith("DELETE"):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return self

    def commit(self):
        if self.stage == "commit":
            raise sqlite3.OperationalError("attempt to write a readonly database")

    def rollback(self):
        self.rolled = True

    def close(self):
        pass

    def fetchone(self):
        return ("wal",)


class SqliteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)
        self._old_data = os.environ.get(DATA_DIR_ENV)
        self._old_local = os.environ.get("LOCALAPPDATA")
        self._old_skip = os.environ.get("MAILMONSTER_SKIP_MUTEX")
        self._old_mutex = os.environ.get("MAILMONSTER_MUTEX_NAME")
        os.environ[DATA_DIR_ENV] = str(self.root / "data")
        os.environ["LOCALAPPDATA"] = str(self.root / "local")
        reset_write_block()
        reset_prepare_cache()

    def tearDown(self):
        reset_write_block()
        reset_prepare_cache()
        if self._old_data is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = self._old_data
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        if self._old_skip is None:
            os.environ.pop("MAILMONSTER_SKIP_MUTEX", None)
        else:
            os.environ["MAILMONSTER_SKIP_MUTEX"] = self._old_skip
        if self._old_mutex is None:
            os.environ.pop("MAILMONSTER_MUTEX_NAME", None)
        else:
            os.environ["MAILMONSTER_MUTEX_NAME"] = self._old_mutex
        self.td.cleanup()

    def test_production_has_no_direct_sqlite_connect(self):
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in root.glob("*.py"):
            if path.name == "db_access.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "sqlite3.connect(" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_same_db_from_other_cwd_and_korean_path(self):
        data = self.root / "새 폴더 (3)"
        data.mkdir()
        other = self.root / "other cwd"
        other.mkdir()
        os.environ[DATA_DIR_ENV] = str(data)
        reset_prepare_cache()
        old = os.getcwd()
        try:
            os.chdir(other)
            first = resolve_state_files()["sent_history.db"]
            con = connect(first, kind="발송 기록")
            con.execute("CREATE TABLE IF NOT EXISTS probe(note TEXT)")
            con.execute("INSERT INTO probe(note) VALUES ('한글')")
            con.commit()
            con.close()
            os.chdir(self.root)
            second = resolve_state_files()["sent_history.db"]
            self.assertEqual(os.path.normcase(first), os.path.normcase(second))
            con = connect(second, kind="발송 기록")
            note = con.execute("SELECT note FROM probe").fetchone()[0]
            con.close()
            self.assertEqual(note, "한글")
            self.assertFalse((other / "sent_history.db").exists())
        finally:
            os.chdir(old)

    def test_each_write_stage_becomes_storage_error(self):
        db = str(self.root / "sent_history.db")
        for stage in ("begin", "insert", "update", "delete", "commit"):
            raw = Raw(stage)
            managed = ManagedConnection(raw, db, "발송 기록")
            with self.assertRaises(StorageWriteError) as caught:
                if stage == "begin":
                    managed.execute("BEGIN IMMEDIATE")
                elif stage == "insert":
                    managed.execute("INSERT INTO t(n) VALUES (1)")
                elif stage == "update":
                    managed.execute("UPDATE t SET n=1")
                elif stage == "delete":
                    managed.execute("DELETE FROM t")
                else:
                    managed.commit()
            self.assertNotIn("attempt to write a readonly database", str(caught.exception))
            self.assertEqual(caught.exception.category, "readonly")
            self.assertTrue(raw.rolled)
            self.assertTrue(os.path.abspath(caught.exception.db_path).endswith("sent_history.db"))

    def test_connect_and_wal_readonly_are_storage_errors(self):
        db = str(self.root / "wal.db")

        def raise_open(*_a, **_k):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        with patch("db_access.sqlite3.connect", side_effect=raise_open):
            with self.assertRaises(StorageWriteError) as caught:
                connect(db, kind="발송 기록")
        self.assertEqual(caught.exception.category, "readonly")
        self.assertNotIn("attempt to write a readonly database", str(caught.exception))

        def open_then_wal(*_a, **_k):
            return Raw("wal")

        with patch("db_access.sqlite3.connect", side_effect=open_then_wal):
            with self.assertRaises(StorageWriteError) as caught:
                connect(db, kind="발송 기록")
        self.assertEqual(caught.exception.category, "readonly")

    def _job(self, store, task_key, email):
        return store.create_job(
            login_user_id="alice",
            task_key=task_key,
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "secret-pw"},
            interval_label="즉시",
            prevent_dup=False,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=[{"업체명": "회사", "이메일": email}],
            status=JOB_RUNNING,
            now=kst_now(),
        )

    def _runner(self, store, job, send_once):
        return CampaignRunner(
            store,
            BusinessHours(extra_dates=set(), now_fn=kst_now),
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=send_once,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=kst_now,
            sleep_fn=lambda _s: None,
            owner="owner",
            worker_id=job.get("worker_id"),
            max_retries=1,
        )

    def test_db_failure_before_smtp_does_not_send(self):
        os.environ[DATA_DIR_ENV] = str(self.root)
        store = CampaignStore(str(self.root / "sent_history.db"))
        job = self._job(store, "네이버_1", "a@ex.com")
        calls = []

        def send_once(payload, job, item):
            calls.append(item["id"])
            return True, ""

        runner = self._runner(store, job, send_once)
        with patch("campaign_runtime.assert_immediate_write", side_effect=StorageWriteError(
            "발송 기록",
            str(self.root),
            preserved=True,
            category="readonly",
            db_path=str(self.root / "sent_history.db"),
            smtp_phase="before",
            auto_resend_blocked=True,
        )):
            result = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(calls, [])
        self.assertEqual(runner.smtp_calls, 0)
        self.assertEqual(result, "needs_attention")
        item = _only_item(store, job["job_id"])
        self.assertNotEqual(item["status"], ITEM_PENDING)

    def test_smtp_success_then_db_failure_does_not_resend(self):
        os.environ[DATA_DIR_ENV] = str(self.root)
        db = str(self.root / "sent_history.db")
        store = CampaignStore(db)
        job = self._job(store, "네이버_1", "a@ex.com")
        calls = []

        def send_once(payload, job, item):
            calls.append(1)
            return True, ""

        original = store.mark_item

        def wrapped(item_id, status, *args, **kwargs):
            if status == ITEM_SENT:
                raise StorageWriteError(
                    "발송 기록",
                    str(self.root),
                    preserved=True,
                    category="readonly",
                    db_path=db,
                    auto_resend_blocked=True,
                )
            return original(item_id, status, *args, **kwargs)

        store.mark_item = wrapped
        runner = self._runner(store, job, send_once)
        result = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(runner.smtp_calls, 1)
        self.assertEqual(result, "needs_attention")
        self.assertTrue(load_recovery_journal())
        self.assertNotIn("secret-pw", str(load_recovery_journal()))
        item = _only_item(store, job["job_id"])
        self.assertEqual(item["status"], ITEM_NEEDS_REVIEW)

        reset_write_block()
        again = CampaignStore(db)
        calls.clear()
        runner2 = self._runner(again, again.get_job(job["job_id"]), send_once)
        runner2.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(calls, [])
        self.assertEqual(runner2.smtp_calls, 0)
        self.assertEqual(again.get_item(item["id"])["status"], ITEM_NEEDS_REVIEW)

    def test_second_account_stops_new_smtp_after_db_error(self):
        os.environ[DATA_DIR_ENV] = str(self.root)
        store = CampaignStore(str(self.root / "sent_history.db"))
        first = self._job(store, "네이버_1", "a@ex.com")
        second = self._job(store, "메일플러그_1", "b@ex.com")
        calls = []

        def send_once(payload, job, item):
            calls.append(job["task_key"])
            return True, ""

        original = store.mark_item

        def wrapped(item_id, status, *args, **kwargs):
            if status == ITEM_SENT:
                raise StorageWriteError(
                    "발송 기록",
                    str(self.root),
                    preserved=True,
                    category="readonly",
                    db_path=store.db_path,
                    auto_resend_blocked=True,
                )
            return original(item_id, status, *args, **kwargs)

        store.mark_item = wrapped
        self._runner(store, first, send_once).run(first["job_id"], wait_off_hours=False)
        self._runner(store, second, send_once).run(second["job_id"], wait_off_hours=False)
        self.assertEqual(calls, ["네이버_1"])

    def test_bad_env_dir_is_not_used_and_both_unwritable_stops(self):
        good = self.root / "good"
        good.mkdir()
        bad = self.root / "bad-file"
        bad.write_text("x", encoding="utf-8")
        os.environ[DATA_DIR_ENV] = str(bad)
        reset_prepare_cache()
        with patch("data_migrate.install_dir", return_value=str(good)):
            report = prepare_user_data(force=True)
        from data_migrate import chosen_data_dir

        self.assertNotEqual(os.path.normcase(chosen_data_dir()), os.path.normcase(str(bad)))
        self.assertTrue(report.used_portable)

        reset_prepare_cache()
        os.environ.pop(DATA_DIR_ENV, None)
        os.environ["LOCALAPPDATA"] = str(self.root / "local")
        with patch("data_migrate.install_dir", return_value=str(good)), patch(
            "data_migrate.storage_root_is_safe", return_value=False
        ):
            with self.assertRaises(StorageUnavailable):
                prepare_user_data(force=True)
        self.assertTrue(good.is_dir())

    def test_second_instance_does_not_prepare_twice(self):
        os.environ.pop("MAILMONSTER_SKIP_MUTEX", None)
        os.environ["MAILMONSTER_MUTEX_NAME"] = "Local\\MM285-" + uuid.uuid4().hex
        calls = []

        def fake_prepare(*_a, **_k):
            calls.append(1)
            from data_migrate import MigrationReport

            return MigrationReport(used_portable=True)

        with patch("main.prepare_user_data", side_effect=fake_prepare):
            self.assertEqual(initialize_user_data(), "ready")
            self.assertEqual(initialize_user_data(), "already-running")
        self.assertEqual(calls, [1])

    def test_legacy_rows_survive_relocation(self):
        src = self.root / "install"
        src.mkdir()
        db = src / "sent_history.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sent_log(email TEXT)")
        con.execute("INSERT INTO sent_log(email) VALUES ('keep@ex.com')")
        con.commit()
        con.close()
        (src / "recipients.json").write_text('{"네이버_1":{"rows":[]}}', encoding="utf-8")
        before = db.read_bytes()
        os.environ.pop(DATA_DIR_ENV, None)
        os.environ["LOCALAPPDATA"] = str(self.root / "local")
        reset_prepare_cache()
        with patch("data_migrate.install_dir", return_value=str(src)), patch(
            "data_migrate.storage_root_is_safe", side_effect=lambda path: not str(path).startswith(str(src))
        ):
            report = prepare_user_data(force=True)
        self.assertFalse(report.used_portable)
        self.assertEqual(db.read_bytes(), before)
        dest = Path(os.environ["LOCALAPPDATA"]) / "MAIL_MONSTER_PRO" / "sent_history.db"
        con = sqlite3.connect(dest)
        email = con.execute("SELECT email FROM sent_log").fetchone()[0]
        con.close()
        self.assertEqual(email, "keep@ex.com")
        self.assertTrue((src / "recipients.json").is_file())


if __name__ == "__main__":
    unittest.main()
