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
    ITEM_PENDING,
    ITEM_SENT,
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
        self.assertEqual(a["job_id"], b["job_id"])
        self.assertTrue(b.get("joined_existing"))
        self.assertNotEqual(a["worker_id"], b["worker_id"])
        self.assertEqual(self.store.remaining_count(a["job_id"]), 4)
        workers = self.store.list_workers(a["job_id"])
        self.assertEqual({w["task_key"] for w in workers}, {"네이버_1", "네이버_2"})

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
            item = store.claim_next_pending(job["job_id"], worker_id=wid)
            with lock:
                claimed.append(item["id"] if item else None)

        t1 = threading.Thread(target=claim, args=(store_a, job["worker_id"]))
        t2 = threading.Thread(target=claim, args=(store_b, b["worker_id"]))
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)
        ids = [x for x in claimed if x]
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(claimed.count(None), 1)

    def test_100_recipients_two_accounts_one_smtp_each(self):
        rows = self._rows(100)
        a = self._job(task_key="네이버_1", recipients=rows)
        b = self._job(task_key="네이버_2", recipients=rows)
        sent = []
        lock = threading.Lock()

        def send_once(payload, job, item):
            with lock:
                sent.append(payload["email"])
            return True, ""

        r1 = self._runner(a["worker_id"], send_once, owner="wa")
        r2 = self._runner(b["worker_id"], send_once, owner="wb")
        out = {}

        def run(name, runner):
            out[name] = runner.run(a["job_id"], wait_off_hours=False)

        t1 = threading.Thread(target=run, args=("a", r1))
        t2 = threading.Thread(target=run, args=("b", r2))
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)
        self.assertEqual(len(sent), 100)
        self.assertEqual(len(set(sent)), 100)
        self.assertEqual(r1.smtp_calls + r2.smtp_calls, 100)
        stats = self.store.stats_dict(a["job_id"])
        wa = self.store.worker_stats(a["job_id"], worker_id=a["worker_id"])
        wb = self.store.worker_stats(a["job_id"], worker_id=b["worker_id"])
        self.assertEqual(wa["success"] + wb["success"], stats["success"])
        self.assertEqual(stats["success"] + stats["skipped"] + stats["failed"], 100)
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
        stb = rb.run(a["job_id"], wait_off_hours=False)
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

        def run(name, runner):
            out[name] = runner.run(a["job_id"], wait_off_hours=False)

        t1 = threading.Thread(target=run, args=("a", ra))
        t2 = threading.Thread(target=run, args=("b", rb))
        t1.start()
        t2.start()
        t1.join(15)
        t2.join(15)
        self.assertEqual(out["a"], JOB_USER_STOPPED)
        self.assertEqual(self.store.get_worker(a["worker_id"])["status"], JOB_USER_STOPPED)
        self.assertNotEqual(self.store.get_worker(b["worker_id"])["status"], JOB_USER_STOPPED)
        self.assertGreaterEqual(len(sent_b), 1)

    def test_cancel_stops_all_accounts(self):
        a = self._job(task_key="네이버_1", n=20)
        b = self._job(task_key="네이버_2", n=20)
        cancelled = {"v": False}
        started = threading.Event()

        def send_once(payload, job, item):
            started.set()
            cancelled["v"] = True
            return True, ""

        ra = self._runner(a["worker_id"], send_once, cancelled=cancelled, owner="wa")
        rb = self._runner(b["worker_id"], send_once, cancelled=cancelled, owner="wb")
        out = {}

        def run(name, runner):
            out[name] = runner.run(a["job_id"], wait_off_hours=False)

        t1 = threading.Thread(target=run, args=("a", ra))
        t2 = threading.Thread(target=run, args=("b", rb))
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        self.assertTrue(started.is_set())
        self.assertEqual(self.store.get_job(a["job_id"])["status"], JOB_CANCELLED)
        self.assertEqual(self.store.get_worker(a["worker_id"])["status"], JOB_CANCELLED)
        self.assertEqual(self.store.get_worker(b["worker_id"])["status"], JOB_CANCELLED)

    def test_after_18_pauses_all_workers(self):
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
        self.assertEqual(self.store.get_worker(b["worker_id"])["status"], JOB_SCHEDULED_PAUSE)
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
        stb = rb.run(a["job_id"], wait_off_hours=True)
        self.assertEqual(sta, JOB_COMPLETED)
        self.assertEqual(stb, JOB_COMPLETED)
        self.assertEqual(len(sent), 2)

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
                item = store.claim_next_pending(job["job_id"], worker_id=wid)
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
        self.assertEqual(len(ids), 40)
        self.assertEqual(len(set(ids)), 40)
        pending = self.store.count_by_status(job["job_id"], ITEM_PENDING)
        self.assertEqual(pending, 0)


if __name__ == "__main__":
    unittest.main()
