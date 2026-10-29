"""Real process regressions for service shutdown and partial startup failures."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import server_rmm
from server_rmm import BuildProcessManager, RMMHTTPServer, RMMServer, run_server

ROOT = Path(__file__).resolve().parents[1]


class ServerShutdownTests(unittest.TestCase):
    def test_partial_listener_startup_releases_first_listener(self):
        first = Mock()
        runtime = Mock()
        runtime.running = True
        with patch.object(server_rmm, "ADMIN_PORT", 8000), patch.object(server_rmm, "BEACON_PORT", 8001), \
                patch.object(server_rmm, "RMMHTTPServer", side_effect=[first, OSError("occupied")]):
            with self.assertRaises(OSError):
                run_server(runtime, SimpleNamespace(cli=False, profile=""))
        first.shutdown.assert_not_called()
        first.abort_requests.assert_called_once()
        first.server_close.assert_called_once()
        runtime.close.assert_called_once()

    def test_runtime_close_is_idempotent_and_continues_after_component_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server_rmm, "LOG_DIR", directory), \
                patch.object(RMMServer, "_archive_orphaned_sessions_on_startup"):
            runtime = RMMServer()
            runtime.log = Mock()
            runtime.profile_manager = Mock()
            runtime.profile_manager.close.side_effect = RuntimeError("failed")
            runtime.socks = Mock()
            runtime.tun = Mock()
            runtime.tun_transport = Mock()
            runtime.event_hub = Mock()
            runtime._embedding_store = Mock()
            runtime._ui_state_store = Mock()
            embedding_store = runtime._embedding_store
            ui_state_store = runtime._ui_state_store
            timer = Mock()
            runtime._sessions_broadcast_timer = timer
            runtime.close()
            runtime.close()
            self.assertFalse(runtime.running)
            timer.cancel.assert_called_once()
            runtime.event_hub.close.assert_called_once()
            runtime.socks.close.assert_called_once()
            embedding_store.close.assert_called_once()
            ui_state_store.close.assert_called_once()
            with self.assertRaisesRegex(ValueError, "stopping"):
                runtime.embedding_store()
            with self.assertRaisesRegex(server_rmm.ProfileError, "stopping"):
                runtime.start_profile()

    def test_build_shutdown_cancels_and_reaps_active_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            owner = BuildProcessManager()
            results = []
            command = [sys.executable, "-c", f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)"]
            worker = threading.Thread(target=lambda: results.append(owner.run(command, cwd=directory, env=os.environ.copy(), timeout=60)))
            worker.start()
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                owner.close()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertLess(results[0].returncode, 0)
                with self.assertRaisesRegex(RuntimeError, "stopping"):
                    owner.run(command, cwd=directory, env={}, timeout=1)
            finally:
                owner.close()
                worker.join(3)

    def test_http_abort_releases_idle_request(self):
        entered = threading.Event()
        finished = threading.Event()

        class Handler(server_rmm.BaseHTTPRequestHandler):
            def handle(self):
                entered.set()
                try:
                    self.connection.recv(1)
                finally:
                    finished.set()

        listener = RMMHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=listener.serve_forever)
        worker.start()
        client = socket.create_connection(listener.server_address)
        try:
            self.assertTrue(entered.wait(2))
            listener.shutdown()
            listener.abort_requests()
            self.assertTrue(finished.wait(2))
        finally:
            client.close()
            listener.shutdown()
            listener.server_close()
            worker.join(2)

    def test_signals_stop_connector_process_group_and_release_ports(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sockets = [socket.socket(), socket.socket()]
                for sock in sockets:
                    sock.bind(("127.0.0.1", 0))
                ports = [sock.getsockname()[1] for sock in sockets]
                for sock in sockets:
                    sock.close()
                bundle = root / "profiles" / "fake"
                bundle.mkdir(parents=True)
                pidfile = root / "child.pid"
                (bundle / "profile.json").write_text(json.dumps({
                    "schema": "minimal-rmm-profile-v1", "name": "fake", "version": "1",
                    "entrypoint": "runner.py", "command": [sys.executable, "{entrypoint}"]}))
                (bundle / "runner.py").write_text(
                    "import json, subprocess, sys, time\nfrom pathlib import Path\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                    f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
                    "print(json.dumps({'event':'ready','public_url':'https://fake.example'}),flush=True)\n"
                    "time.sleep(60)\n")
                env = {key: val for key, val in os.environ.items() if not key.startswith("RMM_")}
                env.update(RMM_LOG_DIR=str(root / "data"), RMM_PROFILES_DIR=str(bundle.parent))
                with tempfile.TemporaryFile(mode="w+") as output:
                    process = subprocess.Popen([sys.executable, str(ROOT / "server_rmm.py"),
                        "--insecure", "--admin-port", str(ports[0]), "--beacon-port", str(ports[1]),
                        "--profile", "fake"], cwd=root, env=env, stdout=output, stderr=output)
                    try:
                        deadline = time.monotonic() + 5
                        while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(pidfile.exists())
                        pid = int(pidfile.read_text())
                        process.send_signal(signum)
                        self.assertEqual(process.wait(timeout=10), 0)
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
                            if not state or state.startswith("Z"):
                                break
                            time.sleep(0.02)
                        self.assertTrue(not state or state.startswith("Z"), "connector child survived service shutdown")
                        for port in ports:
                            with socket.socket() as probe:
                                self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
