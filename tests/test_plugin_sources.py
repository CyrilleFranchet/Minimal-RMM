"""Persistent plugin source configuration, credential references, and owner isolation."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rmm_asset_store import AssetStore
from rmm_cli import RmmApiClient
from rmm_plugin_sources import PluginSourceError, PluginSourceStore, normalize_source, plugin_sources_api
from server_rmm import RMMHandler
from workspace_service import WorkspaceRequestHandler, WorkspaceService

CONFIG = {"label": "Development", "repository": "owner/private", "branch": "feature/plugins",
          "credential_env": "RMM_PLUGIN_TOKEN_DEVELOPMENT"}


class PluginSourceStoreTests(unittest.TestCase):
    """Verify durable multi-source settings without stored credentials."""

    def test_seed_once_and_saved_sources_survive_reopen(self):
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"RMM_PLUGIN_REPOSITORY": "owner/original", "RMM_PLUGIN_TOKEN_DEVELOPMENT": "private-secret"}):
            store = PluginSourceStore(root)
            self.assertEqual(store.get()["repository"], "owner/original")
            saved = store.save("dev", CONFIG)
            self.assertTrue(saved["credential_configured"])
            self.assertEqual(len(saved["revision"]), 64)
            with patch.dict("os.environ", {"RMM_PLUGIN_REPOSITORY": "owner/new"}):
                reopened = PluginSourceStore(root)
                self.assertEqual(reopened.get()["repository"], "owner/original")
                self.assertEqual(reopened.get("dev"), saved)
            raw = (Path(root) / "plugin-sources.json").read_text()
            self.assertNotIn("private-secret", raw)
            self.assertNotIn("credential_configured", raw)
            self.assertEqual(Path(root).stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            updated = store.save("dev", {**CONFIG, "branch": "stable"})
            self.assertNotEqual(updated["revision"], saved["revision"])
            self.assertTrue(store.remove("default"))
            self.assertTrue(store.remove("dev"))
            self.assertEqual(PluginSourceStore(root).list(), [])
            with self.assertRaisesRegex(PluginSourceError, "not found"):
                store.get()

    def test_credentials_are_dedicated_environment_references_only(self):
        for name in ("GH_TOKEN", "HOME", "RMM_API_TOKEN", "RMM_PLUGIN_TOKEN_", "RMM_PLUGIN_TOKEN_bad", "secret-value"):
            with self.subTest(name=name), self.assertRaises(PluginSourceError):
                normalize_source("dev", {**CONFIG, "credential_env": name})
        for extra in ("token", "credential", "password", "revision", "credential_configured"):
            with self.subTest(extra=extra), self.assertRaises(PluginSourceError):
                normalize_source("dev", {**CONFIG, extra: "secret"})
        self.assertEqual(normalize_source("valid", {"repository": "owner/repo"})["branch"], "main")

    def test_invalid_branches_repositories_and_ids_are_rejected(self):
        for branch in ("../main", "/main", "main/", "main..old", "feature//one", "feature/.hidden", "test.lock", "main@{1}", "main:dev", "main*", "a\\b", "ma\nin", "@", "-branch"):
            with self.subTest(branch=branch), self.assertRaises(PluginSourceError):
                normalize_source("dev", {**CONFIG, "branch": branch})
        for repository in ("https://github.com/owner/repo", "../repo", "owner/..", "owner/repo/extra", "owner/repo?query=1"):
            with self.subTest(repository=repository), self.assertRaises(PluginSourceError):
                normalize_source("dev", {**CONFIG, "repository": repository})
        for source_id in ("", "../dev", "id with spaces", "x" * 65):
            with self.subTest(source_id=source_id), self.assertRaises(PluginSourceError):
                normalize_source(source_id, CONFIG)

    def test_corruption_does_not_reset_saved_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            store = PluginSourceStore(root)
            store.path.write_text("invalid JSON")
            status, payload = plugin_sources_api(root, "GET")
            self.assertEqual(status, 400)
            self.assertEqual(store.path.read_text(), "invalid JSON")
            self.assertNotIn("invalid JSON", payload["error"])

    def test_multiple_store_instances_do_not_lose_threaded_updates(self):
        with tempfile.TemporaryDirectory() as root:
            stores = [PluginSourceStore(root) for _ in range(10)]
            threads = [threading.Thread(target=store.save, args=(f"source-{index}", CONFIG)) for index, store in enumerate(stores)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(PluginSourceStore(root).list()), 11)


class PluginSourceRouteTests(unittest.TestCase):
    """Verify authenticated routes and per-owner source storage."""

    def test_server_crud_and_workspace_scope_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            assets = AssetStore(root)
            server = SimpleNamespace(storage_mode="server")
            handler = SimpleNamespace(server_instance=server, _json=Mock(), _api_authorized=lambda *args: True,
                                      _api_path_parts=lambda path: path.removeprefix("/api/v1/").split("/"))
            handler._handle_plugin_sources = RMMHandler._handle_plugin_sources.__get__(handler)
            with patch("server_rmm.asset_store", return_value=assets):
                RMMHandler._handle_api_post(handler, "/api/v1/plugin-sources/dev", CONFIG)
                self.assertEqual(handler._json.call_args.args[0], 200)
                RMMHandler._handle_api_get(handler, "/api/v1/plugin-sources", {})
                self.assertEqual(len(handler._json.call_args.args[1]["sources"]), 2)
                RMMHandler._handle_api_delete(handler, "/api/v1/plugin-sources/dev")
                self.assertEqual(handler._json.call_args.args[0], 200)
            with patch("server_rmm.plugin_sources_api") as api:
                server.storage_mode = "workspace"
                handler._handle_plugin_sources("GET")
                self.assertEqual(handler._json.call_args.args[0], 409)
                server.storage_mode = "server"
                handler._workspace_scope = lambda: "alice"
                handler._handle_plugin_sources("POST", "dev", CONFIG)
                self.assertEqual(handler._json.call_args.args[0], 409)
                api.assert_not_called()

    def test_workspace_http_auth_persistence_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            service = WorkspaceService("http://unused.invalid", "alice", "upstream", Path(root) / "alice", local_token="local-secret")
            handler = type("SourceHandler", (WorkspaceRequestHandler,), {"service": service})
            http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
            try:
                client = RmmApiClient(f"http://127.0.0.1:{http.server_port}", "wrong")
                self.assertEqual(client.request("GET", "/api/v1/plugin-sources")[0], 401)
                client.token = "local-secret"
                with patch.object(service, "_proxy") as proxy:
                    self.assertEqual(client.request("POST", "/api/v1/plugin-sources/dev", CONFIG)[0], 200)
                    rows = client.request("GET", "/api/v1/plugin-sources")[1]["sources"]
                    self.assertEqual([row["id"] for row in rows], ["default", "dev"])
                    proxy.assert_not_called()
                reopened = PluginSourceStore(service.store.asset_store.root)
                self.assertEqual(reopened.get("dev")["branch"], "feature/plugins")
                bob = PluginSourceStore(Path(root) / "bob")
                self.assertEqual([row["id"] for row in bob.list()], ["default"])
                self.assertEqual(client.request("DELETE", "/api/v1/plugin-sources/dev")[0], 200)
                self.assertEqual(client.request("DELETE", "/api/v1/plugin-sources/dev")[0], 404)
            finally:
                http.shutdown(); http.server_close(); thread.join()
                service.pivots.stop_all(); service.store.close()

    def test_release_queries_pass_selected_source_to_owner_backend(self):
        with tempfile.TemporaryDirectory() as root:
            service = WorkspaceService("http://unused.invalid", "alice", "upstream", root, local_token="local-secret")
            try:
                with patch("workspace_service.plugin_release_api", return_value=(200, {"tag": "tag"})) as api:
                    result = service.get_json("/api/v1/plugin-releases", {"source": ["dev"], "tag": ["latest"]})
                    self.assertEqual(result[0], 200)
                    self.assertEqual(api.call_args.kwargs["source"], "dev")
            finally:
                service.pivots.stop_all(); service.store.close()


if __name__ == "__main__":
    unittest.main()
