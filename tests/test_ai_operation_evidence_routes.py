"""Exercise authenticated AI evidence paging and workspace forwarding."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import server_rmm
from workspace_service import WorkspaceService


class OperationEvidenceRouteTests(unittest.TestCase):
    """Keep evidence reads owner scoped and bounded at the HTTP boundary."""

    def handler(self, evidence):
        """Create an already-authenticated handler with a controlled store."""
        handler = object.__new__(server_rmm.RMMHandler)
        handler._workspace_scope = Mock(return_value="workspace-a")
        handler._api_authorized = Mock(return_value=True)
        manager = Mock()
        manager.read_evidence.return_value = evidence
        handler.server_instance = SimpleNamespace(ai_jobs=lambda: manager)
        handler._json = Mock()
        return handler, manager

    def test_read_uses_authenticated_owner_and_page_bounds(self):
        handler, manager = self.handler({"kind": "tool_result", "content": "returned", "characters": 8})
        handler._handle_api_get("/api/v1/ai/jobs/job-a/evidence/context-a", {"offset": ["2"], "limit": ["4"]})
        manager.read_evidence.assert_called_once_with("job-a", "workspace:workspace-a", "context-a", 2, 4)
        self.assertEqual(handler._json.call_args.args[0], 200)

    def test_invalid_pages_do_not_read_store(self):
        for query in ({"offset": ["-1"]}, {"limit": ["4097"]}, {"offset": ["bad"]}, {"limit": ["0"]}):
            with self.subTest(query=query):
                handler, manager = self.handler(None)
                handler._handle_api_get("/api/v1/ai/jobs/job-a/evidence/context-a", query)
                manager.read_evidence.assert_not_called()
                self.assertEqual(handler._json.call_args.args[0], 400)

    def test_missing_or_foreign_evidence_is_not_found(self):
        handler, _ = self.handler(None)
        handler._handle_api_get("/api/v1/ai/jobs/job-a/evidence/context-a", {})
        self.assertEqual(handler._json.call_args.args, (404, {"error": "evidence_not_found"}))

    def test_unauthenticated_read_is_rejected_before_store_access(self):
        handler, manager = self.handler(None)
        handler._api_authorized.return_value = False
        handler._handle_api_get("/api/v1/ai/jobs/job-a/evidence/context-a", {})
        manager.read_evidence.assert_not_called()
        self.assertEqual(handler._json.call_args.args[0], 401)

    def test_workspace_forwards_evidence_read_and_query(self):
        service = object.__new__(WorkspaceService)
        service._proxy = Mock(return_value=(200, {"evidence": {"content": "output"}}, "application/json"))
        path = "/api/v1/ai/jobs/job-a/evidence/context-a"
        self.assertEqual(service.get_json(path, {"offset": ["4096"], "limit": ["4096"]})[0], 200)
        self.assertIn("offset=4096", service._proxy.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
