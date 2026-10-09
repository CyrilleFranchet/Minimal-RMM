"""Unit coverage for the PE plugin operator endpoints and plugin serving."""

import hashlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm
from server_rmm import API_PREFIX, RMMHandler


def _fake_session(session_id="12345678-1234-1234-1234-123456789012"):
    return SimpleNamespace(id=session_id)


class FakeApiHandler:
    """Minimal handler surface required by the plugin API routes."""

    def __init__(self, session=None):
        self.responses = []
        self.server_instance = SimpleNamespace(
            resolve_session=lambda ref: session,
            set_command=Mock(return_value=True),
            record_operator_action=Mock(),
        )

    def _api_authorized(self, qs=None):
        return True

    def _api_unauthorized(self):
        self.responses.append((401, {"error": "unauthorized"}))

    def _api_path_parts(self, path):
        return [part for part in path[len(API_PREFIX) + 1 :].split("/") if part]

    def _json(self, status, body):
        self.responses.append((status, body))


class FakeServeHandler:
    """Minimal handler surface required by _serve_plugin_tool."""

    def __init__(self):
        self.responses = []
        self.headers = []
        self.body = b""
        self.status = None

    def _respond(self, status, text):
        self.responses.append((status, text))

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        pass

    def _safe_write(self, data):
        self.body = data


class PluginDirectoryTests(unittest.TestCase):
    """Verify list_plugin_files only exposes safe plugin names."""

    def test_listing_skips_unsafe_names_and_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = os.path.join(tmp, "tool.dll")
            with open(plugin, "wb") as handle:
                handle.write(b"MZpayload")
            os.makedirs(os.path.join(tmp, "subdir"))
            with open(os.path.join(tmp, "bad name!.dll"), "wb") as handle:
                handle.write(b"skip")
            with patch("server_rmm.PLUGINS_DIR", tmp):
                plugins = server_rmm.list_plugin_files()
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["name"], "tool.dll")
        self.assertEqual(plugins[0]["size"], len(b"MZpayload"))
        self.assertEqual(plugins[0]["sha256"], hashlib.sha256(b"MZpayload").hexdigest())


class PluginApiTests(unittest.TestCase):
    """Verify GET /plugins and POST /sessions/<ref>/pe behavior."""

    def call_get(self, path, session=None):
        handler = FakeApiHandler(session=session)
        RMMHandler._handle_api_get(handler, path, {})
        return handler

    def call_post(self, body, session=_fake_session()):
        handler = FakeApiHandler(session=session)
        RMMHandler._handle_api_post(handler, f"{API_PREFIX}/sessions/abcd1234/pe", body)
        return handler

    def test_get_plugins_lists_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tool.dll"), "wb") as handle:
                handle.write(b"MZpayload")
            with patch("server_rmm.PLUGINS_DIR", tmp):
                handler = self.call_get(f"{API_PREFIX}/plugins")
        status, body = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in body["plugins"]], ["tool.dll"])
        self.assertIn("rclone-exfil", body["targets"])
        for key in ("go", "cross_compiler", "cross_compiler_name"):
            self.assertIn(key, body["toolchain"])

    def test_pe_load_queues_full_command(self):
        handler = self.call_post({"plugin": "tool.dll", "export": "Run", "input": "hello world"})
        status, body = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(body["queued"], "__PE_LOAD__ tool.dll Run hello world")
        handler.server_instance.set_command.assert_called_once_with(
            body["session_id"], "__PE_LOAD__ tool.dll Run hello world", "oneshot"
        )
        handler.server_instance.record_operator_action.assert_called_once()

    def test_pe_load_defaults_export_and_omits_input(self):
        handler = self.call_post({"plugin": "tool.dll"})
        status, body = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(body["queued"], "__PE_LOAD__ tool.dll Run")

    def test_pe_load_rejects_missing_plugin(self):
        status, body = self.call_post({}).responses[-1]
        self.assertEqual((status, body["error"]), (400, "missing_plugin"))

    def test_pe_load_rejects_unsafe_plugin_name(self):
        status, body = self.call_post({"plugin": "../evil.dll"}).responses[-1]
        self.assertEqual((status, body["error"]), (400, "invalid_plugin"))

    def test_pe_load_rejects_invalid_export(self):
        status, body = self.call_post({"plugin": "tool.dll", "export": "9bad"}).responses[-1]
        self.assertEqual((status, body["error"]), (400, "invalid_export"))

    def test_pe_load_rejects_newline_input(self):
        status, body = self.call_post({"plugin": "tool.dll", "input": "a\nb"}).responses[-1]
        self.assertEqual((status, body["error"]), (400, "invalid_input"))

    def test_pe_load_unknown_session_returns_404(self):
        handler = FakeApiHandler(session=None)
        RMMHandler._handle_api_post(handler, f"{API_PREFIX}/sessions/abcd1234/pe", {"plugin": "x.dll"})
        status, body = handler.responses[-1]
        self.assertEqual((status, body["error"]), (404, "session_not_found"))


