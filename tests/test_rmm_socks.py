"""Focused tests for the server-side SOCKS relay state machine."""

import base64
import socket
import struct
import threading
import time
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


class ObservedReadSocket:
    """Use a real socket while making an idle receive deterministic in tests."""

    def __init__(self, sock, hold_read=False):
        self.socket = sock
        self.read_entered = threading.Event()
        self.release_read = threading.Event()
        if not hold_read:
            self.release_read.set()

    def recv(self, size):
        self.read_entered.set()
        if not self.release_read.wait(3):
            raise AssertionError('test did not release the idle read')
        return self.socket.recv(size)

    def __getattr__(self, name):
        return getattr(self.socket, name)


class SocksFullDuplexTests(unittest.TestCase):
    """Verify real full-duplex sockets cannot block each other through an idle read."""

    def setUp(self):
        self.bridges = []
        self.sockets = []
        self.readers = []
        self.threads = []
        self.errors = []

    def tearDown(self):
        for bridge in self.bridges:
            with bridge.lock:
                bridge._running = False
        for reader in self.readers:
            reader.release_read.set()
        for sock in self.sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for bridge in self.bridges:
            bridge.stop()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive(), 'relay worker did not stop')
        for sock in self.sockets:
            sock.close()
        self.assertEqual(self.errors, [])

    def start_worker(self, target, *args):
        finished = threading.Event()
        def run():
            try:
                target(*args)
            except BaseException as error:
                self.errors.append(error)
            finally:
                finished.set()
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return finished

    def start_relay(self, hold_read=False):
        local, peer = socket.socketpair()
        peer.settimeout(2)
        self.sockets.extend([local, peer])
        observed = ObservedReadSocket(local, hold_read)
        self.readers.append(observed)
        bridge = SessionSocksBridge('full-duplex', '127.0.0.1', 0)
        self.bridges.append(bridge)
        bridge._running = True
        bridge._enqueue_task({'op': 'connect', 'id': 'connection', 'host': 'example', 'port': 443})
        bridge.submit_responses([{'op': 'ok', 'id': 'connection'}])
        tunnel = bridge.tunnels['connection']
        self.assertTrue(bridge._attach_client(tunnel, observed))
        reader_finished = self.start_worker(bridge._relay_local_to_remote, tunnel)
        self.assertTrue(observed.read_entered.wait(2))
        return bridge, tunnel, peer, observed, reader_finished

    @staticmethod
    def response(payload):
        return {'op': 'data', 'id': 'connection', 'data_b64': base64.b64encode(payload).decode()}

    def test_ready_response_does_not_wait_for_idle_client_read(self):
        bridge, _, peer, observed, _ = self.start_relay(hold_read=True)
        done = self.start_worker(bridge.submit_responses, [self.response(b'ready-response')])
        self.assertTrue(done.wait(.5), 'ready response was blocked behind the idle client read')
        self.assertFalse(observed.release_read.is_set(), 'read remained blocked during delivery')
        self.assertEqual(peer.recv(64), b'ready-response')

    def test_bidirectional_payloads_preserve_byte_order(self):
        bridge, _, peer, _, _ = self.start_relay()
        upstream = b''.join(bytes([index]) * 300 for index in range(1, 25))
        downstream = b''.join(bytes([index]) * 200 for index in range(25, 49))
        sent = self.start_worker(bridge.submit_responses,
                                 [self.response(downstream[offset:offset + 200])
                                  for offset in range(0, len(downstream), 200)])
        peer.sendall(upstream)
        received = bytearray()
        while len(received) < len(downstream):
            chunk = peer.recv(len(downstream) - len(received))
            self.assertTrue(chunk, 'relay closed before downstream data completed')
            received.extend(chunk)
        self.assertEqual(bytes(received), downstream)
        self.assertTrue(sent.wait(2))
        queued = bytearray()
        deadline = time.monotonic() + 2
        while len(queued) < len(upstream) and time.monotonic() < deadline:
            for task in bridge.fetch_tasks_for_pull():
                if task['op'] == 'send':
                    queued.extend(base64.b64decode(task['data_b64']))
            if len(queued) < len(upstream):
                time.sleep(.005)
        self.assertEqual(bytes(queued), upstream)

    def test_remote_close_can_interrupt_idle_reader(self):
        bridge, _, peer, _, reader_done = self.start_relay()
        closed = self.start_worker(bridge.submit_responses, [{'op': 'closed', 'id': 'connection'}])
        self.assertTrue(closed.wait(.5), 'remote close waited for the client read timeout')
        self.assertTrue(reader_done.wait(2), 'reader did not stop after remote close')
        self.assertEqual(peer.recv(1), b'')
        self.assertNotIn('connection', bridge.tunnels)

    def test_stop_can_interrupt_idle_reader(self):
        bridge, _, peer, _, reader_done = self.start_relay()
        stopped = self.start_worker(bridge.stop)
        self.assertTrue(stopped.wait(.5), 'bridge stop waited for the client read timeout')
        self.assertTrue(reader_done.wait(2), 'reader did not stop after bridge stop')
        self.assertEqual(peer.recv(1), b'')
        self.assertEqual(bridge.tunnels, {})

    def test_response_write_failure_closes_idle_reader_without_deadlock(self):
        bridge, _, peer, observed, reader_done = self.start_relay()
        with patch.object(observed, 'sendall', side_effect=OSError('fixture write failure')):
            failed = self.start_worker(bridge.submit_responses, [self.response(b'ready-response')])
            self.assertTrue(failed.wait(.5), 'failed write could not close the idle reader')
        self.assertTrue(reader_done.wait(2), 'reader did not stop after write failure')
        self.assertEqual(peer.recv(1), b'')
        self.assertNotIn('connection', bridge.tunnels)

    def test_concurrent_response_writers_remain_serialized(self):
        bridge, _, peer, observed, _ = self.start_relay(hold_read=True)
        barrier = threading.Barrier(3)
        payloads = [bytes([value]) * 96 for value in (65, 66, 67)]
        def fragmented_send(payload):
            for offset in range(0, len(payload), 8):
                observed.socket.sendall(payload[offset:offset + 8])
                time.sleep(.001)
        def write(payload):
            barrier.wait(2)
            bridge.submit_responses([self.response(payload)])
        with patch.object(observed, 'sendall', side_effect=fragmented_send):
            done = [self.start_worker(write, payload) for payload in payloads]
            received = bytearray()
            while len(received) < 288:
                chunk = peer.recv(288 - len(received))
                self.assertTrue(chunk, 'relay closed before concurrent writes completed')
                received.extend(chunk)
            self.assertTrue(all(event.wait(2) for event in done))
        self.assertCountEqual([bytes(received[offset:offset + 96]) for offset in range(0, 288, 96)], payloads)


