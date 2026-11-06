"""Workspace ownership survives artifact generation and agent registration."""
import base64
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import server_rmm
from rmm_workspace import WorkspaceRegistry
import test_server_workspace as workspace_fixtures


class AgentWorkspaceBindingTests(unittest.TestCase):
    def registry_server(self, owners=None):
        return SimpleNamespace(workspace_registry=WorkspaceRegistry(
            owners if owners is not None else {'alice': 'alice-secret', 'bob': 'bob-secret'}))

    def test_artifact_owner_uses_authenticated_scope(self):
        server = self.registry_server()
        self.assertEqual(server_rmm.deployment_workspace_id(server, scope='alice'), 'alice')
        self.assertEqual(server_rmm.deployment_workspace_id(server, 'alice', scope='alice'), 'alice')
        self.assertEqual(server_rmm.deployment_workspace_id(server, 'bob'), 'bob')
        for requested, scope in [('bob', 'alice'), ('unknown', None), ('../alice', None), ('', None)]:
            with self.subTest(requested=requested, scope=scope), self.assertRaises(ValueError):
                server_rmm.deployment_workspace_id(server, requested, scope)
        self.assertEqual(server_rmm.deployment_workspace_id(self.registry_server({'alice': 'secret'})), 'alice')
        self.assertEqual(server_rmm.deployment_workspace_id(self.registry_server({})), '')

    def test_powershell_render_binds_workspace_without_operator_token(self):
        template = (Path(server_rmm.__file__).parent / 'agent-powershell' / 'client_rmm.ps1').read_text()
        rendered = server_rmm.render_configured_client_script(template, 'https://beacon.example.test',
                                                              'fixture-beacon', workspace_id='alice')
        self.assertIn("$workspaceId = 'alice'", rendered)
        self.assertIn('$env:RMM_AGENT_WORKSPACE_ID', rendered)
        self.assertIn('&workspace_id=$([uri]::EscapeDataString($workspaceId))', rendered)
        self.assertNotIn('alice-secret', rendered)
        self.assertNotIn('RMM_WORKSPACE_TOKEN', rendered)
        with self.assertRaises(ValueError):
            server_rmm.render_configured_client_script(template, 'https://beacon.example.test',
                                                       'fixture-beacon', workspace_id='../alice')

    def test_go_artifact_embeds_owner_and_launcher_uses_same_binding(self):
        compiler = Mock()
        def compile_stub(command, **kwargs):
            Path(command[command.index('-o') + 1]).write_bytes(b'fixture binary')
            return SimpleNamespace(returncode=0)
        compiler.run.side_effect = compile_stub
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, 'GO_AGENT_BUILD_DIR', directory), \
                patch.object(server_rmm, 'LOG_DIR', directory), \
                patch.object(server_rmm.shutil, 'which', return_value='go'):
            result = server_rmm.build_go_agent('windows', 'amd64',
                agent_config={'workspace_id': 'alice', 'base_url': 'https://beacon.example.test',
                              'beacon_secret': 'fixture-beacon'}, process_owner=compiler)
            flags = compiler.run.call_args.args[0]
            ldflags = shlex.split(flags[flags.index('-ldflags') + 1])
            embedded = 'main.defaultWorkspaceIDB64=' + base64.b64encode(b'alice').decode()
            self.assertIn(embedded, ldflags)
            self.assertEqual(result['workspace_id'], 'alice')
            self.assertNotIn('alice-secret', str(result))
            with zipfile.ZipFile(result['path']) as archive:
                launcher = archive.read('run-agent.ps1').decode()
                self.assertIn("$env:RMM_AGENT_WORKSPACE_ID = 'alice'", launcher)
                self.assertNotIn('RMM_WORKSPACE_TOKEN', launcher)

    def handler(self, scope='alice'):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=scope)
        handler._json = Mock()
        handler.server_instance = self.registry_server()
        handler.server_instance.beacon_public_url = 'https://beacon.example.test'
        handler.server_instance.builds = Mock()
        return handler

    def test_go_build_api_injects_authenticated_owner(self):
        handler = self.handler()
        with patch.object(server_rmm, 'build_go_agent', return_value={'workspace_id': 'alice'}) as build:
            handler._handle_api_post('/api/v1/agent/go/build', {'goos': 'windows', 'goarch': 'amd64'})
        self.assertEqual(handler._json.call_args.args[0], 200)
        self.assertEqual(build.call_args.kwargs['agent_config']['workspace_id'], 'alice')

    def test_go_build_api_rejects_cross_workspace_request_before_compiling(self):
        handler = self.handler()
        with patch.object(server_rmm, 'build_go_agent') as build:
            handler._handle_api_post('/api/v1/agent/go/build', {'goos': 'windows', 'goarch': 'amd64',
                                                            'config': {'workspace_id': 'bob'}})
        self.assertEqual(handler._json.call_args.args[0], 400)
        self.assertEqual(handler._json.call_args.args[1]['error'], 'invalid_workspace')
        build.assert_not_called()

    def test_script_deployment_api_injects_owner_into_artifact_and_metadata(self):
        handler = self.handler()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, 'AGENT_DEPLOY_DIR', directory), \
                patch.object(server_rmm, 'BEACON_SECRET', 'fixture-beacon'):
            handler._handle_api_post('/api/v1/agent/deploy/script', {})
            code, result = handler._json.call_args.args
            self.assertEqual(code, 200)
            self.assertEqual(result['workspace_id'], 'alice')
            self.assertIn("$workspaceId = 'alice'", Path(result['path']).read_text())
            self.assertNotIn('alice-secret', str(result))

    def test_script_deployment_rejects_cross_workspace_request(self):
        handler = self.handler()
        with patch.object(server_rmm, 'render_configured_client_script') as render:
            handler._handle_api_post('/api/v1/agent/deploy/script', {'workspace_id': 'bob'})
        self.assertEqual(handler._json.call_args.args[0], 400)
        render.assert_not_called()


