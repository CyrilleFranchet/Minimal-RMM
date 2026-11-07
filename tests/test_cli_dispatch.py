"""Exercise real CLI parsing and dispatch without running a remote command."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import rmm_cli


class CliDispatchTests(unittest.TestCase):
    def test_lateral_certificate_bypass_flag(self):
        client = Mock()
        client.lateral_move.return_value = (200, {"ok": True})
        self.invoke(["lateral", "winrm", "target", "-s", "-k", "--session", "source", "--json"], client)
        self.assertIs(client.lateral_move.call_args.kwargs["insecure"], True)
        client.reset_mock()
        with self.assertRaises(SystemExit): self.invoke(["lateral", "winrm", "target", "-k", "--session", "source"], client)
        client.lateral_move.assert_not_called()

    def test_lateral_https_and_explicit_session(self):
        client = Mock()
        client.lateral_move.return_value = (200, {"ok": True})
        for arguments in [
            ["lateral", "winrm", "ws01", "-s", "--session", "session-id", "--json"],
            ["lateral", "winrm", "--https", "ws01", "--session", "session-id", "--json"],
        ]:
            client.reset_mock()
            self.invoke(arguments, client)
            self.assertIs(client.lateral_move.call_args.kwargs["https"], True)
            self.assertEqual(client.lateral_move.call_args.args, ("session-id", "winrm", "ws01"))
        client.reset_mock()
        with self.assertRaises(SystemExit):
            self.invoke(["lateral", "psexec", "ws01", "-s", "--session", "session-id"], client)
        client.lateral_move.assert_not_called()

    def test_debug_build_requests_separate_full_agent_artifact(self):
        client = Mock()
        client.build_go_agent.return_value = (200, {"filename": "debug.zip"})
        self.invoke(['agent', 'go-build', '--debug'], client)
        self.assertIs(client.build_go_agent.call_args.kwargs['debug'], True)
        api = rmm_cli.RmmApiClient('http://example.test', 'fixture-token')
        api.request = Mock(return_value=(200, {}))
        api.build_go_agent(debug=True)
        body = api.request.call_args.args[2]
        self.assertIs(body['debug'], True)
        self.assertIs(body['stageless'], True)

    def invoke(self, arguments, client):
        """Capture the real CLI entry point with a deterministic API client."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['rmm_cli.py', *arguments]), \
                patch.object(rmm_cli, 'load_state', return_value={}), \
                patch.object(rmm_cli, 'ensure_client', return_value=(client, {})), \
                patch.object(rmm_cli, 'run_interactive') as interactive, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rmm_cli.main()
            interactive.assert_not_called()
        return stdout.getvalue(), stderr.getvalue()

    def test_run_payload_named_health_is_queued(self):
        client = Mock()
        client.queue_command.return_value = (200, {'ok': True})
        output, _ = self.invoke(['run', 'health', '--session', 'session-id', '--json'], client)
        client.queue_command.assert_called_once_with('session-id', 'health', 'oneshot')
        client.health.assert_not_called()
        self.assertEqual(json.loads(output), {'ok': True})

    def test_exec_file_dispatches_and_keeps_json_stdout_parseable(self):
        client = Mock()
        client.exec_command.return_value = (200, {'event': {'body': 'done'}})
        with tempfile.TemporaryDirectory() as root:
            command = Path(root) / 'command.txt'
            command.write_text('whoami\n')
            output, diagnostics = self.invoke(['exec', '--command-file', str(command),
                '--session', 'session-id', '--json'], client)
        client.exec_command.assert_called_once_with('session-id', 'whoami', timeout=120)
        self.assertEqual(json.loads(output), {'body': 'done'})
        self.assertIn('Waiting for agent', diagnostics)

    def test_health_subcommand_still_dispatches_health(self):
        client = Mock()
        client.health.return_value = (200, {'ok': True})
        output, _ = self.invoke(['health', '--json'], client)
        self.assertEqual(json.loads(output), {'status': 200, 'ok': True})
        client.health.assert_called_once_with()

    def test_stored_credential_id_dispatch(self):
        for arguments, method in [
            (['make-token', '-i', 'cred-123'], 'make_token'),
            (['spawnas', '-i', 'cred-123'], 'spawnas'),
            (['runas', '-i', 'cred-123', 'whoami /all'], 'runas'),
        ]:
            with self.subTest(arguments=arguments):
                client = Mock()
                getattr(client, method).return_value = (200, {'ok': True})
                self.invoke([*arguments, '--session', 'session-id', '--json'], client)
                call = getattr(client, method).call_args
                self.assertEqual(call.kwargs['cred_id'], 'cred-123')
                self.assertEqual(call.args[0], 'session-id')
                if method == 'runas':
                    self.assertEqual(call.args[1], 'whoami /all')
        client = Mock()
        client.creds_del.return_value = (200, {'ok': True})
        self.invoke(['creds', 'del', '-i', 'cred-123', '--json'], client)
        client.creds_del.assert_called_once_with('cred-123')

    def test_stored_credential_validation_prevents_requests(self):
        cases = [['creds', 'del', 'cred-123'], ['creds', 'del', '-i', '']]
        for command in ('make-token', 'spawnas', 'runas'):
            suffix = ['whoami'] if command == 'runas' else []
            cases.extend([
                [command, 'cred-123', *suffix],
                [command, '-i', *suffix],
                [command, '-i', '', *suffix],
                [command, '-i', 'cred-123', *suffix,
                 *(['alice', 'pw'] if command == 'make-token' else ['--user', 'alice', '--password', 'pw'])],
            ])
        for arguments in cases:
            with self.subTest(arguments=arguments):
                client = Mock()
                with self.assertRaises(SystemExit):
                    self.invoke([*arguments, '--session', 'session-id'], client)
                for method in ('make_token', 'spawnas', 'runas', 'creds_del'):
                    getattr(client, method).assert_not_called()

    def test_deployment_debug_flags_are_rejected_before_requests(self):
        for command in (['spawn'], ['spawnas', '-i', 'cred-123'], ['lateral', 'psexec', 'ws01']):
            for flag in ('-d', '--debug'):
                with self.subTest(command=command, flag=flag):
                    client = Mock()
                    with self.assertRaises(SystemExit):
                        self.invoke([*command, flag, '--session', 'session-id', '--json'], client)
                    client.spawn_session.assert_not_called()
                    client.spawnas.assert_not_called()
                    client.lateral_move.assert_not_called()

    def test_deployment_client_requests_do_not_override_source_build_mode(self):
        client = object.__new__(rmm_cli.RmmApiClient)
        client.request = Mock(return_value=(200, {'ok': True}))
        for method, args, kwargs in ((client.spawn_session, ('session-id',), {}),
                                     (client.lateral_move, ('session-id', 'psexec', 'ws01'), {}),
                                     (client.spawnas, ('session-id',), {'cred_id': 'cred-123'})):
            with self.subTest(method=method.__name__):
                method(*args, **kwargs)
                self.assertNotIn('debug', client.request.call_args.args[2])
                with self.assertRaises(TypeError):
                    method(*args, **kwargs, debug=True)

    def test_spawn_and_lateral_reject_unknown_or_duplicate_arguments(self):
        for arguments in (['spawn', '--unexpected'], ['spawn', 'extra'],
                          ['lateral', 'psexec', 'ws01', 'extra'],
                          ['lateral', 'psexec', 'ws01', '--unexpected'],
                          ['lateral', 'psexec', '--session', 'session-id', '--', '-d']):
            with self.subTest(arguments=arguments):
                client = Mock()
                with self.assertRaises(SystemExit):
                    self.invoke(arguments, client)
                client.spawn_session.assert_not_called()
                client.lateral_move.assert_not_called()

    def test_direct_credentials_preserve_empty_password(self):
        for arguments, method in [
            (['make-token', 'alice', ''], 'make_token'),
            (['spawnas', '--user', 'alice', '--password', ''], 'spawnas'),
            (['runas', 'whoami', '--user', 'alice', '--password', ''], 'runas'),
        ]:
            with self.subTest(arguments=arguments):
                client = Mock()
                getattr(client, method).return_value = (200, {'ok': True})
                self.invoke([*arguments, '--session', 'session-id', '--json'], client)
                call = getattr(client, method).call_args
                if method == 'make_token':
                    self.assertEqual(call.args[2:], ('alice', ''))
                else:
                    self.assertEqual(call.kwargs['user'], 'alice')
                    self.assertEqual(call.kwargs['password'], '')
