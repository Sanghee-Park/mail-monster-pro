import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_hours import KST, BusinessHours
from campaign_attachments import missing_attachment_paths
from campaign_runtime import CampaignRunner
from smtp_credentials import snapshot_contains_secrets as snap_secrets
from campaign_store import (
    DuplicateActiveCampaignError,
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING,
    ITEM_SENDING,
    ITEM_SENT,
    JOB_COMPLETED,
    JOB_RUNNING,
    JOB_NEEDS_ATTENTION,
    CampaignStore,
)


def kst(y, m, d, hh=10, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST)


class AuditFixTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.td.name) / "sent_history.db")
        self.store = CampaignStore(self.db)
        self.clock = kst(2026, 9, 16, 10, 0)
        self.hours = BusinessHours(extra_dates=set(), now_fn=lambda: self.clock)

    def tearDown(self):
        self.td.cleanup()

    def _job(self, user="alice", n=2, extra=None, exclusive=True):
        rows = [{"업체명": f"c{i}", "이메일": f"a{i}@ex.com"} for i in range(1, n + 1)]
        kw = dict(
            login_user_id=user,
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="홍길동",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id", "pw": "super-secret-pw"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=JOB_RUNNING,
            now=self.clock,
            exclusive=exclusive,
        )
        if extra:
            kw.update(extra)
        return self.store.create_job(**kw)

    def test_smtp_password_not_stored_in_campaign_db(self):
        job = self._job()
        raw = job.get("smtp_config_json") or ""
        self.assertNotIn("super-secret-pw", raw)
        self.assertNotIn('"pw"', raw)
        self.assertFalse(snap_secrets(raw))
        con = sqlite3.connect(self.db)
        dumped = " ".join(str(r[0]) for r in con.execute("SELECT smtp_config_json FROM campaign_jobs"))
        con.close()
        self.assertNotIn("super-secret-pw", dumped)

    def test_exclusive_create_same_user(self):
        self._job()
        with self.assertRaises(DuplicateActiveCampaignError):
            self._job()

    def test_sending_without_sent_log_needs_review_not_resend(self):
        job = self._job(n=1)
        item = self.store.next_pending(job["job_id"])
        self.store.mark_item(item["id"], ITEM_SENDING, inc_attempts=True, message_id="<j.1.1@mail-monster.pro>")
        rec = self.store.reconcile_interrupted_sending(job["job_id"], sent_lookup=lambda it: False)
        self.assertEqual(rec["review"], 1)
        live = self.store.get_item(item["id"])
        self.assertEqual(live["status"], ITEM_NEEDS_REVIEW)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], JOB_NEEDS_ATTENTION)
        sent = []
        runner = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=lambda p, j, i: (sent.append(p["email"]) or True, ""),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=lambda s: None,
            owner="recover",
        )
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_NEEDS_ATTENTION)
        self.assertEqual(sent, [])

    def test_sending_with_sent_log_becomes_sent(self):
        job = self._job(n=1)
        item = self.store.next_pending(job["job_id"])
        mid = "<confirmed.1@mail-monster.pro>"
        self.store.mark_item(item["id"], ITEM_SENDING, inc_attempts=True, message_id=mid)
        rec = self.store.reconcile_interrupted_sending(job["job_id"], sent_lookup=lambda it: it.get("message_id") == mid)
        self.assertEqual(rec["sent"], 1)
        self.assertEqual(self.store.get_item(item["id"])["status"], ITEM_SENT)

    def test_missing_attachment_halts_without_smtp(self):
        missing = str(Path(self.td.name) / "gone.pdf")
        job = self._job(extra={"attachments": {"files": [missing], "imgs": {}}})
        sent = []
        runner = CampaignRunner(
            self.store,
            self.hours,
            prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
            send_once_fn=lambda p, j, i: (sent.append("x") or True, ""),
            is_user_stopped=lambda: False,
            is_cancelled=lambda: False,
            interval_seconds_fn=lambda: 0,
            now_fn=lambda: self.clock,
            sleep_fn=lambda s: None,
            owner="att",
        )
        st = runner.run(job["job_id"], wait_off_hours=False)
        self.assertEqual(st, JOB_NEEDS_ATTENTION)
        self.assertEqual(sent, [])
        self.assertTrue(missing_attachment_paths({"files": [missing], "imgs": {}}))

    def test_concurrent_start_smtp_once_per_recipient(self):
        job = self._job(n=2, exclusive=True)
        sent = []
        lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()

        def send_once(payload, job, item):
            started.set()
            release.wait(3)
            with lock:
                sent.append(payload["email"])
            return True, ""

        def make(owner):
            return CampaignRunner(
                self.store,
                self.hours,
                prepare_fn=lambda j, i: ("ready", {"email": i["email"]}),
                send_once_fn=send_once,
                is_user_stopped=lambda: False,
                is_cancelled=lambda: False,
                interval_seconds_fn=lambda: 0,
                now_fn=lambda: kst(2026, 9, 16, 10, 0),
                sleep_fn=lambda s: None,
                owner=owner,
            )

        results = {}

        def run1():
            results["r1"] = make("w1").run(job["job_id"], wait_off_hours=False)

        t = threading.Thread(target=run1)
        t.start()
        self.assertTrue(started.wait(2))
        results["r2"] = make("w2").run(job["job_id"], wait_off_hours=False)
        release.set()
        t.join(5)
        self.assertEqual(results["r2"], "locked")
        self.assertEqual(results["r1"], JOB_COMPLETED)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(sent), len(set(sent)))

    def test_concurrent_create_only_one_job(self):
        errors = []
        created = []

        def worker():
            try:
                created.append(self._job(user="same", exclusive=True))
            except DuplicateActiveCampaignError:
                errors.append("dup")
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(len(created), 1)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
