"""Regression coverage for safe, serialized transport connector lifecycles."""

import json
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from rmm_profile import ProfileError, ProfileManager, ProfileProcess, _ready_event


class ProfileLifecycleTests(unittest.TestCase):
    """Exercise real runner handshakes and safe public status."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "fake"
        self.root.mkdir()
        self.manifest = {
            "schema": "minimal-rmm-profile-v1", "name": "fake", "version": "1",
            "entrypoint": "runner.py", "directory": str(self.root),
        }
        (self.root / "profile.json").write_text(json.dumps(self.manifest))
        self.logs = []
        self.manager = ProfileManager(self.directory.name, log_fn=lambda *args: self.logs.append(args))
        self.addCleanup(self.manager.stop)

    def runner(self, source):
        """Install a temporary connector implementation."""
        (self.root / "runner.py").write_text(source)

    def start(self):
        """Start the temporary connector with a generous handshake timeout."""
        return self.manager.start("fake", listen_host="127.0.0.1", listen_port=8081, timeout=5)

    def test_early_successful_exit_fails_without_waiting_for_timeout(self):
        self.runner("pass\n")
        started = time.monotonic()
        with self.assertRaisesRegex(ProfileError, "event stream"):
            self.start()
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(self.manager.status()["active"])

    def test_error_message_and_stderr_do_not_expose_token(self):
        self.runner("import json, sys\nprint('private-token', file=sys.stderr, flush=True)\nprint(json.dumps({'event':'error','message':'private-token'}), flush=True)\n")
        with self.assertRaises(ProfileError) as failure:
            self.start()
        public_output = str(failure.exception) + json.dumps(self.manager.status()) + str(self.logs)
        self.assertNotIn("private-token", public_output)
        self.assertIn("reported a failure", public_output)

    def test_ready_fields_are_allowlisted_and_hostname_is_derived(self):
        self.runner("import json, time\nprint(json.dumps({'event':'ready','public_url':'https://fake.example/','hostname':'private-token','token':'private-token','config':{'token':'private-token'}}), flush=True)\ntime.sleep(60)\n")
        event = self.start()
        self.assertEqual(event, {"event": "ready", "public_url": "https://fake.example", "hostname": "fake.example"})
        self.assertNotIn("private-token", json.dumps(self.manager.status()))

    def test_invalid_url_is_rejected_without_echoing_input(self):
        self.runner("import json\nprint(json.dumps({'event':'ready','public_url':'https://user:private-token@fake.example'}), flush=True)\n")
        with self.assertRaisesRegex(ProfileError, "invalid public URL") as failure:
            self.start()
        self.assertNotIn("private-token", str(failure.exception))

    def test_exited_connector_is_not_listed_as_running(self):
        self.runner("import json, time\nprint(json.dumps({'event':'ready','public_url':'https://fake.example'}), flush=True)\ntime.sleep(0.2)\n")
        self.start()
        deadline = time.monotonic() + 3
        while self.manager.status()["active"]["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        status = self.manager.status()
        self.assertFalse(status["active"]["running"])
        self.assertFalse(status["installed"][0]["running"])
        self.assertIsNone(status["public_url"])

    def test_unknown_events_do_not_replace_ready_handshake(self):
        self.runner("import json, time\nprint(json.dumps({'event':'ready','public_url':'https://fake.example'}), flush=True)\nprint(json.dumps({'event':'diagnostic','public_url':'private-token'}), flush=True)\ntime.sleep(60)\n")
        event = self.start()
        self.assertEqual(event["public_url"], "https://fake.example")

    def test_stop_kills_process_group_even_after_runner_has_exited(self):
        runner = ProfileProcess(self.manifest)
        process = Mock()
        process.pid = 12345
        process.wait.return_value = 0
        runner.process = process
        with patch("rmm_profile.os.killpg") as killpg:
            runner.stop()
        self.assertEqual(killpg.call_args_list[0].args, (12345, signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (12345, signal.SIGKILL))

    def test_stop_waits_for_inflight_start_before_clearing_active_process(self):
        started = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        results = []
        process = Mock()

        def start_process(**kwargs):
            started.set()
            if not release.wait(3):
                raise RuntimeError("test handshake was not released")
            results.append("started")
            return {"event": "ready", "public_url": "https://fake.example"}

        def stop_process():
            results.append("stopped")
            stopped.set()

        process.start.side_effect = start_process
        process.stop.side_effect = stop_process
        self.runner("pass\n")
        with patch("rmm_profile.ProfileProcess", return_value=process):
            start_thread = threading.Thread(target=self.start)
            stop_thread = threading.Thread(target=self.manager.stop)
            start_thread.start()
            try:
                self.assertTrue(started.wait(2))
                stop_thread.start()
                self.assertFalse(stopped.wait(0.1))
            finally:
                release.set()
                start_thread.join(3)
                if stop_thread.ident:
                    stop_thread.join(3)
        self.assertFalse(start_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(results, ["started", "stopped"])
        self.assertIsNone(self.manager.status()["active"])

    def test_failed_start_always_cleans_up_and_redacts_unknown_exception(self):
        process = Mock()
        process.start.side_effect = RuntimeError("private-token")
        self.runner("pass\n")
        with patch("rmm_profile.ProfileProcess", return_value=process), self.assertRaisesRegex(ProfileError, "profile startup failed"):
            self.start()
        process.stop.assert_called_once()
        self.assertIsNone(self.manager.status()["active"])
        self.assertNotIn("private-token", json.dumps(self.manager.status()))

    def test_public_origins_reject_unsafe_url_components(self):
        for value in ("javascript:bad", "https://example/path", "https://example?token=x", "https://example#x", "https://example:99999", "https://example\\secret", "https://exa mple"):
            with self.subTest(value=value), self.assertRaises(ProfileError):
                _ready_event({"public_url": value})


if __name__ == "__main__":
    unittest.main()
