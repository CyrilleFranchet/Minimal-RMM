"""Authoritative plugin reconciliation across outages and concurrent mutations."""
import base64
import hashlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from workspace_service import WorkspaceService


class WorkspacePluginSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        runtime = patch('rmm_exegol_runtime.get_runtime')
        runtime.start()
        self.addCleanup(runtime.stop)
        self.service = WorkspaceService('http://unused.invalid', 'alice', 'secret', self.directory.name)
        self.addCleanup(self.service.stop)
        self.remote = {}
        self.posts = []
        self.available = True
        self.service.upstream.request = self.request

    def request(self, method, path, body=None, **kwargs):
        if not self.available:
            return 503, {}, 'application/json'
        if method == 'GET':
            self.assertEqual(path, '/api/v1/plugins')
            return 200, {'plugins': list(self.remote.values())}, 'application/json'
        self.assertEqual(path, '/api/v1/workspace/assets/plugins')
        self.posts.append(body)
        if body['enabled']:
            content = base64.b64decode(body['content_b64'])
            self.assertEqual(hashlib.sha256(content).hexdigest(), body['sha256'])
            self.remote[body['name']] = {'name': body['name'], 'sha256': body['sha256']}
        else:
            self.remote.pop(body['name'], None)
        return 200, {}, 'application/json'

    def test_outage_and_cache_loss_recover_without_restarting_workspace(self):
        self.service.store.asset_store.add_bytes('plugins', 'tool.dll', b'current')
        self.available = False
        self.service._sync_workspace_plugins()
        self.assertEqual(self.remote, {})
        self.available = True
        self.service._sync_workspace_plugins()
        self.assertIn('tool.dll', self.remote)
        self.service._sync_workspace_plugins()
        self.assertEqual(len(self.posts), 1, 'unchanged bytes must not be republished')
        self.remote.clear()
        self.service._sync_workspace_plugins()
        self.assertEqual(len(self.posts), 2)

    def test_disabled_and_orphaned_cache_entries_are_revoked(self):
        self.service.store.asset_store.add_bytes('plugins', 'tool.dll', b'current')
        self.service._sync_workspace_plugins()
        self.service.store.asset_store.set_enabled('plugins', 'tool.dll', False)
        self.remote['orphan.dll'] = {'name': 'orphan.dll', 'sha256': 'old'}
        self.service._sync_workspace_plugins()
        self.assertEqual(self.remote, {})
        self.assertNotIn('content_b64', self.posts[-1])

    def test_corrupt_inventory_cannot_revoke_the_entire_cache(self):
        self.service.store.asset_store.add_bytes('plugins', 'tool.dll', b'current')
        self.service._sync_workspace_plugins()
        self.service.store.asset_store.metadata_path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.service._sync_workspace_plugins()
        self.assertIn('tool.dll', self.remote)
        self.assertEqual(len(self.posts), 1)

    def test_stale_caller_snapshot_cannot_restore_old_enabled_state(self):
        stale = self.service.store.asset_store.add_bytes('plugins', 'tool.dll', b'old')
        current = self.service.store.asset_store.add_bytes('plugins', 'tool.dll', b'new')
        self.service._sync_plugin_asset(stale)
        self.assertEqual(self.remote['tool.dll']['sha256'], current['sha256'])
        self.service.store.asset_store.set_enabled('plugins', 'tool.dll', False)
        self.service._sync_plugin_asset(stale)
        self.assertNotIn('tool.dll', self.remote)

    def test_plugin_kind_aliases_publish_and_revoke_consistently(self):
        status, _, _ = self.service.post_json('/api/v1/assets/PLUGIN', {
            'name': 'tool.dll', 'content_b64': base64.b64encode(b'current').decode()})
        self.assertEqual(status, 201)
        self.assertIn('tool.dll', self.remote)
        status, _, _ = self.service.patch_json('/api/v1/assets/Plugins/tool.dll', {'enabled': False})
        self.assertEqual(status, 200)
        self.assertNotIn('tool.dll', self.remote)
        status, _, _ = self.service.delete_json('/api/v1/assets/PLUGIN/tool.dll')
        self.assertEqual(status, 200)

    def test_delete_waits_for_inflight_publication_then_revokes_it(self):
        self.service.store.asset_store.add_bytes('plugins', 'tool.dll', b'current')
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        request = self.request
        def slow_request(method, path, body=None, **kwargs):
            if method == 'POST' and body['enabled']:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test did not release publication')
            return request(method, path, body, **kwargs)
        self.service.upstream.request = slow_request
        publishing = threading.Thread(target=self.service._sync_workspace_plugins)
        outcomes = []
        def delete():
            outcomes.append(self.service.delete_json('/api/v1/assets/plugins/tool.dll')[0])
            finished.set()
        deleting = threading.Thread(target=delete)
        publishing.start()
        try:
            self.assertTrue(entered.wait(2))
            deleting.start()
            self.assertFalse(finished.wait(.05))
        finally:
            release.set()
            publishing.join(3)
            if deleting.ident is not None:
                deleting.join(3)
        self.assertEqual(outcomes, [200])
        self.assertEqual(self.remote, {})
        self.assertEqual(self.service.store.asset_list('plugins'), [])
