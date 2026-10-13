"""Coverage for dynamic operator assets and the RMM-Profile process contract."""

import json
import hashlib
from pathlib import Path
import tempfile
import textwrap
import unittest

from rmm_asset_store import AssetError, AssetStore
from rmm_profile import ProfileManager
from server_rmm import RMMHandler


class AssetStoreTests(unittest.TestCase):
    """Verify URL asset storage metadata and enablement state."""

    def test_asset_files_are_atomic_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(directory)
            row = store.add_bytes("plugins", "socks.dll", b"MZpayload", source_url="https://example.test/socks.dll")
            self.assertEqual(row["sha256"], hashlib.sha256(b"MZpayload").hexdigest())
            self.assertEqual(store.read("plugins", "socks.dll"), b"MZpayload")
            store.set_enabled("plugins", "socks.dll", False)
            with self.assertRaisesRegex(AssetError, "disabled"):
                store.read("plugins", "socks.dll")
            self.assertEqual(store.read("plugins", "socks.dll", enabled_only=False), b"MZpayload")

    def test_rclone_and_skill_extensions_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(directory)
            with self.assertRaises(AssetError):
                store.add_bytes("skills", "skill.txt", b"body")
            with self.assertRaises(AssetError):
                store.add_bytes("rclone", "profile.yaml", b"{}")


class ProfileManagerTests(unittest.TestCase):
    """Verify the generic ready event is independent of transport details."""

    def test_runner_ready_event_publishes_public_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fake"
            root.mkdir()
            (root / "profile.json").write_text(
                json.dumps(
                    {
                        "schema": "minimal-rmm-profile-v1",
                        "name": "fake",
                        "version": "1",
                        "entrypoint": "runner.py",
                    }
                ),
                encoding="utf-8",
            )
            (root / "runner.py").write_text(
                textwrap.dedent(
                    """
                    import json, signal, time
                    running = True
                    def stop(_signum, _frame):
                        global running
                        running = False
                    signal.signal(signal.SIGTERM, stop)
                    print(json.dumps({"event":"ready","public_url":"https://fake.example"}), flush=True)
                    while running:
                        time.sleep(0.01)
                    """
                ),
                encoding="utf-8",
            )
            manager = ProfileManager(directory)
            event = manager.start("fake", listen_host="127.0.0.1", listen_port=8081)
            self.assertEqual(event["public_url"], "https://fake.example")
            self.assertTrue(manager.status()["active"]["running"])
            self.assertTrue(manager.stop())
            self.assertIsNone(manager.status()["active"])

    def test_listener_roles_keep_operator_and_beacon_paths_separate(self):
        handler = RMMHandler.__new__(RMMHandler)
        handler.listener_role = "admin"
        self.assertTrue(handler._path_allowed("/api/v1/health"))
        self.assertTrue(handler._path_allowed("/ui/index.html"))
        self.assertFalse(handler._path_allowed("/register"))
        handler.listener_role = "beacon"
        self.assertTrue(handler._path_allowed("/register"))
        self.assertTrue(handler._path_allowed("/tools/plugins/socks.dll"))
        self.assertFalse(handler._path_allowed("/api/v1/health"))


if __name__ == "__main__":
    unittest.main()
