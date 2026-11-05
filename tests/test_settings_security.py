"""Regression coverage for configuration ownership and AI credential isolation."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import rmm_settings
from rmm_mcp_client import McpRmmSession
from rmm_tools import make_client
from server_rmm import RMMHandler
from workspace_service import WorkspaceService


class ScopedHandler:
    """Exercise real route guards using an already authenticated workspace."""

    def __init__(self):
        self.responses = []
        broker = Mock()
        broker.resolve_ai_context.return_value = (200, {
            'exegol_mcp_enabled': True, 'exegol_mcp_url': 'http://trusted:8000/mcp', 'exegol_mcp_token': 'trusted-token'})
        self.server_instance = SimpleNamespace(save_ai_chat=Mock(), bloodhound_broker=broker)
        self.headers = {"X-RMM-Workspace-Token": "workspace-secret"}

    def _api_authorized(self, *_args):
        return True

    def _workspace_scope(self):
        return "alice"

    def _api_path_parts(self, path):
        return path.removeprefix("/api/v1/").split("/")

    def _api_token_from_request(self):
        return ""

    def _operator_api_base_url(self):
        return "http://127.0.0.1:8080/api/v1"

    def _json(self, status, body):
        self.responses.append((status, body))


class SettingsSecurityTests(unittest.TestCase):
    def setUp(self):
        runtime = patch('rmm_exegol_runtime.get_runtime')
        self.runtime = runtime.start().return_value
        self.runtime.status.return_value = {'exegol_status': 'ready'}
        self.addCleanup(runtime.stop)

    def test_retired_global_plugin_path_does_not_override_delivery_cache(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "settings.json"
            path.write_text(json.dumps({"schema": rmm_settings.SCHEMA, "scope": "server",
                                        "settings": {"RMM_PLUGINS_DIR": "/legacy/global-plugins"}}))
            store = rmm_settings.SettingsStore(path, "server", environment={"RMM_LOG_DIR": root})
            self.assertEqual(store.saved_at_start["RMM_PLUGINS_DIR"], "/legacy/global-plugins")
            rows = {row["key"]: row for row in store.snapshot()["settings"]}
            self.assertIn("ignored", rows["RMM_PLUGINS_DIR"]["label"])
            self.assertEqual(rows["RMM_PLUGIN_CACHE_DIR"]["value"], str(Path(root) / "plugin-cache"))
            overridden = rmm_settings.SettingsStore(path, "server", environment={
                "RMM_LOG_DIR": root, "RMM_PLUGIN_CACHE_DIR": "/custom/delivery-cache"})
            rows = {row["key"]: row for row in overridden.snapshot()["settings"]}
            self.assertEqual(rows["RMM_PLUGIN_CACHE_DIR"]["value"], "/custom/delivery-cache")
            self.assertEqual(rows["RMM_PLUGIN_CACHE_DIR"]["source"], "environment")

    def test_workspace_ai_rejects_missing_exegol_without_forwarding(self):
        service = WorkspaceService.__new__(WorkspaceService)
        service.store = SimpleNamespace(data_dir=Path('/tmp/test-workspace'))
        service._proxy = Mock()
        self.runtime.status.return_value = {'state': 'blocked', 'exegol_status': 'unavailable'}
        status, payload, _ = service.post_json('/api/v1/ai/jobs', {'messages': []})
        self.assertEqual(status, 503)
        self.assertEqual(payload['error'], 'exegol_workspace_not_ready')
        service._proxy.assert_not_called()

    """Keep workspace configuration separate from central administration."""

    def test_workspace_cannot_read_or_write_central_settings_or_ui_secrets(self):
        for path in ("settings", "ui/config"):
            for method in ("GET", "PATCH"):
                with self.subTest(path=path, method=method):
                    handler = ScopedHandler()
                    if method == "GET":
                        RMMHandler._handle_api_get(handler, "/api/v1/" + path, {})
                    else:
                        RMMHandler._handle_api_patch(handler, "/api/v1/" + path, {"settings": {}})
                    self.assertEqual(handler.responses, [(403, {"error": "administrator_required"})])

    def test_workspace_cannot_start_or_stop_central_profile(self):
        for body in ({"name": "cloudflared"}, {"stop": True}):
            handler = ScopedHandler()
            RMMHandler._handle_api_post(handler, "/api/v1/profile", body)
            self.assertEqual(handler.responses, [(403, {"error": "administrator_required"})])

    def test_workspace_ai_never_receives_central_admin_token(self):
        handler = ScopedHandler()
        with patch("server_rmm.API_TOKEN", "central-admin-secret"), patch(
            "rmm_ai.run_ai_chat", return_value={"ok": True, "message": "done"}
        ) as run:
            RMMHandler._handle_api_post(handler, "/api/v1/ai/chat", {
                "provider": "openai", "api_key": "provider-secret", "skill_documents": [],
                "exegol_mcp_enabled": False, "exegol_mcp_url": "http://untrusted:8000/mcp", "exegol_mcp_token": "untrusted",
                "messages": [{"role": "user", "content": "show settings"}],
            })
        self.assertEqual(handler.responses[0][0], 200)
        self.assertEqual(run.call_args.kwargs["rmm_token"], "")
        self.assertEqual(run.call_args.kwargs["exegol_mcp_url"], "http://trusted:8000/mcp")
        self.assertEqual(run.call_args.kwargs["exegol_mcp_token"], "trusted-token")
        self.assertTrue(run.call_args.kwargs["exegol_mcp_enabled"])
        self.assertEqual(run.call_args.kwargs["rmm_headers"], {
            "X-RMM-Operator-Relay": "1",
            "X-RMM-Workspace": "alice", "X-RMM-Workspace-Token": "workspace-secret"})

    def test_shared_client_does_not_restore_admin_token_for_workspace(self):
        with patch("rmm_tools.DEFAULT_RMM_TOKEN", "central-admin-secret"), patch(
            "rmm_tools.RmmApiClient"
        ) as client:
            make_client("http://localhost:8080", "", {"X-RMM-Workspace": "alice"})
        self.assertEqual(client.call_args.args[1], "")

    def test_internal_bootstrap_ignores_external_client_config(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "settings.json"
            path.write_text(json.dumps({"schema": rmm_settings.SCHEMA, "scope": "server",
                                        "settings": {"RMM_API_TOKEN": "admin-secret"}}))
            with patch.object(rmm_settings, "_active", None), patch.dict(os.environ, {
                "RMM_INTERNAL_MCP": "1", "RMM_CONFIG_FILE": str(path),
                "RMM_WORKSPACE_ID": "alice", "RMM_WORKSPACE_TOKEN": "workspace-secret",
            }, clear=True):
                store = rmm_settings.bootstrap("client", ["--config", str(path)])
                self.assertEqual(store.saved_at_start, {})
                self.assertEqual(rmm_settings.getenv("RMM_API_TOKEN"), "")
                self.assertEqual(rmm_settings.getenv("RMM_WORKSPACE_TOKEN"), "workspace-secret")

    def test_internal_mcp_subprocess_scrubs_inherited_auth_and_config(self):
        captured = []
        class AsyncResource:
            async def __aenter__(self):
                return "read", "write"

            async def __aexit__(self, *_args):
                return False

        stack = AsyncResource()
        session = AsyncResource()

        def parameters(**kwargs):
            captured.append(kwargs)
            return kwargs

        fake_mcp = SimpleNamespace(ClientSession=lambda *_args: session, StdioServerParameters=parameters)
        fake_stdio = SimpleNamespace(stdio_client=lambda _params: stack)
        inherited = {"RMM_CONFIG_FILE": "/private/central.json", "RMM_API_TOKEN": "admin-secret",
                     "RMM_WORKSPACE_ID": "wrong", "RMM_WORKSPACE_TOKEN": "wrong-secret",
                     "RMM_BASE_URL": "http://wrong"}
        for headers in ({}, {"X-RMM-Workspace": "alice", "X-RMM-Workspace-Token": "workspace-secret"}):
            with self.subTest(workspace=bool(headers)), patch.dict(os.environ, inherited, clear=True), patch.dict(
                "sys.modules", {"mcp": fake_mcp, "mcp.client": SimpleNamespace(), "mcp.client.stdio": fake_stdio}
            ), patch.object(McpRmmSession, "_load_tools_and_instructions", new=AsyncMock()):
                async def exercise():
                    async with McpRmmSession("http://central:8080", "request-token", headers):
                        pass

                asyncio.run(exercise())
            env = captured[-1]["env"]
            self.assertEqual(env["RMM_INTERNAL_MCP"], "1")
            self.assertNotIn("RMM_CONFIG_FILE", env)
            self.assertNotIn("RMM_BASE_URL", env)
            if headers:
                self.assertNotIn("RMM_API_TOKEN", env)
                self.assertEqual(env["RMM_WORKSPACE_ID"], "alice")
                self.assertEqual(env["RMM_WORKSPACE_TOKEN"], "workspace-secret")
            else:
                self.assertEqual(env["RMM_API_TOKEN"], "request-token")
                self.assertNotIn("RMM_WORKSPACE_ID", env)
                self.assertNotIn("RMM_WORKSPACE_TOKEN", env)

    def test_workspace_forwards_provider_defaults_but_strips_mcp_overrides(self):
        service = WorkspaceService.__new__(WorkspaceService)
        service.store = SimpleNamespace(data_dir=Path('/tmp/test-workspace'), selected_skill_documents=lambda _ids: [])
        service._proxy = Mock(return_value=(200, {"ok": True}, "application/json"))
        defaults = {"OPENAI_API_KEY": "saved-key", "RMM_EXEGOL_MCP_URL": "http://exegol:8000",
                    "RMM_EXEGOL_MCP_TOKEN": "saved-exegol-token"}
        with patch("workspace_service.settings_getenv", side_effect=lambda key, fallback="": defaults.get(key, fallback)):
            service.post_json("/api/v1/ai/chat", {"messages": []})
            forwarded = service._proxy.call_args.args[2]
            self.assertEqual(forwarded["api_key"], "saved-key")
            self.assertNotIn("exegol_mcp_token", forwarded)
            self.assertTrue(forwarded["exegol_mcp_enabled"])
            service.post_json("/api/v1/ai/chat", {"api_key": "request-key", "exegol_mcp_enabled": False,
                                                "exegol_mcp_url": "http://request:8000", "messages": []})
            forwarded = service._proxy.call_args.args[2]
            self.assertEqual(forwarded["api_key"], "request-key")
            self.assertNotIn("exegol_mcp_url", forwarded)
            self.assertTrue(forwarded["exegol_mcp_enabled"])

    def test_workspace_does_not_send_saved_exegol_token_to_request_url(self):
        service = WorkspaceService.__new__(WorkspaceService)
        service.store = SimpleNamespace(data_dir=Path('/tmp/test-workspace'), selected_skill_documents=lambda _ids: [])
        service._proxy = Mock(return_value=(200, {"ok": True}, "application/json"))
        defaults = {"OPENAI_API_KEY": "saved-key", "RMM_EXEGOL_MCP_URL": "http://trusted:8000",
                    "RMM_EXEGOL_MCP_TOKEN": "trusted-token"}
        with patch("workspace_service.settings_getenv", side_effect=lambda key, fallback="": defaults.get(key, fallback)):
            service.post_json("/api/v1/ai/chat", {"openai_api_key": "legacy-request-key",
                                                "exegol_mcp_url": "http://different:8000", "messages": []})
        forwarded = service._proxy.call_args.args[2]
        self.assertEqual(forwarded.get("api_key") or forwarded.get("openai_api_key"), "legacy-request-key")
        self.assertNotIn("exegol_mcp_url", forwarded)
        self.assertNotIn("exegol_mcp_token", forwarded)

    def test_explicit_flags_override_environment_and_saved_values(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "settings.json"
            seed = rmm_settings.SettingsStore(path, "server", environment={})
            seed.update({"settings": {"RMM_RCLONE_MAX_BYTES": 100}})
            store = rmm_settings.SettingsStore(path, "server", environment={"RMM_RCLONE_MAX_BYTES": "200"})
            with patch.object(rmm_settings, "_active", store):
                rmm_settings.record_flags({"--rclone-max-bytes": ("RMM_RCLONE_MAX_BYTES", 300)},
                                          ["--rclone-max-bytes=300"])
                self.assertEqual(rmm_settings.getenv("RMM_RCLONE_MAX_BYTES"), "300")
                row = next(row for row in store.snapshot()["settings"] if row["key"] == "RMM_RCLONE_MAX_BYTES")
                self.assertEqual(row["value"], 300)
                self.assertEqual(row["source"], "command line")
                self.assertEqual(row["saved_value"], 100)


class SettingsLiveRefreshTests(unittest.TestCase):
    def test_saved_credentials_are_visible_to_getenv_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/settings.json"
            store = rmm_settings.SettingsStore(path, "workspace", environment={})
            with patch.object(rmm_settings, "_active", store):
                self.assertFalse(rmm_settings.getenv("RMM_BLOODHOUND_TOKEN_ID"))
                store.update({"settings": {
                    "RMM_BLOODHOUND_TOKEN_ID": "0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3",
                    "RMM_BLOODHOUND_TOKEN_KEY": "c2VjcmV0LXRva2VuLXZhbHVl",
                }})
                self.assertEqual(rmm_settings.getenv("RMM_BLOODHOUND_TOKEN_ID"),
                                 "0d8ce6ec-e5d1-4f4a-9d67-7f0b13b1c2d3")
            # A startup environment override still wins over the saved value.
            overridden = rmm_settings.SettingsStore(
                path, "workspace", environment={"RMM_BLOODHOUND_TOKEN_ID": "env-wins"})
            self.assertEqual(overridden.effective["RMM_BLOODHOUND_TOKEN_ID"], "env-wins")
            snapshot = store.snapshot()
            row = next(item for item in snapshot["settings"]
                       if item["key"] == "RMM_BLOODHOUND_TOKEN_ID")
            self.assertTrue(row["secret"])
            self.assertIsNone(row["value"])
            self.assertIsNone(row["saved_value"])


if __name__ == "__main__":
    unittest.main()