class PluginBuildTests(unittest.TestCase):
    """Verify POST /plugins/build validation and toolchain probing."""

    def call_post(self, body):
        handler = FakeApiHandler()
        RMMHandler._handle_api_post(handler, f"{API_PREFIX}/plugins/build", body)
        return handler.responses[-1]

    def test_build_targets_include_checked_in_plugins(self):
        self.assertIn("rclone-exfil", server_rmm.plugin_build_targets())

    def test_missing_name_returns_400(self):
        status, body = self.call_post({})
        self.assertEqual((status, body["error"]), (400, "missing_name"))

    def test_unknown_target_returns_400(self):
        status, body = self.call_post({"name": "does-not-exist"})
        self.assertEqual((status, body["error"]), (400, "invalid_plugin_build"))

    def test_missing_toolchain_returns_503(self):
        with patch("server_rmm.shutil.which", return_value=None):
            status, body = self.call_post({"name": "rclone-exfil"})
        self.assertEqual((status, body["error"]), (503, "plugin_build_failed"))

    def test_rejects_unsafe_output_names(self):
        with patch("server_rmm.shutil.which", return_value="/usr/local/bin/go"):
            with self.assertRaises(ValueError):
                server_rmm.build_plugin("rclone-exfil", "../evil.dll")
            with self.assertRaises(ValueError):
                server_rmm.build_plugin("rclone-exfil", "tool.exe")

    def test_success_maps_build_result(self):
        built = {"ok": True, "name": "rclone-exfil", "output": "x.dll", "size": 10, "sha256": "abc"}
        with patch("server_rmm.build_plugin", return_value=built):
            status, body = self.call_post({"name": "rclone-exfil", "output": "x.dll"})
        self.assertEqual(status, 200)
        self.assertEqual(body["output"], "x.dll")


class PluginServeTests(unittest.TestCase):
    """Verify _serve_plugin_tool serving and rejection paths."""

    def test_serves_plugin_bytes(self):
        payload = b"MZpayload"
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tool.dll"), "wb") as handle:
                handle.write(payload)
            with patch("server_rmm.PLUGINS_DIR", tmp):
                handler = FakeServeHandler()
                served = RMMHandler._serve_plugin_tool(handler, "tool.dll")
        self.assertTrue(served)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.body, payload)

    def test_rejects_unsafe_and_missing_names(self):
        handler = FakeServeHandler()
        self.assertTrue(RMMHandler._serve_plugin_tool(handler, "../escape"))
        self.assertEqual(handler.responses[-1][0], 400)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("server_rmm.PLUGINS_DIR", tmp):
                handler = FakeServeHandler()
                served = RMMHandler._serve_plugin_tool(handler, "missing.dll")
        self.assertTrue(served)
        self.assertEqual(handler.responses[-1][0], 404)


if __name__ == "__main__":
    unittest.main()
