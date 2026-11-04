"""Deletion must not hide a failed central cache revocation."""
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace_service import WorkspaceService


class WorkspacePluginRevocationTests(unittest.TestCase):
    def test_failed_revocation_retains_disabled_asset_for_retry(self):
        with tempfile.TemporaryDirectory() as root, patch('rmm_exegol_runtime.get_runtime'):
            service = WorkspaceService('http://unused.invalid', 'alice', 'secret', root)
            try:
                service.store.asset_store.add_bytes('plugins', 'tool.dll', b'alice')
                service.upstream.request = Mock(return_value=(503, {}, 'application/json'))
                status, payload, _ = service.delete_json('/api/v1/assets/plugins/tool.dll')
                self.assertEqual(status, 502)
                self.assertEqual(payload['error'], 'central_plugin_cache_sync_failed')
                row = service.store.asset_list('plugins')[0]
                self.assertFalse(row['enabled'])
                service.upstream.request = Mock(side_effect=lambda method, *args, **kwargs:
                    (200, {'plugins': [{'name': 'tool.dll', 'sha256': row['sha256']}]}, 'application/json')
                    if method == 'GET' else (200, {}, 'application/json'))
                service._sync_workspace_plugins()
                self.assertFalse(service.upstream.request.call_args.args[2]['enabled'])
                status, _, _ = service.delete_json('/api/v1/assets/plugins/tool.dll')
                self.assertEqual(status, 200)
                self.assertEqual(service.store.asset_list('plugins'), [])
            finally:
                service.store.close()
