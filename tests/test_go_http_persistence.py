"""Verify persistence reaches embedded agents and generated launchers."""
import base64
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import server_rmm


class GoHTTPPersistenceTests(unittest.TestCase):
    def test_build_embeds_mode_and_launcher_overrides_stale_environment(self):
        for value, stageless in ((v, s) for v in (None, False, True) for s in (False, True)):
            with self.subTest(persistent_http=value, stageless=stageless), tempfile.TemporaryDirectory() as directory:
                commands = []

                def compile_fixture(command, **kwargs):
                    commands.append(command)
                    Path(command[command.index("-o") + 1]).write_bytes(b"MZfixture")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                config = {"base_url": "https://example.invalid"}
                if value is not None:
                    config["persistent_http"] = value
                with patch.object(server_rmm, "GO_AGENT_BUILD_DIR", directory), \
                        patch.object(server_rmm.shutil, "which", side_effect=lambda name: "go" if name == "go" else None):
                    result = server_rmm.build_go_agent(
                        "windows", "amd64", config, process_owner=SimpleNamespace(run=compile_fixture),
                        stageless=stageless, staging_fn=lambda _: "fixture-stage")
                expected = "1" if value else "0"
                encoded = base64.b64encode(expected.encode()).decode()
                self.assertIn(f"main.defaultPersistentHTTPB64={encoded}", " ".join(commands[-1]))
                with zipfile.ZipFile(result["path"]) as archive:
                    launcher = "run-agent.ps1" if stageless else "run-stage1.ps1"
                    self.assertIn(f"$env:RMM_PERSISTENT_HTTP = '{expected}'", archive.read(launcher).decode())

    def test_nonboolean_mode_is_rejected_before_compilation(self):
        for value in ("false", "true", 0, 1, None, {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "persistent_http must be a boolean"):
                server_rmm._build_go_agent_from_source(
                    ".", "go", "windows", "amd64", {"persistent_http": value}, None, True, None)
