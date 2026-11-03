"""BloodHound workflow transport and storage-owner contracts."""
import contextlib
import asyncio
import io
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
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
            self.assertEqual(set(definition.inputSchema['properties']), {'action', 'run_id', 'options'})
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
            launch.assert_called_once_with()
            self.assertEqual(bloodhound_api('/tmp/unused-ui', {'action': 'ui', 'options': {'url': 'http://evil'}})[0], 400)
            self.assertEqual(launch.call_count, 1)

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
        for key in ('RMM_BLOODHOUND_TOKEN_ID', 'RMM_BLOODHOUND_TOKEN_KEY'):
            definition = rmm_settings.FIELDS[key]
            self.assertTrue(definition['secret'])
            self.assertEqual(set(definition['scopes']), {'server', 'workspace'})
