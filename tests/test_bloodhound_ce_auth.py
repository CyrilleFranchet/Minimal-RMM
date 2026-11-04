"""Verify the automatic CE credential bootstrap contract and secret handling."""
import json
import unittest
from unittest.mock import patch

from rmm_bloodhound_ce_auth import (
    CeAuthError, bootstrap_credentials, ce_login, create_api_token, read_initial_password,
)
import rmm_bloodhound_ce_auth as ce_auth


def connection_mock(responses):
    """Return an http.client connection mock replaying bounded responses."""
    connection = unittest.mock.Mock()
    response = connection.getresponse.return_value
    status, payload = responses.pop(0)
    response.status = status
    response.read.return_value = json.dumps(payload).encode()
    return connection


class InitialPasswordTests(unittest.TestCase):
    def test_reads_the_last_banner_from_a_bounded_tail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = f'{directory}/rmm-bloodhound-ce.log'
            banner = lambda token: (
                '###############################\n'
                f'# Initial Password Set To:    {token}    #\n'
                '###############################\n'
            )
            with open(path, 'w') as handle:
                handle.write('noise ' * 1000 + banner('firstpass_firstpass_0001') +
                             'noise ' * 10 + banner('secondpass_secondpass_02'))
            self.assertEqual(read_initial_password(path), 'secondpass_secondpass_02')
            with open(path, 'w') as handle:
                handle.write('no banner here')
            with self.assertRaisesRegex(CeAuthError, 'ce_initial_password_unavailable'):
                read_initial_password(path)

    def test_missing_log_is_a_bounded_error(self):
        with self.assertRaisesRegex(CeAuthError, 'ce_launch_log_unavailable'):
            read_initial_password('/nonexistent/rmm-bloodhound-ce.log')


class CeApiContractTests(unittest.TestCase):
    def test_login_uses_secret_method_and_returns_session_token(self):
        connection = connection_mock([(200, {'data': {
            'user_id': 'u', 'auth_expired': False, 'session_token': 'jwt-opaque'}})])
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
            session = ce_login('http://127.0.0.1:1030', 'admin', 'initialpass_initialpass')
        self.assertEqual(session, 'jwt-opaque')
        method, path, body, headers = connection.request.call_args[0][0], \
            connection.request.call_args[0][1], connection.request.call_args[1]['body'], \
            connection.request.call_args[1]['headers']
        self.assertEqual((method, path), ('POST', '/api/v2/login'))
        self.assertEqual(json.loads(body), {'login_method': 'secret',
                                            'username': 'admin', 'secret': 'initialpass_initialpass'})
        for rejected in (401, 500, 200):
            connection = connection_mock([(rejected, {'data': {}})])
            with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
                with self.assertRaisesRegex(CeAuthError, 'ce_login_rejected'):
                    ce_login('http://127.0.0.1:1030', 'admin', 'wrong')

    def test_token_creation_sends_bearer_session_and_returns_id_and_key(self):
        connection = connection_mock([(200, {'data': {
            'id': '0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3',
            'key': 'c2VjcmV0LXRva2VuLXZhbHVl',
            'hmac_method': 'HMAC-SHA-256'}})])
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
            token_id, key = create_api_token('http://127.0.0.1:1030', 'jwt-opaque')
        self.assertEqual((token_id, key), ('0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3',
                                           'c2VjcmV0LXRva2VuLXZhbHVl'))
        call = connection.request.call_args
        self.assertEqual((call[0][0], call[0][1]), ('POST', '/api/v2/tokens'))
        self.assertEqual(json.loads(call[1]['body']), {'token_name': 'minimal-rmm'})
        self.assertEqual(call[1]['headers']['Authorization'], 'bearer jwt-opaque')
        for payload in ({'data': {'id': 'not-a-uuid', 'key': 'x'}}, {'data': {'id': '0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3'}}):
            connection = connection_mock([(403, payload)])
            with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
                with self.assertRaisesRegex(CeAuthError, 'ce_token_creation_failed'):
                    create_api_token('http://127.0.0.1:1030', 'jwt-opaque')


class BootstrapTests(unittest.TestCase):
    def test_end_to_end_bootstrap_never_leaks_values_in_codes(self):
        responses = [
            (200, {'data': {'session_token': 'jwt-opaque', 'user_id': 'u'}}),
            (200, {'data': {'id': '0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3',
                            'key': 'c2VjcmV0LXRva2VuLXZhbHVl'}}),
        ]
        connection = unittest.mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.side_effect = [
            json.dumps(responses[0][1]).encode(), json.dumps(responses[1][1]).encode()]
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection), \
                patch('rmm_bloodhound_ce_auth.read_initial_password', return_value='generatedpass_123456789'):
            credentials = bootstrap_credentials('http://127.0.0.1:1030')
        self.assertEqual(credentials['username'], 'admin')
        self.assertEqual(credentials['password'], 'generatedpass_123456789')
        self.assertEqual(credentials['token_id'], '0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3')
        self.assertEqual(credentials['token_key'], 'c2VjcmV0LXRva2VuLXZhbHVl')

    def test_invalid_origin_fails_closed_before_any_request(self):
        with self.assertRaisesRegex(CeAuthError, 'ce_endpoint_invalid'):
            bootstrap_credentials('http://user:secret@127.0.0.1:1030/path')


if __name__ == '__main__':
    unittest.main()
