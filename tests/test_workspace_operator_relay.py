"""Central AI local-storage calls use authenticated owner relay without mutation replay."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rmm_bloodhound_broker import BloodHoundBroker, execute_workspace_request, local_operator_path
import server_rmm
import rmm_tools


class OperatorRelayTests(unittest.TestCase):
    def test_intent_directory_is_durable_before_mutation_and_result_after_rename(self):
        with tempfile.TemporaryDirectory() as root:
            task = {'id': 'd' * 32, 'body': {'action': '_operator', 'method': 'DELETE',
                    'path': '/api/v1/history/abc', 'payload': {}, 'qs': {}}}
            events = []
            def sync(directory):
                journal = Path(root) / 'relay-results' / ('d' * 32 + '.json')
                state = json.loads(journal.read_text())['state'] if journal.exists() else 'absent'
                events.append(('sync', Path(directory).name, state))
            def operator(*args):
                self.assertEqual(events[-1], ('sync', 'relay-results', 'started'))
                events.append(('mutation',))
                return 200, {'deleted': True}, 'application/json'
            with patch('rmm_bloodhound_broker.sync_directory', side_effect=sync):
                self.assertEqual(execute_workspace_request(root, task, operator=operator)[0], 200)
            self.assertEqual(events[-1], ('sync', 'relay-results', 'complete'))

    def test_failed_intent_directory_sync_never_dispatches_or_replays_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            task = {'id': 'e' * 32, 'body': {'action': '_operator', 'method': 'DELETE',
                    'path': '/api/v1/history/abc', 'payload': {}, 'qs': {}}}
            operator = Mock()
            with patch('rmm_bloodhound_broker.sync_directory', side_effect=[None, OSError('disk failure')]):
                with self.assertRaises(OSError):
                    execute_workspace_request(root, task, operator=operator)
            self.assertEqual(execute_workspace_request(root, task, operator=operator)[0], 409)
            operator.assert_not_called()

    def test_http_client_relay_reaches_real_workspace_settings(self):
        from http.server import ThreadingHTTPServer
        import time
        from workspace_service import WorkspaceService
        from rmm_settings import SettingsStore, SCHEMA
        from rmm_cli import RmmApiClient
        broker = BloodHoundBroker()
        class Handler(server_rmm.RMMHandler):
            server_instance = SimpleNamespace(bloodhound_broker=broker)
            def _api_authorized(self, *args):
                self._workspace_scope_id = None
                if self.headers.get('X-RMM-Workspace') == 'alice' and self.headers.get('X-RMM-Workspace-Token') == 'fixture-secret':
                    self._workspace_scope_id = 'alice'
                    return True
                return False
            def _path_allowed(self, *args, **kwargs):
                return True
            def log_message(self, *args):
                pass
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as root:
            settings = Path(root) / 'settings.json'
            settings.write_text(json.dumps({'schema': SCHEMA, 'scope': 'workspace', 'settings': {'RMM_WORKSPACE_ID': 'alice'}}))
            service = object.__new__(WorkspaceService)
            service.settings = SettingsStore(settings, 'workspace', environment={})
            client = RmmApiClient(f'http://127.0.0.1:{http.server_port}', '', extra_headers={
                'X-RMM-Workspace': 'alice', 'X-RMM-Workspace-Token': 'fixture-secret', 'X-RMM-Operator-Relay': '1'})
            responses = []
            request = threading.Thread(target=lambda: responses.append(client.request('GET', '/api/v1/settings', timeout=5)))
            request.start()
            try:
                deadline = time.monotonic() + 2
                tasks = []
                while not tasks and time.monotonic() < deadline:
                    tasks = broker.pending('alice')
                    if not tasks:
                        time.sleep(.01)
                self.assertEqual(len(tasks), 1)
                status, payload = execute_workspace_request(root, tasks[0], operator=lambda method, path, body, qs: service.get_json(path, qs))
                broker.complete('alice', tasks[0]['id'], {'status': status, 'payload': payload})
                request.join(6)
                self.assertEqual(responses[0][0], 200)
                self.assertEqual(responses[0][1]['scope'], 'workspace')
            finally:
                request.join(6)

    def test_real_workspace_settings_are_returned_without_central_or_secret_data(self):
        from workspace_service import WorkspaceService
        from rmm_settings import SettingsStore, SCHEMA
        with tempfile.TemporaryDirectory() as root:
            settings = Path(root) / 'settings.json'
            settings.write_text(json.dumps({'schema': SCHEMA, 'scope': 'workspace', 'settings': {
                'RMM_WORKSPACE_ID': 'alice', 'RMM_WORKSPACE_TOKEN': 'private-secret'}}))
            service = object.__new__(WorkspaceService)
            service.settings = SettingsStore(settings, 'workspace', environment={})
            task = {'id': 'c' * 32, 'body': {'action': '_operator', 'method': 'GET',
                    'path': '/api/v1/settings', 'payload': {}, 'qs': {}}}
            status, payload = execute_workspace_request(root, task,
                operator=lambda method, path, body, qs: service.get_json(path, qs))
            self.assertEqual(status, 200)
            self.assertEqual(payload['scope'], 'workspace')
            self.assertNotIn('private-secret', json.dumps(payload))
            self.assertNotIn('private-secret', (Path(root) / 'relay-results' / ('c' * 32 + '.json')).read_text())

    def test_roundtrip_scope_and_no_replay(self):
        broker = BloodHoundBroker()
        response = []
        submitted = threading.Event()
        original_pending = broker.pending
        def worker():
            response.append(broker.submit_operator('alice', 'PATCH', '/api/v1/assets/skills/example.md', {'enabled': False}, timeout=3))
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            import time
            deadline = time.monotonic() + 2
            tasks = []
            while not tasks and time.monotonic() < deadline:
                tasks = original_pending('alice')
                if not tasks:
                    submitted.wait(.01)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(original_pending('bob'), [])
            task = tasks[0]
            operator = Mock(return_value=(200, {'ok': True, 'workspace': 'alice'}, 'application/json'))
            with tempfile.TemporaryDirectory() as root:
                first = execute_workspace_request(root, task, operator=operator)
                self.assertEqual(execute_workspace_request(root, task, operator=operator), first)
                operator.assert_called_once_with('PATCH', '/api/v1/assets/skills/example.md', {'enabled': False}, {})
                self.assertEqual(broker.complete('bob', task['id'], {'status': first[0], 'payload': first[1]})[0], 404)
                broker.complete('alice', task['id'], {'status': first[0], 'payload': first[1]})
            thread.join(3)
            self.assertEqual(response, [first])
            self.assertNotIn(task['id'], broker.requests)
        finally:
            thread.join(4)

    def test_timeout_keeps_request_for_result_without_resubmission(self):
        broker = BloodHoundBroker()
        status, payload = broker.submit_operator('alice', 'DELETE', '/api/v1/history/abc', timeout=0)
        self.assertEqual(status, 504)
        task = broker.pending('alice')[0]
        self.assertEqual(task['id'], payload['workspace_request_id'])
        broker.complete('alice', task['id'], {'status': 200, 'payload': {'deleted': True}})
        self.assertEqual(broker.result('alice', task['id']), (200, {'deleted': True}))
        self.assertEqual(broker.result('bob', task['id'])[0], 404)

    def test_only_marked_authenticated_owner_routes_locally(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._json = Mock()
        handler._workspace_scope = Mock(return_value='alice')
        handler.server_instance = SimpleNamespace(bloodhound_broker=Mock())
        handler.server_instance.bloodhound_broker.submit_operator.return_value = (200, {'local': True})
        handler.headers = {}
        self.assertFalse(handler._relay_local_operator('GET', '/api/v1/history'))
        handler.headers = {'X-RMM-Operator-Relay': '1'}
        self.assertTrue(handler._relay_local_operator('GET', '/api/v1/history'))
        handler._workspace_scope.return_value = None
        self.assertFalse(handler._relay_local_operator('GET', '/api/v1/history'))
        handler.server_instance.bloodhound_broker.submit_operator.assert_called_once()

    def test_allowlist_excludes_nested_relay_and_binary_or_arbitrary_targets(self):
        for path in ('/api/v1/workspace/bloodhound/requests', '/api/v1/ai/jobs', '/api/v1/bloodhound',
                     '/api/v1/plugins', '/api/v1/artifacts/downloads/file', 'https://evil.invalid/api/v1/history'):
            for method in ('GET', 'POST', 'PATCH', 'DELETE'):
                self.assertFalse(local_operator_path(method, path), (method, path))
        broker = BloodHoundBroker()
        self.assertEqual(broker.submit('alice', {'action': '_operator'})[0], 400)
        self.assertEqual(broker.submit_operator('alice', 'GET', '/api/v1/workspace/bloodhound/requests', timeout=0)[0], 400)
        with tempfile.TemporaryDirectory() as root:
            operator = Mock()
            task = {'id': 'a' * 32, 'body': {'action': '_operator', 'method': 'GET', 'path': '/api/v1/ai/jobs', 'payload': {}, 'qs': {}}}
            self.assertEqual(execute_workspace_request(root, task, operator=operator)[0], 400)
            operator.assert_not_called()

    def test_tool_client_propagates_relay_marker_only_with_workspace_identity(self):
        values = {'RMM_WORKSPACE_ID': 'alice', 'RMM_WORKSPACE_TOKEN': 'secret', 'RMM_WORKSPACE_OPERATOR_RELAY': '1'}
        with patch('rmm_tools.settings_getenv', side_effect=lambda key, default='': values.get(key, default)):
            client = rmm_tools.make_client('http://central', '')
            self.assertEqual(client.extra_headers['X-RMM-Operator-Relay'], '1')
            explicit = rmm_tools.make_client('http://workspace', 'local-token')
            self.assertNotIn('X-RMM-Operator-Relay', explicit.extra_headers)

    def test_large_result_is_cached_as_error_without_reexecution(self):
        with tempfile.TemporaryDirectory() as root:
            task = {'id': 'b' * 32, 'body': {'action': '_operator', 'method': 'GET', 'path': '/api/v1/history', 'payload': {}, 'qs': {}}}
            operator = Mock(return_value=(200, {'large': 'x' * 1048576}, 'application/json'))
            result = execute_workspace_request(root, task, operator=operator)
            self.assertEqual(result[0], 502)
            self.assertEqual(execute_workspace_request(root, task, operator=operator), result)
            operator.assert_called_once()
