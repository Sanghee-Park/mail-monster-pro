import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from login_network import AUTOSTART_NET_DELAYS, is_auth_error, is_transient_network_error


class _Net(Exception):
    pass


class LoginNetworkTests(unittest.TestCase):
    def test_auth_not_retried(self):
        self.assertTrue(is_auth_error(Exception("401 invalid credentials")))
        self.assertTrue(is_auth_error(Exception("계정 정보가 틀립니다")))
        self.assertFalse(is_transient_network_error(Exception("401 unauthorized")))

    def test_timeout_is_transient(self):
        self.assertTrue(is_transient_network_error(TimeoutError("timed out")))
        self.assertTrue(is_transient_network_error(OSError("getaddrinfo failed")))
        self.assertTrue(is_transient_network_error(Exception("Max retries exceeded")))

    def test_autostart_retry_budget_is_finite(self):
        self.assertGreaterEqual(len(AUTOSTART_NET_DELAYS), 2)
        self.assertLessEqual(len(AUTOSTART_NET_DELAYS), 8)
        self.assertEqual(AUTOSTART_NET_DELAYS[0], 0)


if __name__ == "__main__":
    unittest.main()
