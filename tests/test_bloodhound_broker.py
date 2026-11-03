"""Central AI requests execute once at the owning workspace."""
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rmm_bloodhound_broker import BloodHoundBroker, execute_workspace_request
from rmm_bloodhound import bloodhound_api
import server_rmm
from workspace_service import WorkspaceService


class BrokerTests(unittest.TestCase):
    def test_private_ai_context_round_trip_is_not_public_or_durable(self):
        broker = BloodHoundBroker()
        result = []
        worker = threading.Thread(target=lambda: result.append(broker.resolve_ai_context('alice', timeout=2)))
        worker.start()
        deadline = time.monotonic() + 1
        tasks = []
        while not tasks and time.monotonic() < deadline:
            tasks = broker.pending('alice')
            if not tasks:
                time.sleep(0.001)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task['body'], {'action': '_ai_context'})
        self.assertEqual(broker.pending('bob'), [])
        self.assertEqual(broker.result('alice', task['id'])[0], 404)
        self.assertEqual(broker.submit('alice', {'action': '_ai_context'})[0], 400)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'owner'
            with patch('rmm_exegol_runtime.get_runtime') as runtime, \
                    patch('rmm_bloodhound_broker.getenv', side_effect=lambda key, default: {
                        'RMM_EXEGOL_MCP_URL': 'https://mcp.workspace/mcp',
                        'RMM_EXEGOL_MCP_TOKEN': 'private-secret',
                    }[key]):
                runtime.return_value.status.return_value = {'exegol_status': 'ready'}
                status, payload = execute_workspace_request(root, task)
                self.assertFalse(root.exists())
                self.assertTrue(payload['exegol_mcp_enabled'])
                self.assertEqual(payload['exegol_mcp_token'], 'private-secret')
            self.assertEqual(broker.complete('bob', task['id'], {'status': status, 'payload': payload})[0], 404)
            broker.complete('alice', task['id'], {'status': status, 'payload': payload})
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [(200, payload)])
        self.assertNotIn(task['id'], broker.requests)
        self.assertEqual(broker.result('alice', task['id'])[0], 404)

    def test_private_ai_context_timeout_removes_request(self):
        broker = BloodHoundBroker()
        self.assertEqual(broker.resolve_ai_context('alice', timeout=0),
                         (503, {'error': 'workspace_ai_context_unavailable'}))
        self.assertEqual(broker.requests, {})

    def test_private_completed_result_cannot_be_read_by_public_action(self):
        broker = BloodHoundBroker()
        request_id = 'd' * 32
        broker.requests[request_id] = {
            'owner': 'alice', 'body': {'action': '_ai_context'}, 'private': True,
            'created': time.monotonic(), 'result': (200, {'exegol_mcp_token': 'secret'}),
        }
        self.assertEqual(broker.submit('alice', {'action': 'result', 'run_id': request_id})[0], 404)

    def test_private_context_requires_ready_workspace_exegol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'owner'
            with patch('rmm_exegol_runtime.get_runtime') as runtime, patch('rmm_bloodhound_broker.getenv') as settings:
                runtime.return_value.status.return_value = {'exegol_status': 'unavailable', 'state': 'blocked'}
                status, payload = execute_workspace_request(root, {'id': 'c' * 32, 'body': {'action': '_ai_context'}})
                self.assertEqual(status, 503)
                self.assertEqual(payload['error'], 'workspace_exegol_not_ready')
                settings.assert_not_called()
                self.assertFalse(root.exists())

    def test_health_is_read_only_without_journal_and_expires_promptly(self):
        broker = BloodHoundBroker()
        _, queued = broker.submit('alice', {'action': 'health'})
        task = broker.pending('alice')[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'owner'
            with patch('rmm_bloodhound_broker.bloodhound_api', return_value=(200, {'runtime': {'state': 'ready'}})) as api:
                self.assertEqual(execute_workspace_request(root, task)[0], 200)
                self.assertEqual(execute_workspace_request(root, task)[0], 200)
                self.assertEqual(api.call_count, 2)
                self.assertFalse(root.exists())
        request_id = queued['workspace_request_id']
        broker.complete('alice', request_id, {'status': 200, 'payload': {'runtime': {'state': 'ready'}}})
        broker.requests[request_id]['completed_at'] -= 31
        self.assertEqual(broker.result('alice', request_id)[0], 404)

    def test_slow_discovery_retains_reply_for_thirty_seconds_after_completion(self):
        for action in ('health', 'library_search', 'library_get'):
            with self.subTest(action=action), patch('rmm_bloodhound_broker.time.monotonic') as clock:
                clock.return_value = 100
                broker = BloodHoundBroker()
                _, queued = broker.submit('alice', {'action': action})
                request_id = queued['workspace_request_id']
                clock.return_value = 145
                broker.complete('alice', request_id, {'status': 200, 'payload': {'ok': True}})
                self.assertEqual(broker.result('alice', request_id), (200, {'ok': True}))
                clock.return_value = 174
                self.assertEqual(broker.result('alice', request_id)[0], 200)
                clock.return_value = 176
                self.assertEqual(broker.result('alice', request_id)[0], 404)

    def test_workspace_scope_and_reply_polling(self):
        broker = BloodHoundBroker()
        code, response = broker.submit('alice', {'action': 'preflight'})
        request_id = response['workspace_request_id']
        self.assertEqual(code, 202)
        self.assertEqual(broker.pending('bob'), [])
        self.assertEqual(broker.result('bob', request_id)[0], 404)
        self.assertEqual(broker.complete('bob', request_id, {'status': 200, 'payload': {}})[0], 404)
        broker.complete('alice', request_id, {'status': 200, 'payload': {'ready': True}})
        self.assertEqual(broker.submit('alice', {'action': 'result', 'run_id': request_id}), (200, {'ready': True}))
        self.assertEqual(broker.pending('alice'), [])

    def test_duplicate_delivery_returns_cached_reply_without_execution(self):
        with tempfile.TemporaryDirectory() as root:
            task = {'id': 'a' * 32, 'body': {'action': 'create'}}
            with patch('rmm_bloodhound_broker.bloodhound_api', return_value=(201, {'run': {'id': 'new'}})) as execute:
                first = execute_workspace_request(root, task)
                second = execute_workspace_request(root, task)
                self.assertEqual(first, second)
                execute.assert_called_once()
            self.assertEqual(bloodhound_api(root, {'action': 'result', 'run_id': task['id']}), first)

    def test_interrupted_dispatch_is_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as root:
            task = {'id': 'b' * 32, 'body': {'action': 'create'}}
            with patch('rmm_bloodhound_broker.bloodhound_api', side_effect=RuntimeError('process ended')):
                with self.assertRaises(RuntimeError):
                    execute_workspace_request(root, task)
            with patch('rmm_bloodhound_broker.bloodhound_api') as execute:
                self.assertEqual(execute_workspace_request(root, task)[0], 409)
                execute.assert_not_called()

    def test_queue_capacity_and_late_reply(self):
        broker = BloodHoundBroker()
        for _ in range(8):
            self.assertEqual(broker.submit('alice', {'action': 'preflight'})[0], 202)
        self.assertEqual(broker.submit('alice', {'action': 'preflight'})[0], 503)
        request_id = next(iter(broker.requests))
        broker.requests[request_id]['created'] -= 301
        self.assertEqual(broker.result('alice', request_id)[0], 409)
        broker.complete('alice', request_id, {'status': 200, 'payload': {'ready': True}})
        self.assertEqual(broker.result('alice', request_id), (200, {'ready': True}))

    def test_central_tool_to_workspace_round_trip(self):
        broker = BloodHoundBroker()
        handler = object.__new__(server_rmm.RMMHandler)
        handler.server_instance = SimpleNamespace(storage_mode='workspace', bloodhound_broker=broker)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value='alice')
        handler._json = Mock()
        with tempfile.TemporaryDirectory() as root:
            service = object.__new__(WorkspaceService)
            service._running = True
            service.store = SimpleNamespace(data_dir=Path(root))
            def upstream(method, path, body=None, **kwargs):
                if method == 'GET':
                    return 200, {'requests': broker.pending('alice')}, 'application/json'
                code, payload = broker.complete('alice', path.rsplit('/', 1)[1], body)
                return code, payload, 'application/json'
            service.upstream = SimpleNamespace(request=upstream)
            handler._handle_api_post('/api/v1/bloodhound', {'action': 'queries'})
            self.assertEqual(handler._json.call_args.args[0], 202)
            request_id = handler._json.call_args.args[1]['workspace_request_id']
            service.bloodhound_sync_once()
            handler._handle_api_post('/api/v1/bloodhound', {'action': 'result', 'run_id': request_id})
            code, payload = handler._json.call_args.args
            self.assertEqual(code, 200)
            self.assertIn('candidate_path', [q['id'] for q in payload['queries']])
            self.assertTrue((Path(root) / 'bloodhound' / 'relay-results' / (request_id + '.json')).exists())

    def test_broker_transport_requires_workspace_auth(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler.server_instance = SimpleNamespace(bloodhound_broker=Mock())
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler._handle_api_get('/api/v1/workspace/bloodhound/requests', {})
        self.assertEqual(handler._json.call_args.args[0], 403)
        handler.server_instance.bloodhound_broker.pending.assert_not_called()
