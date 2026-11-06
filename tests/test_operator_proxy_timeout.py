"""Workspace HTTP dispatch preserves central operation wait budgets."""
import unittest
from unittest.mock import Mock
from workspace_service import WorkspaceRequestHandler, operator_proxy_timeout


class OperatorProxyTimeoutTests(unittest.TestCase):
    def test_defaults_and_custom_waits(self):
        for action, expected in [('spawn', 450), ('lateral', 630), ('make_token', 150), ('rev2self', 150), ('uid', 150)]:
            with self.subTest(action=action):
                self.assertEqual(operator_proxy_timeout('/api/v1/sessions/abc/' + action, {}), expected)
        self.assertEqual(operator_proxy_timeout('/api/v1/sessions/abc/spawn', {'verify': False, 'spawn_timeout': 40}), 70)
        self.assertEqual(operator_proxy_timeout('/api/v1/sessions/abc/lateral', {
            'exec_timeout': 500, 'register_timeout': 90, 'verify_timeout': 150}), 920)
        self.assertEqual(operator_proxy_timeout('/api/v1/sessions/abc/uid', {'timeout': 900}), 930)
        for value in ['bad', None, float('nan'), float('inf'), 999999]:
            self.assertEqual(operator_proxy_timeout('/api/v1/sessions/abc/uid', {'timeout': value}), 150)
        self.assertEqual(operator_proxy_timeout('/api/v1/sessions', {}), 30)
        self.assertEqual(operator_proxy_timeout('/api/v1/agent/go/build', {}), 300)

    def test_real_handler_uses_operation_timeout(self):
        handler = object.__new__(WorkspaceRequestHandler)
        handler.path = '/api/v1/sessions/abc/spawn'
        handler._authorized = Mock(return_value=True)
        handler._request_body = Mock(return_value={})
        handler._json = Mock()
        handler.service = Mock()
        handler.service.post_json.return_value = None
        handler.service._proxy.return_value = (200, {'verified': True}, 'application/json')
        handler._handle('POST')
        handler.service._proxy.assert_called_once_with('POST', handler.path, {}, timeout=450)
        handler._json.assert_called_once_with(200, {'verified': True})
