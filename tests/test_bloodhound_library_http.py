"""Exercise discovery through the real operator HTTP route and API client."""
from http.server import ThreadingHTTPServer
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rmm_cli import RmmApiClient
import server_rmm


class LibraryHttpTests(unittest.TestCase):
    def test_client_route_search_detail_and_validation_without_exegol(self):
        class Handler(server_rmm.RMMHandler):
            server_instance = SimpleNamespace(storage_mode='server')

            def _api_authorized(self, *args):
                return self.headers.get('Authorization') == 'Bearer fixture-token'

            def _workspace_scope(self):
                return None

            def _path_allowed(self, *args, **kwargs):
                return True

            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        client = RmmApiClient(f'http://127.0.0.1:{http.server_port}', 'fixture-token')
        with tempfile.TemporaryDirectory() as directory, patch('server_rmm.LOG_DIR', directory), \
                patch('rmm_exegol_runtime.get_runtime') as runtime:
            code, result = client.bloodhound('library_search', options={'text': 'delegation', 'limit': 1})
            self.assertEqual(code, 200)
            self.assertEqual(len(result['entries']), 1)
            self.assertNotIn('query', result['entries'][0])
            code, detail = client.bloodhound('library_get', options={'id': result['entries'][0]['id']})
            self.assertEqual(code, 200)
            self.assertTrue(detail['entry']['query'])
            self.assertFalse(detail['entry']['compatibility']['upstream_executable'])
            code, error = client.bloodhound('library_search', options={'limit': 26})
            self.assertEqual(code, 400)
            self.assertEqual(error['error'], 'invalid_library_search_options')
            runtime.assert_not_called()