class SocksBridgeTests(unittest.TestCase):
    """Verify relay state transitions that do not require network sockets."""

    def test_wait_connect_accepts_response_received_before_waiter_creation(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge._enqueue_task({"op": "connect", "id": "fast-connect", "host": "example", "port": 443})
        bridge.submit_responses([{"id": "fast-connect", "op": "ok"}])

        self.assertTrue(bridge._wait_connect("fast-connect"))

    def test_wait_connect_reports_error_received_before_waiter_creation(self):
        bridge = SessionSocksBridge("test-session", "127.0.0.1", 0)
        bridge._enqueue_task({"op": "connect", "id": "fast-connect", "host": "example", "port": 443})
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
        bridge._enqueue_task({"op": "connect", "id": "early-data", "host": "example", "port": 443})
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
        bridge._enqueue_task({"op": "connect", "id": "fast-close", "host": "example", "port": 443})
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



class ScanLoadRegressionTests(unittest.TestCase):
    """Cover bounded control queues and timeout cancellation under scan load."""

    def bridge(self, external=False):
        bridge = SessionSocksBridge('scan', '127.0.0.1', 0, external=external)
        bridge._running = True
        self.addCleanup(bridge.stop)
        return bridge

    def test_repeated_workspace_controls_are_deduplicated_but_sends_are_not(self):
        bridge = self.bridge(external=True)
        controls = [{'op': op, 'id': 'flow'} for op in ('connect', 'close', 'udp_close')]
        for _ in range(100):
            bridge.submit_external_tasks(controls + [{'op': 'send', 'id': 'flow', 'data_b64': 'YQ=='}])
        self.assertEqual(sum(task['op'] == 'send' for task in bridge.task_queue), 100)
        tasks = bridge.fetch_tasks()
        for control in controls:
            self.assertEqual(tasks.count(control), 1)
        self.assertEqual(sum(task['op'] == 'send' for task in tasks), 32)

    def test_timeout_cancels_connect_and_closes_late_success(self):
        bridge = self.bridge()
        bridge._enqueue_task({'op': 'connect', 'id': 'flow', 'host': 'example', 'port': 443})
        with patch('rmm_socks.threading.Event') as event:
            event.return_value.wait.return_value = False
            self.assertFalse(bridge._wait_connect('flow'))
        self.assertEqual(bridge.fetch_tasks(), [{'op': 'close', 'id': 'flow'}])
        # Even a close acknowledgment preceding a delayed success must not
        # resurrect a connection that no longer has a local client.
        bridge.submit_responses([{'op': 'closed', 'id': 'flow'}, {'op': 'ok', 'id': 'flow'}])
        self.assertNotIn('flow', bridge.tunnels)
        self.assertEqual(bridge.fetch_tasks(), [{'op': 'close', 'id': 'flow'}])

    def test_timeout_closes_success_arriving_at_wait_boundary(self):
        bridge = self.bridge()
        bridge._enqueue_task({"op": "connect", "id": "flow", "host": "example", "port": 443})
        def complete_before_timeout(_timeout=None, **kwargs):
            bridge.submit_responses([{'op': 'ok', 'id': 'flow'}])
            return False
        with patch('rmm_socks.threading.Event') as event:
            event.return_value.wait.side_effect = complete_before_timeout
            self.assertFalse(bridge._wait_connect('flow'))
        self.assertNotIn('flow', bridge.tunnels)
        self.assertEqual(bridge.fetch_tasks(), [{'op': 'close', 'id': 'flow'}])

    def test_malformed_control_id_is_ignored(self):
        bridge = self.bridge(external=True)
        bridge.submit_external_tasks([{'op': 'connect', 'id': ['invalid']}])
        self.assertEqual(bridge.fetch_tasks(), [])

    def test_unsolicited_success_never_creates_orphan(self):
        bridge = self.bridge()
        bridge.submit_responses([{'op': 'ok', 'id': 'old-flow'}])
        self.assertNotIn('old-flow', bridge.tunnels)
        self.assertEqual(bridge.fetch_tasks(), [{'op': 'close', 'id': 'old-flow'}])

    def test_pending_acknowledgment_prevents_connect_requeue(self):
        bridge = self.bridge(external=True)
        task = {'op': 'connect', 'id': 'flow', 'host': 'example', 'port': 443}
        bridge.submit_external_tasks([task])
        bridge.submit_responses([{'op': 'ok', 'id': 'flow'}])
        bridge.submit_external_tasks([task])
        self.assertEqual(bridge.fetch_tasks(), [])
        self.assertEqual(bridge.drain_external_responses(), [{'op': 'ok', 'id': 'flow'}])

    def test_workspace_consumes_ack_before_resubmitting_connect(self):
        from workspace_pivot import WorkspacePivotManager
        submitted = []
        with patch('workspace_pivot.threading.Thread.start'):
            manager = WorkspacePivotManager(
                'unused', lambda _sid, _channel, tasks: submitted.extend(tasks) or True,
                lambda _sid, _channel: [{'op': 'ok', 'id': 'flow'}],
            )
        self.addCleanup(manager.stop_all)
        bridge = self.bridge()
        bridge._enqueue_task({'op': 'connect', 'id': 'flow', 'host': 'example', 'port': 443})
        with patch.object(manager.socks, 'list_relays', return_value=[{'session_id': 'scan'}]), \
                patch.object(manager.socks, 'get', return_value=bridge):
            manager.sync_once()
        self.assertIn('flow', bridge.tunnels)
        self.assertEqual(submitted, [])

    def test_workspace_requeues_identical_data_on_regular_bridge(self):
        bridge = self.bridge()
        control = {'op': 'connect', 'id': 'flow', 'host': 'example', 'port': 443}
        send = {'op': 'send', 'id': 'flow', 'data_b64': 'YQ=='}
        datagram = {
            'op': 'udp_send', 'id': 'udp-flow', 'host': 'example',
            'port': 53, 'data_b64': 'Yg==',
        }
        with bridge.lock:
            bridge.task_queue.extend([control, send, send, datagram])
            bridge._queued_udp_tasks = 1

        drained = bridge.fetch_tasks()
        with bridge.lock:
            bridge.task_queue.append(dict(send))
        bridge.requeue_tasks(drained)

        with bridge.lock:
            queued = list(bridge.task_queue)
            queued_udp = bridge._queued_udp_tasks
        self.assertEqual(sum(task == control for task in queued), 1)
        self.assertEqual(sum(task == send for task in queued), 3)
        self.assertEqual(sum(task == datagram for task in queued), 1)
        self.assertEqual(queued_udp, 1)


if __name__ == "__main__":
    unittest.main()
