"""Verify the automatic CE credential bootstrap contract and secret handling."""
import json
import unittest
from unittest.mock import patch

from rmm_bloodhound_ce_auth import (
    CeAuthError, bootstrap_credentials, ce_login, change_password, create_api_token,
    generate_password, read_initial_password, PASSWORD_SPECIALS,
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
    def test_request_preserves_size_limit_and_invalid_origin_codes(self):
        connection = unittest.mock.Mock()
        connection.getresponse.return_value.read.return_value = b'x' * (ce_auth.MAX_BODY_BYTES + 1)
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(CeAuthError, 'ce_response_too_large'):
                ce_auth._request('http://127.0.0.1:1030', 'GET', '/api/version')
        for origin in ('http://127.0.0.1:bad', 'http://[invalid'):
            with self.subTest(origin=origin), self.assertRaisesRegex(CeAuthError, 'ce_endpoint_invalid'):
                bootstrap_credentials(origin)

    def test_login_uses_secret_method_and_returns_session_flags(self):
        connection = connection_mock([(200, {'data': {
            'user_id': 'u-1', 'auth_expired': True, 'session_token': 'jwt-opaque'}})])
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
            login = ce_login('http://127.0.0.1:1030', 'admin', 'initialpass_initialpass')
        self.assertEqual(login, {'session': 'jwt-opaque', 'user_id': 'u-1', 'auth_expired': True})
        call = connection.request.call_args
        self.assertEqual((call[0][0], call[0][1]), ('POST', '/api/v2/login'))
        self.assertEqual(json.loads(call[1]['body']), {'login_method': 'secret',
                                            'username': 'admin', 'secret': 'initialpass_initialpass'})
        for rejected, code in ((401, 'ce_login_rejected'), (500, 'ce_login_failed'),
                               (200, 'ce_response_invalid')):
            connection = connection_mock([(rejected, {'data': {}})])
            with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
                with self.assertRaisesRegex(CeAuthError, code):
                    ce_login('http://127.0.0.1:1030', 'admin', 'wrong')
        connection = connection_mock([(200, {'data': {
            'user_id': 'u-1', 'auth_expired': 'false', 'session_token': 'jwt-opaque'}})])
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(CeAuthError, 'ce_response_invalid'):
                ce_login('http://127.0.0.1:1030', 'admin', 'secret')

    def test_password_change_targets_the_self_service_endpoint(self):
        connection = connection_mock([(200, {})])
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
            change_password('http://127.0.0.1:1030', 'jwt-opaque', 'u-1', 'old', 'NewPassword_1!')
        call = connection.request.call_args
        self.assertEqual((call[0][0], call[0][1]), ('PUT', '/api/v2/bloodhound-users/u-1/secret'))
        self.assertEqual(json.loads(call[1]['body']), {'current_secret': 'old',
                                                       'secret': 'NewPassword_1!',
                                                       'needs_password_reset': False})
        self.assertEqual(call[1]['headers']['Authorization'], 'bearer jwt-opaque')
        connection = connection_mock([(403, {'error': 'invalid current password'})])
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(CeAuthError, 'ce_password_change_failed'):
                change_password('http://127.0.0.1:1030', 'jwt-opaque', 'u-1', 'old', 'NewPassword_1!')

    def test_generated_passwords_satisfy_the_ce_policy(self):
        import string
        for _ in range(20):
            password = generate_password()
            self.assertGreaterEqual(len(password), 12)
            self.assertTrue(any(c in string.ascii_lowercase for c in password))
            self.assertTrue(any(c in string.ascii_uppercase for c in password))
            self.assertTrue(any(c in string.digits for c in password))
            self.assertTrue(any(c in PASSWORD_SPECIALS for c in password))

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
    def test_pending_candidate_recovers_a_lost_password_change_reply(self):
        session = {'session': 'jwt', 'user_id': 'u-1', 'auth_expired': False}
        with patch.object(ce_auth, 'read_initial_password') as banner, \
                patch.object(ce_auth, 'ce_login', return_value=session) as login, \
                patch.object(ce_auth, 'create_api_token', return_value=('id', 'key')):
            credentials = bootstrap_credentials('http://127.0.0.1:1030',
                password='old', pending_password='candidate')
        login.assert_called_once_with('http://127.0.0.1:1030', 'admin', 'candidate')
        banner.assert_not_called()
        self.assertEqual(credentials['password'], 'candidate')

    def test_pending_password_is_saved_before_ce_change(self):
        events = []
        session = {'session': 'jwt', 'user_id': 'u-1', 'auth_expired': True}
        def lost_reply(*args):
            events.append('change')
            raise CeAuthError('ce_api_unreachable')
        with patch.object(ce_auth, 'ce_login', return_value=session), \
                patch.object(ce_auth, 'generate_password', return_value='candidate'), \
                patch.object(ce_auth, 'change_password', side_effect=lost_reply):
            with self.assertRaisesRegex(CeAuthError, 'ce_api_unreachable'):
                bootstrap_credentials('http://127.0.0.1:1030', password='old',
                    save_pending_password=lambda candidate: events.append(candidate))
        self.assertEqual(events, ['candidate', 'change'])

    def test_rejected_pending_candidate_reuses_previous_password(self):
        session = {'session': 'jwt', 'user_id': 'u-1', 'auth_expired': False}
        with patch.object(ce_auth, 'read_initial_password') as banner, \
                patch.object(ce_auth, 'ce_login', side_effect=[CeAuthError('ce_login_rejected'), session]) as login, \
                patch.object(ce_auth, 'create_api_token', return_value=('id', 'key')):
            credentials = bootstrap_credentials('http://127.0.0.1:1030',
                password='old', pending_password='candidate')
        self.assertEqual([call.args[2] for call in login.call_args_list], ['candidate', 'old'])
        self.assertEqual(credentials['password'], 'old')
        banner.assert_not_called()

    def test_token_failure_preserves_password_for_retry_without_launch_log(self):
        saved = []
        session = {'session': 'jwt', 'user_id': 'u-1', 'auth_expired': False}
        with patch.object(ce_auth, 'read_initial_password', return_value='initial'), \
                patch.object(ce_auth, 'ce_login', side_effect=[
                    {**session, 'auth_expired': True}, session]), \
                patch.object(ce_auth, 'change_password'), \
                patch.object(ce_auth, 'generate_password', return_value='Generated_Secret1!'), \
                patch.object(ce_auth, 'create_api_token', side_effect=CeAuthError('ce_token_creation_failed')):
            with self.assertRaisesRegex(CeAuthError, 'ce_token_creation_failed'):
                bootstrap_credentials('http://127.0.0.1:1030', save_password=saved.append)
        self.assertEqual(saved, ['Generated_Secret1!'])
        with patch.object(ce_auth, 'read_initial_password') as banner, \
                patch.object(ce_auth, 'ce_login', return_value=session) as login, \
                patch.object(ce_auth, 'change_password') as change, \
                patch.object(ce_auth, 'create_api_token', return_value=('id', 'key')):
            credentials = bootstrap_credentials('http://127.0.0.1:1030', password=saved[0])
        banner.assert_not_called()
        change.assert_not_called()
        login.assert_called_once_with('http://127.0.0.1:1030', 'admin', saved[0])
        self.assertEqual(credentials['password'], saved[0])

    def test_rejected_saved_password_falls_back_to_fresh_database_banner(self):
        session = {'session': 'jwt', 'user_id': 'u-1', 'auth_expired': False}
        with patch.object(ce_auth, 'read_initial_password', return_value='fresh'), \
                patch.object(ce_auth, 'ce_login', side_effect=[CeAuthError('ce_login_rejected'), session]), \
                patch.object(ce_auth, 'create_api_token', return_value=('id', 'key')):
            credentials = bootstrap_credentials('http://127.0.0.1:1030', password='stale')
        self.assertEqual(credentials['password'], 'fresh')

    def test_network_failure_does_not_try_another_password(self):
        with patch.object(ce_auth, 'read_initial_password') as banner, \
                patch.object(ce_auth, 'ce_login', side_effect=CeAuthError('ce_api_unreachable')):
            with self.assertRaisesRegex(CeAuthError, 'ce_api_unreachable'):
                bootstrap_credentials('http://127.0.0.1:1030', password='saved')
        banner.assert_not_called()

    def test_end_to_end_bootstrap_changes_the_expired_password_first(self):
        requests = [
            ('POST', '/api/v2/login', 200, {'data': {'session_token': 'jwt-1', 'user_id': 'u-1', 'auth_expired': True}}),
            ('PUT', '/api/v2/bloodhound-users/u-1/secret', 200, {}),
            ('POST', '/api/v2/login', 200, {'data': {'session_token': 'jwt-2', 'user_id': 'u-1', 'auth_expired': False}}),
            ('POST', '/api/v2/tokens', 200, {'data': {'id': '0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3',
                                                      'key': 'c2VjcmV0LXRva2VuLXZhbHVl'}}),
        ]
        connection = unittest.mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.side_effect = [json.dumps(body).encode() for _, _, _, body in requests]
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection), \
                patch('rmm_bloodhound_ce_auth.read_initial_password', return_value='generatedpass_123456789'), \
                patch('rmm_bloodhound_ce_auth.generate_password', return_value='NewGenerated_Pass1!'):
            credentials = bootstrap_credentials('http://127.0.0.1:1030')
        calls = connection.request.call_args_list
        self.assertEqual([(c[0][0], c[0][1]) for c in calls], [r[:2] for r in requests])
        # The token is minted with the post-change session, not the first one.
        self.assertEqual(calls[3][1]['headers']['Authorization'], 'bearer jwt-2')
        self.assertEqual(credentials['username'], 'admin')
        self.assertEqual(credentials['password'], 'NewGenerated_Pass1!')
        self.assertEqual(credentials['token_id'], '0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3')
        self.assertEqual(credentials['token_key'], 'c2VjcmV0LXRva2VuLXZhbHVl')

    def test_bootstrap_skips_the_change_when_the_secret_is_not_expired(self):
        requests = [
            ('POST', '/api/v2/login', 200, {'data': {'session_token': 'jwt-1', 'user_id': 'u-1', 'auth_expired': False}}),
            ('POST', '/api/v2/tokens', 200, {'data': {'id': '0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3',
                                                      'key': 'c2VjcmV0LXRva2VuLXZhbHVl'}}),
        ]
        connection = unittest.mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.side_effect = [json.dumps(body).encode() for _, _, _, body in requests]
        with patch.object(ce_auth.http.client, 'HTTPConnection', return_value=connection), \
                patch('rmm_bloodhound_ce_auth.read_initial_password', return_value='generatedpass_123456789'):
            credentials = bootstrap_credentials('http://127.0.0.1:1030')
        self.assertEqual(len(connection.request.call_args_list), 2)
        self.assertEqual(credentials['password'], 'generatedpass_123456789')

    def test_invalid_origin_fails_closed_before_any_request(self):
        with self.assertRaisesRegex(CeAuthError, 'ce_endpoint_invalid'):
            bootstrap_credentials('http://user:secret@127.0.0.1:1030/path')


if __name__ == '__main__':
    unittest.main()
