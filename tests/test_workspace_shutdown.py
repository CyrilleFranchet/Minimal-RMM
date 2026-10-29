"""Exercise workspace shutdown with real sockets and blocked network workers."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from workspace_service import WorkspaceService, WorkspaceUpstream
from workspace_pivot import WorkspacePivotManager


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceShutdownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def service(self, **kwargs):
        service = WorkspaceService("http://127.0.0.1:1", "alice", "test-token",
                                   data_dir=self.directory.name, port=0, **kwargs)
        service.log = Mock()
        service.upstream.request = Mock(return_value=(0, {}, "application/json"))
        self.addCleanup(service.stop)
        return service

    def test_stop_before_serving_closes_listener_workers_and_database(self):
        service = self.service(poll_interval=3600)
        server = service.start()
        service.stop()
        service.stop()
        self.assertEqual(server.socket.fileno(), -1)
        self.assertFalse(service._sync_thread.is_alive())
        self.assertFalse(service.pivots._thread.is_alive())
        with self.assertRaises(sqlite3.ProgrammingError):
            service.store.get_cursor()

    def test_bind_failure_stops_constructor_pivot_thread_and_closes_store(self):
        service = self.service()
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        service.port = occupied.getsockname()[1]
        with self.assertRaises(OSError):
            service.start()
        self.assertFalse(service.pivots._thread.is_alive())
        self.assertTrue(service._closed)

    def test_stop_interrupts_partial_client_and_waits_before_database_close(self):
        service = self.service(poll_interval=3600)
        server = service.start()
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        client = socket.create_connection(("127.0.0.1", service.port))
        self.addCleanup(client.close)
        client.sendall(b"POST /api/v1/settings HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{")
        deadline = time.monotonic() + 2
        while not server._requests and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server._requests)
        service.stop()
        serving.join(timeout=2)
        self.assertFalse(serving.is_alive())
        self.assertFalse(server._requests)
        self.assertTrue(service._closed)

    def test_serve_forever_returns_cleanly_on_stop_request(self):
        service = self.service()
        serving = threading.Thread(target=service.serve_forever, daemon=True)
        serving.start()
        deadline = time.monotonic() + 2
        while service.http_server is None and time.monotonic() < deadline:
            time.sleep(0.01)
        service.request_stop()
        serving.join(timeout=2)
        self.assertFalse(serving.is_alive())
        self.assertTrue(service._closed)

    def test_upstream_close_interrupts_response_headers_and_body(self):
        for send_headers in (False, True):
            with self.subTest(send_headers=send_headers):
                entered = threading.Event()
                release = threading.Event()

                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        if send_headers:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", "100")
                            self.end_headers()
                            self.wfile.write(b"{")
                            self.wfile.flush()
                        entered.set()
                        release.wait(5)

                    def log_message(self, *_args):
                        pass

                central = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                serving = threading.Thread(target=central.serve_forever, daemon=True)
                serving.start()
                upstream = WorkspaceUpstream(f"http://127.0.0.1:{central.server_port}", "alice", "token")
                result = []
                worker = threading.Thread(target=lambda: result.append(upstream.request("GET", "/slow", timeout=300)), daemon=True)
                try:
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    upstream.close()
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(result[0][0], 0)
                    self.assertEqual(upstream.request("GET", "/after-stop")[1]["error"], "workspace_stopping")
                finally:
                    release.set()
                    upstream.close()
                    central.shutdown()
                    central.server_close()
                    serving.join(timeout=2)

    def test_pivot_manager_cannot_restart_after_stop(self):
        manager = WorkspacePivotManager("missing", lambda *_args: True, lambda *_args: [])
        manager.stop_all()
        manager.stop_all()
        self.assertFalse(manager._thread.is_alive())
        with self.assertRaises(RuntimeError):
            manager.start_socks("session", port=0)

    def test_sigterm_exits_workspace_without_traceback(self):
        config = Path(self.directory.name) / "settings.json"
        config.write_text(json.dumps({"schema": "minimal-rmm-settings-v1", "scope": "workspace", "settings": {
            "RMM_WORKSPACE_ID": "alice", "RMM_WORKSPACE_TOKEN": "token", "RMM_C2_URL": "http://127.0.0.1:1",
            "RMM_WORKSPACE_POLL_INTERVAL": 3600}}))
        env = {key: value for key, value in os.environ.items() if not key.startswith("RMM_")}
        env["PYTHONUNBUFFERED"] = "1"
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        with tempfile.TemporaryFile(mode="w+") as output:
            process = subprocess.Popen([sys.executable, str(ROOT / "workspace_service.py"), "--config", str(config),
                                        "--data-dir", self.directory.name, "--port", str(port)], env=env,
                                       stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail("workspace did not start")
                        time.sleep(0.025)
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                output.seek(0)
                self.assertNotIn("Traceback", output.read())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
