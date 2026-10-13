"""Unit coverage for central workspace ownership and result forwarding."""

from collections import deque
import json
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import server_rmm
from rmm_rclone import RcloneConfigError
from rmm_workspace import WorkspaceEventBroker, WorkspaceRegistry
from server_rmm import RMMHandler, RMMServer, Session, _resolve_api_session


class ServerWorkspaceTests(unittest.TestCase):
    """Verify workspace scoping and non-persistent central result handling."""

    def make_server(self):
        server = RMMServer.__new__(RMMServer)
        server.storage_mode = "workspace"
        server.sessions = {}
        server.session_lock = threading.RLock()
        server.workspace_registry = WorkspaceRegistry(
            {"alice": "alice-secret", "bob": "bob-secret"}
        )
        server.workspace_events = WorkspaceEventBroker()
        server._persisted_session_records = {}
        server._persisted_session_configs = {}
        server.workspace_socks_configs = {}
        server.workspace_tun_configs = {}
        server._broadcast_sessions_async = MagicMock()
        server.event_hub = MagicMock()
        server.tun_transport = MagicMock()
        server.socks = MagicMock()
        server.tty_lock = threading.Lock()
        server._cli_use_ptk = False
        server.log = MagicMock()
        server.tty_print = MagicMock()
        server.touch_session = MagicMock()
        return server

    @staticmethod
    def add_session(server, session_id="session-1234"):
        session = Session(session_id, "target", "user")
        server.sessions[session.id] = session
        return session

    def test_claims_are_exclusive_and_workspace_resolution_is_scoped(self):
        server = self.make_server()
        session = self.add_session(server)
        with patch.object(server, "save_session") as save_session:
            claimed, error = server.claim_workspace_session("alice", session.id[:8])

        self.assertIs(claimed, session)
        self.assertIsNone(error)
        self.assertEqual(session.workspace_id, "alice")
        save_session.assert_called_once_with(session)
        self.assertIs(server.resolve_workspace_session("alice", session.id), session)
        self.assertIsNone(server.resolve_workspace_session("bob", session.id))

        claimed, error = server.claim_workspace_session("bob", session.id)
        self.assertIsNone(claimed)
        self.assertEqual(error, "session_claimed")

    def test_workspace_headers_authenticate_and_limit_session_lookup(self):
        server = self.make_server()
        session = self.add_session(server)
        session.workspace_id = "alice"
        handler = SimpleNamespace(
            server_instance=server,
            headers={
                "X-RMM-Workspace": "alice",
                "X-RMM-Workspace-Token": "alice-secret",
                "Authorization": "",
                "X-RMM-Token": "",
            },
            _workspace_scope_id=None,
        )
        handler._workspace_scope = RMMHandler._workspace_scope.__get__(handler)
        handler._api_session = RMMHandler._api_session.__get__(handler)
        handler._api_token_from_request = lambda _qs=None: ""

        with patch.object(server_rmm, "API_TOKEN", ""), patch.object(server_rmm, "INSECURE", False):
            self.assertTrue(RMMHandler._api_authorized(handler))

        self.assertEqual(handler._workspace_scope(), "alice")
        self.assertIs(_resolve_api_session(handler, session.id[:8]), session)

        session.workspace_id = "bob"
        self.assertIsNone(_resolve_api_session(handler, session.id))

    def test_workspace_result_payload_is_forwarded_without_server_artifact_write(self):
        server = self.make_server()
        session = self.add_session(server)
        session.workspace_id = "alice"
        server._record_event = MagicMock()
        server.pop_pending_download_path = lambda _session, filename: filename
        server._publish_workspace_chunk = MagicMock()

        result = json.dumps(
            {
                "filename": "report.txt",
                "content": "aGVsbG8=",
                "offset": 0,
                "eof": True,
                "upload_id": "upload-1",
            }
        )
        with patch.object(server_rmm, "LOG_DIR", "/private/tmp/minimal-rmm-workspace-test"):
            server.handle_result(session.id, result, "file_upload")

        server._record_event.assert_called_once()
        payload = server._record_event.call_args.kwargs["workspace_payload"]
        self.assertEqual(payload["kind"], "file_upload")
        self.assertFalse(Path("/private/tmp/minimal-rmm-workspace-test/downloads/report.txt").exists())

    def test_workspace_event_broker_receives_full_event_payload(self):
        server = self.make_server()
        session = self.add_session(server)
        session.workspace_id = "alice"
        event = {"id": 1, "type": "output", "body": "hello", "workspace_payload": {"kind": "test"}}

        server.workspace_publish_event(session, event)
        result = server.workspace_events.read("alice", since=0)

        self.assertEqual(result["events"][0]["event"], event)

    def test_workspace_exfil_queues_job_assembled_by_workspace(self):
        server = self.make_server()
        session = self.add_session(server)
        session.workspace_id = "alice"
        session.agent_type = "go"
        session.capabilities = ["pe"]
        server.set_command = MagicMock()
        job = {
            "plugin_job": {
                "source": "C:/Temp/report.txt",
                "account": "RMM",
                "backend": "s3",
                "target": "report.txt",
                "settings": {"secret_access_key": "workspace-secret"},
                "make_link": False,
                "max_minutes": 30,
            },
            "max_bytes": 0,
        }
        with patch.object(
            server_rmm,
            "list_plugin_files",
            return_value=[{"name": "rclone-exfil.dll"}],
        ):
            command = server.queue_agent_exfil(
                session,
                "C:/Temp/report.txt",
                "unused-profile-name",
                mode="plugin",
                workspace_job=job,
            )

        self.assertTrue(command.startswith("__PE_LOAD__ rclone-exfil.dll Run "))
        self.assertIn("workspace-secret", command)
        server.set_command.assert_called_once_with(session.id, command, "oneshot")

    def test_workspace_exfil_rejects_central_profile_fallback(self):
        server = self.make_server()
        session = self.add_session(server)
        session.workspace_id = "alice"
        with self.assertRaises(RcloneConfigError):
            server.queue_agent_exfil(session, "C:/Temp/report.txt", "central-profile")


if __name__ == "__main__":
    unittest.main()
