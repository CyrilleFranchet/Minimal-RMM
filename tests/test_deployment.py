"""Deployment presets share services while isolating credentials and state."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rmm_deployment import child_environment, commands, prepare, run_services, central_compose, deployment_paths, import_rclone
from rmm_settings import SettingsStore


class DeploymentTests(unittest.TestCase):
    def test_children_receive_only_their_scoped_setting_overrides(self):
        values = {'RMM_API_TOKEN': 'admin-secret', 'RMM_BEACON_SECRET': 'beacon-secret',
                  'RMM_WORKSPACES': '{"alice":"registry-secret"}',
                  'RMM_WORKSPACE_ID': 'alice', 'RMM_WORKSPACE_TOKEN': 'workspace-secret',
                  'RMM_WORKSPACE_LOCAL_TOKEN': 'browser-secret', 'RMM_WORKSPACE_PORT': '9999',
                  'RMM_EXEGOL_MCP_URL': 'http://mcp.example/mcp',
                  'RMM_CONFIG_FILE': '/wrong/settings.json', 'PATH': '/usr/bin'}
        with patch.dict('os.environ', values, clear=True):
            workspace = child_environment(['python', '/repo/workspace_service.py'], {}, allow_env_overrides=True)
            central = child_environment(['python', '/repo/server_rmm.py'], {'RMM_STORAGE_MODE': 'workspace'}, allow_env_overrides=True)
        for key in ('RMM_API_TOKEN', 'RMM_BEACON_SECRET', 'RMM_WORKSPACES', 'RMM_CONFIG_FILE'):
            self.assertNotIn(key, workspace)
        self.assertEqual(workspace['RMM_WORKSPACE_TOKEN'], 'workspace-secret')
        self.assertEqual(workspace['RMM_WORKSPACE_PORT'], '9999')
        self.assertEqual(workspace['RMM_EXEGOL_MCP_URL'], values['RMM_EXEGOL_MCP_URL'])
        for key in ('RMM_WORKSPACE_ID', 'RMM_WORKSPACE_TOKEN', 'RMM_WORKSPACE_LOCAL_TOKEN', 'RMM_WORKSPACE_PORT'):
            self.assertNotIn(key, central)
        self.assertEqual(central['RMM_API_TOKEN'], 'admin-secret')
        self.assertEqual(central['RMM_WORKSPACES'], values['RMM_WORKSPACES'])
        self.assertEqual(central['RMM_STORAGE_MODE'], 'workspace')
        self.assertEqual(workspace['PATH'], '/usr/bin')

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'deployment'

    def test_single_credentials_match_without_admin_token_reuse(self):
        prepare(self.root, 'single', None, 'http://127.0.0.1:8080')
        central = SettingsStore(self.root / 'central/settings.json', 'server', environment={}).effective
        workspace = SettingsStore(self.root / 'workspaces/default/settings.json', 'workspace', environment={}).effective
        self.assertEqual(central['RMM_WORKSPACES']['default'], workspace['RMM_WORKSPACE_TOKEN'])
        secrets = [central['RMM_API_TOKEN'], central['RMM_BEACON_SECRET'],
                   workspace['RMM_WORKSPACE_TOKEN'], workspace['RMM_WORKSPACE_LOCAL_TOKEN']]
        self.assertEqual(len(set(secrets)), 4)
        self.assertTrue(workspace['RMM_WORKSPACE_AUTO_CLAIM'])
        for path in self.root.rglob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(value in (self.root / 'deployment.json').read_text() for value in secrets))
        plans = commands(self.root, 'single')
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0][1]['RMM_STORAGE_MODE'], 'workspace')
        self.assertIn('workspace_service.py', plans[1][0][1])
        self.assertEqual(commands(self.root, 'workspace'), [plans[1]])

    def test_multi_requires_explicit_selection_and_disables_auto_claim(self):
        prepare(self.root, 'multi', ['alice', 'bob'], 'https://central.example')
        for name in ('alice', 'bob'):
            values = SettingsStore(self.root / 'workspaces' / name / 'settings.json', 'workspace', environment={}).effective
            self.assertFalse(values['RMM_WORKSPACE_AUTO_CLAIM'])
            self.assertEqual(len(commands(self.root, 'workspace', name)), 1)
        with self.assertRaises(ValueError):
            commands(self.root, 'workspace')
        with self.assertRaises(ValueError):
            commands(self.root, 'single')
        with self.assertRaises(ValueError):
            commands(self.root, 'workspace', '../escape')

    def test_legacy_central_secrets_are_preserved_when_explicitly_imported(self):
        from rmm_settings import SCHEMA
        source = self.root.parent / 'old-settings.json'
        source.write_text(json.dumps({'schema': SCHEMA, 'scope': 'server', 'settings': {
            'RMM_API_TOKEN': 'existing-admin', 'RMM_BEACON_SECRET': 'existing-agent-secret',
            'RMM_BEACON_PUBLIC_URL': 'https://agents.example'}}))
        before = source.read_bytes()
        prepare(self.root, 'single', None, 'http://localhost:8080', source)
        values = SettingsStore(self.root / 'central/settings.json', 'server', environment={}).effective
        self.assertEqual(values['RMM_BEACON_SECRET'], 'existing-agent-secret')
        self.assertEqual(values['RMM_API_TOKEN'], 'existing-admin')
        self.assertEqual(values['RMM_BEACON_PUBLIC_URL'], 'https://agents.example')
        self.assertEqual(before, source.read_bytes())

    def test_prepare_never_overwrites_existing_configuration(self):
        prepare(self.root, 'single', None, 'http://localhost:8080')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*.json')}
        with self.assertRaises(ValueError):
            prepare(self.root, 'single', None, 'http://localhost:8080')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*.json')})
        for preset, ids, url in [('single', ['a', 'b'], 'http://localhost'),
                                 ('multi', ['a', 'a'], 'http://localhost'),
                                 ('single', ['../escape'], 'http://localhost'),
                                 ('single', None, 'http://user:secret@localhost')]:
            with self.assertRaises(ValueError):
                prepare(self.root.parent / 'invalid', preset, ids, url)

    def test_imported_admin_port_drives_default_workspace_url(self):
        from rmm_settings import SCHEMA
        source = self.root.parent / 'settings.json'
        source.write_text(json.dumps({'schema': SCHEMA, 'scope': 'server',
                                     'settings': {'RMM_ADMIN_PORT': 9090, 'RMM_ADMIN_BIND': '192.0.2.25'}}))
        prepare(self.root, 'single', None, legacy_settings=source)
        settings = SettingsStore(self.root / 'workspaces/default/settings.json', 'workspace', environment={}).effective
        self.assertEqual(settings['RMM_C2_URL'], 'http://192.0.2.25:9090')

    def test_invalid_metadata_or_mismatched_workspace_refused_before_launch(self):
        prepare(self.root, 'single', None)
        metadata = self.root / 'deployment.json'
        original = metadata.read_text()
        for invalid in ([], {'schema': 1, 'preset': 'multi', 'workspaces': ['a', 'a']}):
            metadata.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError):
                commands(self.root, 'single')
        metadata.write_text(original)
        path = self.root / 'workspaces/default/settings.json'
        settings = json.loads(path.read_text())
        settings['settings']['RMM_WORKSPACE_ID'] = 'another-owner'
        path.write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError, 'identity'):
            commands(self.root, 'workspace')

    def test_inherited_identity_cannot_rebind_another_workspace_data_directory(self):
        prepare(self.root, 'multi', ['alice', 'bob'])
        with patch.dict('os.environ', {'RMM_WORKSPACE_ID': 'bob', 'RMM_WORKSPACE_TOKEN': 'bob-token'}):
            argv, overrides = commands(self.root, 'workspace', 'alice')[0]
            self.assertNotIn('RMM_WORKSPACE_TOKEN', child_environment(argv, overrides))
            with self.assertRaisesRegex(ValueError, 'selected workspace'):
                child_environment(argv, overrides, allow_env_overrides=True)
            self.assertEqual(len(commands(self.root, 'workspace', 'bob')), 1)
            self.assertEqual(len(commands(self.root, 'control')), 1)

    def test_default_environment_cannot_redirect_storage_or_replace_saved_configuration(self):
        prepare(self.root, 'single', None)
        values = {'RMM_WORKSPACE_DATA': '/wrong', 'RMM_LOG_DIR': '/wrong', 'RMM_ASSETS_DIR': '/wrong',
                  'RMM_WORKSPACE_PORT': '9999', 'RMM_API_TOKEN': 'wrong-secret', 'OPENAI_API_KEY': 'wrong-key',
                  'RMM_WORKSPACE_TOKEN': 'wrong-token', 'PATH': '/usr/bin', 'KRB5CCNAME': '/private/cache'}
        with patch.dict('os.environ', values, clear=True):
            for argv, overrides in commands(self.root, 'single'):
                environment = child_environment(argv, overrides)
                self.assertEqual(environment['PATH'], '/usr/bin')
                self.assertEqual(environment['KRB5CCNAME'], '/private/cache')
                self.assertNotIn('RMM_API_TOKEN', environment)
                self.assertNotIn('OPENAI_API_KEY', environment)
                self.assertNotIn('RMM_WORKSPACE_TOKEN', environment)
                self.assertNotIn('RMM_WORKSPACE_PORT', environment)
                self.assertNotEqual(environment.get('RMM_LOG_DIR'), '/wrong')

    def test_role_roots_work_without_the_other_services_files(self):
        import shutil
        prepare(self.root, 'single', None)
        control = self.root.parent / 'control-only'
        workspace = self.root.parent / 'workspace-only'
        for target in (control, workspace):
            target.mkdir()
            shutil.copyfile(self.root / 'deployment.json', target / 'deployment.json')
        shutil.copytree(self.root / 'central', control / 'central')
        shutil.copytree(self.root / 'workspaces/default', workspace / 'workspaces/default')
        self.assertEqual(len(commands(control, 'control')), 1)
        self.assertEqual(len(commands(workspace, 'workspace')), 1)
        self.assertFalse((workspace / 'central/settings.json').exists())
        self.assertFalse((control / 'workspaces').exists())

    def test_storage_paths_and_relative_helper_are_owner_local(self):
        prepare(self.root, 'single', None)
        store = SettingsStore(self.root / 'workspaces/default/settings.json', 'workspace', environment={})
        store.update({'settings': {'RMM_WORKSPACE_TUN_HELPER': 'tools/rmm-tun'}})
        argv, overrides = commands(self.root, 'workspace')[0]
        self.assertEqual(overrides['RMM_WORKSPACE_TUN_HELPER'], str((self.root / 'workspaces/default/tools/rmm-tun').resolve()))
        paths = deployment_paths(self.root)
        self.assertEqual(paths['rclone_profiles_file'], str((self.root / 'workspaces/default/assets/rclone/profiles.json').resolve()))
        store.update({'settings': {'RMM_AI_SKILLS_DIR': '/outside/skills'}})
        with self.assertRaisesRegex(ValueError, 'canonical'):
            commands(self.root, 'workspace')

    def test_distributed_compose_contains_only_central_state_and_derived_ports(self):
        prepare(self.root, 'distributed', None, 'http://192.0.2.10:9090')
        store = SettingsStore(self.root / 'central/settings.json', 'server', environment={})
        store.update({'settings': {'RMM_ADMIN_PORT': 9090, 'RMM_BEACON_PORT': 9091}})
        spec = json.loads(central_compose(self.root).read_text())
        self.assertEqual(list(spec['services']), ['rmm-control'])
        service = spec['services']['rmm-control']
        self.assertIn('rmm_deployment.py', service['command'])
        self.assertEqual(service['ports'], ['127.0.0.1:9090:9090', '0.0.0.0:9091:9091'])
        self.assertEqual(len(service['volumes']), 2)
        self.assertFalse(any('workspaces' in mount['source'] for mount in service['volumes']))
        self.assertNotIn('cap_add', service)
        self.assertNotIn('devices', service)
        self.assertNotIn('environment', service)
        self.assertNotIn('RMM_API_TOKEN', json.dumps(spec))

    def test_canonical_profile_import_refuses_overwrite(self):
        prepare(self.root, 'single', None)
        source = self.root.parent / 'old-profiles.json'
        source.write_text('{"lab":{"type":"s3","secret_access_key":"test-secret"}}')
        target = import_rclone(self.root, None, source)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            import_rclone(self.root, None, source)

    def test_container_rejects_host_absolute_storage_before_launch(self):
        prepare(self.root, 'distributed', None, 'http://192.0.2.10:8080')
        store = SettingsStore(self.root / 'central/settings.json', 'server', environment={})
        store.update({'settings': {'RMM_PROFILES_DIR': str((self.root / 'central/profiles').resolve())}})
        with self.assertRaisesRegex(ValueError, 'absolute host storage path'):
            central_compose(self.root)
        store.update({'settings': {'RMM_PROFILES_DIR': 'profiles'}})
        self.assertTrue(central_compose(self.root).is_file())

    def test_helper_home_expansion_reaches_child_environment(self):
        prepare(self.root, 'single', None)
        store = SettingsStore(self.root / 'workspaces/default/settings.json', 'workspace', environment={})
        store.update({'settings': {'RMM_WORKSPACE_TUN_HELPER': '~/bin/rmm-tun'}})
        argv, overrides = commands(self.root, 'workspace')[0]
        self.assertEqual(child_environment(argv, overrides)['RMM_WORKSPACE_TUN_HELPER'], str(Path('~/bin/rmm-tun').expanduser()))

    def test_supervisor_stops_peer_when_one_process_fails(self):
        first, second = Mock(), Mock()
        first.poll.return_value = 3
        first.returncode = 3
        second.poll.return_value = None
        second.returncode = None
        with patch('rmm_deployment.subprocess.Popen', side_effect=[first, second]), \
                patch('rmm_deployment.signal.signal'):
            self.assertEqual(run_services([(['first'], {}), (['second'], {})]), 3)
        second.terminate.assert_called_once()
        first.terminate.assert_not_called()
        second.wait.assert_called_once()
