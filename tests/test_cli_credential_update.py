"""Verify credential update contracts across the API client, CLI, and shared tools."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock

import rmm_cli
import rmm_tools


class CredentialUpdateSurfaceTests(unittest.TestCase):
    def test_client_distinguishes_omitted_empty_and_exact_password(self):
        client = rmm_cli.RmmApiClient('http://example.test', 'fixture')
        client.request = Mock(return_value=(200, {'ok': True}))
        for password in (None, '', "  exact ' é "):
            client.creds_update('cred/id?#', 'LAB', 'alice', password)
            method, path, body = client.request.call_args.args
            self.assertEqual((method, path), ('PATCH', '/api/v1/creds/cred%2Fid%3F%23'))
            self.assertEqual(body, {'domain': 'LAB', 'user': 'alice',
                                    **({'password': password} if password is not None else {})})

    def test_cli_parsing_preserves_password_semantics(self):
        for password in (None, '', ' exact password '):
            client = Mock()
            client.creds_update.return_value = (200, {'ok': True, 'cred_id': 'cred-123'})
            argv = ['creds', 'update', '-i', 'cred-123', 'LAB\\alice']
            if password is not None:
                argv += ['--password', password]
            args = rmm_cli.build_parser().parse_args(argv)
            with contextlib.redirect_stdout(io.StringIO()):
                args.func(client, {}, args)
            client.creds_update.assert_called_once_with('cred-123', 'LAB', 'alice', password)

    def test_cli_failure_exits_without_echoing_password(self):
        client = Mock()
        client.creds_update.return_value = (503, {'error': 'fixture-secret'})
        args = rmm_cli.build_parser().parse_args(
            ['creds', 'update', '-i', 'cred-123', 'alice', '--password', 'fixture-secret'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit):
                args.func(client, {}, args)
        self.assertNotIn('fixture-secret', output.getvalue())

    def test_shared_tool_dispatch_keeps_update_arguments_in_order(self):
        client = Mock()
        client.creds_update.return_value = (200, {'ok': True, 'cred_id': 'cred-123'})
        for password in (None, '', ' exact password '):
            client.reset_mock()
            args = {'cred_id': 'cred-123', 'domain': 'LAB', 'user': 'alice'}
            if password is not None:
                args['password'] = password
            result = json.loads(rmm_tools.TOOL_HANDLERS['creds_update'](client, args))
            self.assertTrue(result['ok'])
            client.creds_update.assert_called_once_with('cred-123', 'LAB', 'alice', password)
