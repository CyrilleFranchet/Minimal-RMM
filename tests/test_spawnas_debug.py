"""Verify spawnas inherits build mode without accepting per-command controls."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import rmm_tools
import server_rmm


class SpawnasDebugTests(unittest.TestCase):
    def test_server_inherits_build_without_recording_credentials(self):
        server = object.__new__(server_rmm.RMMServer)
        source = SimpleNamespace(id="source-session", agent_type="go", workspace_id="alice")
        server.resolve_session = Mock(return_value=source)
        server._new_deployment_id = Mock(return_value="target-session")
        server.exec_and_wait = Mock(return_value=({"body": ""}, "ok"))
        server.record_operator_action = Mock()
        server._verify_deployment = Mock(side_effect=lambda source, target, payload, *args: (payload, "ok"))
        for enabled in (False, True):
            with self.subTest(build_debug=enabled):
                source.agent_debug = enabled
                result, _ = server.spawnas(source.id, "unused", "LAB", "alice", "fixture-secret")
                self.assertIs(result["debug"], enabled)
                command = server.exec_and_wait.call_args.args[1]
                self.assertEqual(command.split("\n", 1)[0], "__SPAWNAS__")
                payload = json.loads(command.split("\n", 1)[1])
                self.assertNotIn("debug", payload)
                self.assertEqual(payload["password"], "fixture-secret")
                self.assertEqual(payload["session_id"], "target-session")
                self.assertNotIn("fixture-secret", str(server.record_operator_action.call_args))

    def test_deployment_tool_schemas_have_no_debug_control(self):
        for name in ("spawn_session", "spawnas", "lateral_move"):
            schema = next(tool["function"] for tool in rmm_tools.OPENAI_TOOLS
                          if tool["function"]["name"] == name)
            self.assertNotIn("debug", schema["parameters"]["properties"])
        build = next(tool["function"] for tool in rmm_tools.OPENAI_TOOLS
                     if tool["function"]["name"] == "build_go_agent")
        self.assertEqual(build["parameters"]["properties"]["debug"]["type"], "boolean")
