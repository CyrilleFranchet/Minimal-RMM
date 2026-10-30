"""Operator surfaces preserve destination identity and event pagination."""
import json
import unittest
from unittest.mock import Mock
from rmm_cli import RmmApiClient, build_parser, cmd_events, _event_poller
from rmm_tools import TOOL_HANDLERS, tool_exec_command, tool_get_history_events


class TranscriptParityTests(unittest.TestCase):
    def test_client_and_cli_expose_live_and_archived_forward_pages(self):
        client = RmmApiClient('http://localhost')
        client.request = Mock(return_value=(200, {'events': []}))
        for archived in (False, True):
            args = build_parser().parse_args(['events', '--session', 'session-b', '--since', '50', '--limit', '100', '--forward'] + (['--history'] if archived else []))
            cmd_events(client, {}, args)
            route = 'history' if archived else 'sessions'
            client.request.assert_called_with('GET', f'/api/v1/{route}/session-b/events?since=50&limit=100&forward=1')

    def test_ai_tools_pin_target_id_and_expose_result_identity(self):
        client = Mock()
        client.list_sessions.return_value = (200, {'sessions': [{'id': 'id-a', 'hostname': 'HOST-A'}, {'id': 'id-b', 'hostname': 'HOST-B'}]})
        for reference, sid in [('HOST-A', 'id-a'), ('HOST-B', 'id-b')]:
            for status in (200, 408, 409, 410):
                client.exec_command.return_value = (status, {'event': {'body': 'result'}, 'error': 'session_killed'})
                result = json.loads(tool_exec_command(client, reference, 'echo marker'))
                client.exec_command.assert_called_with(sid, 'echo marker', timeout=120)
                self.assertEqual(result['session_id'], sid)
        client.get_events.return_value = (200, {'events': []})
        TOOL_HANDLERS['get_events'](client, {'session_ref': 'HOST-B', 'since': 20, 'limit': 10, 'forward': True})
        client.get_events.assert_called_once_with('id-b', since=20, limit=10, forward=True)

    def test_archived_tool_passes_forward_and_rejects_ambiguous_hosts(self):
        client = Mock()
        client.get_history_session.return_value = (200, {'session': {'session_id': 'id-b'}})
        client.get_history_events.return_value = (200, {'events': []})
        TOOL_HANDLERS['get_history_events'](client, {'session_ref': 'HOST-B', 'forward': True})
        client.get_history_events.assert_called_once_with('id-b', since=0, limit=500, forward=True)
        client.get_history_events.reset_mock()
        client.get_history_session.return_value = (404, {})
        client.list_history.return_value = (200, {'sessions': [{'session_id': 'id-a', 'hostname': 'HOST'}, {'session_id': 'id-b', 'hostname': 'HOST'}]})
        result = json.loads(tool_get_history_events(client, 'HOST'))
        self.assertEqual(result['error'], 'ambiguous_session_ref')
        client.get_history_events.assert_not_called()
