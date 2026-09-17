import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from version_compare import evaluate_update_prompt, should_prompt_update, compare_versions


class VersionCompareTests(unittest.TestCase):
    def test_remote_newer_prompts(self):
        self.assertTrue(should_prompt_update("v2.8.1", "v2.8.0"))
        prompt, reason = evaluate_update_prompt("2.9.0", "v2.8.0")
        self.assertTrue(prompt)
        self.assertEqual(reason, "remote_newer")

    def test_equal_no_prompt(self):
        self.assertFalse(should_prompt_update("v2.8.0", "v2.8.0"))
        prompt, reason = evaluate_update_prompt("2.8.0", "v2.8.0")
        self.assertFalse(prompt)
        self.assertEqual(reason, "equal")

    def test_remote_older_no_downgrade_prompt(self):
        self.assertFalse(should_prompt_update("v2.7.3", "v2.8.0"))
        prompt, reason = evaluate_update_prompt("v2.7.3", "v2.8.0")
        self.assertFalse(prompt)
        self.assertEqual(reason, "remote_older")
        self.assertEqual(compare_versions("v2.7.3", "v2.8.0"), -1)

    def test_invalid_version_ignored(self):
        prompt, reason = evaluate_update_prompt("not-a-version", "v2.8.0")
        self.assertFalse(prompt)
        self.assertEqual(reason, "invalid_version")
        prompt2, reason2 = evaluate_update_prompt("", "v2.8.0")
        self.assertFalse(prompt2)
        self.assertEqual(reason2, "empty_remote")


if __name__ == "__main__":
    unittest.main()
