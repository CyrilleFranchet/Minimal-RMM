"""Verify spawn and lateral debug selection never falls back to normal payloads."""
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
        source = SimpleNamespace(id="source-session", agent_type="go", workspace_id="alice")
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

    def test_spawn_and_non_psexec_commands_carry_debug(self):
        server = self._server()
        for debug in (False, True):
            with self.subTest(debug=debug):
                server.spawn_session("source-session", debug=debug)
                command = server.exec_and_wait.call_args.args[1]
                self.assertEqual(command.split("\n", 1)[0], "__SPAWN__")
                self.assertIs(json.loads(command.split("\n", 1)[1])["debug"], debug)
                for method in ("wmiexec", "winrm"):
                    server.lateral_move("source-session", method, "target", debug=debug)
                    command = server.exec_and_wait.call_args.args[1]
                    payload = json.loads(command.split("\n", 1)[1])
                    self.assertIs(payload["debug"], debug)
                    self.assertEqual(payload["method"], method)
                    self.assertEqual(payload["stage"], "")
        server.create_staging.assert_not_called()

    def test_psexec_stages_exact_variant_and_refuses_missing_debug_payload(self):
        server = self._server()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, "GO_AGENT_BUILD_DIR", directory):
            normal = Path(directory, "normal-minimal-rmm-agent-windows-amd64.exe")
            normal.write_bytes(b"MZnormal")
            self.assertEqual(server.lateral_move("source-session", "psexec", "target", debug=True),
                             (None, "debug_payload_unavailable"))
            server.exec_and_wait.assert_not_called()
            server.create_staging.assert_not_called()
            diagnostic = Path(directory, "debug-minimal-rmm-agent-debug-windows-amd64.exe")
            diagnostic.write_bytes(b"MZdebug")
            for debug, expected in ((False, b"MZnormal"), (True, b"MZdebug")):
                with self.subTest(debug=debug):
                    server.lateral_move("source-session", "psexec", "target", debug=debug)
                    self.assertEqual(server.create_staging.call_args.args, (expected,))
                    command = server.exec_and_wait.call_args.args[1]
                    self.assertIs(json.loads(command.split("\n", 1)[1])["debug"], debug)
            normal.unlink()
            self.assertIsNone(server_rmm.latest_go_agent_binary())
            self.assertEqual(server_rmm.latest_go_agent_binary(debug=True), str(diagnostic))

    def test_unreadable_debug_payload_has_no_normal_fallback(self):
        server = self._server()
        with patch.object(server_rmm, "latest_go_agent_binary", return_value="missing-debug.exe") as latest:
            self.assertEqual(server.lateral_move("source-session", "psexec", "target", debug=True),
                             (None, "debug_payload_unavailable"))
            latest.assert_called_once_with(debug=True)
            server.exec_and_wait.assert_not_called()
            server.create_staging.assert_not_called()

    def test_rest_routes_validate_and_forward_boolean_debug(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(spawn_session=Mock(return_value=({"ok": True}, "ok")),
                                                 lateral_move=Mock(return_value=({"ok": True}, "ok")))
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="source-session")):
            for name, action in (("spawn", handler.server_instance.spawn_session),
                                 ("lateral", handler.server_instance.lateral_move)):
                route = f"/api/v1/sessions/source-session/{name}"
                for invalid in ("false", "true", 0, 1, None, []):
                    with self.subTest(route=route, debug=invalid):
                        action.reset_mock()
                        handler._handle_api_post(route, {"method": "psexec", "host": "target", "debug": invalid})
                        self.assertEqual(handler._json.call_args.args[0], 400)
                        action.assert_not_called()
                for debug in (False, True):
                    handler._handle_api_post(route, {"method": "psexec", "host": "target", "debug": debug})
                    self.assertEqual(handler._json.call_args.args[0], 200)
                    self.assertIs(action.call_args.kwargs["debug"], debug)

    def test_tool_dispatch_preserves_boolean_debug(self):
        client = Mock()
        client.list_sessions.return_value = (200, {"sessions": [{"id": "source-session"}]})
        for name in ("spawn_session", "lateral_move"):
            action = getattr(client, name)
            action.return_value = (200, {"ok": True})
            options = {"session_ref": "source-session", "method": "psexec", "host": "target"}
            for debug in (False, True):
                result = rmm_tools.TOOL_HANDLERS[name](client, {**options, "debug": debug})
                self.assertTrue(json.loads(result)["ok"])
                self.assertIs(action.call_args.kwargs["debug"], debug)
            action.reset_mock()
            result = rmm_tools.TOOL_HANDLERS[name](client, {**options, "debug": "false"})
            self.assertFalse(json.loads(result)["ok"])
            action.assert_not_called()
