import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_hours import KST, BusinessHours
from campaign_runtime import CampaignRunner
from campaign_store import (
    DuplicateActiveCampaignError,
    ITEM_CANCELLED,
    ITEM_PENDING,
    ITEM_SENT,
    ITEM_SKIPPED,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_NEEDS_ATTENTION,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
    CampaignStore,
)
from smtp_credentials import snapshot_contains_secrets


def kst(y, m, d, hh=10, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST)


class CampaignPoolTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.td.name) / "sent_history.db")
        self.store = CampaignStore(self.db)
        self.clock = kst(2026, 9, 16, 10, 0, 0)
        self.hours = BusinessHours(extra_dates=set(), now_fn=lambda: self.clock)
        self.sleeps = []

        def sleep_fn(s):
            self.sleeps.append(s)
            self.clock = self.clock + timedelta(seconds=int(s) if s else 0)

        self.sleep_fn = sleep_fn

    def tearDown(self):
        self.td.cleanup()

    def _rows(self, n, prefix="a"):
        return [{"업체명": f"c{i}", "이메일": f"{prefix}{i}@ex.com"} for i in range(1, n + 1)]

    def _job(self, *, user="alice", task_key="네이버_1", n=3, status=JOB_RUNNING, extra=None, recipients=None):
        kw = dict(
            login_user_id=user,
            task_key=task_key,
            provider=task_key.rsplit("_", 1)[0],
            account_idx=int(task_key.rsplit("_", 1)[-1]) if task_key.rsplit("_", 1)[-1].isdigit() else 1,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "super-secret-pw"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=recipients if recipients is not None else self._rows(n),
            status=status,
            now=self.clock,
            exclusive=True,
        )
        if extra:
            kw.update(extra)
        return self.store.create_job(**kw)

    def _runner(self, worker_id, send_fn, stopped=None, cancelled=None, owner=None):
        stopped = stopped if stopped is not None else {"v": False}
        cancelled = cancelled if cancelled is not None else {"v": False}
        return CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=send_fn,
            is_user_stopped=lambda: bool(stopped["v"]),
            is_cancelled=lambda: bool(cancelled["v"]),
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=self.sleep_fn,
            max_retries=3,
            wait_poll_seconds=1,
            owner=owner or worker_id,
            worker_id=worker_id,
        )

    def test_same_user_two_accounts_start(self):
        a = self._job(task_key="네이버_1", n=4)
        b = self._job(task_key="네이버_2", n=99)
        self.assertNotEqual(a["job_id"], b["job_id"])
        self.assertFalse(b.get("joined_existing"))
        self.assertNotEqual(a["worker_id"], b["worker_id"])
        self.assertEqual(self.store.remaining_count(a["job_id"]), 4)
        self.assertEqual(self.store.remaining_count(b["job_id"]), 99)
        self.assertEqual(
            {w["task_key"] for w in self.store.list_workers(a["job_id"])},
            {"네이버_1"},
        )
        self.assertEqual(
            {w["task_key"] for w in self.store.list_workers(b["job_id"])},
            {"네이버_2"},
        )

    def test_same_account_second_start_blocked(self):
        self._job(task_key="네이버_1")
        with self.assertRaises(DuplicateActiveCampaignError):
            self._job(task_key="네이버_1")

    def test_two_workers_do_not_claim_same_recipient(self):
        job = self._job(task_key="네이버_1", n=1)
        b = self._job(task_key="네이버_2", n=1)
        store_a = CampaignStore(self.db)
        store_b = CampaignStore(self.db)
        barrier = threading.Barrier(2)
        claimed = []
        lock = threading.Lock()

        def claim(store, wid):
            barrier.wait(2)
            target_job = job["job_id"] if wid == job["worker_id"] else b["job_id"]
            item = store.claim_next_pending(target_job, worker_id=wid)
            with lock:
                claimed.append(item["id"] if item else None)

        t1 = threading.Thread(target=claim, args=(store_a, job["worker_id"]))
        t2 = threading.Thread(target=claim, args=(store_b, b["worker_id"]))
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)
        ids = [x for x in claimed if x]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(claimed.count(None), 0)

    def test_180_recipients_two_accounts_are_fully_isolated(self):
        rows_a = self._rows(180, "a")
        rows_b = self._rows(180, "b")
        a = self._job(task_key="네이버_1", recipients=rows_a)
        b = self._job(task_key="네이버_2", recipients=rows_b)
        sent_a = []
        sent_b = []
        lock = threading.Lock()

        def send_a(payload, job, item):
            with lock:
                sent_a.append(payload["email"])
            return True, ""

        def send_b(payload, job, item):
            with lock:
                sent_b.append(payload["email"])
            return True, ""

        r1 = self._runner(a["worker_id"], send_a, owner="wa")
        r2 = self._runner(b["worker_id"], send_b, owner="wb")
        out = {}

        def run(name, runner, job_id):
            out[name] = runner.run(job_id, wait_off_hours=False)

        t1 = threading.Thread(target=run, args=("a", r1, a["job_id"]))
        t2 = threading.Thread(target=run, args=("b", r2, b["job_id"]))
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)
        self.assertEqual(len(sent_a), 180)
        self.assertEqual(len(sent_b), 180)
        self.assertEqual(set(sent_a), {r["이메일"] for r in rows_a})
        self.assertEqual(set(sent_b), {r["이메일"] for r in rows_b})
        self.assertTrue(set(sent_a).isdisjoint(set(sent_b)))
        self.assertEqual(r1.smtp_calls + r2.smtp_calls, 360)
        self.assertEqual(self.store.stats_dict(a["job_id"])["total"], 180)
        self.assertEqual(self.store.stats_dict(b["job_id"])["total"], 180)
        self.assertEqual(out["a"], JOB_COMPLETED)
        self.assertEqual(out["b"], JOB_COMPLETED)

    def test_auth_failure_does_not_stop_other_account(self):
        a = self._job(task_key="네이버_1", n=6)
        b = self._job(task_key="네이버_2", n=6)
        sent = []
        lock = threading.Lock()

        def send_a(payload, job, item):
            return False, "535 Authentication failed"

        def send_b(payload, job, item):
            with lock:
                sent.append(payload["email"])
            return True, ""

        ra = self._runner(a["worker_id"], send_a, owner="wa")
        rb = self._runner(b["worker_id"], send_b, owner="wb")
        sta = ra.run(a["job_id"], wait_off_hours=False)
        stb = rb.run(b["job_id"], wait_off_hours=False)
        self.assertEqual(sta, JOB_NEEDS_ATTENTION)
        self.assertEqual(self.store.get_worker(a["worker_id"])["status"], JOB_NEEDS_ATTENTION)
        self.assertEqual(stb, JOB_COMPLETED)
        self.assertGreaterEqual(len(sent), 1)
        self.assertEqual(self.store.get_worker(b["worker_id"])["status"], JOB_COMPLETED)

    def test_stop_one_account_other_continues(self):
        a = self._job(task_key="네이버_1", n=8)
        b = self._job(task_key="네이버_2", n=8)
        stop_a = {"v": False}
        sent_b = []
        lock = threading.Lock()
        started = threading.Event()

        def send_a(payload, job, item):
            started.set()
            stop_a["v"] = True
            return True, ""

        def send_b(payload, job, item):
            started.wait(2)
            with lock:
                sent_b.append(payload["email"])
            return True, ""

        ra = self._runner(a["worker_id"], send_a, stopped=stop_a, owner="wa")
        rb = self._runner(b["worker_id"], send_b, owner="wb")
        out = {}

        def run(name, runner, job_id):
            out[name] = runner.run(job_id, wait_off_hours=False)

        t1 = threading.Thread(target=run, args=("a", ra, a["job_id"]))
        t2 = threading.Thread(target=run, args=("b", rb, b["job_id"]))
        t1.start()
        t2.start()
        t1.join(15)
        t2.join(15)
        self.assertEqual(out["a"], JOB_USER_STOPPED)
        self.assertEqual(self.store.get_worker(a["worker_id"])["status"], JOB_USER_STOPPED)
        self.assertNotEqual(self.store.get_worker(b["worker_id"])["status"], JOB_USER_STOPPED)
        self.assertGreaterEqual(len(sent_b), 1)

    def test_cancel_stops_only_selected_account(self):
        a = self._job(task_key="네이버_1", n=20)
        b = self._job(task_key="네이버_2", n=20)
        cancelled = {"v": False}
        started = threading.Event()

        def send_once(payload, job, item):
            started.set()
            cancelled["v"] = True
            return True, ""

        ra = self._runner(a["worker_id"], send_once, cancelled=cancelled, owner="wa")
        rb = self._runner(b["worker_id"], lambda p, j, i: (True, ""), owner="wb")
        out = {}

        def run(name, runner, job_id):
            out[name] = runner.run(job_id, wait_off_hours=False)

        t1 = threading.Thread(target=run, args=("a", ra, a["job_id"]))
        t2 = threading.Thread(target=run, args=("b", rb, b["job_id"]))
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        self.assertTrue(started.is_set())
        self.assertEqual(self.store.get_job(a["job_id"])["status"], JOB_CANCELLED)
        self.assertEqual(self.store.get_worker(a["worker_id"])["status"], JOB_CANCELLED)
        self.assertEqual(self.store.get_job(b["job_id"])["status"], JOB_COMPLETED)
        self.assertEqual(self.store.get_worker(b["worker_id"])["status"], JOB_COMPLETED)

    def test_after_18_pauses_only_running_account(self):
        self.clock = kst(2026, 9, 16, 17, 59, 0)
        a = self._job(task_key="네이버_1", n=5, extra={"now": self.clock})
        b = self._job(task_key="네이버_2", n=5, extra={"now": self.clock})

        def send_once(payload, job, item):
            self.clock = kst(2026, 9, 16, 18, 0, 1)
            return True, ""

        ra = self._runner(a["worker_id"], send_once, owner="wa")
        st = ra.run(a["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_SCHEDULED_PAUSE)
        self.assertEqual(self.store.get_job(a["job_id"])["status"], JOB_SCHEDULED_PAUSE)
        self.assertEqual(self.store.get_worker(a["worker_id"])["status"], JOB_SCHEDULED_PAUSE)
        self.assertEqual(self.store.get_worker(b["worker_id"])["status"], JOB_RUNNING)
        nxt = self.store.get_job(a["job_id"])["next_resume_at"]
        self.assertIn("09:00", nxt)

    def test_next_business_day_09_resumes_all_healthy(self):
        self.clock = kst(2026, 9, 16, 18, 1, 0)
        a = self._job(task_key="네이버_1", n=2, extra={"now": self.clock, "status": JOB_SCHEDULED_PAUSE})
        b = self._job(task_key="네이버_2", n=2, extra={"now": self.clock, "status": JOB_SCHEDULED_PAUSE})
        self.store.pause_workers_scheduled(
            a["job_id"], next_resume_at="2026-09-17T09:00:00+09:00", now=self.clock
        )
        sent = []

        def send_once(payload, job, item):
            sent.append(payload["email"])
            return True, ""

        def sleep_fn(s):
            if self.clock.hour >= 18:
                self.clock = kst(2026, 9, 17, 9, 0, 0)
            else:
                self.clock = self.clock + timedelta(seconds=int(s) if s else 0)

        ra = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=send_once,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=sleep_fn,
            owner="wa",
            worker_id=a["worker_id"],
        )
        rb = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=send_once,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=sleep_fn,
            owner="wb",
            worker_id=b["worker_id"],
        )
        sta = ra.run(a["job_id"], wait_off_hours=True)
        stb = rb.run(b["job_id"], wait_off_hours=True)
        self.assertEqual(sta, JOB_COMPLETED)
        self.assertEqual(stb, JOB_COMPLETED)
        self.assertEqual(len(sent), 4)

    def test_process_restart_recovers_all_accounts_for_user(self):
        a = self._job(user="alice", task_key="네이버_1", n=3)
        b = self._job(user="alice", task_key="네이버_2", n=3)
        bob = self._job(user="bob", task_key="네이버_1", n=2)
        store2 = CampaignStore(self.db)
        recovered = store2.list_resumable_workers("alice")
        keys = {w["task_key"] for w in recovered}
        self.assertEqual(keys, {"네이버_1", "네이버_2"})
        self.assertTrue(all(w["login_user_id"] == "alice" for w in recovered))
        other = store2.list_resumable_workers("bob")
        self.assertEqual({w["job_id"] for w in other}, {bob["job_id"]})
        self.assertNotIn(a["job_id"], [w["job_id"] for w in other])

    def test_delete_block_only_active_task_key(self):
        self._job(task_key="네이버_1", n=2)
        self.assertTrue(self.store.has_active_job_for_user("alice", "네이버_1"))
        self.assertFalse(self.store.has_active_job_for_user("alice", "네이버_2"))

    def test_smtp_password_not_in_worker_or_job(self):
        a = self._job(task_key="네이버_1")
        b = self._job(task_key="네이버_2")
        con = sqlite3.connect(self.db)
        dumped = " ".join(
            str(r[0])
            for r in con.execute("SELECT smtp_config_json FROM campaign_jobs")
        )
        dumped += " ".join(
            str(r[0] or "")
            for r in con.execute("SELECT smtp_config_json FROM campaign_workers")
        )
        con.close()
        self.assertNotIn("super-secret-pw", dumped)
        self.assertNotIn('"pw"', dumped)
        self.assertFalse(snapshot_contains_secrets(a.get("smtp_config_json") or ""))
        self.assertFalse(snapshot_contains_secrets((self.store.get_worker(b["worker_id"]) or {}).get("smtp_config_json") or ""))

    def test_no_real_smtp_or_sheet_or_hkcu(self):
        import smtplib
        import winreg

        orig_smtp = smtplib.SMTP_SSL
        orig_win = winreg.CreateKeyEx if hasattr(winreg, "CreateKeyEx") else None

        class Boom:
            def __init__(self, *a, **k):
                raise AssertionError("실제 SMTP 호출 금지")

        def boom_key(*a, **k):
            raise AssertionError("실제 HKCU 호출 금지")

        smtplib.SMTP_SSL = Boom
        with patch("gspread.service_account", side_effect=AssertionError("시트 호출 금지")):
            if orig_win:
                winreg.CreateKeyEx = boom_key
            try:
                job = self._job(task_key="네이버_1", n=1)
                sent = []
                runner = self._runner(job["worker_id"], lambda p, j, i: (sent.append(p["email"]) or True, ""))
                st = runner.run(job["job_id"], wait_off_hours=False)
                self.assertEqual(st, JOB_COMPLETED)
                self.assertEqual(sent, ["a1@ex.com"])
            finally:
                smtplib.SMTP_SSL = orig_smtp
                if orig_win:
                    winreg.CreateKeyEx = orig_win

    def test_claim_stress_unique(self):
        job = self._job(task_key="네이버_1", n=40)
        b = self._job(task_key="네이버_2", n=40)
        ids = []
        lock = threading.Lock()
        stores = [CampaignStore(self.db) for _ in range(6)]

        def worker(store, wid):
            while True:
                target_job = job["job_id"] if wid == job["worker_id"] else b["job_id"]
                item = store.claim_next_pending(target_job, worker_id=wid)
                if item is None:
                    return
                with lock:
                    ids.append(item["id"])

        threads = []
        for i, store in enumerate(stores):
            wid = job["worker_id"] if i % 2 == 0 else b["worker_id"]
            t = threading.Thread(target=worker, args=(store, wid))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(len(ids), 80)
        self.assertEqual(len(set(ids)), 80)
        pending = self.store.count_by_status(job["job_id"], ITEM_PENDING)
        self.assertEqual(pending, 0)
        self.assertEqual(self.store.count_by_status(b["job_id"], ITEM_PENDING), 0)

    def test_account_recipient_sets_import_deduplicate_and_isolate(self):
        rows_a = self._rows(180, "a")
        rows_b = self._rows(180, "b")
        rows_a.append({"업체명": "dup", "이메일": " A1@EX.COM "})
        result_a = self.store.import_account_recipients("alice", "네이버_1", rows_a)
        result_b = self.store.import_account_recipients("alice", "네이버_2", rows_b)
        self.assertEqual(result_a["total"], 180)
        self.assertEqual(result_b["total"], 180)
        self.assertEqual(len(self.store.list_account_recipients("alice", "네이버_1")), 180)
        self.assertEqual(len(self.store.list_account_recipients("alice", "네이버_2")), 180)

        self.store.replace_account_recipients("alice", "네이버_1", self._rows(3, "new"))
        self.assertEqual(len(self.store.list_account_recipients("alice", "네이버_1")), 3)
        self.assertEqual(len(self.store.list_account_recipients("alice", "네이버_2")), 180)

    def test_same_file_reimport_has_no_duplicate_recipient(self):
        rows = self._rows(10)
        self.store.import_account_recipients("alice", "네이버_1", rows)
        self.store.import_account_recipients("alice", "네이버_1", rows)
        self.assertEqual(len(self.store.list_account_recipients("alice", "네이버_1")), 10)

    def test_queue_normalized_email_unique(self):
        rows = [
            {"업체명": "one", "이메일": " DUP@EX.COM "},
            {"업체명": "two", "이메일": "dup@ex.com"},
        ]
        job = self._job(recipients=rows)
        self.assertEqual(self.store.stats_dict(job["job_id"])["total"], 1)

    def test_cross_account_concurrent_same_body_calls_smtp_once(self):
        same = [{"업체명": "same", "이메일": "same@example.com"}]
        a = self._job(task_key="네이버_1", recipients=same)
        b = self._job(task_key="네이버_2", recipients=same)
        calls = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def runner(job, worker, owner):
            def prepare(_job, item):
                return "ready", {"email": item["email"], "body_hash": "hash-1"}

            def send(payload, _job, _item):
                with lock:
                    calls.append((owner, payload["email"]))
                return True, ""

            return CampaignRunner(
                self.store,
                self.hours,
                prepare_fn=prepare,
                send_once_fn=send,
                is_user_stopped=lambda: False,
                is_cancelled=lambda: False,
                interval_seconds_fn=lambda: 0,
                now_fn=lambda: self.clock,
                sleep_fn=self.sleep_fn,
                owner=owner,
                worker_id=worker["worker_id"],
            )

        ra = runner(a, a, "wa")
        rb = runner(b, b, "wb")
        out = {}

        def run(name, r, jid):
            barrier.wait(2)
            out[name] = r.run(jid, wait_off_hours=False)

        ta = threading.Thread(target=run, args=("a", ra, a["job_id"]))
        tb = threading.Thread(target=run, args=("b", rb, b["job_id"]))
        ta.start()
        tb.start()
        ta.join(10)
        tb.join(10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.store.count_by_status(a["job_id"], ITEM_SENT)
            + self.store.count_by_status(b["job_id"], ITEM_SENT),
            1,
        )
        self.assertEqual(
            self.store.count_by_status(a["job_id"], ITEM_SKIPPED)
            + self.store.count_by_status(b["job_id"], ITEM_SKIPPED),
            1,
        )

    def test_changed_body_hash_can_send_again(self):
        same = [{"업체명": "same", "이메일": "same@example.com"}]
        calls = []
        for index, body_hash in enumerate(("hash-old", "hash-new"), 1):
            job = self._job(task_key="네이버_1", recipients=same)
            runner = CampaignRunner(
                self.store,
                self.hours,
                prepare_fn=lambda j, i, h=body_hash: (
                    "ready",
                    {"email": i["email"], "body_hash": h},
                ),
                send_once_fn=lambda p, j, i: (calls.append(p["body_hash"]) or True, ""),
                is_user_stopped=lambda: False,
                is_cancelled=lambda: False,
                interval_seconds_fn=lambda: 0,
                now_fn=lambda: self.clock,
                sleep_fn=self.sleep_fn,
                owner=f"w{index}",
                worker_id=job["worker_id"],
            )
            self.assertEqual(runner.run(job["job_id"], wait_off_hours=False), JOB_COMPLETED)
        self.assertEqual(calls, ["hash-old", "hash-new"])

    def test_ambiguous_smtp_result_becomes_review_and_never_auto_resends(self):
        job = self._job(
            recipients=[{"업체명": "same", "이메일": "uncertain@example.com"}]
        )
        calls = []
        runner = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: (
                "ready",
                {"email": i["email"], "body_hash": "same-body"},
            ),
            send_once_fn=lambda p, j, i: (
                calls.append(p["email"]) or False,
                "Connection reset by peer",
            ),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=self.sleep_fn,
            owner="uncertain-1",
            worker_id=job["worker_id"],
        )
        self.assertEqual(runner.run(job["job_id"], wait_off_hours=False), JOB_NEEDS_ATTENTION)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            self.store.count_by_status(job["job_id"], "needs_review"),
            1,
        )

        calls_after_restart = []
        runner2 = CampaignRunner(
            CampaignStore(self.db),
            self.hours,
            prepare_fn=lambda j, i: (
                "ready",
                {"email": i["email"], "body_hash": "same-body"},
            ),
            send_once_fn=lambda p, j, i: (
                calls_after_restart.append(p["email"]) or True,
                "",
            ),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=self.sleep_fn,
            owner="uncertain-2",
            worker_id=job["worker_id"],
        )
        self.assertEqual(runner2.run(job["job_id"], wait_off_hours=False), JOB_NEEDS_ATTENTION)
        self.assertEqual(calls_after_restart, [])

    def _insert_blacklist(self, value):
        con = sqlite3.connect(self.db)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE COLLATE NOCASE,
                reason TEXT
            )
            """
        )
        con.execute("INSERT OR IGNORE INTO blacklist(email, reason) VALUES (?,?)", (value, "test"))
        con.commit()
        con.close()

    def test_blacklist_checked_at_queue_creation(self):
        self._insert_blacklist(" BLOCK@EX.COM ")
        job = self._job(recipients=[{"업체명": "x", "이메일": "block@ex.com"}])
        self.assertEqual(self.store.count_by_status(job["job_id"], ITEM_SKIPPED), 1)
        item = self.store.list_items_by_status(job["job_id"], ITEM_SKIPPED)[0]
        self.assertEqual(item["skip_reason"], "blacklist")

    def test_blacklist_added_after_queue_blocks_before_smtp(self):
        job = self._job(recipients=[{"업체명": "x", "이메일": "later@ex.com"}])
        self._insert_blacklist("LATER@EX.COM")
        calls = []
        runner = self._runner(
            job["worker_id"],
            lambda p, j, i: (calls.append(p["email"]) or True, ""),
        )
        self.assertEqual(runner.run(job["job_id"], wait_off_hours=False), JOB_COMPLETED)
        self.assertEqual(calls, [])
        item = self.store.list_items_by_status(job["job_id"], ITEM_SKIPPED)[0]
        self.assertEqual(item["skip_reason"], "blacklist")

    def test_blacklist_added_between_retries_blocks_next_smtp_call(self):
        job = self._job(recipients=[{"업체명": "x", "이메일": "retry@ex.com"}])
        calls = []

        def send_once(payload, _job, _item):
            calls.append(payload["email"])
            self._insert_blacklist(" RETRY@EX.COM ")
            return False, "550 rejected"

        runner = self._runner(job["worker_id"], send_once)
        self.assertEqual(runner.run(job["job_id"], wait_off_hours=False), JOB_COMPLETED)
        self.assertEqual(calls, ["retry@ex.com"])
        item = self.store.list_items_by_status(job["job_id"], ITEM_SKIPPED)[0]
        self.assertEqual(item["skip_reason"], "blacklist")

    def test_cancel_marks_only_selected_pending_queue_cancelled(self):
        a = self._job(task_key="네이버_1", n=4)
        b = self._job(task_key="네이버_2", n=4)
        self.store.cancel_campaign_workers(a["job_id"], now=self.clock)
        self.assertEqual(self.store.count_by_status(a["job_id"], ITEM_CANCELLED), 4)
        self.assertEqual(self.store.count_by_status(b["job_id"], ITEM_PENDING), 4)

    def test_legacy_shared_pool_is_needs_attention_not_split(self):
        job = self._job(task_key="네이버_1", n=4)
        con = sqlite3.connect(self.db)
        con.execute(
            """
            INSERT INTO campaign_workers(
                worker_id, job_id, login_user_id, task_key, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            ("legacy-w2", job["job_id"], "alice", "네이버_2", JOB_RUNNING, "now", "now"),
        )
        con.commit()
        con.close()
        reopened = CampaignStore(self.db)
        migrated = reopened.get_job(job["job_id"])
        self.assertEqual(migrated["status"], JOB_NEEDS_ATTENTION)
        self.assertEqual(migrated["legacy_pool"], 1)
        self.assertEqual(migrated["migration_state"], "needs_review")
        self.assertEqual(reopened.count_by_status(job["job_id"], ITEM_PENDING), 4)


if __name__ == "__main__":
    unittest.main()
