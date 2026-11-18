"""Real SOCKS5 round trips through the shared SOCKS/TUN relay on loopback."""
import socket
import statistics
import threading
import time
import unittest

from rmm_socks import SessionSocksBridge


class PivotRoundTripTests(unittest.TestCase):
    """Exercise the public socket path with a local agent-protocol echo fixture."""

    def test_repeated_round_trips_preserve_bytes_without_idle_reader_stalls(self):
        bridge = SessionSocksBridge('loopback-fixture', bind_host='127.0.0.1', port=0)
        stop = threading.Event()
        errors = []
        bridge.start()

        def agent_echo():
            """Echo transport tasks through real response delivery, without targets."""
            try:
                while not stop.is_set():
                    for task in bridge.fetch_tasks():
                        op, key = task['op'], task['id']
                        if op == 'connect':
                            bridge.submit_responses([{'op': 'ok', 'id': key}])
                        elif op == 'send':
                            bridge.submit_responses([{'op': 'data', 'id': key,
                                                      'data_b64': task['data_b64']}])
                        elif op == 'close':
                            bridge.submit_responses([{'op': 'closed', 'id': key}])
                    stop.wait(.001)
            except BaseException as error:
                errors.append(error)

        agent = threading.Thread(target=agent_echo, daemon=True)
        agent.start()
        client = None
        samples = []
        try:
            client = socket.create_connection(('127.0.0.1', bridge.port), timeout=.75)
            client.sendall(b'\x05\x01\x00')
            self.assertEqual(SessionSocksBridge._read_exact(client, 2), b'\x05\x00')
            # The protocol fixture acknowledges CONNECT without contacting this address.
            client.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01\x01\x85')
            reply = SessionSocksBridge._read_exact(client, 10)
            self.assertIsNotNone(reply)
            self.assertEqual(reply[:2], b'\x05\x00')
            for index in range(50):
                payload = index.to_bytes(4, 'big') + bytes(range(256)) * (129 if index % 10 == 0 else 1)
                started = time.monotonic()
                client.sendall(payload)
                returned = SessionSocksBridge._read_exact(client, len(payload))
                samples.append(time.monotonic() - started)
                self.assertEqual(returned, payload)
            self.assertFalse(errors, errors)
            print('SOCKS loopback: 50 round trips, median %.2f ms, max %.2f ms' %
                  (statistics.median(samples) * 1000, max(samples) * 1000), flush=True)
        finally:
            stop.set()
            bridge.stop()
            if client is not None:
                client.close()
            agent.join(3)
        self.assertFalse(agent.is_alive(), 'agent fixture must not remain blocked in response delivery')
        self.assertFalse(bridge._client_threads, 'all local reader threads must stop')
