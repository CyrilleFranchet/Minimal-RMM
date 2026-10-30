"""CLI authentication must not silently broaden workspace access."""
import contextlib
import io
import unittest
from unittest.mock import patch

import rmm_cli


class CliWorkspaceAuthTests(unittest.TestCase):
    def connect(self, config, token=None, status=200):
        output = io.StringIO()
        with patch.object(rmm_cli, 'settings_getenv', side_effect=lambda key, default='': config.get(key, default)), patch.object(rmm_cli, 'DEFAULT_TOKEN', 'inherited-admin'), patch.object(rmm_cli.RmmApiClient, 'health', return_value=(status, {})), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            return rmm_cli.ensure_client('http://central', token)[0]

    def test_workspace_overrides_inherited_admin(self):
        client = self.connect({'RMM_WORKSPACE_ID': 'w1', 'RMM_WORKSPACE_TOKEN': 'workspace-secret'})
        self.assertEqual(client.token, '')
        self.assertNotIn('Authorization', client._headers())
        self.assertEqual(client._headers()['X-RMM-Workspace'], 'w1')
        self.assertEqual(client._headers()['X-RMM-Workspace-Token'], 'workspace-secret')

    def test_explicit_bearer_overrides_workspace_for_local_or_admin_access(self):
        client = self.connect({'RMM_WORKSPACE_ID': 'w1', 'RMM_WORKSPACE_TOKEN': 'workspace-secret'}, 'explicit-local-token')
        self.assertEqual(client._headers()['Authorization'], 'Bearer explicit-local-token')
        self.assertNotIn('X-RMM-Workspace', client._headers())

    def test_no_workspace_preserves_default_bearer(self):
        self.assertEqual(self.connect({}).token, 'inherited-admin')

    def test_partial_workspace_fails_before_any_request(self):
        for config in ({'RMM_WORKSPACE_ID': 'w1'}, {'RMM_WORKSPACE_TOKEN': 'secret'}):
            with self.subTest(config=config), patch.object(rmm_cli, 'settings_getenv', side_effect=lambda key, default='': config.get(key, default)), patch.object(rmm_cli.RmmApiClient, 'health') as health, patch.object(rmm_cli, 'say'):
                with self.assertRaises(SystemExit):
                    rmm_cli.ensure_client('http://central')
                health.assert_not_called()

    def test_rejected_workspace_does_not_retry_with_admin_token(self):
        with self.assertRaises(SystemExit):
            self.connect({'RMM_WORKSPACE_ID': 'w1', 'RMM_WORKSPACE_TOKEN': 'bad'}, status=401)

    def test_parser_preserves_explicit_token_distinction(self):
        parser = rmm_cli.build_parser()
        self.assertIsNone(parser.parse_args(['sessions', 'list']).token)
        self.assertEqual(parser.parse_args(['--token', 'explicit', 'sessions', 'list']).token, 'explicit')


if __name__ == '__main__':
    unittest.main()
