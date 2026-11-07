"""Verify deployment inherits compiled mode and rejects obsolete runtime controls."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import rmm_tools
import server_rmm


class DebugDeploymentTests(unittest.TestCase):
    def _server(self):
        server = object.__new__(server_rmm.RMMServer)
        source = SimpleNamespace(id="source-session", agent_type="go", workspace_id="alice", agent_debug=False)
        server.resolve_session = Mock(return_value=source)
        server._new_deployment_id = Mock(return_value="target-session")
        server.exec_and_wait = Mock(return_value=({"body": ""}, "ok"))
        server.record_operator_action = Mock()
        server._verify_deployment = Mock(side_effect=lambda source, target, payload, *args: (payload, "ok"))
        server._lateral_cleanup = Mock(return_value={"status": "skipped"})
        server.create_staging = Mock(return_value="staging-fixture")
        server.staging_status = Mock(return_value={})
        server.log = Mock()
        return server

    def test_all_descendants_inherit_source_build_without_json_control(self):
        server = self._server()
        actions = [
            lambda: server.spawn_session("source-session"),
            lambda: server.spawnas("source-session", "unused", "LAB", "alice", "fixture"),
            lambda: server.lateral_move("source-session", "wmiexec", "target"),
            lambda: server.lateral_move("source-session", "winrm", "target"),
        ]
        for build_debug in (False, True):
            server.resolve_session.return_value.agent_debug = build_debug
            for action in actions:
                with self.subTest(build_debug=build_debug, action=action):
                    result, status = action()
                    self.assertEqual(status, "ok")
                    self.assertIs(result["debug"], build_debug)
                    command = server.exec_and_wait.call_args.args[1]
                    self.assertNotIn("debug", json.loads(command.split("\n", 1)[1]))
        server.create_staging.assert_not_called()

    def test_psexec_uses_exact_source_variant_without_normal_fallback(self):
        server = self._server()
        server.resolve_session.return_value.agent_debug = True
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, "GO_AGENT_BUILD_DIR", directory):
            normal = Path(directory, "id-minimal-rmm-agent-windows-amd64.exe")
            normal.write_bytes(b"MZnormal")
            self.assertEqual(server.lateral_move("source-session", "psexec", "target"),
                             (None, "debug_payload_unavailable"))
            server.create_staging.assert_not_called()
            server.exec_and_wait.assert_not_called()
            diagnostic = Path(directory, "id-minimal-rmm-agent-debug-windows-amd64.exe")
            diagnostic.write_bytes(b"MZdebug")
            for build_debug, expected in ((False, b"MZnormal"), (True, b"MZdebug")):
                server.resolve_session.return_value.agent_debug = build_debug
                result, status = server.lateral_move("source-session", "psexec", "target")
                self.assertEqual(status, "ok")
                self.assertIs(result["debug"], build_debug)
                self.assertEqual(server.create_staging.call_args.args, (expected,))
                command = server.exec_and_wait.call_args.args[1]
                self.assertNotIn("debug", json.loads(command.split("\n", 1)[1]))
            normal.unlink()
            self.assertIsNone(server_rmm.latest_go_agent_binary())
            self.assertEqual(server_rmm.latest_go_agent_binary(debug=True), str(diagnostic))

    def test_unreadable_debug_payload_has_no_normal_fallback(self):
        server = self._server()
        server.resolve_session.return_value.agent_debug = True
        with patch.object(server_rmm, "latest_go_agent_binary", return_value="missing-debug.exe") as latest:
            self.assertEqual(server.lateral_move("source-session", "psexec", "target"),
                             (None, "debug_payload_unavailable"))
            latest.assert_called_once_with(debug=True)
            server.exec_and_wait.assert_not_called()
            server.create_staging.assert_not_called()

    def test_rest_rejects_every_obsolete_debug_value_before_deployment(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value="alice")
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(
            spawn_session=Mock(return_value=({"ok": True}, "ok")),
            spawnas=Mock(return_value=({"ok": True}, "ok")),
            lateral_move=Mock(return_value=({"ok": True}, "ok")))
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="source-session")), \
                patch.object(server_rmm, "plugin_directory", return_value="unused"):
            for name, action in (("spawn", handler.server_instance.spawn_session),
                                 ("spawnas", handler.server_instance.spawnas),
                                 ("lateral", handler.server_instance.lateral_move)):
                route = f"/api/v1/sessions/source-session/{name}"
                options = {"method": "psexec", "host": "target", "user": "alice"}
                for value in (False, True, "false", "true", 0, 1, None, []):
                    with self.subTest(route=route, debug=value):
                        action.reset_mock()
                        handler._handle_api_post(route, {**options, "debug": value})
                        self.assertEqual(handler._json.call_args.args[0], 400)
                        self.assertEqual(handler._json.call_args.args[1]["error"], "debug_is_build_time_only")
                        action.assert_not_called()
                handler._handle_api_post(route, options)
                self.assertEqual(handler._json.call_args.args[0], 200)
                self.assertNotIn("debug", action.call_args.kwargs)

    def test_tool_dispatch_rejects_obsolete_debug_before_any_client_action(self):
        client = Mock()
        client.list_sessions.return_value = (200, {"sessions": [{"id": "source-session"}]})
        for name in ("spawn_session", "spawnas", "lateral_move"):
            action = getattr(client, name)
            action.return_value = (200, {"ok": True})
            options = {"session_ref": "source-session", "cred_id": "cred-fixture", "method": "psexec", "host": "target"}
            for value in (False, True, None, "false"):
                client.reset_mock()
                result = json.loads(rmm_tools.execute_tool(client, name, {**options, "debug": value}))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "debug_is_build_time_only")
                self.assertEqual(client.mock_calls, [])
            result = json.loads(rmm_tools.execute_tool(client, name, options))
            self.assertTrue(result["ok"])
            self.assertNotIn("debug", action.call_args.kwargs)