class AgentRegistrationWorkspaceTests(unittest.TestCase):
    def server(self):
        server = workspace_fixtures.ServerWorkspaceTests().make_server()
        server.killed_sessions = set()
        server._persisted_config_from_history_meta = Mock(return_value=None)
        server.backfill_download_artifacts = Mock()
        server._apply_history_meta_to_session = Mock()
        server._rehydrate_session_events_from_history = Mock(return_value=0)
        server._history_write_meta = Mock()
        server.save_session = Mock()
        return server

    def test_explicit_owner_is_assigned_before_history_and_persistence(self):
        server = self.server()
        owners_at_history = []
        server._history_write_meta.side_effect = lambda session: owners_at_history.append(session.workspace_id)
        self.assertTrue(server.register_session('new-agent-session', 'host', 'user', workspace_id='bob'))
        self.assertEqual(owners_at_history, ['bob'])
        self.assertEqual(server.save_session.call_args.args[0].workspace_id, 'bob')
        self.assertIsNone(server.resolve_workspace_session('alice', 'new-agent-session'))
        self.assertIsNotNone(server.resolve_workspace_session('bob', 'new-agent-session'))

    def test_live_owner_cannot_be_changed_by_registration(self):
        server = self.server()
        server.register_session('new-agent-session', 'host', 'user', workspace_id='alice')
        with self.assertRaises(ValueError):
            server.register_session('new-agent-session', 'other-host', 'user', workspace_id='bob')
        self.assertEqual(server.sessions['new-agent-session'].workspace_id, 'alice')
        self.assertEqual(server.sessions['new-agent-session'].hostname, 'host')

    def test_persisted_owner_cannot_be_changed_by_registration(self):
        server = self.server()
        server._persisted_session_records['new-agent-session'] = {'workspace_id': 'alice'}
        with self.assertRaises(ValueError):
            server.register_session('new-agent-session', 'host', 'user', workspace_id='bob')
        self.assertNotIn('new-agent-session', server.sessions)
        server.save_session.assert_not_called()

    def test_unknown_or_invalid_owner_is_rejected_before_session_creation(self):
        server = self.server()
        for workspace_id in ['unknown', '../alice']:
            with self.subTest(workspace_id=workspace_id), self.assertRaises(ValueError):
                server.register_session('new-agent-session', 'host', 'user', workspace_id=workspace_id)
        self.assertEqual(server.sessions, {})

    def test_legacy_unassigned_session_can_receive_explicit_owner(self):
        server = self.server()
        server.register_session('new-agent-session', 'host', 'user')
        self.assertIsNone(server.sessions['new-agent-session'].workspace_id)
        server.register_session('new-agent-session', 'host', 'user', workspace_id='bob')
        self.assertEqual(server.sessions['new-agent-session'].workspace_id, 'bob')
        self.assertEqual(server.save_session.call_args.args[0].workspace_id, 'bob')

    def test_http_register_forwards_binding_and_rejects_unknown_owner(self):
        server = self.server()
        for workspace_id, expected_code in [('bob', 200), ('unknown', 403)]:
            handler = object.__new__(server_rmm.RMMHandler)
            handler.path = '/register?id=new-agent-session&h=host&u=user&workspace_id=' + workspace_id
            handler.client_address = ('127.0.0.1', 12345)
            handler._path_allowed = Mock(return_value=True)
            handler._beacon_authorized = Mock(return_value=True)
            handler._serve_web = Mock(return_value=False)
            handler._respond = Mock()
            handler.server_instance = server
            handler.do_GET()
            self.assertEqual(handler._respond.call_args.args[0], expected_code)
        self.assertEqual(server.sessions['new-agent-session'].workspace_id, 'bob')


if __name__ == '__main__':
    unittest.main()
