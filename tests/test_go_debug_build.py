"""Verify debug artifacts are opt-in, separate, and excluded from normal deployment."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import rmm_tools
import server_rmm


class GoDebugBuildTests(unittest.TestCase):
    def test_debug_tag_artifact_and_launcher_are_separate_from_normal_build(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, "LOG_DIR", directory), \
                patch.object(server_rmm, "GO_AGENT_BUILD_DIR", os.path.join(directory, "builds")), \
                patch.object(server_rmm.shutil, "which", side_effect=lambda name: "go" if name == "go" else None):
            commands = []

            def compile_fixture(command, **kwargs):
                commands.append(command)
                Path(command[command.index("-o") + 1]).write_bytes(b"MZfixture")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            owner = SimpleNamespace(run=compile_fixture)
            results = []
            for debug in (False, True):
                result = server_rmm.build_go_agent("windows", "amd64", process_owner=owner,
                                                   stageless=True, debug=debug)
                results.append(result)
                command = commands[-1]
                self.assertEqual([arg for arg in command if arg.startswith("-tags=")],
                                 ["-tags=rmmdebug" if debug else "-tags="])
                self.assertIs(result["debug"], debug)
                expected = "minimal-rmm-agent-debug-windows-amd64.exe" if debug else "minimal-rmm-agent-windows-amd64.exe"
                self.assertEqual(result["binary_filename"], expected)
                with zipfile.ZipFile(result["path"]) as archive:
                    self.assertIn(expected, archive.namelist())
                    launcher = archive.read("run-agent.ps1").decode()
                    self.assertNotIn("RMM_DEBUG", launcher)
            normal, diagnostic = results
            self.assertNotEqual(normal["filename"], diagnostic["filename"])
            self.assertEqual(os.path.basename(server_rmm.latest_go_agent_binary()).split("-", 1)[1],
                             normal["binary_filename"])

    def test_staged_debug_and_nonboolean_options_fail_before_toolchain_lookup(self):
        with patch.object(server_rmm.shutil, "which") as which:
            with self.assertRaisesRegex(ValueError, "debug builds require stageless=true"):
                server_rmm.build_go_agent("windows", "amd64", debug=True)
            for invalid in ("false", "true", 0, 1, None):
                with self.subTest(debug=invalid), self.assertRaisesRegex(ValueError, "debug must be a boolean"):
                    server_rmm.build_go_agent("windows", "amd64", stageless=True, debug=invalid)
            which.assert_not_called()

    def test_rest_debug_is_strict_boolean_and_is_forwarded(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(beacon_public_url="", builds=Mock())
        with patch.object(server_rmm, "deployment_workspace_id", return_value=""), \
                patch.object(server_rmm, "build_go_agent", return_value={"debug": True}) as build:
            for value in ("false", 0, 1, None, {}):
                handler._handle_api_post("/api/v1/agent/go/build", {"debug": value})
                self.assertEqual(handler._json.call_args.args[0], 400)
                build.assert_not_called()
            handler._handle_api_post("/api/v1/agent/go/build", {
                "goos": "windows", "goarch": "amd64", "stageless": True, "debug": True,
            })
            self.assertEqual(handler._json.call_args.args, (200, {"debug": True}))
            self.assertIs(build.call_args.kwargs["debug"], True)
            self.assertIs(build.call_args.kwargs["stageless"], True)

    def test_build_tool_dispatch_preserves_explicit_debug(self):
        client = Mock()
        client.build_go_agent.return_value = (200, {"debug": True})
        result = rmm_tools.TOOL_HANDLERS["build_go_agent"](client, {"debug": True, "stageless": True})
        self.assertTrue(json.loads(result)["ok"])
        self.assertIs(client.build_go_agent.call_args.kwargs["debug"], True)
        self.assertIs(client.build_go_agent.call_args.kwargs["stageless"], True)
        client.build_go_agent.reset_mock()
        result = rmm_tools.TOOL_HANDLERS["build_go_agent"](client, {"debug": "false"})
        self.assertFalse(json.loads(result)["ok"])
        client.build_go_agent.assert_not_called()
