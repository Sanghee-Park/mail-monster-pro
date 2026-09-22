import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campaign_store import JOB_RUNNING, CampaignStore
from template_prefs import ensure_template_ids, find_template_by_id


class RecipientScaleTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.td.name) / "sent_history.db")
        self.store = CampaignStore(self.db)

    def tearDown(self):
        self.td.cleanup()

    def test_no_fixed_recipient_cap_across_pages(self):
        total = 10050
        rows = [{"업체명": f"c{i}", "이메일": f"user{i}@example.com"} for i in range(total)]
        saved = self.store.import_account_recipients("alice", "네이버_1", rows, source_label="scale")
        self.assertEqual(saved["total"], total)
        self.assertEqual(self.store.count_account_recipients("alice", "네이버_1"), total)
        first = self.store.list_account_recipients_page("alice", "네이버_1", 0, 100)
        last = self.store.list_account_recipients_page("alice", "네이버_1", 10000, 100)
        self.assertEqual(len(first), 100)
        self.assertEqual(len(last), 50)
        self.assertNotEqual(first[0]["이메일"], last[0]["이메일"])
        self.assertEqual(self.store.count_account_recipients("alice", "네이버_1"), total)

    def test_accounts_keep_independent_counts(self):
        self.store.import_account_recipients(
            "alice", "네이버_1", [{"이메일": f"a{i}@ex.com", "업체명": "a"} for i in range(3)]
        )
        self.store.import_account_recipients(
            "alice", "네이버_12", [{"이메일": f"b{i}@ex.com", "업체명": "b"} for i in range(7)]
        )
        self.store.import_account_recipients("alice", "지메일_1", [])
        counts = self.store.recipient_counts_by_task("alice")
        self.assertEqual(counts["네이버_1"], 3)
        self.assertEqual(counts["네이버_12"], 7)
        self.assertEqual(self.store.count_account_recipients("alice", "지메일_1"), 0)
        self.assertEqual(sum(counts.values()), 10)

    def test_delete_selected_emails_does_not_touch_other_account(self):
        self.store.import_account_recipients(
            "alice", "네이버_1", [{"이메일": " A@EX.com ", "업체명": "a"}, {"이메일": "b@ex.com", "업체명": "b"}]
        )
        self.store.import_account_recipients(
            "alice", "네이버_2", [{"이메일": "a@ex.com", "업체명": "other"}]
        )
        deleted = self.store.delete_account_recipients("alice", "네이버_1", ["a@ex.com"])
        self.assertEqual(deleted, 1)
        self.assertEqual(self.store.count_account_recipients("alice", "네이버_1"), 1)
        self.assertEqual(self.store.count_account_recipients("alice", "네이버_2"), 1)

    def test_template_preference_restores_by_id_not_name_index(self):
        templates = {"안내": {"title": "제목", "body": "본문"}}
        templates, changed = ensure_template_ids(templates)
        self.assertTrue(changed)
        template_id = templates["안내"]["id"]
        self.store.set_last_template_id("alice", "네이버_1", template_id)
        self.store.set_last_template_id("alice", "네이버_2", "other-id")
        self.store.set_last_template_id("bob", "네이버_1", "bob-id")
        reopened = CampaignStore(self.db)
        self.assertEqual(reopened.get_last_template_id("alice", "네이버_1"), template_id)
        self.assertEqual(reopened.get_last_template_id("alice", "네이버_2"), "other-id")
        self.assertEqual(reopened.get_last_template_id("bob", "네이버_1"), "bob-id")
        name, item = find_template_by_id(templates, template_id)
        self.assertEqual(name, "안내")
        self.assertEqual(item["body"], "본문")
        reopened.clear_template_preferences(template_id)
        self.assertEqual(reopened.get_last_template_id("alice", "네이버_1"), "")
        self.assertEqual(reopened.get_last_template_id("alice", "네이버_2"), "other-id")


