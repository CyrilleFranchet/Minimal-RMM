"""Verify the protected workspace serves its login assets without exposing APIs."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest

from workspace_service import WorkspaceRequestHandler, WorkspaceService


class WorkspaceLoginTests(unittest.TestCase):
    """Exercise the static-login/API authentication boundary through real HTTP."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = WorkspaceService("http://unused.invalid", "alice", "upstream-secret",
                                        self.directory.name, local_token="local-secret")
        handler = type("LoginHandler", (WorkspaceRequestHandler,), {"service": self.service})
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.service.store.close()
        self.directory.cleanup()

    def request(self, method, path, token=""):
        """Send one exact local path without URL normalization or token persistence."""
        connection = HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_login_assets_load_before_api_authentication(self):
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/ui/")
        for path in ("/ui/", "/ui/app.js", "/ui/style.css", "/ui/workspace-config.js"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertNotIn(b"local-secret", body)
                self.assertNotIn(b"upstream-secret", body)
        self.assertEqual(self.request("GET", "/api/v1/health")[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/health", "wrong")[0], 401)
        status, _, body = self.request("GET", "/api/v1/health", "local-secret")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["workspace_id"], "alice")

    def test_public_static_routing_does_not_allow_traversal_or_other_methods(self):
        for path in ("/ui/../server_rmm.py", "/ui/../../.env"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 403)
        self.assertEqual(self.request("GET", "/ui/%2e%2e/server_rmm.py")[0], 404)
        self.assertEqual(self.request("GET", "/ui-missing")[0], 401)
        self.assertEqual(self.request("POST", "/ui/")[0], 401)
        self.assertEqual(self.request("DELETE", "/ui/app.js")[0], 401)


if __name__ == "__main__":
    unittest.main()
