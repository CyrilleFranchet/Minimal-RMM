"""Focused tests for the server-side SOCKS relay state machine."""

import unittest

from rmm_socks import SessionSocksBridge


class SocksBridgeTests(unittest.TestCase):
    """Verify relay state transitions that do not require network sockets."""

    def test_wait_connect_accepts_response_received_before_waiter_creation(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge.submit_responses([{"id": "fast-connect", "op": "ok"}])

        self.assertTrue(bridge._wait_connect("fast-connect"))

    def test_wait_connect_reports_error_received_before_waiter_creation(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge.submit_responses(
            [{"id": "fast-connect", "op": "error", "msg": "connection refused"}]
        )

        self.assertFalse(bridge._wait_connect("fast-connect"))


if __name__ == "__main__":
    unittest.main()
