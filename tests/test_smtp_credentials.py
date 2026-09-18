import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smtp_credentials import public_smtp_snapshot, resolve_smtp_for_send, snapshot_contains_secrets


class SmtpCredentialTests(unittest.TestCase):
    def test_public_snapshot_strips_password(self):
        snap = public_smtp_snapshot(
            {"smtp": "smtp.ex.com", "port": 465, "id": "user", "pw": "secret-value", "token": "tok"},
            "네이버_1",
        )
        self.assertEqual(snap.get("smtp"), "smtp.ex.com")
        self.assertEqual(snap.get("id"), "user")
        self.assertEqual(snap.get("task_key"), "네이버_1")
        self.assertNotIn("pw", snap)
        self.assertNotIn("token", snap)
        self.assertFalse(snapshot_contains_secrets(snap))

    def test_resolve_loads_live_password_from_config_store(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            cfg = Path(td.name) / "config.json"
            cfg.write_text(
                json.dumps({"네이버_1": {"smtp": "smtp.ex.com", "port": 465, "id": "user", "pw": "live-secret"}}),
                encoding="utf-8",
            )
            live = resolve_smtp_for_send(str(cfg), "네이버_1", {"smtp": "old", "id": "user", "task_key": "네이버_1"})
            self.assertIsNotNone(live)
            self.assertEqual(live["pw"], "live-secret")
            missing = resolve_smtp_for_send(str(cfg), "없는계정", {})
            self.assertIsNone(missing)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
