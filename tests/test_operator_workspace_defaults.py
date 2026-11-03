"""Operator defaults target workspace storage, while explicit central URLs remain usable."""
import unittest
from unittest.mock import patch

import rmm_cli
import rmm_settings
import rmm_tools


class WorkspaceDefaultTests(unittest.TestCase):
    def test_central_exec_id_never_skips_workspace_transcript_events(self):
        state = {'last_event_by_session': {'session': 12}}
        rmm_cli.record_exec_cursor(state, 'session', {'event_id_scope': 'central', 'event': {'id': 900}})
        self.assertEqual(state['last_event_by_session']['session'], 12)
        rmm_cli.record_exec_cursor(state, 'session', {'event': {'id': 13}})
        self.assertEqual(state['last_event_by_session']['session'], 13)
    def test_default_cli_and_settings_target_workspace(self):
        with patch('rmm_cli.settings_getenv', return_value=''):
            self.assertEqual(rmm_cli._default_server_url(), 'http://127.0.0.1:8787')
        self.assertEqual(rmm_settings.FIELDS['RMM_SERVER_URL']['default'], 'http://127.0.0.1:8787')

    def test_explicit_central_and_remote_workspace_urls_remain_supported(self):
        for value in ('http://central:8080', 'https://workspace.example'):
            with patch('rmm_cli.settings_getenv', side_effect=lambda key, default='': value if key == 'RMM_SERVER_URL' else ''):
                self.assertEqual(rmm_cli._default_server_url(), value)
            client = rmm_tools.make_client(value, 'operator-token')
            self.assertEqual(client.base_url, value)
