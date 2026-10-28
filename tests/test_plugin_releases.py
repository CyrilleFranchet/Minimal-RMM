"""Private GitHub plugin release verification and storage-owner boundaries."""
import copy
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error
import urllib.request

from rmm_asset_store import AssetStore
from rmm_cli import RmmApiClient
from rmm_plugin_releases import PluginReleases, ReleaseError, ReleaseRedirect, plugin_release_api
from server_rmm import RMMHandler
from workspace_service import WorkspaceRequestHandler, WorkspaceService

SHA = "a" * 40
TAG = "plugins-" + SHA
CONTENT = b"verified test plugin"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def fixture():
    """Return a manifest, GitHub release, and mocked API byte responses."""
    manifest = {"schema": "minimal-rmm-plugin-bundle-v1", "version": SHA, "source_commit": SHA,
                "plugins": [{"name": "test.dll", "size": len(CONTENT), "sha256": DIGEST}]}
    raw = json.dumps(manifest).encode()
    release = {"tag_name": TAG, "draft": False, "prerelease": False, "assets": [
        {"name": "plugins.json", "id": 1, "size": len(raw)},
        {"name": "test.dll", "id": 2, "size": len(CONTENT)},
    ]}
    return manifest, release, {"/releases/latest": json.dumps(release).encode(),
                               f"/releases/tags/{TAG}": json.dumps(release).encode(),
                               "/releases/assets/1": raw, "/releases/assets/2": CONTENT}


