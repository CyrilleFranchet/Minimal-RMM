"""Unit coverage for central workspace ownership and result forwarding."""

from collections import deque
import contextlib
import io
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

    def test_workspace_credentials_cannot_join_administrator_websocket_feed(self):
        handler = SimpleNamespace(
            headers={"Upgrade": "websocket"},
            _api_authorized=lambda _qs: True,
            _workspace_scope=lambda: "alice",
            _respond=MagicMock(),
            server_instance=SimpleNamespace(event_hub=MagicMock(), sessions_to_json=MagicMock()),
        )
        with patch('server_rmm.WebSocketConnection.from_http_request') as handshake:
            self.assertTrue(RMMHandler._handle_websocket_upgrade(handler, {}))
        self.assertEqual(handler._respond.call_args.args[0], 403)
        handshake.assert_not_called()
        handler.server_instance.event_hub.add.assert_not_called()
        handler.server_instance.sessions_to_json.assert_not_called()

    def test_direct_workspace_storage_requests_do_not_touch_central_state(self):
        for method, path in (
            ('GET', '/assets'), ('GET', '/assets/rclone'),
            ('POST', '/assets/skills'), ('POST', '/ai/skills'), ('POST', '/rclone/profiles'),
            ('PATCH', '/assets/rclone/profile.json'), ('DELETE', '/assets/skills/a.md'),
            ('DELETE', '/history'), ('DELETE', '/history/bob-session'),
        ):
            with self.subTest(method=method, path=path), patch('server_rmm.asset_store') as store:
                server = SimpleNamespace(clear_session_history=MagicMock(), delete_history_session=MagicMock())
                handler = SimpleNamespace(
                    headers={}, _api_authorized=lambda *_args: True,
                    _api_path_parts=lambda value: value.removeprefix('/api/v1/').split('/'),
                    _workspace_scope=lambda: 'alice', _json=MagicMock(), server_instance=server,
                )
                function = getattr(RMMHandler, '_handle_api_' + method.lower())
                if method == 'DELETE':
                    function(handler, '/api/v1' + path)
                else:
                    function(handler, '/api/v1' + path, {})
                self.assertEqual(handler._json.call_args.args[0], 409)
                self.assertEqual(handler._json.call_args.args[1]['error'], 'storage_is_workspace_local')
                store.assert_not_called()
                server.clear_session_history.assert_not_called()
                server.delete_history_session.assert_not_called()

    def test_storage_mode_defaults_to_workspace_and_rejects_legacy(self):
        with patch('server_rmm.settings_getenv', side_effect=lambda key, default='': default):
            self.assertEqual(server_rmm.configured_storage_mode(), 'workspace')
        for value in ('server', 'typo'):
            with patch('server_rmm.settings_getenv', return_value=value), self.assertRaisesRegex(ValueError, 'rmm_deployment.py'):
                server_rmm.configured_storage_mode()

    def test_embedded_central_cli_rejected_before_runtime_creation(self):
        with patch('sys.argv', ['server_rmm.py', '--cli']), \
                patch('server_rmm.RMMServer') as runtime, \
                contextlib.redirect_stderr(io.StringIO()) as output, \
                self.assertRaises(SystemExit) as stopped:
            server_rmm.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('workspace service', output.getvalue())
        runtime.assert_not_called()

    def test_registration_assigns_only_sole_workspace_and_preserves_existing_owner(self):
        for registry, persisted, expected in (
                ({'alice': 'secret'}, {}, 'alice'),
                ({'alice': 'secret', 'bob': 'other'}, {}, None),
                ({'alice': 'secret'}, {'workspace_id': 'bob'}, 'bob'),
                ({}, {}, None)):
            with self.subTest(registry=list(registry), persisted=persisted):
                server = self.make_server()
                server.killed_sessions = set()
                server.workspace_registry = WorkspaceRegistry(registry)
                server._persisted_session_records = {'new-session': persisted}
                server._persisted_config_from_history_meta = MagicMock(return_value=None)
                server.backfill_download_artifacts = MagicMock()
                server._apply_history_meta_to_session = MagicMock()
                server._rehydrate_session_events_from_history = MagicMock(return_value=0)
                server._history_write_meta = MagicMock()
                server.save_session = MagicMock()
                self.assertTrue(server.register_session('new-session', 'host', 'user'))
                self.assertEqual(server.sessions['new-session'].workspace_id, expected)
                self.assertEqual(server.save_session.call_args.args[0].workspace_id, expected)
                server.workspace_registry = WorkspaceRegistry({'different': 'secret'})
                self.assertFalse(server.register_session('new-session', 'host', 'user'))
                self.assertEqual(server.sessions['new-session'].workspace_id, expected)

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

    def test_workspace_session_prefix_ambiguity_considers_only_visible_sessions(self):
        server = self.make_server()
        alice = self.add_session(server, 'abcd-alice')
        alice.workspace_id = 'alice'
        bob = self.add_session(server, 'abcd-bob')
        bob.workspace_id = 'bob'
        self.assertIs(server.resolve_workspace_session('alice', 'abcd'), alice)
        self.assertIs(server.resolve_workspace_session('bob', 'abcd'), bob)
        self.assertIsNone(server.resolve_workspace_session('alice', bob.id))
        self.assertIsNone(server.resolve_workspace_session('alice', 'abc'))
        other = self.add_session(server, 'abcd-alice-two')
        other.workspace_id = 'alice'
        self.assertIsNone(server.resolve_workspace_session('alice', 'abcd'))

    def test_event_and_chat_routes_reuse_the_scoped_canonical_session_id(self):
        server = self.make_server()
        alice = self.add_session(server, 'abcd-alice')
        alice.workspace_id = 'alice'
        bob = self.add_session(server, 'abcd-bob')
        bob.workspace_id = 'bob'
        server.get_events = MagicMock(return_value=[])
        server.get_ai_chat = MagicMock(return_value=(alice.id, []))
        server.clear_ai_chat = MagicMock()
        server.ai_jobs = MagicMock()
        handler = SimpleNamespace(
            headers={}, _api_authorized=lambda *_args: True, _workspace_scope=lambda: 'alice',
            _api_path_parts=lambda path: path.removeprefix('/api/v1/').split('/'),
            _json=MagicMock(), server_instance=server)
        handler._api_session = lambda ref: server.resolve_workspace_session('alice', ref)
        RMMHandler._handle_api_get(handler, '/api/v1/sessions/abcd/events', {})
        server.get_events.assert_called_once_with(alice.id, since_id=0, limit=50)
        RMMHandler._handle_api_get(handler, '/api/v1/sessions/abcd/ai/chat', {})
        server.get_ai_chat.assert_called_with(alice.id)
        RMMHandler._handle_api_delete(handler, '/api/v1/sessions/abcd/ai/chat')
        server.get_ai_chat.assert_called_with(alice.id)
        server.clear_ai_chat.assert_called_once_with(alice.id)

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
