import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_hours import KST, BusinessHours
from campaign_store import (
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_SENT,
    JOB_COMPLETED,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
    CampaignStore,
)


def kst(y, m, d, hh=10, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST)


class CampaignStoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.td.name) / "sent_history.db")
        self.store = CampaignStore(self.db)

    def tearDown(self):
        self.td.cleanup()

    def _job(self, user="u1", n=3, status=JOB_RUNNING, extra=None):
        rows = [{"업체명": f"c{i}", "이메일": f"a{i}@ex.com"} for i in range(1, n + 1)]
        kw = dict(
            login_user_id=user,
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "localhost", "port": 465, "id": "id", "pw": "pw"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=status,
            now=kst(2026, 9, 16, 10, 0),
        )
        if extra:
            kw.update(extra)
        return self.store.create_job(**kw)

    def test_schema_does_not_drop_sent_log(self):
        import sqlite3

        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE IF NOT EXISTS sent_log (id INTEGER PRIMARY KEY, email TEXT)"
        )
        con.execute("INSERT INTO sent_log(email) VALUES ('keep@ex.com')")
        con.commit()
        con.close()
        CampaignStore(self.db)
        con = sqlite3.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0]
        con.close()
        self.assertEqual(n, 1)

    def test_user_isolation(self):
        j1 = self._job(user="alice")
        j2 = self._job(user="bob")
        a = self.store.list_active_jobs("alice")
        b = self.store.list_active_jobs("bob")
        self.assertEqual([x["job_id"] for x in a], [j1["job_id"]])
        self.assertEqual([x["job_id"] for x in b], [j2["job_id"]])

    def test_queue_preserved_on_pause(self):
        job = self._job()
        item = self.store.next_pending(job["job_id"])
        self.store.mark_item(item["id"], ITEM_SENT)
        self.store.set_status(job["job_id"], JOB_SCHEDULED_PAUSE, next_resume_at="2026-09-17T09:00:00+09:00")
        self.assertEqual(self.store.remaining_count(job["job_id"]), 2)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_SCHEDULED_PAUSE)

    def test_restart_recovers_queue(self):
        job = self._job()
        item = self.store.next_pending(job["job_id"])
        self.store.mark_item(item["id"], "sending")
        store2 = CampaignStore(self.db)
        store2.reset_interrupted_sending(job["job_id"])
        n = store2.remaining_count(job["job_id"])
        self.assertEqual(n, 3)
        self.assertEqual(store2.next_pending(job["job_id"])["email"], "a1@ex.com")

    def test_claim_exclusive(self):
        job = self._job()
        ok1 = self.store.try_claim_job(job["job_id"], "w1", now=kst(2026, 9, 16, 10, 0), lease_seconds=180)
        ok2 = self.store.try_claim_job(job["job_id"], "w2", now=kst(2026, 9, 16, 10, 0), lease_seconds=180)
        self.assertTrue(ok1)
        self.assertFalse(ok2)

    def test_smtp_snapshot_omits_password(self):
        job = self._job()
        raw = job["smtp_config_json"]
        self.assertNotIn("pw", raw)
        self.assertIn("task_key", raw)

    def test_user_stopped_not_in_active(self):
        job = self._job()
        self.store.set_status(job["job_id"], JOB_USER_STOPPED)
        self.assertFalse(self.store.has_active_job_for_user("u1"))
        self.assertEqual(self.store.list_active_jobs("u1"), [])


class CampaignRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.td.name) / "sent_history.db")
        self.store = CampaignStore(self.db)
        self.clock = kst(2026, 9, 16, 10, 0, 0)
        self.hours = BusinessHours(extra_dates=set(), now_fn=lambda: self.clock)
        self.sleeps = []
        self.smtp_network = []
        self.stopped = False
        self.cancelled = False
        self.interval = 0

        def sleep_fn(s):
            self.sleeps.append(s)
            self.clock = self.clock.replace(second=self.clock.second)  # no-op time; tests jump clock explicitly
            # 대기 루프가 멈추지 않도록 최소 진행
            from datetime import timedelta

            self.clock = self.clock + timedelta(seconds=int(s) if s else 0)

        self.sleep_fn = sleep_fn

    def tearDown(self):
        self.td.cleanup()

    def _make_job(self, n=2, status=JOB_RUNNING, hour=10):
        self.clock = kst(2026, 9, 16, hour, 0, 0)
        rows = [{"업체명": f"c{i}", "이메일": f"a{i}@ex.com"} for i in range(1, n + 1)]
        return self.store.create_job(
            login_user_id="alice",
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "pw"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=status,
            now=self.clock,
        )

    def _runner(self, send_ok=True, fail_times=0, skip_email=None):
        from campaign_runtime import CampaignRunner
        import smtplib

        fails = {"n": 0}

        def prepare(job, item):
            rec = item.get("recipient") or {}
            email = item.get("email")
            if skip_email and email == skip_email:
                return "skipped", "dup"
            return "ready", {"email": email, "row": rec}

        def send_once(payload, job, item):
            # 실제 네트워크 금지
            if getattr(smtplib, "SMTP_SSL", None):
                pass
            self.smtp_network.append(payload["email"])
            if fails["n"] < fail_times:
                fails["n"] += 1
                return False, "smtp error"
            return bool(send_ok), "" if send_ok else "smtp error"

        return CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=prepare,
            send_once_fn=send_once,
            is_user_stopped=lambda: self.stopped,
            is_cancelled=lambda: self.cancelled,
            interval_seconds_fn=lambda: self.interval,
            now_fn=lambda: self.clock,
            sleep_fn=self.sleep_fn,
            max_retries=3,
            wait_poll_seconds=1,
            owner="worker-a",
        )

    def test_off_hours_start_scheduled_pause(self):
        job = self._make_job(hour=18)
        runner = self._runner()
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_SCHEDULED_PAUSE)
        self.assertEqual(self.store.remaining_count(job["job_id"]), 2)
        self.assertEqual(runner.smtp_calls, 0)
        nxt = self.store.get_job(job["job_id"])["next_resume_at"]
        self.assertIn("09:00", nxt)

    def test_after_6pm_preserves_queue(self):
        job = self._make_job(n=3, hour=17)
        self.clock = kst(2026, 9, 16, 17, 59, 0)
        self.interval = 0
        calls = {"n": 0}

        def send_once(payload, job, item):
            calls["n"] += 1
            self.smtp_network.append(payload["email"])
            self.clock = kst(2026, 9, 16, 18, 0, 1)
            return True, ""

        from campaign_runtime import CampaignRunner

        runner = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=send_once,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=self.sleep_fn,
            owner="w",
        )
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_SCHEDULED_PAUSE)
        self.assertGreaterEqual(self.store.remaining_count(job["job_id"]), 1)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_SCHEDULED_PAUSE)

    def test_reopen_recovers_and_finishes(self):
        job = self._make_job(n=2, hour=10)
        item = self.store.next_pending(job["job_id"])
        self.store.mark_item(item["id"], ITEM_SENT)
        self.store.set_status(job["job_id"], JOB_SCHEDULED_PAUSE)
        store2 = CampaignStore(self.db)
        hours = BusinessHours(extra_dates=set(), now_fn=lambda: kst(2026, 9, 16, 11, 0))
        from campaign_runtime import CampaignRunner

        sent = []
        runner = CampaignRunner(
            store2,
            hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=lambda p, j, i: (sent.append(p["email"]) or True, ""),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: kst(2026, 9, 16, 11, 0),
            sleep_fn=lambda s: None,
            owner="recover",
        )
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_COMPLETED)
        self.assertEqual(sent, ["a2@ex.com"])

    def test_user_stopped_does_not_autoresume(self):
        job = self._make_job()
        self.store.set_status(job["job_id"], JOB_USER_STOPPED)
        runner = self._runner()
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_USER_STOPPED)
        self.assertEqual(runner.smtp_calls, 0)

    def test_user_isolation_runtime(self):
        j_alice = self._make_job()
        rows = [{"업체명": "cb", "이메일": "bob@ex.com"}]
        j_bob = self.store.create_job(
            login_user_id="bob",
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="s",
            body="b",
            sender_name="n",
            smtp_config={},
            interval_label="1분",
            prevent_dup=False,
            apply_public_filter=False,
            template_name="",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=JOB_RUNNING,
            now=self.clock,
        )
        alice_jobs = self.store.list_active_jobs("alice")
        self.assertTrue(all(j["login_user_id"] == "alice" for j in alice_jobs))
        self.assertNotIn(j_bob["job_id"], [j["job_id"] for j in alice_jobs])
        self.assertEqual(j_alice["login_user_id"], "alice")

    def test_gate_before_smtp(self):
        from campaign_runtime import evaluate_send_gate

        job = {"status": JOB_RUNNING}
        item = {"status": ITEM_PENDING}
        hours = BusinessHours(extra_dates=set())
        r = evaluate_send_gate(
            job=job,
            item=item,
            hours=hours,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            now=kst(2026, 9, 16, 18, 0, 0),
        )
        self.assertEqual(r, JOB_SCHEDULED_PAUSE)
        r2 = evaluate_send_gate(
            job=job,
            item=item,
            hours=hours,
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            now=kst(2026, 9, 16, 9, 0, 0),
        )
        self.assertIsNone(r2)

    def test_last_recipient_completes_without_interval_wait(self):
        job = self._make_job(n=1, hour=10)
        self.interval = 600
        runner = self._runner()
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_COMPLETED)
        self.assertFalse(any(s >= 600 for s in self.sleeps))
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_COMPLETED)

    def test_smtp_failures_then_next_recipient(self):
        job = self._make_job(n=2, hour=10)
        runner = self._runner(fail_times=3)
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_COMPLETED)
        stats = self.store.stats_dict(job["job_id"])
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["success"], 1)

    def test_no_real_smtp_module_send(self):
        import smtplib

        orig = smtplib.SMTP_SSL

        class Boom:
            def __init__(self, *a, **k):
                raise AssertionError("실제 SMTP 네트워크 요청이 발생하면 안 됩니다")

        smtplib.SMTP_SSL = Boom
        try:
            job = self._make_job(n=1, hour=10)
            runner = self._runner()
            st = runner.run(job["job_id"], wait_off_hours=False)
            self.assertEqual(st, JOB_COMPLETED)
            self.assertEqual(self.smtp_network, ["a1@ex.com"])
        finally:
            smtplib.SMTP_SSL = orig

    def test_two_runners_do_not_double_send(self):
        job = self._make_job(n=3, hour=10)
        started = threading.Event()
        release = threading.Event()
        sent = []
        lock = threading.Lock()

        def prepare(job, item):
            return "ready", {"email": item["email"]}

        def send_slow(payload, job, item):
            started.set()
            release.wait(3)
            with lock:
                sent.append(payload["email"])
            return True, ""

        from campaign_runtime import CampaignRunner

        def make(owner):
            return CampaignRunner(
                self.store,
                self.hours,
                prepare_fn=prepare,
                send_once_fn=send_slow,
                is_user_stopped=lambda: False,
                is_cancelled=lambda: False,
                interval_seconds_fn=lambda: 0,
                now_fn=lambda: kst(2026, 9, 16, 10, 0),
                sleep_fn=lambda s: None,
                owner=owner,
            )

        r1 = make("w1")
        r2 = make("w2")
        results = {}

        def run1():
            results["r1"] = r1.run(job["job_id"], wait_off_hours=False)

        t = threading.Thread(target=run1)
        t.start()
        self.assertTrue(started.wait(2))
        results["r2"] = r2.run(job["job_id"], wait_off_hours=False)
        release.set()
        t.join(5)
        self.assertEqual(results["r2"], "locked")
        self.assertEqual(results["r1"], JOB_COMPLETED)
        self.assertEqual(len(sent), len(set(sent)))

    def test_skip_does_not_smtp(self):
        job = self._make_job(n=1, hour=10)
        runner = self._runner(skip_email="a1@ex.com")
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_COMPLETED)
        self.assertEqual(runner.smtp_calls, 0)
        self.assertEqual(self.store.stats_dict(job["job_id"])["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
