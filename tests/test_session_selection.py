"""Session selection must respect API visibility and reject ambiguity."""
import json
import unittest
from unittest.mock import Mock

from rmm_cli import RmmApiClient
from rmm_tools import _resolve_session_id, tool_exec_command, tool_list_sessions


class SessionSelectionTests(unittest.TestCase):
    def client(self, sessions):
        client = RmmApiClient('http://localhost', extra_headers={
            'X-RMM-Workspace': 'workspace-a', 'X-RMM-Workspace-Token': 'test-token'})
        client.list_sessions = Mock(return_value=(200, {'sessions': sessions}))
        client.get_session = Mock()
        client.exec_command = Mock()
        return client

    def test_workspace_headers_without_bearer_can_list(self):
        client = self.client([{'id': 'alpha-1', 'hostname': 'HOST-A'}])
        result = json.loads(tool_list_sessions(client))
        self.assertTrue(result['ok'])
        self.assertEqual(result['sessions'][0]['id'], 'alpha-1')
        client.list_sessions.assert_called_once()

    def test_api_auth_failure_remains_failure(self):
        client = self.client([])
        client.list_sessions.return_value = (401, {'error': 'unauthorized'})
        result = json.loads(tool_list_sessions(client))
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 401)

    def test_duplicate_hostname_and_id_prefix_never_execute(self):
        client = self.client([{'id': 'abcd-1', 'hostname': 'HOST'}, {'id': 'abcd-2', 'hostname': 'HOST'}])
        for reference in ('host', 'abcd', 'hos'):
            with self.subTest(reference=reference):
                result = json.loads(tool_exec_command(client, reference, 'hostname'))
                self.assertEqual(result['error'], 'ambiguous_session_ref')
                self.assertEqual({row['id'] for row in result['candidates']}, {'abcd-1', 'abcd-2'})
        client.exec_command.assert_not_called()
        client.get_session.assert_not_called()

    def test_exact_id_and_hostname_precede_prefixes(self):
        client = self.client([{'id': 'abcd-1', 'hostname': 'HOST-1'}, {'id': 'abcd-2', 'hostname': 'HOST'}])
        self.assertEqual(_resolve_session_id(client, 'abcd-1')[0], 'abcd-1')
        self.assertEqual(_resolve_session_id(client, 'host')[0], 'abcd-2')
        self.assertEqual(_resolve_session_id(client, 'host-')[0], 'abcd-1')

    def test_invisible_session_is_not_queried_or_executed(self):
        client = self.client([{'id': 'visible', 'hostname': 'HOST-A'}])
        result = json.loads(tool_exec_command(client, 'other-workspace-id', 'hostname'))
        self.assertEqual(result['error'], 'session_not_found')
        client.get_session.assert_not_called()
        client.exec_command.assert_not_called()

    def test_cross_kind_prefix_collision_is_ambiguous(self):
        client = self.client([{'id': 'abc-1', 'hostname': 'HOST'}, {'id': 'other', 'hostname': 'ABC-HOST'}])
        self.assertEqual(_resolve_session_id(client, 'abc')[1]['error'], 'ambiguous_session_ref')


if __name__ == '__main__':
    unittest.main()
