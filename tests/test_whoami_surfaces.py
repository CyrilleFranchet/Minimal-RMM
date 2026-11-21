"""Exercise identity-report forwarding and interactive command validation."""
import json
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import rmm_cli
import rmm_tools
import test_cli_dispatch
import test_cli_slash_commands


class WhoamiSurfaceTests(unittest.TestCase):
    def test_failed_report_exits_nonzero_in_text_and_json_modes(self):
        for json_mode in (False, True):
            for status, result in ((200, {'ok': False, 'output': 'Whoami failed: denied'}),
                                   (408, {'error': 'timeout'})):
                with self.subTest(json=json_mode, status=status):
                    client = Mock()
                    client.session_whoami.return_value = status, result
                    output = io.StringIO()
                    args = SimpleNamespace(session='session', timeout=120, tickets=False, json=json_mode)
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as caught:
                            rmm_cli.cmd_whoami(client, {}, args)
                    self.assertEqual(caught.exception.code, 1)
                    if json_mode:
                        self.assertEqual(json.loads(output.getvalue()), result)

    def test_client_preserves_ticket_option_and_wait(self):
        client = rmm_cli.RmmApiClient('http://example.test', 'token')
        client.request = Mock(return_value=(200, {}))
        client.session_whoami('session', timeout=240, tickets=True)
        client.request.assert_called_once_with(
            'POST', '/api/v1/sessions/session/whoami',
            {'timeout': 240, 'tickets': True}, timeout=245)

    def test_cli_entry_point(self):
        client = Mock()
        client.session_whoami.return_value = (200, {'ok': True, 'output': 'report'})
        output, _ = test_cli_dispatch.CliDispatchTests().invoke(
            ['whoami', '--tickets', '--session', 'session', '--json'], client)
        self.assertEqual(json.loads(output)['output'], 'report')
        client.session_whoami.assert_called_once_with('session', timeout=120, tickets=True)

    def test_slash_validation_and_bare_command(self):
        with patch.object(rmm_cli.RmmApiClient, 'session_whoami'):
            # Supply the result expected by the existing interactive harness.
            original = rmm_cli.run_interactive
            def run(client, state):
                client.session_whoami.return_value = (200, {'output': 'report'})
                return original(client, state)
            with patch.object(rmm_cli, 'run_interactive', side_effect=run):
                client, _, warn = test_cli_slash_commands.SlashCommandTests().run_lines([
                    '/whoami', '/whoami --tickets', '/whoami --bad',
                    '/whoami --tickets --tickets', 'whoami'])
        self.assertEqual(client.session_whoami.call_count, 2)
        self.assertEqual(client.session_whoami.call_args_list[0].kwargs, {'tickets': False})
        self.assertEqual(client.session_whoami.call_args_list[1].kwargs, {'tickets': True})
        self.assertEqual(warn.call_count, 2)
        client.queue_command.assert_called_once_with('session-id', 'whoami')

    def test_tool_resolution_timeout_and_strict_boolean(self):
        client = Mock()
        client.list_sessions.return_value = (200, {'sessions': [{'id': 'session', 'hostname': 'host'}]})
        client.session_whoami.return_value = (408, {})
        result = json.loads(rmm_tools.tool_session_whoami(client, 'host', tickets=True))
        self.assertEqual(result['command'], '__WHOAMI__ --tickets')
        client.session_whoami.assert_called_once_with('session', timeout=120, tickets=True)
        client.reset_mock()
        result = json.loads(rmm_tools.tool_session_whoami(client, 'host', tickets='false'))
        self.assertFalse(result['ok'])
        client.list_sessions.assert_not_called()


if __name__ == '__main__':
    unittest.main()
