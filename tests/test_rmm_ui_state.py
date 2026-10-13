"""Coverage for SQLite-backed operator WebUI state and local asset uploads."""

import base64
from pathlib import Path
import tempfile
import unittest

from rmm_asset_store import AssetStore, decode_asset_content
from rmm_ui_state import UiStateStore
from workspace_service import WorkspaceStore


class UiStateStoreTests(unittest.TestCase):
    """Verify state persistence is allowlisted and survives a reopen."""

    def test_state_round_trip_excludes_api_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-state.db"
            store = UiStateStore(path)
            store.set(
                "server",
                {
                    "storage": {
                        "session": {
                            "rmm_ai_provider": "openai",
                            "rmm_api_token": "must-not-persist",
                        },
                        "local": {"rmm_theme": "light"},
                    }
                },
            )
            store.close()

            reopened = UiStateStore(path)
            self.assertEqual(
                reopened.get("server"),
                {
                    "session": {"rmm_ai_provider": "openai"},
                    "local": {"rmm_theme": "light"},
                },
            )
            reopened.close()

    def test_workspace_store_uses_its_existing_sqlite_database(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory, "alice")
            store.set_ui_state(
                {"storage": {"session": {"rmm_sidebar_width": "320"}}}
            )
            self.assertEqual(
                store.get_ui_state(),
                {"session": {"rmm_sidebar_width": "320"}, "local": {}},
            )
            self.assertIn("agent-command-dispatch", {row["id"] for row in store.list_skills()})
            store.close()


class AssetUploadTests(unittest.TestCase):
    """Verify the bounded file upload path feeds the existing asset store."""

    def test_base64_rclone_upload_is_decoded_and_persisted(self):
        content = b'{"mega-lab":{"type":"mega"}}'
        encoded = base64.b64encode(content).decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(directory)
            row = store.add_bytes("rclone", "profiles.json", decode_asset_content(encoded))
            self.assertEqual(row["name"], "profiles.json")
            self.assertEqual(store.read("rclone", "profiles.json"), content)


if __name__ == "__main__":
    unittest.main()
