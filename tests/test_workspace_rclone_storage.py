"""Rclone configuration is registered owner-local data, never a loose file or example."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rmm_rclone import RcloneConfigError, load_profiles
from workspace_service import WorkspaceStore, WorkspaceService


class WorkspaceRcloneStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = WorkspaceStore(Path(self.directory.name) / 'alice', 'alice')
        self.addCleanup(self.store.close)

    def install(self, name='profiles.json', profiles=None, enabled=True):
        payload = profiles if profiles is not None else {'lab': {'type': 's3', 'secret_access_key': 'private-test-key'}}
        return self.store.assets.add_bytes('rclone', name, json.dumps(payload).encode(), enabled=enabled)

    def service(self):
        service = object.__new__(WorkspaceService)
        service.store = self.store
        service._proxy = Mock(return_value=(200, {'rclone_plugin': {'name': 'rclone-exfil.dll'}}, 'application/json'))
        return service

    def test_loose_file_is_inactive_until_registered(self):
        target = self.store.assets.root / 'rclone/profiles.json'
        target.write_text('{"lab":{"type":"s3"}}')
        self.assertEqual(self.store.rclone_profiles(), {})
        self.install()
        self.assertIn('lab', self.store.rclone_profiles())
        self.assertTrue((self.store.assets.root / 'metadata.json').is_file())

    def test_disabled_or_empty_inventory_does_not_resurrect_legacy_profiles(self):
        self.install(enabled=False)
        with patch('workspace_service.load_profiles', side_effect=AssertionError('Legacy source must not be read')):
            self.assertEqual(self.store.rclone_profiles(), {})
        self.store.assets.set_enabled('rclone', 'profiles.json', True)
        self.install(profiles={})
        self.assertEqual(self.store.rclone_profiles(), {})

    def test_invalid_registered_json_is_visible_and_blocks_jobs_without_exposing_content(self):
        self.store.assets.add_bytes('rclone', 'profiles.json', b'{private-test-key')
        with self.assertRaises(RcloneConfigError):
            self.store.rclone_profiles()
        service = self.service()
        config = service._rclone_config()
        self.assertEqual(config['profiles'], [])
        self.assertTrue(config['load_error'])
        self.assertNotIn('private-test-key', json.dumps(config))
        with self.assertRaises(RcloneConfigError):
            service._workspace_exfil_job({'agent_type': 'go', 'capabilities': ['pe']}, 'C:/Temp/demo', 'lab', None, 'plugin')
        self.assertTrue(all(call.args[0] == 'GET' for call in service._proxy.call_args_list))

    def test_invalid_profile_shape_and_duplicate_names_are_rejected(self):
        for payload in ({'lab': 'private-test-key'}, {'lab': {'type': ''}}, [],
                        {'type': 's3', 'name': ['private-test-key']}):
            self.install(profiles=payload)
            with self.assertRaises(RcloneConfigError) as error:
                self.store.rclone_profiles()
            self.assertNotIn('private-test-key', str(error.exception))
        self.install()
        self.install('other.json')
        with self.assertRaisesRegex(RcloneConfigError, 'Duplicate'):
            self.store.rclone_profiles()

    def test_registered_updates_are_read_by_the_next_request(self):
        self.install(profiles={'first': {'type': 's3'}})
        self.assertEqual(list(self.store.rclone_profiles()), ['first'])
        self.install(profiles={'second': {'type': 's3'}})
        self.assertEqual(list(self.store.rclone_profiles()), ['second'])

    def test_workspaces_remain_isolated_and_public_status_omits_credentials(self):
        self.install()
        other = WorkspaceStore(Path(self.directory.name) / 'bob', 'bob')
        self.addCleanup(other.close)
        self.assertEqual(other.rclone_profiles(), {})
        config = self.service()._rclone_config()
        self.assertNotIn('private-test-key', json.dumps(config))
        self.assertIn('registered', config['profiles_source'])
        self.assertEqual(config['profiles_scope'], 'workspace')
        self.assertEqual(config['profiles_directory'], str(self.store.assets.root / 'rclone'))

    def test_corrupt_inventory_never_silently_activates_a_file(self):
        self.install()
        self.store.assets.metadata_path.write_text('{private-test-key')
        with self.assertRaises(RcloneConfigError) as error:
            self.store.rclone_profiles()
        self.assertNotIn('private-test-key', str(error.exception))

    def test_examples_are_not_runtime_configuration(self):
        with patch('builtins.open', side_effect=AssertionError('No fallback file may be opened')):
            self.assertEqual(load_profiles(path_override='', raw_override=''), {})
