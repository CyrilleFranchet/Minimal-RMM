"""Exercise malformed bodies and workspace proxy behavior at HTTP boundaries."""

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from server_rmm import RMMHandler, RMMServer
from rmm_asset_store import AssetError
from workspace_service import WorkspaceRequestHandler, WorkspaceUpstream


class CentralRequestBodyTests(unittest.TestCase):
    """Keep bad JSON and invalid framing out of central operator handlers."""

    def test_post_and_patch_reject_invalid_body_before_dispatch(self):
        for method in ("POST", "PATCH"):
            for content, expected in ((b"{broken", "invalid_json"), (b"[]", "invalid_request_body"),
                                      (b"null", "invalid_request_body"), (b'"text"', "invalid_request_body")):
                with self.subTest(method=method, content=content):
                    handler = SimpleNamespace(path="/api/v1/settings", headers={"Content-Length": str(len(content))},
                                              rfile=io.BytesIO(content), _path_allowed=lambda *_a, **_k: True,
                                              _json=MagicMock(), _respond=MagicMock(),
                                              _handle_api_post=MagicMock(), _handle_api_patch=MagicMock())
                    handler._read_body_bytes = RMMHandler._read_body_bytes.__get__(handler)
                    handler._read_json_body = RMMHandler._read_json_body.__get__(handler)
                    getattr(RMMHandler, "do_" + method)(handler)
                    handler._json.assert_called_once_with(400, {"error": expected})
                    handler._handle_api_post.assert_not_called()
                    handler._handle_api_patch.assert_not_called()

    def test_negative_lengths_are_rejected_without_reading(self):
        for handler_class, reader in ((RMMHandler, "_read_body_bytes"),
                                      (WorkspaceRequestHandler, "_request_body")):
            with self.subTest(handler=handler_class.__name__):
                handler = SimpleNamespace(headers={"Content-Length": "-1"}, rfile=MagicMock())
                with self.assertRaisesRegex(ValueError, "invalid_content_length"):
                    getattr(handler_class, reader)(handler)
                handler.rfile.read.assert_not_called()

    def test_profile_validation_preserves_running_transport(self):
        runtime = RMMServer.__new__(RMMServer)
        runtime._shutdown_lock = threading.RLock()
        runtime._closed = False
        runtime.profile_name = "cloudflared"
        runtime.profile_manager = MagicMock()
        runtime.stop_profile = MagicMock()
        handler = SimpleNamespace(
            headers={}, _api_authorized=lambda *_a: True, _workspace_scope=lambda: None,
            _api_path_parts=lambda _path: ["profile"], _json=MagicMock(), server_instance=runtime)
        for body in ({"name": []}, {"name": 7}, {"config": []}, {"config": None},
                     {"stop": "false"}, {"config": {"mode": []}},
                     {"config": {"mode": "invalid"}}, {"config": {"token": {}}}):
            with self.subTest(body=body), patch("server_rmm.settings_getenv", side_effect=lambda _k, default="": default):
                handler._json.reset_mock()
                self.assertTrue(RMMHandler._handle_api_post(handler, "/api/v1/profile", body))
                self.assertEqual(handler._json.call_args.args[0], 400)
                self.assertEqual(handler._json.call_args.args[1]["error"], "invalid_profile_config")
        runtime.profile_manager.start.assert_not_called()
        runtime.stop_profile.assert_not_called()


class WorkspaceProxyBoundaryTests(unittest.TestCase):
    """Verify valid HTTP errors and query preservation using a local HTTP client."""

    def setUp(self):
        self.service = SimpleNamespace(_local_authorized=lambda *_a: True,
                                       get_json=MagicMock(return_value=None),
                                       post_json=MagicMock(return_value=None),
                                       patch_json=MagicMock(return_value=None),
                                       _proxy=MagicMock(return_value=(200, {"ok": True}, "application/json")))
        handler = type("BoundaryHandler", (WorkspaceRequestHandler,), {"service": self.service})
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def request(self, method, path, body=None):
        """Return the actual HTTP status and decoded response body."""
        connection = HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_proxy_preserves_repeated_and_blank_parameters_but_not_local_token(self):
        status, _ = self.request("GET", "/api/v1/workspace/events?since=41&epoch=&tag=a&tag=b&token=local-secret")
        self.assertEqual(status, 200)
        self.service._proxy.assert_called_once_with(
            "GET", "/api/v1/workspace/events?since=41&epoch=&tag=a&tag=b", None, timeout=30)

    def test_upstream_connection_failure_is_a_valid_bad_gateway_response(self):
        for local_response in (False, True):
            with self.subTest(local_response=local_response):
                failure = (0, {"error": "upstream_unreachable"}, "application/json")
                self.service.get_json.return_value = failure if local_response else None
                self.service._proxy.return_value = failure
                self.assertEqual(self.request("GET", "/api/v1/plugins"),
                                 (502, {"error": "upstream_unreachable"}))

    def test_nonobject_bodies_never_reach_a_mutation_handler(self):
        for method in ("POST", "PATCH"):
            for content in (b"[]", b"null", b"false", b'"text"'):
                with self.subTest(method=method, content=content):
                    status, payload = self.request(method, "/api/v1/settings", content)
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["error"], "json_object_required")
        self.service.post_json.assert_not_called()
        self.service.patch_json.assert_not_called()
        self.service._proxy.assert_not_called()

    def test_oversized_workspace_body_remains_payload_too_large(self):
        with patch("workspace_service.MAX_LOCAL_BODY", 2):
            self.assertEqual(self.request("POST", "/api/v1/settings", b"{ }")[0], 413)

    def test_corrupt_asset_inventory_returns_json_error_without_proxying(self):
        self.service.get_json.side_effect = AssetError("metadata is corrupt")
        self.assertEqual(self.request("GET", "/api/v1/assets"),
                         (500, {"error": "workspace_asset_store_failed"}))
        self.service._proxy.assert_not_called()


class WorkspaceUpstreamRedirectTests(unittest.TestCase):
    """Do not resend workspace credentials to an HTTP redirect destination."""

    def test_redirect_is_returned_without_following_location(self):
        paths = []

        class RedirectHandler(BaseHTTPRequestHandler):
            """Record paths and expose a controlled credential-redirect response."""

            def log_message(self, *_args):
                pass

            def do_GET(self):
                paths.append(self.path)
                self.send_response(302 if self.path == "/start" else 200)
                self.send_header("Location", "/destination")
                self.send_header("Content-Length", "0")
                self.end_headers()

        http = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        upstream = WorkspaceUpstream(f"http://127.0.0.1:{http.server_port}", "alice", "workspace-secret")
        try:
            status, _, _ = upstream.request("GET", "/start")
            self.assertEqual(status, 302)
            self.assertEqual(paths, ["/start"])
        finally:
            upstream.close()
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