class PluginReleaseTests(unittest.TestCase):
    """Verify integrity, explicit versions, bounded fetches, and secret handling."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AssetStore(self.temp.name)
        self.manifest, self.release, self.responses = fixture()
        self.env = patch.dict("os.environ", {"RMM_PLUGIN_REPOSITORY": "owner/private", "RMM_PLUGIN_GITHUB_TOKEN": "private-secret"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.fetch = patch.object(PluginReleases, "_request", side_effect=lambda endpoint, *args, **kwargs: self.responses[endpoint])
        self.request = self.fetch.start(); self.addCleanup(self.fetch.stop)

    def update_manifest(self):
        raw = json.dumps(self.manifest).encode()
        self.responses["/releases/assets/1"] = raw
        self.release["assets"][0]["size"] = len(raw)
        self.update_release()

    def update_release(self):
        raw = json.dumps(self.release).encode()
        self.responses["/releases/latest"] = raw
        self.responses[f"/releases/tags/{TAG}"] = raw

    def test_browse_and_install_reuses_verified_bytes_and_survives_store_reopen(self):
        self.assertEqual(plugin_release_api(self.store, "GET")[1]["tag"], TAG)
        body = {"tag": TAG, "name": "test.dll", "sha256": DIGEST}
        code, first = plugin_release_api(self.store, "POST", body=body)
        self.assertEqual(code, 200); self.assertFalse(first["cached"])
        self.assertEqual(self.store.read("plugins", "test.dll"), CONTENT)
        self.request.reset_mock()
        code, second = plugin_release_api(AssetStore(self.temp.name), "POST", body=body)
        self.assertEqual(code, 200); self.assertTrue(second["cached"])
        self.assertNotIn("/releases/assets/2", [call.args[0] for call in self.request.call_args_list])
        self.assertNotIn("private-secret", (Path(self.temp.name) / "metadata.json").read_text())

    def test_tampering_leaves_installed_bytes_unchanged(self):
        self.store.add_bytes("plugins", "test.dll", b"previous version")
        self.responses["/releases/assets/2"] = b"x" * len(CONTENT)
        code, payload = plugin_release_api(self.store, "POST", body={"tag": TAG, "name": "test.dll"})
        self.assertEqual(code, 400); self.assertIn("checksum", payload["error"])
        self.assertEqual(self.store.read("plugins", "test.dll"), b"previous version")

    def test_install_requires_exact_tag_and_selected_checksum(self):
        for body in ({"tag": "latest", "name": "test.dll"},
                     {"tag": TAG, "name": "test.dll", "sha256": "b" * 64},
                     {"tag": TAG, "name": "test.dll", "token": "not-accepted"}):
            with self.subTest(body=body):
                self.assertEqual(plugin_release_api(self.store, "POST", body=body)[0], 400)
        self.assertEqual(self.store.list(), [])

    def test_release_and_manifest_must_match_pinned_commit(self):
        for mutate in (lambda: self.release.update(draft=True),
                       lambda: self.release.update(prerelease=True),
                       lambda: self.release.update(tag_name="plugins-" + "b" * 40),
                       lambda: self.manifest.update(source_commit="b" * 40)):
            with self.subTest(mutate=mutate):
                self.manifest, self.release, self.responses = fixture()
                mutate(); self.update_manifest()
                self.assertEqual(plugin_release_api(self.store, "GET", tag=TAG)[0], 400)

    def test_invalid_names_duplicates_and_missing_assets_are_rejected(self):
        for name in ("../test.dll", "test.exe", "path/test.dll", "test\\test.dll"):
            self.manifest["plugins"][0]["name"] = name
            self.update_manifest()
            self.assertEqual(plugin_release_api(self.store, "GET")[0], 400)
        self.manifest, self.release, self.responses = fixture()
        self.manifest["plugins"].append(copy.deepcopy(self.manifest["plugins"][0]))
        self.update_manifest()
        self.assertEqual(plugin_release_api(self.store, "GET")[0], 400)

    def test_size_limit_blocks_download_before_write(self):
        self.manifest["plugins"][0]["size"] = 2048
        self.release["assets"][1]["size"] = 2048
        self.update_manifest()
        with patch.dict("os.environ", {"RMM_ASSET_MAX_BYTES": "1024"}):
            self.assertEqual(plugin_release_api(self.store, "POST", body={"tag": TAG, "name": "test.dll"})[0], 400)
        self.assertNotIn("/releases/assets/2", [call.args[0] for call in self.request.call_args_list])
        self.assertEqual(self.store.list(), [])

    def test_github_digest_is_verified(self):
        self.release["assets"][1]["digest"] = "sha256:" + "b" * 64
        self.update_release()
        self.assertEqual(plugin_release_api(self.store, "POST", body={"tag": TAG, "name": "test.dll"})[0], 400)
        self.assertEqual(self.store.list(), [])

    def test_invalid_json_returns_safe_error(self):
        self.responses["/releases/latest"] = b'{"secret":"private-secret", bad json'
        code, payload = plugin_release_api(self.store, "GET")
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"error": "invalid plugin release data"})

    def test_sync_failure_preserves_local_install_for_retry(self):
        body = {"tag": TAG, "name": "test.dll"}
        code, payload = plugin_release_api(self.store, "POST", body=body, publish=lambda row: False)
        self.assertEqual(code, 502); self.assertTrue(payload["local_installed"])
        self.assertEqual(self.store.read("plugins", "test.dll"), CONTENT)
        code, payload = plugin_release_api(self.store, "POST", body=body, publish=lambda row: True)
        self.assertEqual(code, 200); self.assertTrue(payload["cached"])

    def test_sync_exception_reports_local_copy_without_exposing_details(self):
        publish = Mock(side_effect=OSError("internal path and private-secret"))
        code, payload = plugin_release_api(self.store, "POST", body={"tag": TAG, "name": "test.dll"}, publish=publish)
        self.assertEqual(code, 502); self.assertTrue(payload["local_installed"])
        self.assertNotIn("private-secret", json.dumps(payload))
        self.assertEqual(self.store.read("plugins", "test.dll"), CONTENT)


class ReleaseTransportTests(unittest.TestCase):
    """Ensure private API credentials never follow signed download redirects."""

    def test_authorization_is_unredirected_and_response_is_bounded(self):
        with patch.dict("os.environ", {"RMM_PLUGIN_GITHUB_TOKEN": "secret", "RMM_PLUGIN_REPOSITORY": "owner/private"}):
            client = PluginReleases()
        client.opener = MagicMock()
        response = client.opener.open.return_value.__enter__.return_value
        response.read.return_value = b"{}"
        self.assertEqual(client._request("/releases/latest", 100), b"{}")
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.unredirected_hdrs["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", req.headers)
        self.assertEqual(req.full_url, "https://api.github.com/repos/owner/private/releases/latest")
        response.read.assert_called_once_with(101)
        response.read.return_value = b"x" * 101
        with self.assertRaises(ReleaseError):
            client._request("/releases/latest", 100)

    def test_redirect_strips_credentials_and_rejects_foreign_origins(self):
        request = urllib.request.Request("https://api.github.com/repos/owner/private/releases/assets/1")
        request.add_header("Authorization", "Bearer secret")
        request.add_unredirected_header("Authorization", "Bearer secret")
        handler = ReleaseRedirect()
        allowed = "https://release-assets.githubusercontent.com/github-production-release-asset/asset?signature=value"
        redirected = handler.redirect_request(request, None, 302, "Found", {}, allowed)
        self.assertNotIn("Authorization", dict(redirected.header_items()))
        for url in ("http://release-assets.githubusercontent.com/asset", "https://evil.example/asset",
                    "https://api.github.com.evil.example/asset", "https://secret@api.github.com/asset",
                    "https://api.github.com:444/asset"):
            with self.subTest(url=url), self.assertRaises(ReleaseError):
                handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_upstream_error_is_redacted(self):
        client = PluginReleases(); client.opener = MagicMock()
        client.opener.open.side_effect = urllib.error.HTTPError("https://api.github.com/?secret=hidden", 403, "private-secret", {}, io.BytesIO(b"sensitive"))
        with self.assertRaises(ReleaseError) as caught:
            client._request("/releases/latest", 100)
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertNotIn("hidden", str(caught.exception))


class ReleaseOwnerTests(unittest.TestCase):
    """Exercise authenticated workspace routing and central ownership rejection."""

    def test_server_routes_install_verified_bytes_into_runtime_store(self):
        with tempfile.TemporaryDirectory() as root:
            store = AssetStore(Path(root) / "assets")
            runtime = Path(root) / "plugins"
            runtime.mkdir()
            server = SimpleNamespace(storage_mode="server", log=Mock())
            handler = SimpleNamespace(server_instance=server, _json=Mock(), _api_authorized=lambda *args: True,
                                      _api_path_parts=lambda path: path.removeprefix("/api/v1/").split("/"))
            handler._handle_plugin_releases = RMMHandler._handle_plugin_releases.__get__(handler)
            _, _, responses = fixture()
            with patch("server_rmm.asset_store", return_value=store), patch("server_rmm.plugin_directory", return_value=str(runtime)), patch.object(PluginReleases, "_request", side_effect=lambda endpoint, *a, **kw: responses[endpoint]):
                RMMHandler._handle_api_get(handler, "/api/v1/plugin-releases", {"tag": ["latest"]})
                self.assertEqual(handler._json.call_args.args[0], 200)
                RMMHandler._handle_api_post(handler, "/api/v1/plugin-releases/install", {"tag": TAG, "name": "test.dll"})
                self.assertEqual(handler._json.call_args.args[0], 200)
                self.assertEqual((runtime / "test.dll").read_bytes(), CONTENT)
                server.log.assert_called_once()

    def test_central_rejects_workspace_requests_before_github_access(self):
        handler = SimpleNamespace(server_instance=SimpleNamespace(storage_mode="workspace"), _json=Mock())
        with patch("server_rmm.plugin_release_api") as api:
            RMMHandler._handle_plugin_releases(handler, "GET")
            self.assertEqual(handler._json.call_args.args[0], 409)
            handler.server_instance.storage_mode = "server"
            handler._workspace_scope = lambda: "alice"
            RMMHandler._handle_plugin_releases(handler, "POST", body={"tag": TAG, "name": "test.dll"})
            self.assertEqual(handler._json.call_args.args[0], 409)
            api.assert_not_called()

    def test_workspace_http_auth_install_and_failed_sync(self):
        with tempfile.TemporaryDirectory() as root:
            service = WorkspaceService("http://unused.invalid", "alice", "upstream", root, local_token="local-secret")
            handler = type("ReleaseHandler", (WorkspaceRequestHandler,), {"service": service})
            http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
            try:
                client = RmmApiClient(f"http://127.0.0.1:{http.server_port}", "wrong")
                self.assertEqual(client.request("GET", "/api/v1/plugin-releases")[0], 401)
                client.token = "local-secret"
                _, _, responses = fixture()
                with patch.object(PluginReleases, "_request", side_effect=lambda endpoint, *a, **kw: responses[endpoint]), patch.object(service, "_sync_plugin_asset", return_value=False) as sync:
                    self.assertEqual(client.request("GET", "/api/v1/plugin-releases?tag=latest")[1]["tag"], TAG)
                    code, payload = client.request("POST", "/api/v1/plugin-releases/install", {"tag": TAG, "name": "test.dll"})
                    self.assertEqual(code, 502); self.assertTrue(payload["local_installed"])
                    sync.assert_called_once()
                    self.assertEqual(service.store.asset_store.read("plugins", "test.dll"), CONTENT)
            finally:
                http.shutdown(); http.server_close(); thread.join()
                service.pivots.stop_all(); service.store.close()


if __name__ == "__main__":
    unittest.main()
