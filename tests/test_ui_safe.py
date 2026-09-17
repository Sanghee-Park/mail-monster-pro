import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui_safe import schedule_on_ui


class FakeRoot:
    def __init__(self, exists=True):
        self._closing = False
        self.exists = exists
        self.calls = []

    def winfo_exists(self):
        return self.exists

    def after(self, ms, fn):
        self.calls.append(("after", ms, fn))
        fn()


class UiSafeTests(unittest.TestCase):
    def test_schedule_uses_after(self):
        root = FakeRoot()
        ran = []
        schedule_on_ui(root, lambda: ran.append(1))
        self.assertEqual(ran, [1])
        self.assertEqual(root.calls[0][0], "after")

    def test_destroyed_widget_not_called(self):
        root = FakeRoot(exists=False)
        ran = []
        schedule_on_ui(root, lambda: ran.append(1))
        self.assertEqual(ran, [])

    def test_closing_flag_skips(self):
        root = FakeRoot()
        root._closing = True
        ran = []
        schedule_on_ui(root, lambda: ran.append(1))
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
