"""Exercise real tool dispatch and server transcripts across two fake agents."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import rmm_ai
import server_rmm


class AISessionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch.object(server_rmm, 'LOG_DIR', self.directory.name)
        self.patch.start()
        with patch.object(server_rmm.RMMServer, '_archive_orphaned_sessions_on_startup'):
            self.server = server_rmm.RMMServer()
        for name in ('_history_append_event', '_history_write_meta', 'workspace_publish_event', 'tty_print', 'log'):
            setattr(self.server, name, Mock())
        self.server.event_hub = Mock()
        self.sessions = [server_rmm.Session('session-a', 'host-a', 'user'),
                         server_rmm.Session('session-b', 'host-b', 'user')]
        self.server.sessions = {session.id: session for session in self.sessions}
        self.server.current_session = 'session-a'

    def tearDown(self):
        self.server.close()
        self.patch.stop()
        self.directory.cleanup()

    def handler(self):
        return SimpleNamespace(
            server_instance=self.server, _api_authorized=lambda: True,
            _api_path_parts=lambda path: path.split('/')[3:], _json=Mock())

    def test_ai_switches_target_without_mixing_shell_transcripts(self):
        destinations = []
        def queue(sid, command, cmd_type='oneshot'):
            destinations.append(sid)
            handler = self.handler()
            server_rmm.RMMHandler._handle_api_post(
                handler, f'/api/v1/sessions/{sid}/commands', {'command': command, 'type': cmd_type})
            return handler._json.call_args.args
        client = SimpleNamespace(
            list_sessions=lambda: (200, {'sessions': self.server.sessions_to_json()}),
            queue_command=queue)
        responses = []
        for index, session in enumerate(self.sessions):
            responses.append({'choices': [{'message': {'tool_calls': [{
                'id': str(index), 'type': 'function', 'function': {
                    'name': 'queue_command', 'arguments': json.dumps({
                        'session_ref': session.id, 'command': f'echo {session.id}'})}}]}}]})
        responses.append({'choices': [{'message': {'content': 'Done'}}]})
        with patch('rmm_ai.make_client', return_value=client), patch('rmm_ai._json_request', side_effect=responses):
            result = rmm_ai._run_ai_chat_direct(
                rmm_base_url='http://unused', rmm_token='test', provider='openai', api_key='test',
                messages=[{'role': 'user', 'content': 'Check both test sessions'}], model='test',
                selected_session_id='session-a', max_rounds=4, skill_documents=[])
        self.assertTrue(result['ok'])
        self.assertEqual(destinations, ['session-a', 'session-b'])
        for session in reversed(self.sessions):
            command, kind = self.server.get_command(session.id)
            self.assertEqual((command, kind), (f'echo {session.id}', 'execute'))
            self.server.handle_result(session.id, json.dumps({'rmm_cmd': command, 'rmm_output': session.id}))
        for session in self.sessions:
            events = list(session.result_events)
            self.assertEqual([event['type'] for event in events], ['operator', 'output'])
            self.assertEqual([event['session_id'] for event in events], [session.id, session.id])
            self.assertEqual(events[-1]['body'], session.id)
            self.assertTrue(all(event['command'] == f'echo {session.id}' for event in events))

    def test_rejected_scoped_exec_never_falls_back_to_global_session(self):
        handler = self.handler()
        handler._api_session = lambda ref: None
        self.server.exec_and_wait = Mock()
        server_rmm.RMMHandler._handle_api_post(
            handler, '/api/v1/sessions/session-b/exec', {'command': 'echo test'})
        self.assertEqual(handler._json.call_args.args[0], 404)
        self.server.exec_and_wait.assert_not_called()
        self.assertFalse(self.sessions[1].result_events)
