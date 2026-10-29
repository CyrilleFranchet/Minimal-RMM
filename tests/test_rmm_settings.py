"""Persistence, startup consumption, and HTTP configuration regressions."""
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import rmm_settings
from rmm_settings import SettingsError, SettingsStore

ROOT = Path(__file__).resolve().parents[1]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"

    def store(self, scope="server", environment=None):
        return SettingsStore(self.path, scope, environment=environment or {})

    def test_restart_redaction_permissions_and_reset(self):
        store = self.store()
        patch_values = {"RMM_CLOUDFLARE_TOKEN": "never-return-me", "RMM_MAX_BODY_BYTES": 65536}
        snapshot = store.update({"settings": patch_values})
        self.assertTrue(snapshot["restart_required"])
        self.assertNotIn("never-return-me", json.dumps(snapshot))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("RMM_MAX_BODY_BYTES", store.effective)
        reopened = self.store()
        self.assertEqual(reopened.effective, patch_values)
        self.assertFalse(reopened.snapshot()["restart_required"])
        reopened.update({"settings": {"RMM_CLOUDFLARE_TOKEN": None}})
        self.assertNotIn("RMM_CLOUDFLARE_TOKEN", self.store().effective)

    def test_invalid_patch_is_atomic_and_wrong_scope_refused(self):
        store = self.store()
        store.update({"settings": {"RMM_MAX_BODY_BYTES": 65536}})
        before = self.path.read_bytes()
        for values in ({"RMM_MAX_BODY_BYTES": 131072, "RMM_PROFILE_AUTOSTART": "false"},
                       {"RMM_WORKSPACE_BIND": "127.0.0.1"},
                       {"RMM_ASSET_MAX_BYTES": 536870913},
                       {"RMM_RCLONE_PROFILES": []},
                       {"RMM_CLOUDFLARE_PUBLIC_URL": "https://user:secret@example.com"}):
            with self.subTest(values=values), self.assertRaises(SettingsError):
                store.update({"settings": values})
            self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(SettingsError):
            self.store("workspace")

    def test_explicit_environment_wins_empty_environment_does_not(self):
        self.store().update({"settings": {"RMM_MAX_BODY_BYTES": 65536, "RMM_API_TOKEN": "saved"}})
        store = self.store(environment={"RMM_MAX_BODY_BYTES": "131072", "RMM_API_TOKEN": ""})
        rows = {row["key"]: row for row in store.snapshot()["settings"]}
        self.assertEqual(rows["RMM_MAX_BODY_BYTES"]["source"], "environment")
        self.assertEqual(rows["RMM_MAX_BODY_BYTES"]["value"], 131072)
        self.assertEqual(rows["RMM_MAX_WORKSPACE_PAYLOAD_BYTES"]["value"], 131072)
        self.assertEqual(store.effective["RMM_API_TOKEN"], "saved")

    def test_corruption_never_resets_or_overwrites_settings(self):
        self.path.write_text("not-json")
        with self.assertRaises(SettingsError):
            self.store()
        self.assertEqual(self.path.read_text(), "not-json")

    def test_named_plugin_secrets_are_supported_and_redacted(self):
        store = self.store("workspace")
        store.update({"settings": {"RMM_PLUGIN_TOKEN_INTERNAL": "private-secret"}})
        reopened = self.store("workspace")
        self.assertEqual(reopened.effective["RMM_PLUGIN_TOKEN_INTERNAL"], "private-secret")
        self.assertNotIn("private-secret", json.dumps(reopened.snapshot()))

    def test_internal_client_ignores_saved_admin_config(self):
        self.path.write_text("invalid and wrong-scope")
        with patch.dict(os.environ, {"RMM_INTERNAL_MCP": "1", "RMM_CONFIG_FILE": str(self.path)}, clear=True), patch.object(rmm_settings, "_active", None):
            store = rmm_settings.bootstrap("client", [])
            self.assertEqual(store.effective, {})
            self.assertIsNone(store.path)
            with self.assertRaises(SettingsError):
                store.update({"settings": {"RMM_API_TOKEN": "admin"}})

    def test_saved_values_reach_import_time_consumers_in_fresh_process(self):
        self.store().update({"settings": {
            "RMM_API_TOKEN": "test-api", "RMM_BEACON_SECRET": "test-beacon",
            "RMM_MAX_BODY_BYTES": 65536, "RMM_ASSET_MAX_BYTES": 131072,
            "RMM_AI_MAX_TOOL_ROUNDS": 7, "RMM_TUN_LOG_LEVEL": "warn",
            "RMM_RCLONE_MAX_BYTES": 12345, "RMM_PROFILE_AUTOSTART": False,
        }})
        script = '''
import json, os
from rmm_settings import bootstrap
bootstrap("server", ["--config", os.environ["TEST_CONFIG"]])
import server_rmm as server, rmm_ai, rmm_asset_store, rmm_tun, rmm_rclone
print(json.dumps([server.API_TOKEN, server.BEACON_SECRET, server.MAX_BODY_BYTES,
server.MAX_WORKSPACE_PAYLOAD_BYTES, rmm_asset_store._max_bytes(), rmm_ai.MAX_TOOL_ROUNDS,
rmm_tun.DEFAULT_TUN_LOG_LEVEL, rmm_rclone.get_rclone_max_bytes(), "RMM_API_TOKEN" in os.environ]))
'''
        env = {key: value for key, value in os.environ.items() if not key.startswith("RMM_")}
        env["TEST_CONFIG"] = str(self.path)
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["test-api", "test-beacon", 65536, 65536, 131072, 7, "warn", 12345, False])

    def test_offline_import_stdin_and_show_never_print_secret(self):
        args = [sys.executable, str(ROOT / "rmm_settings.py"), "--scope", "server", "--config", str(self.path)]
        result = subprocess.run([*args, "import", "-"], input=json.dumps({"RMM_API_TOKEN": "stdin-secret"}), capture_output=True, text=True, check=True)
        self.assertNotIn("stdin-secret", result.stdout + result.stderr)
        self.assertEqual(self.store().effective["RMM_API_TOKEN"], "stdin-secret")

    def test_real_server_restart_and_named_profile_autostart(self):
        """Exercise persisted auth, CLI precedence, API saves and connector startup."""
        listeners = [socket.socket(), socket.socket()]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        admin, beacon = [listener.getsockname()[1] for listener in listeners]
        for listener in listeners:
            listener.close()
        bundle = Path(self.temp.name) / "profiles" / "cloudflared"
        bundle.mkdir(parents=True)
        (bundle / "profile.json").write_text(json.dumps({
            "schema": "minimal-rmm-profile-v1", "name": "cloudflared", "version": "test",
            "entrypoint": "runner.py", "command": [sys.executable, "{entrypoint}"]}))
        (bundle / "runner.py").write_text('''
import json, os, time
config = json.loads(os.environ["RMM_PROFILE_CONFIG_JSON"])
assert config["mode"] == "named" and config["token"] == "connector-secret"
print(json.dumps({"event":"ready", "public_url":config["public_url"]}), flush=True)
time.sleep(60)
''')
        self.store().update({"settings": {
            "RMM_ADMIN_PORT": admin, "RMM_BEACON_PORT": beacon,
            "RMM_API_TOKEN": "before-restart", "RMM_BEACON_SECRET": "test-beacon",
            "RMM_PROFILES_DIR": str(bundle.parent), "RMM_CLOUDFLARE_MODE": "named",
            "RMM_CLOUDFLARE_PUBLIC_URL": "https://beacon.example.com",
            "RMM_CLOUDFLARE_TOKEN": "connector-secret", "RMM_RCLONE_MAX_BYTES": 123,
        }})
        env = {key: value for key, value in os.environ.items() if not key.startswith("RMM_")}
        env["RMM_LOG_DIR"] = str(Path(self.temp.name) / "data")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, token, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{admin}/api/v1/{path}",
                                         data=json.dumps(body).encode() if body else None,
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                         method="PATCH" if body else "GET")
            with opener.open(req, timeout=1) as response:
                return json.load(response)

        for token in ("before-restart", "after-restart"):
            with tempfile.TemporaryFile(mode="w+") as output:
                process = subprocess.Popen([sys.executable, str(ROOT / "server_rmm.py"), "--config", str(self.path),
                                            "--rclone-max-bytes", "456"], cwd=self.temp.name, env=env,
                                           stdout=output, stderr=output)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            snapshot = request("settings", token)
                            transport = request("transport", token)
                            if transport["beacon_url"] == "https://beacon.example.com":
                                break
                        except (OSError, urllib.error.URLError):
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            output.seek(0)
                            self.fail("server did not become ready: " + output.read())
                        time.sleep(0.05)
                    rows = {row["key"]: row for row in snapshot["settings"]}
                    self.assertEqual(rows["RMM_RCLONE_MAX_BYTES"]["value"], 456)
                    self.assertEqual(rows["RMM_RCLONE_MAX_BYTES"]["source"], "command line")
                    self.assertNotIn("connector-secret", json.dumps(snapshot))
                    if token == "before-restart":
                        changed = request("settings", token, {"settings": {"RMM_API_TOKEN": "after-restart"}})
                        self.assertTrue(changed["restart_required"])
                        request("health", token)
                    else:
                        self.assertFalse(snapshot["restart_required"])
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            request("health", "before-restart")
                        self.assertEqual(error.exception.code, 401)
                finally:
                    process.send_signal(2)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    unittest.main()
