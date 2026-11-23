"""Verify staged spawnas inherits build mode without accepting per-command controls."""
import hashlib
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import rmm_tools
import server_rmm


class SpawnasDebugTests(unittest.TestCase):
    def setUp(self):
        self.server = object.__new__(server_rmm.RMMServer)
        self.source = SimpleNamespace(id="source-session", agent_type="go", workspace_id="alice")
        self.server.resolve_session = Mock(return_value=self.source)
        self.server._new_deployment_id = Mock(return_value="target-session")
        self.server.exec_and_wait = Mock(return_value=({"body": ""}, "ok"))
        self.server.record_operator_action = Mock()
        self.server._verify_deployment = Mock(side_effect=lambda source, target, payload, *args: (payload, "ok"))
        self.server.staging_lock = __import__("threading").Lock()
        self.server.staging_entries = {}
        agent_binary = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
        agent_binary.write(b"MZfake-agent-payload-for-spawnas")
        agent_binary.close()
        self.agent_binary_path = agent_binary.name
        self.addCleanup(os.unlink, self.agent_binary_path)
        patcher = patch.object(server_rmm, "latest_go_agent_binary", return_value=self.agent_binary_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_server_inherits_build_and_retains_credentials(self):
        for enabled in (False, True):
            with self.subTest(build_debug=enabled):
                self.source.agent_debug = enabled
                result, _ = self.server.spawnas(self.source.id, "unused", "LAB", "alice", "fixture-secret")
                self.assertIs(result["debug"], enabled)
                command = self.server.exec_and_wait.call_args.args[1]
                self.assertEqual(command.split("\n", 1)[0], "__SPAWNAS_STAGED__")
                payload = json.loads(command.split("\n", 1)[1])
                self.assertNotIn("debug", payload)
                self.assertEqual(payload["password"], "fixture-secret")
                self.assertEqual(payload["session_id"], "target-session")
                self.assertEqual(payload["workspace_id"], "alice")
                self.assertIn("fixture-secret", str(self.server.record_operator_action.call_args))

    def test_spawnas_deploys_the_workspace_staged_artifact(self):
        captured = []
        original = self.server.create_staging
        with patch.object(self.server, "create_staging", side_effect=lambda artifact: (
            captured.append(artifact), "stage-fixture-0001")[1]) as stage:
            result, status = self.server.spawnas(self.source.id, "unused", "", "LAB\\alice", "pw")
        self.assertEqual(status, "ok")
        stage.assert_called_once()
        artifact = captured[0]
        self.assertEqual(artifact, b"MZfake-agent-payload-for-spawnas")
        command = self.server.exec_and_wait.call_args.args[1]
        payload = json.loads(command.split("\n", 1)[1])
        self.assertEqual(payload["stage"], "stage-fixture-0001")
        self.assertEqual(payload["payload_sha256"],
                         hashlib.sha256(b"MZfake-agent-payload-for-spawnas").hexdigest())
        self.assertEqual(payload["domain"], "LAB")
        self.assertEqual(payload["user"], "alice")
        self.assertEqual(round(payload["timeout_seconds"]), 60,
                         "the agent worker budget reserves beacon delivery time")
        self.assertIn("stage_status", result)

    def test_spawnas_requires_the_workspace_artifact(self):
        with patch.object(server_rmm, "latest_go_agent_binary", return_value=None):
            self.assertEqual(self.server.spawnas(self.source.id, "unused", "LAB", "alice", "pw"),
                             (None, "agent_payload_unavailable"))
        self.source.agent_debug = True
        with patch.object(server_rmm, "latest_go_agent_binary", return_value=None):
            self.assertEqual(self.server.spawnas(self.source.id, "unused", "LAB", "alice", "pw"),
                             (None, "debug_payload_unavailable"))
        self.server.exec_and_wait.assert_not_called()

    def test_deployment_tool_schemas_have_no_debug_control(self):
        for name in ("spawn_session", "spawnas", "lateral_move"):
            schema = next(tool["function"] for tool in rmm_tools.OPENAI_TOOLS
                          if tool["function"]["name"] == name)
            self.assertNotIn("debug", schema["parameters"]["properties"])
        build = next(tool["function"] for tool in rmm_tools.OPENAI_TOOLS
                     if tool["function"]["name"] == "build_go_agent")
        self.assertEqual(build["parameters"]["properties"]["debug"]["type"], "boolean")
