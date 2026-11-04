"""Deployment/development Exegol MCP diagnostic: no credentials or raw exec output leak."""
import asyncio
import re
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import check_exegol_mcp as diag  # noqa: E402

TOOLS = [
    {'function': {'name': 'list_exegol_containers', 'parameters': {'required': []}}},
    {'function': {'name': 'execute_command_in_container', 'parameters': {
        'properties': {'container_name': {'type': 'string'}, 'command': {'type': 'string'}},
        'required': []}}},
]


class FakeSession:
    openai_tools = TOOLS

    def __init__(self, containers='[{"name":"exegol-lab-1","status":"running"}]', match=True):
        self.containers = containers
        self.match = match

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def call_tool(self, name, arguments):
        if name == diag.LIST_TOOL:
            return self.containers
        if not self.match:
            return 'RMM_CHECK_ID=wrong\n'
        nonce = re.search(r"'([0-9a-f]{48})'", arguments['command']).group(1)
        return f"RMM_CHECK_ID={nonce}:{socket.gethostname()}\n"


class Args:
    def __init__(self, container=None, no_probe=False):
        self.container = container
        self.no_probe = no_probe


class CheckExegolMcpTests(unittest.TestCase):
    def setUp(self):
        self._url_patch = patch('check_exegol_mcp.getenv', side_effect=self._getenv)
        self._url_patch.start()
        self.addCleanup(self._url_patch.stop)

    @staticmethod
    def _getenv(key, default=''):
        return {'RMM_EXEGOL_MCP_URL': 'http://127.0.0.1:8000/mcp'}.get(key, default)

    def test_connected_single_container_confirms_identity(self):
        with patch('check_exegol_mcp.McpHttpSession', return_value=FakeSession()):
            report = asyncio.run(diag.run(Args()))
        self.assertTrue(report['ok'])
        self.assertTrue(report['mcp_connected'])
        self.assertEqual(report['running_containers'], ['exegol-lab-1'])
        self.assertEqual(report['probed_container'], 'exegol-lab-1')
        self.assertTrue(report['identity_confirmed'])

    def test_mismatched_identity_fails_without_crashing(self):
        with patch('check_exegol_mcp.McpHttpSession', return_value=FakeSession(match=False)):
            report = asyncio.run(diag.run(Args()))
        self.assertFalse(report['ok'])
        self.assertFalse(report['identity_confirmed'])
        self.assertIn('probe_output', report)

    def test_ambiguous_containers_skip_probe_without_guessing(self):
        containers = '[{"name":"exegol-a","status":"running"},{"name":"exegol-b","status":"running"}]'
        with patch('check_exegol_mcp.McpHttpSession', return_value=FakeSession(containers=containers)):
            report = asyncio.run(diag.run(Args()))
        self.assertNotIn('probed_container', report)
        self.assertIn('note', report)
        self.assertEqual(report['running_containers'], ['exegol-a', 'exegol-b'])

    def test_no_probe_flag_lists_without_executing_anything(self):
        with patch('check_exegol_mcp.McpHttpSession', return_value=FakeSession()):
            report = asyncio.run(diag.run(Args(no_probe=True)))
        self.assertTrue(report['ok'])
        self.assertNotIn('probed_container', report)

    def test_connection_failure_is_classified_without_raising(self):
        class BrokenSession(FakeSession):
            async def __aenter__(self):
                raise ConnectionError('refused')
        with patch('check_exegol_mcp.McpHttpSession', return_value=BrokenSession()):
            report = asyncio.run(diag.run(Args()))
        self.assertFalse(report['ok'])
        self.assertEqual(report['error'], 'exegol_mcp_connection_failed')

    def test_invalid_container_name_is_rejected_before_any_connection(self):
        with self.assertRaises(SystemExit):
            diag.validated_container('bad name; rm -rf /')


if __name__ == '__main__':
    unittest.main()
