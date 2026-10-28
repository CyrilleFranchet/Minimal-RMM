"""Verify release selection and checksum pins across CLI and shared tools."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock

from rmm_cli import RmmApiClient, build_parser
from rmm_tools import TOOL_HANDLERS

TAG = "plugins-" + "a" * 40
DIGEST = "b" * 64


class PluginReleaseSurfaceTests(unittest.TestCase):
    """Ensure operator clients install the inspected release without credentials."""

    def test_cli_inspects_then_installs_exact_release_and_digest(self):
        client = RmmApiClient("http://unused")
        client.request = Mock(return_value=(200, {"repository": "owner/plugins", "tag": TAG, "plugins": []}))
        parser = build_parser()
        with contextlib.redirect_stdout(io.StringIO()):
            args = parser.parse_args(["plugin", "releases", "--json"])
            args.func(client, {}, args)
        client.request.assert_called_with("GET", "/api/v1/plugin-releases?tag=latest", timeout=150)
        client.request.return_value = (200, {"asset": {"name": "example.dll", "sha256": DIGEST}})
        with contextlib.redirect_stdout(io.StringIO()):
            args = parser.parse_args(["plugin", "install", "example.dll", "--tag", TAG, "--sha256", DIGEST])
            args.func(client, {}, args)
        client.request.assert_called_with("POST", "/api/v1/plugin-releases/install", {
            "name": "example.dll", "tag": TAG, "sha256": DIGEST,
        }, timeout=240)

    def test_latest_cannot_be_installed_by_client_or_tool(self):
        client = RmmApiClient("http://unused")
        client.request = Mock()
        with self.assertRaisesRegex(ValueError, "exact release tag"):
            client.install_plugin_release("example.dll", "latest")
        result = json.loads(TOOL_HANDLERS["install_plugin_release"](client, {"name": "example.dll", "tag": "latest"}))
        self.assertFalse(result["ok"])
        client.request.assert_not_called()

    def test_shared_tool_preserves_pin_and_failure_status(self):
        client = Mock()
        client.install_plugin_release.return_value = (502, {"local_installed": True, "error": "sync failed"})
        result = json.loads(TOOL_HANDLERS["install_plugin_release"](client, {"name": "example.dll", "tag": TAG, "sha256": DIGEST}))
        client.install_plugin_release.assert_called_once_with("example.dll", TAG, DIGEST)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 502)
        self.assertTrue(result["data"]["local_installed"])
