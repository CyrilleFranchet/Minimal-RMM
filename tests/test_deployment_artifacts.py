"""Regression coverage for deployment artifact routing and build isolation."""
import hashlib
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm
from workspace_service import WorkspaceService


class DeploymentArtifactTests(unittest.TestCase):
    def test_workspace_routes_only_deployment_artifacts_upstream(self):
        service = object.__new__(WorkspaceService)
        service.store = Mock()
        service.store.artifact_path.return_value = None
        for kind in ("agent-deploy", "agent-builds"):
            self.assertIsNone(service.get_json(f"/api/v1/artifacts/{kind}/test.zip", {}))
        service.store.artifact_path.assert_not_called()
        self.assertEqual(service.get_json("/api/v1/artifacts/downloads/missing", {})[0], 404)
        service.store.artifact_path.assert_called_once_with("downloads", "missing")

    def test_central_workspace_access_preserves_local_artifact_boundary(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value="test-workspace")
        handler.server_instance = SimpleNamespace()
        handler._serve_artifact = Mock(return_value=True)
        handler._json = Mock()
        for kind in ("agent-deploy", "agent-builds"):
            handler._handle_api_get(f"/api/v1/artifacts/{kind}/test.zip", {})
            handler._serve_artifact.assert_called_with(kind, "test.zip", {})
        handler._serve_artifact.reset_mock()
        handler._handle_api_get("/api/v1/artifacts/downloads/private.txt", {})
        handler._serve_artifact.assert_not_called()
        self.assertEqual(handler._json.call_args.args[0], 404)

    def test_concurrent_builds_keep_distinct_binaries_and_archives(self):
        barrier = threading.Barrier(2)
        outputs = []

        def compile_stub(command, **kwargs):
            output = Path(command[command.index("-o") + 1])
            outputs.append(output)
            output.write_bytes(output.name.encode())
            barrier.wait(timeout=5)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server_rmm, "GO_AGENT_BUILD_DIR", directory), patch.object(server_rmm, "LOG_DIR", directory), patch.object(server_rmm.shutil, "which", return_value="go"):
                owner = Mock()
                owner.run.side_effect = compile_stub
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: server_rmm.build_go_agent("windows", "amd64", process_owner=owner), range(2)))
            self.assertEqual(len(set(outputs)), 2)
            self.assertNotEqual(results[0]["artifact_ref"], results[1]["artifact_ref"])
            binary_contents = []
            for result in results:
                data = Path(result["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), result["sha256"])
                with zipfile.ZipFile(result["path"]) as archive:
                    binary_contents.append(archive.read(result["binary_filename"]))
                    self.assertIn(result["binary_filename"], archive.read("run-agent.ps1").decode())
            self.assertEqual(set(binary_contents), {output.name.encode() for output in outputs})


if __name__ == "__main__":
    unittest.main()
