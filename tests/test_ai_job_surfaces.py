"""Read-only AI job CLI and shared tool contracts."""
import json
import unittest
from unittest.mock import Mock

from rmm_cli import RmmApiClient, build_parser
from rmm_tools import tool_get_ai_job, tool_list_ai_jobs


class AIJobSurfaceTests(unittest.TestCase):
    def test_client_encodes_identifiers(self):
        client = RmmApiClient("https://example.invalid")
        client.request = Mock(return_value=(200, {}))
        client.list_ai_jobs("session & other")
        client.request.assert_called_with("GET", "/api/v1/ai/jobs?session_id=session+%26+other")
        client.get_ai_job("job/other")
        client.request.assert_called_with("GET", "/api/v1/ai/jobs/job%2Fother")

    def test_cli_parses_job_inspection(self):
        parser = build_parser()
        args = parser.parse_args(["ai", "jobs", "--session-id", "sid"])
        self.assertEqual(args.session_id, "sid")
        args = parser.parse_args(["ai", "job", "job-id"])
        self.assertEqual(args.job_id, "job-id")

    def test_tools_preserve_scope_errors(self):
        client = Mock()
        client.list_ai_jobs.return_value = (200, {"jobs": []})
        self.assertTrue(json.loads(tool_list_ai_jobs(client))["ok"])
        client.get_ai_job.return_value = (404, {"error": "not_found"})
        result = json.loads(tool_get_ai_job(client, "hidden"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 404)
        client.get_ai_job.assert_called_once_with("hidden")


if __name__ == "__main__":
    unittest.main()
