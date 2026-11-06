"""Exercise credential API routing with real path parsing and an isolated store."""
import tempfile
import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm
import rmm_creds


class CredentialRoutingTests(unittest.TestCase):
    def test_store_preserves_passwords_and_isolates_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = os.path.join(directory, "a"), os.path.join(directory, "b")
            password = " spaces ' quotes \\ and unicode é "
            result = rmm_creds.creds_add(first, "", "LAB\\alice", password)
            self.assertTrue(result["ok"])
            self.assertRegex(result["cred_id"], r"^cred-[0-9a-f]{8}$")
            self.assertEqual(rmm_creds.creds_resolve(first, result["cred_id"])["password"], password)
            self.assertIsNone(rmm_creds.creds_resolve(second, result["cred_id"]))
            self.assertEqual(rmm_creds.creds_del(second, result["cred_id"])["error"], "cred_not_found")
            self.assertEqual(os.stat(os.path.join(first, "creds.json")).st_mode & 0o777, 0o600)
            self.assertNotIn(password, str(rmm_creds.creds_list(first)))
            self.assertTrue(rmm_creds.creds_del(first, result["cred_id"])["ok"])
            self.assertIsNone(rmm_creds.creds_resolve(first, result["cred_id"]))

    def test_corrupt_store_is_preserved_and_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "creds.json"
            for content in ('{broken', '[]', '{"cred-bad": {"password": "secret"}}'):
                path.write_text(content)
                for operation in (
                    lambda: rmm_creds.creds_add(directory, "", "alice", "pw"),
                    lambda: rmm_creds.creds_del(directory, "cred-bad"),
                    lambda: rmm_creds.creds_list(directory),
                ):
                    self.assertEqual(operation(), {"ok": False, "error": "credential_store_unavailable"})
                    self.assertEqual(path.read_text(), content)

    def test_add_list_delete_round_trip(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value="test-workspace")
        handler._json = Mock()
        handler.server_instance = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, "plugin_directory", return_value=directory):
            self.assertTrue(handler._handle_api_post("/api/v1/creds", {
                "domain": "LAB", "user": "alice", "password": "fixture-password"}))
            status, result = handler._json.call_args.args
            self.assertEqual(status, 200)
            cred_id = result["cred_id"]
            self.assertNotIn("fixture-password", str(result))
            self.assertTrue(handler._handle_api_get("/api/v1/creds", {}))
            status, result = handler._json.call_args.args
            self.assertEqual(status, 200)
            self.assertEqual(len(result["creds"]), 1)
            self.assertEqual(result["creds"][0]["label"], "LAB\\alice")
            self.assertNotIn("fixture-password", str(result))
            self.assertTrue(handler._handle_api_delete(f"/api/v1/creds/{cred_id}"))
            self.assertEqual(handler._json.call_args.args[0], 200)
            handler._handle_api_get("/api/v1/creds", {})
            self.assertEqual(handler._json.call_args.args[1]["creds"], [])

    def test_add_requires_workspace_authentication(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace()
        with patch.object(server_rmm._rmm_creds, "creds_add") as add:
            self.assertTrue(handler._handle_api_post("/api/v1/creds", {"user": "alice"}))
        self.assertEqual(handler._json.call_args.args[0], 403)
        add.assert_not_called()

    def test_all_routes_reject_missing_workspace_and_report_storage_failure(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace()
        operations = [
            lambda: handler._handle_api_post("/api/v1/creds", {"user": "alice"}),
            lambda: handler._handle_api_get("/api/v1/creds", {}),
            lambda: handler._handle_api_delete("/api/v1/creds/cred-123"),
        ]
        for operation in operations:
            operation()
            self.assertEqual(handler._json.call_args.args[0], 403)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, "plugin_directory", return_value=directory):
            (Path(directory) / "creds.json").write_text('{broken')
            handler._workspace_scope.return_value = "alice"
            for operation in operations:
                operation()
                self.assertEqual(handler._json.call_args.args,
                                 (503, {"ok": False, "error": "credential_store_unavailable"}))
