"""BloodHound workflow transport and storage-owner contracts."""
import contextlib
import asyncio
import io
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from rmm_cli import RmmApiClient, build_parser
from rmm_tools import OPENAI_TOOLS, TOOL_HANDLERS, tool_bloodhound
import rmm_settings
import server_rmm
from workspace_service import WorkspaceService


class BloodHoundSurfaceTests(unittest.TestCase):
    def test_interactive_cli_dispatch_stays_local_and_preserves_json(self):
        from rmm_cli import dispatch_interactive_bloodhound
        client = Mock()
        client.bloodhound.return_value = (202, {'workspace_request_id': 'abc'})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            dispatch_interactive_bloodhound(client, {}, 'bloodhound library_search --options-json \'{"text":"Tier Zero"}\'')
        client.bloodhound.assert_called_once_with('library_search', '', {'text': 'Tier Zero'})
        self.assertEqual(json.loads(output.getvalue())['workspace_request_id'], 'abc')
        client.reset_mock()
        for line in ('bloodhound nonsense', 'bloodhound query --options-json []', 'bloodhound "unfinished'):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                dispatch_interactive_bloodhound(client, {}, line)
        client.bloodhound.assert_not_called()
        client.queue_command.assert_not_called()

    def test_registered_mcp_tool_invokes_shared_transport(self):
        if importlib.util.find_spec('mcp') is None:
            self.skipTest('Optional MCP SDK is not installed; registered-tool integration requires it')
        import mcp_rmm_server as mcp_server
        client = Mock()
        client.bloodhound.return_value = (202, {'workspace_request_id': 'abc'})
        async def invoke():
            definitions = await mcp_server.mcp.list_tools()
            definition = next(tool for tool in definitions if tool.name == 'bloodhound')
            schema = getattr(definition, 'input_schema', None) or getattr(definition, 'inputSchema', None)
            self.assertEqual(set(schema['properties']), {'action', 'run_id', 'options'})
            return await mcp_server.mcp.call_tool('bloodhound', {'action': 'library_search', 'options': {'text': 'delegation'}})
        with patch.object(mcp_server, '_client', return_value=client):
            result = asyncio.run(invoke())
        client.bloodhound.assert_called_once_with('library_search', '', {'text': 'delegation'})
        self.assertIn('workspace_request_id', str(result))

    def test_library_discovery_needs_no_runtime_or_workflow_and_stays_reference_only(self):
        from rmm_bloodhound import bloodhound_api, render_query, WorkflowError
        with patch('rmm_bloodhound.BloodHoundWorkflow') as workflow, patch('rmm_exegol_runtime.get_runtime') as runtime:
            code, result = bloodhound_api('/tmp/unused-library', {'action': 'library_search', 'options': {'text': 'delegation', 'limit': 2}})
            self.assertEqual(code, 200)
            self.assertLessEqual(len(result['entries']), 2)
            self.assertTrue(result['entries'])
            entry_id = result['entries'][0]['id']
            self.assertNotIn('query', result['entries'][0])
            code, detail = bloodhound_api('/tmp/unused-library', {'action': 'library_get', 'options': {'id': entry_id}})
            self.assertEqual(code, 200)
            self.assertTrue(detail['entry']['query'])
            self.assertFalse(detail['entry']['compatibility']['upstream_executable'])
            with self.assertRaises(WorkflowError):
                render_query(entry_id, {})
            self.assertEqual(bloodhound_api('/tmp/unused-library', {'action': 'library_search', 'options': {'limit': 1000}})[0], 400)
            workflow.assert_not_called()
            runtime.assert_not_called()
        schema = next(t['function'] for t in OPENAI_TOOLS if t['function']['name'] == 'bloodhound')
        for action in ('library_search', 'library_get'):
            self.assertEqual(build_parser().parse_args(['bloodhound', action]).action, action)
            self.assertIn(action, schema['parameters']['properties']['action']['enum'])

    def test_health_is_read_only_and_ui_waits_for_runtime(self):
        from rmm_bloodhound import bloodhound_api
        with patch('rmm_exegol_runtime.get_runtime') as getter, patch('rmm_bloodhound_proxy.open_ui') as launch:
            runtime = getter.return_value
            runtime.status.return_value = {'state': 'checking', 'exegol_status': 'checking'}
            runtime.ensure.return_value = runtime.status.return_value
            self.assertEqual(bloodhound_api('/tmp/unused-ui', {'action': 'health'})[0], 200)
            runtime.ensure.assert_not_called()
            self.assertEqual(bloodhound_api('/tmp/unused-ui', {'action': 'ui'})[0], 202)
            launch.assert_not_called()
            runtime.ensure.return_value = {'state': 'uncertain', 'error': 'bloodhound_startup_uncertain'}
            code, payload = bloodhound_api('/tmp/unused-ui', {'action': 'ui'})
            self.assertEqual(code, 503)
            self.assertEqual(payload['error'], 'bloodhound_startup_uncertain')
            launch.assert_not_called()

    def test_missing_exegol_blocks_work_but_not_health(self):
        from rmm_bloodhound import bloodhound_api
        with patch('rmm_exegol_runtime.get_runtime') as getter:
            getter.return_value.status.return_value = {'state': 'blocked', 'exegol_status': 'unavailable'}
            for action in ('create', 'collect', 'import', 'query', 'verify'):
                self.assertEqual(bloodhound_api('/tmp/unused-ui', {'action': action})[0], 503)
            self.assertEqual(bloodhound_api('/tmp/unused-ui', {'action': 'health'})[0], 200)

    def test_ui_action_is_available_across_operator_surfaces(self):
        args = build_parser().parse_args(['bloodhound', 'ui'])
        self.assertEqual(args.action, 'ui')
        schema = next(t['function'] for t in OPENAI_TOOLS if t['function']['name'] == 'bloodhound')
        self.assertIn('ui', schema['parameters']['properties']['action']['enum'])
        from rmm_bloodhound import bloodhound_api
        with patch('rmm_exegol_runtime.get_runtime') as runtime, patch('rmm_bloodhound_proxy.open_ui', return_value=(200, {'url': 'http://localhost:1031/launch'})) as launch:
            runtime.return_value.ensure.return_value = {'state': 'ready', 'exegol_status': 'ready'}
            self.assertEqual(bloodhound_api('/tmp/unused-ui', {'action': 'ui'})[0], 200)
            launch.assert_called_once_with('/tmp/unused-ui')
            self.assertEqual(bloodhound_api('/tmp/unused-ui', {'action': 'ui', 'options': {'url': 'http://evil'}})[0], 400)
            self.assertEqual(launch.call_count, 1)

    def test_reset_action_is_available_across_operator_surfaces(self):
        args = build_parser().parse_args(['bloodhound', 'reset'])
        self.assertEqual(args.action, 'reset')
        schema = next(t['function'] for t in OPENAI_TOOLS if t['function']['name'] == 'bloodhound')
        self.assertIn('reset', schema['parameters']['properties']['action']['enum'])
        from rmm_bloodhound import bloodhound_api
        with patch('rmm_exegol_runtime.get_runtime') as getter:
            runtime = getter.return_value
            runtime.reset.return_value = {'state': 'blocked', 'ready': False}
            code, payload = bloodhound_api('/tmp/unused-reset', {'action': 'reset'})
            self.assertEqual(code, 200)
            self.assertTrue(payload['ok'])
            self.assertEqual(payload['runtime']['state'], 'blocked')
            runtime.reset.assert_called_once_with()
            self.assertEqual(bloodhound_api('/tmp/unused-reset', {'action': 'reset', 'options': {'force': True}})[0], 400)
            self.assertEqual(bloodhound_api('/tmp/unused-reset', {'action': 'reset', 'run_id': 'abc'})[0], 400)

    def test_reset_refuses_outside_uncertain_state(self):
        from rmm_exegol_runtime import RuntimeErrorCode
        from rmm_bloodhound import bloodhound_api
        with patch('rmm_exegol_runtime.get_runtime') as getter:
            runtime = getter.return_value
            runtime.reset.side_effect = RuntimeErrorCode('bloodhound_already_ready')
            runtime.status.return_value = {'state': 'ready', 'ready': True}
            code, payload = bloodhound_api('/tmp/unused-reset', {'action': 'reset'})
            self.assertEqual(code, 409)
            self.assertEqual(payload['error'], 'bloodhound_already_ready')
            self.assertEqual(payload['runtime']['state'], 'ready')

    def test_client_sends_structured_options_to_fixed_endpoint(self):
        client = RmmApiClient('http://localhost', 'test-token')
        client.request = Mock(return_value=(202, {'run_id': 'abc'}))
        options = {'query_id': 'identity', 'parameters': {'sid': 'S-1-5-21-1-2-3-1000'}}
        client.bloodhound('query', 'abc', options)
        client.request.assert_called_once_with('POST', '/api/v1/bloodhound', {
            'action': 'query', 'run_id': 'abc', 'options': options,
        })

    def test_tool_preserves_async_acceptance_and_failure(self):
        client = Mock()
        client.bloodhound.return_value = (202, {'run_id': 'abc'})
        result = json.loads(tool_bloodhound(client, 'collect', 'abc', {}))
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 202)
        client.bloodhound.return_value = (409, {'error': 'wrong_owner'})
        self.assertFalse(json.loads(tool_bloodhound(client))['ok'])
        self.assertIn('bloodhound', TOOL_HANDLERS)
        self.assertEqual(sum(tool['function']['name'] == 'bloodhound' for tool in OPENAI_TOOLS), 1)

    def test_cli_dispatches_options_and_prints_result(self):
        args = build_parser().parse_args(['bloodhound', 'query', '--run-id', 'abc', '--options-json', '{"query_id":"identity"}'])
        client = Mock()
        client.bloodhound.return_value = (200, {'result': 'verified'})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            args.func(client, {}, args)
        self.assertEqual(json.loads(output.getvalue()), {'result': 'verified'})
        client.bloodhound.assert_called_once_with('query', 'abc', {'query_id': 'identity'})

    def handler(self, storage_mode='server', workspace=None):
        handler = object.__new__(server_rmm.RMMHandler)
        handler.server_instance = SimpleNamespace(storage_mode=storage_mode)
        handler._api_authorized = Mock(return_value=True)
        handler._api_unauthorized = Mock()
        handler._workspace_scope = Mock(return_value=workspace)
        handler._json = Mock()
        return handler

    def test_central_rejects_workspace_requests_before_work(self):
        for mode, scope in (('workspace', None), ('server', 'workspace-a')):
            with self.subTest(mode=mode, scope=scope), patch('server_rmm.bloodhound_api') as api:
                handler = self.handler(mode, scope)
                self.assertTrue(handler._handle_api_post('/api/v1/bloodhound', {'action': 'preflight'}))
                self.assertEqual(handler._json.call_args.args[0], 409)
                api.assert_not_called()

    def test_server_uses_owned_root_and_preserves_status(self):
        with patch('server_rmm.bloodhound_api', return_value=(202, {'run_id': 'abc'})) as api, patch('server_rmm.LOG_DIR', '/tmp/owner'):
            handler = self.handler()
            body = {'action': 'collect', 'run_id': 'abc'}
            handler._handle_api_post('/api/v1/bloodhound', body)
            api.assert_called_once_with('/tmp/owner/bloodhound', body)
            handler._json.assert_called_once_with(202, {'run_id': 'abc'})

    def test_unauthorized_request_never_reaches_workflow(self):
        with patch('server_rmm.bloodhound_api') as api:
            handler = self.handler()
            handler._api_authorized.return_value = False
            handler._handle_api_post('/api/v1/bloodhound', {'action': 'collect'})
            handler._api_unauthorized.assert_called_once()
            api.assert_not_called()

    def test_workspace_owns_local_workflow_without_proxy(self):
        service = object.__new__(WorkspaceService)
        service.store = SimpleNamespace(data_dir=Path('/tmp/workspace-a'))
        service._proxy = Mock()
        with patch('workspace_service.bloodhound_api', return_value=(200, {'ready': True})) as api:
            body = {'action': 'preflight'}
            result = service.post_json('/api/v1/bloodhound', body)
            self.assertEqual(result, (200, {'ready': True}, 'application/json'))
            api.assert_called_once_with(Path('/tmp/workspace-a/bloodhound'), body)
            service._proxy.assert_not_called()

    def test_credentials_are_secret_owner_scoped_settings(self):
        for key in ('RMM_BLOODHOUND_TOKEN_ID', 'RMM_BLOODHOUND_TOKEN_KEY',
                    'RMM_BLOODHOUND_ADMIN_PASSWORD'):
            definition = rmm_settings.FIELDS[key]
            self.assertTrue(definition['secret'])
            self.assertEqual(set(definition['scopes']), {'server', 'workspace'})

    def test_reset_ce_action_validates_options_and_clears_credentials(self):
        from rmm_bloodhound import bloodhound_api, _CE_AUTH_LOCK, _CE_AUTH_STATE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'bloodhound'
            root.mkdir()
            store = rmm_settings.SettingsStore(Path(directory) / 'settings.json', 'workspace', environment={})
            with patch.object(rmm_settings, '_active', store), \
                    patch('rmm_exegol_runtime.get_runtime') as getter:
                runtime = getter.return_value
                runtime.reset_ce.return_value = {'state': 'blocked', 'error': None}
                store.update({'settings': {'RMM_BLOODHOUND_TOKEN_ID': 'x', 'RMM_BLOODHOUND_TOKEN_KEY': 'y'}})
                _CE_AUTH_STATE['attempted'] = True
                code, payload = bloodhound_api(root, {'action': 'reset_ce', 'options': {'clear_graph': True}})
                self.assertEqual(code, 200)
                runtime.reset_ce.assert_called_once_with(True)
                self.assertFalse(rmm_settings.getenv('RMM_BLOODHOUND_TOKEN_ID'))
                self.assertFalse(_CE_AUTH_STATE['attempted'])
                for invalid in ({'clear_graph': 'yes'}, {'unexpected': 1}, {'clear_graph': True, 'other': 0}):
                    with self.subTest(invalid=invalid):
                        code, payload = bloodhound_api(root, {'action': 'reset_ce', 'options': invalid})
                        self.assertEqual((code, payload['error']), (400, 'invalid_reset_ce_options'))
                code, payload = bloodhound_api(root, {'action': 'reset_ce', 'run_id': 'abc'})
                self.assertEqual(code, 400)

    def test_auth_action_reports_status_without_ever_exposing_values(self):
        from rmm_bloodhound import bloodhound_api
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'bloodhound'
            root.mkdir()
            store = rmm_settings.SettingsStore(Path(directory) / 'settings.json', 'workspace', environment={})
            with patch.object(rmm_settings, '_active', store):
                with patch('rmm_bloodhound.bootstrap_ce_credentials', return_value='ce_initial_password_unavailable') as bootstrap:
                    code, payload = bloodhound_api(root, {'action': 'auth'})
                    self.assertEqual(code, 503)
                    self.assertEqual(payload['error'], 'ce_initial_password_unavailable')
                    # Only bounded status fields; no credential value can appear.
                    self.assertEqual(set(payload), {'ok', 'auth', 'error'})
                store.update({'settings': {'RMM_BLOODHOUND_TOKEN_ID': 'id', 'RMM_BLOODHOUND_TOKEN_KEY': 'key'}})
                before = bootstrap.call_count
                with patch('rmm_bloodhound.BloodHoundClient') as client:
                    code, payload = bloodhound_api(root, {'action': 'auth'})
                    client.return_value.request.assert_called_once_with('GET', '/api/v2/file-upload?limit=1')
                self.assertEqual((code, payload['auth']), (200, 'already_configured'))
                self.assertEqual(bootstrap.call_count, before)

    def test_auth_repairs_rejected_token_but_does_not_rotate_on_transport_failure(self):
        from rmm_bloodhound import bloodhound_api, WorkflowError
        with patch('rmm_bloodhound.ce_credentials_configured', return_value=True), \
                patch('rmm_bloodhound.BloodHoundClient') as client, \
                patch('rmm_bloodhound.bootstrap_ce_credentials', return_value='bootstrapped') as bootstrap:
            client.return_value.request.side_effect = WorkflowError('bloodhound_http_401')
            code, payload = bloodhound_api('/unused', {'action': 'auth'})
            self.assertEqual((code, payload['auth']), (200, 'bootstrapped'))
            bootstrap.assert_called_once()
            bootstrap.reset_mock()
            client.return_value.request.side_effect = WorkflowError('bloodhound_transport_uncertain')
            code, payload = bloodhound_api('/unused', {'action': 'auth'})
            self.assertEqual((code, payload['error']), (503, 'bloodhound_transport_uncertain'))
            bootstrap.assert_not_called()

    def test_failed_graph_reset_invalidates_credentials_and_returns_failure(self):
        from rmm_bloodhound import bloodhound_api
        with tempfile.TemporaryDirectory() as directory:
            store = rmm_settings.SettingsStore(Path(directory) / 'settings.json', 'workspace', environment={})
            store.update({'settings': {'RMM_BLOODHOUND_TOKEN_ID': 'id',
                'RMM_BLOODHOUND_PENDING_PASSWORD': 'pending'}})
            with patch.object(rmm_settings, '_active', store), patch('rmm_exegol_runtime.get_runtime') as getter:
                getter.return_value.reset_ce.return_value = {'error': 'graph_wipe_failed', 'credentials_invalidated': True}
                code, payload = bloodhound_api(Path(directory) / 'bloodhound', {'action': 'reset_ce'})
                self.assertEqual(code, 503)
                self.assertFalse(payload['ok'])
                self.assertFalse(rmm_settings.getenv('RMM_BLOODHOUND_TOKEN_ID'))
                self.assertFalse(rmm_settings.getenv('RMM_BLOODHOUND_PENDING_PASSWORD'))

    def test_bootstrap_storage_failure_is_bounded(self):
        from rmm_bloodhound import bootstrap_ce_credentials
        with tempfile.TemporaryDirectory() as directory, \
                patch('rmm_exegol_runtime.configuration') as configuration, \
                patch('rmm_bloodhound.active_store', side_effect=OSError('secret path')):
            configuration.return_value = (None, None, None, SimpleNamespace(geturl=lambda: 'http://127.0.0.1:1030'))
            self.assertEqual(bootstrap_ce_credentials(directory), 'ce_credentials_storage_failed')

    def test_bootstrap_refuses_reset_in_progress_or_uncertain_without_mutation(self):
        from rmm_bloodhound import bootstrap_ce_credentials
        for snapshot, code in (({'state': 'resetting'}, 'bloodhound_reset_in_progress'),
                ({'state': 'uncertain', 'error': 'ce_reset_outcome_uncertain'}, 'ce_reset_outcome_uncertain')):
            with self.subTest(code=code), patch('rmm_exegol_runtime.get_runtime') as runtime, \
                    patch('rmm_bloodhound._bootstrap_ce_credentials') as bootstrap, \
                    patch('rmm_bloodhound.active_store') as settings:
                runtime.return_value.status.return_value = snapshot
                self.assertEqual(bootstrap_ce_credentials('/unused'), code)
            bootstrap.assert_not_called()
            settings.assert_not_called()

    def test_bootstrap_refuses_to_claim_repair_of_environment_token(self):
        from rmm_bloodhound import bootstrap_ce_credentials
        for key in ('RMM_BLOODHOUND_TOKEN_ID', 'RMM_BLOODHOUND_TOKEN_KEY',
                    'RMM_BLOODHOUND_ADMIN_PASSWORD', 'RMM_BLOODHOUND_PENDING_PASSWORD'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                store = rmm_settings.SettingsStore(Path(directory) / 'settings.json', 'workspace',
                    environment={key: 'overridden'})
                with patch.object(rmm_settings, '_active', store), \
                        patch('rmm_exegol_runtime.configuration') as configuration, \
                        patch('rmm_bloodhound_ce_auth.bootstrap_credentials') as bootstrap:
                    configuration.return_value = (None, None, None, SimpleNamespace(geturl=lambda: 'http://127.0.0.1:1030'))
                    self.assertEqual(bootstrap_ce_credentials(directory), 'ce_credentials_environment_override')
                    bootstrap.assert_not_called()

    def test_reset_storage_failure_is_bounded(self):
        from rmm_bloodhound import bloodhound_api
        with patch('rmm_bloodhound.reset_ce_credentials', side_effect=OSError('secret path')):
            self.assertEqual(bloodhound_api('/unused', {'action': 'reset_ce'}),
                (503, {'ok': False, 'error': 'ce_credentials_storage_failed'}))

    def test_reset_during_auth_or_reset_does_not_queue_another_destructive_action(self):
        import threading
        from rmm_bloodhound import bloodhound_api, _CE_AUTH_LOCK
        held = threading.Event()
        release = threading.Event()
        def hold_auth():
            with _CE_AUTH_LOCK:
                held.set()
                release.wait(5)
        worker = threading.Thread(target=hold_auth)
        worker.start()
        try:
            self.assertTrue(held.wait(2))
            with patch('rmm_exegol_runtime.get_runtime') as getter:
                self.assertEqual(bloodhound_api('/unused', {'action': 'reset_ce'}),
                    (409, {'ok': False, 'error': 'bloodhound_auth_or_reset_in_progress'}))
                getter.assert_not_called()
        finally:
            release.set()
            worker.join(2)

    def test_automatic_auth_scheduler_does_not_block_or_enqueue_behind_auth(self):
        import threading
        from rmm_bloodhound import _schedule_ce_auth_bootstrap, _CE_AUTH_LOCK, _CE_AUTH_STATE
        held, release, returned = threading.Event(), threading.Event(), threading.Event()
        def hold_auth():
            with _CE_AUTH_LOCK:
                held.set()
                release.wait(5)
        def schedule():
            _schedule_ce_auth_bootstrap('/unused')
            returned.set()
        owner = threading.Thread(target=hold_auth)
        caller = threading.Thread(target=schedule)
        owner.start()
        try:
            self.assertTrue(held.wait(2))
            with patch.dict(_CE_AUTH_STATE, {'attempted': False}), \
                    patch('rmm_bloodhound.threading.Thread') as background:
                caller.start()
                self.assertTrue(returned.wait(1), 'health scheduler blocked behind authentication')
                background.assert_not_called()
                self.assertFalse(_CE_AUTH_STATE['attempted'])
        finally:
            release.set()
            owner.join(2)
            if caller.ident is not None:
                caller.join(2)

    def test_ce_operations_refuse_active_and_uncertain_reset(self):
        from rmm_bloodhound import bloodhound_api
        for snapshot in ({'state': 'resetting', 'exegol_status': 'ready'},
                         {'state': 'uncertain', 'error': 'ce_reset_outcome_uncertain', 'exegol_status': 'ready'}):
            for action in ('auth', 'create', 'collect', 'import', 'verify', 'query'):
                with self.subTest(snapshot=snapshot, action=action), \
                        patch('rmm_exegol_runtime.get_runtime') as getter, \
                        patch('rmm_bloodhound.BloodHoundClient') as client, \
                        patch('rmm_bloodhound.BloodHoundWorkflow') as workflow:
                    getter.return_value.status.return_value = snapshot
                    code, payload = bloodhound_api('/unused', {'action': action})
                    self.assertEqual(code, 503)
                    self.assertIn(payload['error'], ('bloodhound_reset_in_progress', 'ce_reset_outcome_uncertain'))
                    client.assert_not_called()
                    workflow.assert_not_called()
