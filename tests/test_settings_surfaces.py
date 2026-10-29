"""CLI and MCP settings requests, redaction, and one-run profile configuration."""
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from rmm_cli import RmmApiClient, build_parser
from rmm_tools import OPENAI_TOOLS, TOOL_HANDLERS, make_client, tool_settings


class SettingsSurfaceTests(unittest.TestCase):
    """Exercise real command parsing and API request payloads without a service."""

    def setUp(self):
        self.client = RmmApiClient("http://example.invalid", "operator")
        self.client.request = Mock(return_value=(200, {"scope": "server", "settings": [], "restart_required": False}))

    def run_command(self, argv):
        """Run a parsed CLI action and capture its safe response output."""
        args = build_parser().parse_args(argv)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            args.func(self.client, {}, args)
            return output.getvalue()

    def test_show_set_unset_and_typed_values(self):
        self.run_command(["settings", "show"])
        self.client.request.assert_called_with("GET", "/api/v1/settings")
        for raw, expected in (("42", 42), ("false", False), ('{"alice":"token"}', {"alice": "token"}), ("named", "named")):
            self.run_command(["settings", "set", "KEY", raw])
            self.client.request.assert_called_with("PATCH", "/api/v1/settings", {"settings": {"KEY": expected}})
        self.run_command(["settings", "unset", "KEY"])
        self.client.request.assert_called_with("PATCH", "/api/v1/settings", {"settings": {"KEY": None}})

    def test_explicit_workspace_auth_never_inherits_administrator_credentials(self):
        """Per-request workspace credentials must not inherit host-level defaults."""
        headers = {"X-RMM-Workspace": "alice", "X-RMM-Workspace-Token": "alice-token"}
        with patch("rmm_tools.DEFAULT_RMM_TOKEN", "administrator"), patch("rmm_tools.settings_getenv", return_value="other-workspace"):
            client = make_client("http://example.invalid", token="", extra_headers=headers)
            self.assertEqual(client.token, "")
            self.assertEqual(client.extra_headers, headers)
            self.assertNotIn("Authorization", client._headers())
            self.assertNotIn("Authorization", make_client("http://example.invalid", extra_headers=headers)._headers())
            explicit = make_client("http://example.invalid", token="operator")
            self.assertEqual(explicit.extra_headers, {})
            default = make_client("http://example.invalid")
            self.assertEqual(default.token, "administrator")

    def test_secret_value_file_and_stdin_do_not_echo_values(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "secret"
            secret.write_text("private-token\n", encoding="utf-8")
            output = self.run_command(["settings", "set", "RMM_PLUGIN_TOKEN_TEAM", "--value-file", str(secret)])
            self.client.request.assert_called_with("PATCH", "/api/v1/settings", {"settings": {"RMM_PLUGIN_TOKEN_TEAM": "private-token"}})
            self.assertNotIn("private-token", output)
        with patch("sys.stdin", io.StringIO('{"alice":"private-token"}\n')):
            output = self.run_command(["settings", "set", "RMM_WORKSPACES", "--value-file", "-"])
            self.client.request.assert_called_with("PATCH", "/api/v1/settings", {"settings": {"RMM_WORKSPACES": {"alice": "private-token"}}})
            self.assertNotIn("private-token", output)

    def test_import_and_profile_configuration_from_stdin(self):
        payload = {"settings": {"RMM_CLOUDFLARE_MODE": "named", "RMM_PROFILE_AUTOSTART": False}}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            self.run_command(["settings", "import", "-"])
        self.client.request.assert_called_with("PATCH", "/api/v1/settings", payload)
        with patch("sys.stdin", io.StringIO('{"mode":"named","public_url":"https://lab.example"}')):
            self.run_command(["profile", "start", "cloudflared", "--config-file", "-"])
        self.client.request.assert_called_with("POST", "/api/v1/profile", {"name": "cloudflared", "config": {"mode": "named", "public_url": "https://lab.example"}}, timeout=45)

    def test_invalid_input_does_not_make_requests(self):
        for argv in (["settings", "set", "KEY"], ["settings", "set", "KEY", "value", "--value-file", "-"]):
            with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
                self.run_command(argv)
        with patch("sys.stdin", io.StringIO('{"wrong":{}}')), self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
            self.run_command(["settings", "import", "-"])
        self.client.request.assert_not_called()

    def test_mcp_catalog_and_handlers_match_settings_contract(self):
        schema = next(item["function"] for item in OPENAI_TOOLS if item["function"]["name"] == "settings")
        self.assertEqual(schema["parameters"]["properties"]["action"]["enum"], ["show", "update"])
        result = json.loads(TOOL_HANDLERS["settings"](self.client, {"action": "update", "settings": {"RMM_PROFILE_AUTOSTART": False}}))
        self.assertTrue(result["ok"])
        self.client.request.assert_called_with("PATCH", "/api/v1/settings", {"settings": {"RMM_PROFILE_AUTOSTART": False}})
        self.client.request.reset_mock()
        self.assertFalse(json.loads(tool_settings(self.client, "update"))["ok"])
        self.client.request.assert_not_called()

    def test_mcp_settings_uses_cached_api_client(self):
        """Load optional MCP wrappers with a small decorator adapter, without MCP dependencies."""
        module = ModuleType("mcp.server.fastmcp")
        module.FastMCP = lambda *args, **kwargs: Mock(tool=lambda: lambda function: function)
        path = Path(__file__).resolve().parents[1] / "mcp_rmm_server.py"
        spec = importlib.util.spec_from_file_location("settings_mcp_fixture", path)
        wrappers = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"mcp": ModuleType("mcp"), "mcp.server": ModuleType("mcp.server"), "mcp.server.fastmcp": module}):
            spec.loader.exec_module(wrappers)
        with patch.object(wrappers, "make_client", return_value=self.client) as factory:
            self.assertTrue(json.loads(wrappers.settings())["ok"])
            self.assertTrue(json.loads(wrappers.settings("update", {"RMM_PROFILE_AUTOSTART": False}))["ok"])
            factory.assert_called_once()
        self.client.request.assert_called_with("PATCH", "/api/v1/settings", {"settings": {"RMM_PROFILE_AUTOSTART": False}})

    def test_cli_bootstrap_reads_selected_client_file_before_defaults(self):
        """An executable CLI loads its file before argparse captures the URL default."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "client.json"
            config.write_text(json.dumps({"schema": "minimal-rmm-settings-v1", "scope": "client",
                                         "settings": {"RMM_SERVER_URL": "http://configured-client.invalid:9999"}}), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if key not in {
                "RMM_SERVER_URL", "RMM_BASE_URL", "RMM_API_TOKEN", "RMM_CONFIG_FILE", "RMM_INTERNAL_MCP"}}
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "rmm_cli.py"),
                                     "--config", str(config), "--help"], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("http://configured-client.invalid:9999", result.stdout)


if __name__ == "__main__":
    unittest.main()