class SingleDetailUiTests(unittest.TestCase):
    def test_many_accounts_reuse_one_detail_and_restore_template(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        old_dir = os.environ.get("MAILMONSTER_DATA_DIR")

        def _restore_env():
            if old_dir is None:
                os.environ.pop("MAILMONSTER_DATA_DIR", None)
            else:
                os.environ["MAILMONSTER_DATA_DIR"] = old_dir

        self.addCleanup(_restore_env)
        os.environ["MAILMONSTER_DATA_DIR"] = td.name
        root = Path(td.name)
        keys = [f"네이버_{n}" for n in range(1, 31)]
        config = {
            key: {"id": f"user{n}@example.com", "pw": "secret", "smtp": "smtp.example.com", "port": "465"}
            for n, key in enumerate(keys, 1)
        }
        config["__account_order__"] = keys
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (root / "user_profiles.json").write_text("{}", encoding="utf-8")
        (root / "recipients.json").write_text("{}", encoding="utf-8")
        templates = {
            "A템플릿": {"id": "tpl-a", "title": "제목A", "body": "본문A", "sender": "보낸A", "files": [], "imgs": {}},
            "B템플릿": {"id": "tpl-b", "title": "제목B", "body": "본문B", "sender": "보낸B", "files": [], "imgs": {}},
        }
        (root / "templates.json").write_text(json.dumps(templates), encoding="utf-8")
        from main_ui import ModernMailSender

        started = time.perf_counter()
        app = ModernMailSender(user_name="bench", grade="유료", remaining="1", login_user_id="scale-user")
        app.withdraw()
        startup = time.perf_counter() - started

        def count_widgets(widget):
            total = 1
            for child in widget.winfo_children():
                total += count_widgets(child)
            return total

        widgets_before = count_widgets(app)
        self.assertEqual(len(app.profile_frames), 1)
        self.assertLess(widgets_before, 2500)
        self.assertLess(startup, 15)
        self.assertLess(threading.active_count(), 8)
        store = app.campaign_store
        store.import_account_recipients(
            "scale-user",
            "네이버_30",
            [{"업체명": "c", "이메일": f"r{i}@ex.com"} for i in range(250)],
        )
        store.set_last_template_id("scale-user", "네이버_1", "tpl-a")
        store.set_last_template_id("scale-user", "네이버_30", "tpl-b")
        app.update()
        switch_times = []
        for index in range(30):
            t0 = time.perf_counter()
            app._switch_profile(keys[index])
            app.update()
            switch_times.append(time.perf_counter() - t0)
        widgets_after = count_widgets(app)
        self.assertLess(widgets_after - widgets_before, 80)
        slow = max(range(len(switch_times)), key=switch_times.__getitem__)
        self.assertLess(switch_times[slow], 1.0, f"index={slow} key={keys[slow]} time={switch_times[slow]:.3f}")
        app._switch_profile("네이버_30")
        app.update_idletasks()
        self.assertEqual(app._shared_widgets["title"].get(), "제목B")
        self.assertIn("본문B", app._shared_widgets["body"].get("1.0", "end-1c"))
        self.assertEqual(len(app._shared_widgets["tree"].get_children()), 100)
        self.assertIn("전체 250건", app._shared_widgets["count"].cget("text"))
        app._recipient_next_page()
        self.assertEqual(len(app._shared_widgets["tree"].get_children()), 100)
        app._recipient_next_page()
        self.assertEqual(len(app._shared_widgets["tree"].get_children()), 50)
        self.assertEqual(store.count_account_recipients("scale-user", "네이버_30"), 250)
        app.destroy()


class QueueUsesAllStoredRecipientsTests(unittest.TestCase):
    def test_queue_is_not_limited_to_page_size(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        store = CampaignStore(str(Path(td.name) / "sent_history.db"))
        rows = [{"업체명": "c", "이메일": f"q{i}@ex.com"} for i in range(250)]
        job = store.create_job(
            login_user_id="alice",
            task_key="네이버_1",
            provider="네이버",
            account_idx=1,
            subject="제목",
            body="본문",
            sender_name="홍",
            smtp_config={"smtp": "127.0.0.1", "port": 465, "id": "id"},
            interval_label="1분",
            prevent_dup=True,
            apply_public_filter=False,
            template_name="T",
            attachments={"files": [], "imgs": {}},
            recipients=rows,
            status=JOB_RUNNING,
        )
        self.assertEqual(store.stats_dict(job["job_id"])["total"], 250)


if __name__ == "__main__":
    unittest.main()
