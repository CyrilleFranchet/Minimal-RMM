"""Credential ID completion stays scoped to explicit registered ID arguments."""
import unittest
from unittest.mock import Mock, patch

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

import rmm_cli


class CredentialCompletionTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.creds_list.return_value = (200, {'creds': [
            {'cred_id': 'abc-123', 'label': 'LAB\\alice', 'password': 'secret'},
            {'cred_id': 'def-456', 'label': 'bob'},
        ]})
        self.completer = rmm_cli.RmmCliPTKCompleter({}, self.client)

    def matches(self, text):
        return list(self.completer.get_completions(Document(text), CompleteEvent()))

    def test_registered_commands_insert_ids_with_labels(self):
        for command in ('make_token', 'spawnas', 'runas', 'creds del', 'lateral winrm host'):
            with self.subTest(command=command):
                matches = self.matches('/' + command + ' -i a')
                self.assertEqual([item.text for item in matches], ['abc-123 '])
                self.assertEqual(matches[0].start_position, -1)
                self.assertEqual(matches[0].display_meta_text, 'LAB\\alice')
        self.client.creds_list.assert_called_once_with(include_passwords=False)
        self.assertNotIn('secret', repr(self.completer._words.credentials._items))

    def test_empty_prefix_and_negative_contexts(self):
        self.assertEqual(len(self.matches('/runas -i ')), 2)
        self.client.reset_mock()
        for text in ('runas -i ', '/unknown -i ', '/creds add -i ',
                     '/runas -i abc command -i ', '/runas bob password command -i ',
                     '/make_token "unterminated -i '):
            self.assertIsNone(rmm_cli.credential_completion_prefix(text), text)
        self.client.creds_list.assert_not_called()

    def test_provider_refreshes_and_handles_failure_without_stale_ids(self):
        provider = rmm_cli.CredentialIdCompletionProvider(self.client)
        with patch.object(rmm_cli.time, 'monotonic', side_effect=[0, 1, 6, 12]):
            self.assertEqual(len(provider.candidates('')), 2)
            self.assertEqual(len(provider.candidates('a')), 1)
            self.client.creds_list.return_value = (503, {})
            self.assertEqual(provider.candidates(''), [])
            self.client.creds_list.side_effect = OSError('offline')
            self.assertEqual(provider.candidates(''), [])

    def test_registration_and_readline_reuse(self):
        with patch.dict(rmm_cli.CREDENTIAL_ID_COMPLETION_COMMANDS, {('custom',): ('-i',)}):
            self.assertEqual(rmm_cli.credential_completion_prefix('/custom -i ab'), 'ab')
        completer = rmm_cli.RmmCliCompleter({}, self.client)
        with patch.object(rmm_cli, 'readline') as readline:
            readline.get_line_buffer.return_value = '/spawnas -i a'
            self.assertEqual(completer.complete('a', 0), 'abc-123 ')
            self.assertIsNone(completer.complete('a', 1))
