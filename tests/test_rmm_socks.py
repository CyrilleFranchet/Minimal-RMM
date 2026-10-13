"""Focused tests for the server-side SOCKS relay state machine."""

import base64
import struct
import unittest
from unittest.mock import MagicMock, patch

from rmm_socks import SessionSocksBridge, SocksManager


class FragmentedSocket:
    """Socket-shaped test double that fragments every recv result."""

    def __init__(self, payload):
        self.payload = bytearray(payload)
        self.sent = []

    def recv(self, size):
        if not self.payload:
            return b""
        count = min(size, 1, len(self.payload))
        chunk = bytes(self.payload[:count])
        del self.payload[:count]
        return chunk

    def sendall(self, payload):
        self.sent.append(bytes(payload))


class RecordingSocket:
    """Socket-shaped test double for data delivered during tunnel setup."""

    def __init__(self):
        self.sent = []

    def sendall(self, payload):
        self.sent.append(bytes(payload))

    def shutdown(self, _how):
        pass

    def close(self):
        pass


class RecordingDatagramSocket:
    """Socket-shaped test double for UDP packets returned to the TUN stack."""

    def __init__(self):
        self.sent = []

    def sendto(self, payload, address):
        self.sent.append((bytes(payload), address))


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

    def test_socks_handshake_accepts_fragmented_no_auth_request(self):
        sock = FragmentedSocket(b"\x05\x01\x00")

        self.assertTrue(SessionSocksBridge._socks5_handshake(sock))
        self.assertEqual(sock.sent, [b"\x05\x00"])

    def test_socks_handshake_rejects_missing_no_auth_method(self):
        sock = FragmentedSocket(b"\x05\x01\x02")

        self.assertFalse(SessionSocksBridge._socks5_handshake(sock))
        self.assertEqual(sock.sent, [b"\x05\xff"])

    def test_socks_request_accepts_fragmented_domain_name(self):
        target = b"example.com"
        payload = b"\x05\x01\x00\x03" + bytes([len(target)]) + target + b"\x01\xbb"
        sock = FragmentedSocket(payload)

        self.assertEqual(
            SessionSocksBridge._socks5_read_request(sock),
            ("example.com", 443),
        )

    def test_udp_packet_round_trip_preserves_domain_and_payload(self):
        payload = b"dns-query"
        packet = (
            b"\x00\x00\x00\x03\x0bexample.com"
            + struct.pack("!H", 53)
            + payload
        )

        self.assertEqual(
            SessionSocksBridge._decode_socks5_udp(packet),
            ("example.com", 53, payload),
        )
        self.assertEqual(
            SessionSocksBridge._encode_socks5_udp("example.com", 53, payload),
            packet,
        )

    def test_udp_response_is_written_to_association_client(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        udp = RecordingDatagramSocket()
        bridge._udp_sock = udp
        bridge.udp_tunnels["8.8.8.8:53"] = {
            "id": "udp-1",
            "client_addr": ("127.0.0.1", 40000),
        }

        bridge.submit_responses(
            [
                {
                    "id": "udp-1",
                    "op": "udp_data",
                    "host": "8.8.8.8",
                    "port": 53,
                    "data_b64": base64.b64encode(b"dns-response").decode(),
                }
            ]
        )

        self.assertEqual(len(udp.sent), 1)
        packet, address = udp.sent[0]
        self.assertEqual(address, ("127.0.0.1", 40000))
        self.assertEqual(
            SessionSocksBridge._decode_socks5_udp(packet),
            ("8.8.8.8", 53, b"dns-response"),
        )

    def test_close_task_retries_until_agent_acknowledges_it(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge.task_queue.append({"op": "close", "id": "closed-once"})

        self.assertEqual(
            bridge.fetch_tasks_for_pull(),
            [{"op": "close", "id": "closed-once"}],
        )
        self.assertEqual(
            bridge.fetch_tasks_for_pull(),
            [{"op": "close", "id": "closed-once"}],
        )
        bridge.submit_responses([{"id": "closed-once", "op": "closed"}])
        self.assertEqual(bridge.fetch_tasks_for_pull(), [])

    def test_udp_send_task_is_not_replayed(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge.task_queue.append(
            {"op": "udp_send", "id": "udp-1", "host": "127.0.0.1", "port": 53}
        )
        bridge._queued_udp_tasks = 1

        self.assertEqual(len(bridge.fetch_tasks_for_pull()), 1)
        self.assertEqual(bridge.fetch_tasks_for_pull(), [])

    def test_udp_close_retries_until_agent_acknowledges_it(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge.task_queue.append({"op": "udp_close", "id": "udp-1"})

        self.assertEqual(bridge.fetch_tasks_for_pull(), [{"op": "udp_close", "id": "udp-1"}])
        self.assertEqual(bridge.fetch_tasks_for_pull(), [{"op": "udp_close", "id": "udp-1"}])
        bridge.submit_responses([{"id": "udp-1", "op": "udp_closed"}])
        self.assertEqual(bridge.fetch_tasks_for_pull(), [])

    def test_send_tasks_are_capped_without_being_replayed(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge.task_queue.extend(
            {"op": "send", "id": "bulk", "data_b64": str(index)}
            for index in range(33)
        )

        first = bridge.fetch_tasks_for_pull()
        second = bridge.fetch_tasks_for_pull()

        self.assertEqual(len(first), 32)
        self.assertEqual(len(second), 1)
        self.assertEqual(
            [task["data_b64"] for task in first + second],
            [str(index) for index in range(33)],
        )

    def test_remote_data_is_buffered_until_local_client_attaches(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge._running = True
        bridge.submit_responses(
            [
                {"id": "early-data", "op": "ok"},
                {"id": "early-data", "op": "data", "data_b64": "aGVsbG8="},
            ]
        )
        tunnel = bridge.tunnels["early-data"]
        client = RecordingSocket()

        self.assertTrue(bridge._attach_client(tunnel, client))
        self.assertEqual(client.sent, [b"hello"])
        self.assertIs(tunnel["client"], client)

    def test_remote_close_after_connect_waits_for_socks_success_reply(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge._running = True
        bridge.submit_responses(
            [
                {"id": "fast-close", "op": "ok"},
                {"id": "fast-close", "op": "closed"},
            ]
        )

        tunnel = bridge.tunnels["fast-close"]
        self.assertTrue(tunnel["remote_closed"])
        client = RecordingSocket()
        self.assertTrue(bridge._attach_client(tunnel, client))
        self.assertIs(tunnel["client"], client)
        bridge._close_tunnel(tunnel, notify_client=False)

    def test_failed_start_does_not_mark_bridge_running_or_register_it(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        fake_socket = MagicMock()
        fake_socket.bind.side_effect = OSError("address in use")

        with patch("rmm_socks.socket.socket", return_value=fake_socket):
            with self.assertRaises(OSError):
                bridge.start()

        self.assertFalse(bridge._running)
        manager = SocksManager()
        with patch.object(SessionSocksBridge, "start", side_effect=OSError("address in use")):
            with self.assertRaises(OSError):
                manager.start("test-session", port=0)
        self.assertIsNone(manager.get("test-session"))


if __name__ == "__main__":
    unittest.main()
