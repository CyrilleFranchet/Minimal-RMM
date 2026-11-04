"""Authenticated API calls must stay on the configured RMM endpoint."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from rmm_cli import RmmApiClient


class ApiRedirectTests(unittest.TestCase):
    """Exercise real urllib redirects for API bodies and artifact downloads."""

    def test_redirects_never_forward_credentials_or_upload_artifacts(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/destination"):
                    received.append(dict(self.headers))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                else:
                    self.send_response(302)
                    self.send_header("Location", "/destination")
                    self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            client = RmmApiClient(f"http://127.0.0.1:{server.server_port}", "operator-secret",
                                  {"X-RMM-Workspace-Token": "workspace-secret"})
            self.assertEqual(client.health()[0], 302)
            self.assertEqual(client.upload_file("test", "artifact:agent-builds/test.exe", "target")[0], 302)
            self.assertEqual(received, [])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)
