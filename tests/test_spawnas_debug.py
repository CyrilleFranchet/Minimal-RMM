"""Verify opt-in spawnas diagnostics survive API and tool dispatch."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import rmm_tools
import server_rmm


class SpawnasDebugTests(unittest.TestCase):
    def test_server_passes_debug_to_agent_without_recording_credentials(self):
        server = object.__new__(server_rmm.RMMServer)
        source = SimpleNamespace(id="source-session", agent_type="go", workspace_id="alice")
        server.resolve_session = Mock(return_value=source)
        server._new_deployment_id = Mock(return_value="target-session")
        server.exec_and_wait = Mock(return_value=({"body": ""}, "ok"))
        server.record_operator_action = Mock()
        server._verify_deployment = Mock(return_value=({"ok": True}, "ok"))
        for enabled in (False, True):
            with self.subTest(debug=enabled):
                server.spawnas(source.id, "unused", "LAB", "alice", "fixture-secret", debug=enabled)
                command = server.exec_and_wait.call_args.args[1]
                self.assertEqual(command.split("\n", 1)[0], "__SPAWNAS__")
                payload = json.loads(command.split("\n", 1)[1])
                self.assertIs(payload["debug"], enabled)
                self.assertEqual(payload["password"], "fixture-secret")
                self.assertEqual(payload["session_id"], "target-session")
                self.assertNotIn("fixture-secret", str(server.record_operator_action.call_args))

    def test_rest_validates_boolean_and_defaults_debug_off(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value="alice")
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(spawnas=Mock(return_value=({"ok": True}, "ok")))
        route = "/api/v1/sessions/source-session/spawnas"
        session = SimpleNamespace(id="source-session")
        with patch.object(server_rmm, "_resolve_api_session", return_value=session), \
                patch.object(server_rmm, "plugin_directory", return_value="unused"):
            for options, expected in (({}, False), ({"debug": False}, False), ({"debug": True}, True)):
                with self.subTest(options=options):
                    handler._handle_api_post(route, {"user": "alice", **options})
                    self.assertEqual(handler._json.call_args.args[0], 200)
                    self.assertIs(handler.server_instance.spawnas.call_args.kwargs["debug"], expected)
            for invalid in ("true", "false", 1, 0, None, [], {}):
                with self.subTest(invalid=invalid):
                    handler.server_instance.spawnas.reset_mock()
                    handler._handle_api_post(route, {"user": "alice", "debug": invalid})
                    self.assertEqual(handler._json.call_args.args,
                                     (400, {"error": "invalid_spawnas_options"}))
                    handler.server_instance.spawnas.assert_not_called()

    def test_tool_dispatch_preserves_debug_and_rejects_string_values(self):
        client = Mock()
        client.list_sessions.return_value = (200, {"sessions": [{"id": "source-session"}]})
        client.spawnas.return_value = (200, {"ok": True})
        for enabled in (False, True):
            with self.subTest(debug=enabled):
                result = rmm_tools.TOOL_HANDLERS["spawnas"](client, {
                    "session_ref": "source-session", "cred_id": "cred-fixture", "debug": enabled,
                })
                self.assertTrue(json.loads(result)["ok"])
                self.assertIs(client.spawnas.call_args.kwargs["debug"], enabled)
        client.spawnas.reset_mock()
        result = rmm_tools.TOOL_HANDLERS["spawnas"](client, {
            "session_ref": "source-session", "cred_id": "cred-fixture", "debug": "false",
        })
        self.assertEqual(json.loads(result), {"ok": False, "error": "debug must be a boolean"})
        client.spawnas.assert_not_called()

    def test_tool_schema_documents_opt_in_debug(self):
        schema = next(tool["function"] for tool in rmm_tools.OPENAI_TOOLS
                      if tool["function"]["name"] == "spawnas")
        option = schema["parameters"]["properties"]["debug"]
        self.assertEqual(option["type"], "boolean")
        self.assertIs(option["default"], False)
        self.assertNotIn("debug", schema["parameters"]["required"])
