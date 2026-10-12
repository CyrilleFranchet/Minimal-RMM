"""Unit coverage for the PE plugin operator endpoints and plugin serving."""

import hashlib
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm
from rmm_tun import normalize_tun_config
from server_rmm import API_PREFIX, RMMHandler


def _fake_session(session_id="12345678-1234-1234-1234-123456789012"):
    return SimpleNamespace(id=session_id)


class FakeApiHandler:
    """Minimal handler surface required by the plugin API routes."""

    def __init__(self, session=None, server_instance=None):
        self.responses = []
        self.server_instance = server_instance or SimpleNamespace(
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
        self.assertIn("socks", server_rmm.plugin_build_targets())

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


class TunApiTests(unittest.TestCase):
    """Verify server-owned TUN lifecycle endpoint validation and state."""

    def _handler(self, session=None, **overrides):
        server = SimpleNamespace(
            resolve_session=lambda ref: session,
            tun_active=Mock(return_value=False),
            tun_plugin_error=Mock(return_value=None),
            start_tun=Mock(return_value={"session_id": session.id if session else ""}),
            stop_tun=Mock(return_value=True),
            list_tun_status=Mock(return_value=[]),
        )
        for name, value in overrides.items():
            setattr(server, name, value)
        return FakeApiHandler(session=session, server_instance=server)

    def test_normalize_accepts_device_route_and_stack_options(self):
        config = normalize_tun_config(
            {
                "device": "tun://rmm0",
                "route": "10.20.0.0/16",
                "tun_address": "198.18.0.1/15",
                "interface": "eth0",
                "mtu": 1400,
                "udp_timeout_seconds": 90,
            }
        )
        self.assertEqual(config["device"], "tun://rmm0")
        self.assertEqual(config["device_name"], "rmm0")
        self.assertEqual(config["route"], "10.20.0.0/16")
        self.assertEqual(config["mtu"], 1400)
        self.assertEqual(config["udp_timeout_seconds"], 90)

    def test_start_passes_normalized_config(self):
        session = _fake_session()
        handler = self._handler(session)
        RMMHandler._handle_api_post(
            handler,
            f"{API_PREFIX}/sessions/abcd1234/tun",
            {
                "device": "tun://rmm0",
                "route": "10.20.0.0/16",
                "tun_address": "198.18.0.1/15",
                "interface": "eth0",
                "mtu": 1400,
                "udp_timeout_seconds": 90,
            },
        )
        status, body = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body["tun_active"])
        handler.server_instance.start_tun.assert_called_once_with(
            session.id,
            {
                "device": "tun://rmm0",
                "device_name": "rmm0",
                "route": "10.20.0.0/16",
                "tun_address": "198.18.0.1/15",
                "interface": "eth0",
                "mtu": 1400,
                "udp_timeout_seconds": 90,
            },
        )

    def test_start_rejects_unsafe_address(self):
        handler = self._handler(_fake_session())
        RMMHandler._handle_api_post(
            handler,
            f"{API_PREFIX}/sessions/abcd1234/tun",
            {"device": "https://proxy.example:11601"},
        )
        status, body = handler.responses[-1]
        self.assertEqual((status, body["error"]), (400, "invalid_tun_config"))

    def test_stop_and_list(self):
        session = _fake_session()
        handler = self._handler(session)
        RMMHandler._handle_api_post(
            handler,
            f"{API_PREFIX}/sessions/abcd1234/tun",
            {"stop": True},
        )
        self.assertEqual(handler.responses[-1][0], 200)
        handler = self._handler(
            list_tun_status=Mock(return_value=[{"session_id": session.id}])
        )
        RMMHandler._handle_api_get(handler, f"{API_PREFIX}/tun", {})
        self.assertEqual(handler.responses[-1], (200, {"count": 1, "relays": [{"session_id": session.id}]}))


class PluginServeTests(unittest.TestCase):
    """Verify _serve_plugin_tool serving and rejection paths."""

    def test_serves_plugin_bytes(self):
        payload = b"MZpayload"
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tool.dll"), "wb") as handle:
                handle.write(payload)
            with patch("server_rmm.PLUGINS_DIR", tmp):
                handler = FakeServeHandler()
                handler.server_instance = SimpleNamespace(log=Mock())
                served = RMMHandler._serve_plugin_tool(
                    handler,
                    "tool.dll",
                    "12345678-1234-1234-1234-123456789012",
                )
        self.assertTrue(served)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.body, payload)
        handler.server_instance.log.assert_called_once()
        self.assertIn("Plugin downloaded: session=12345678 plugin=tool.dll bytes=9", handler.server_instance.log.call_args.args[0])

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

    def test_serves_checked_in_powershell_pivot_plugin(self):
        payload = b"function global:Start-RmmSocksChannelPlugin {}"
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = os.path.join(tmp, "socks.ps1")
            with open(plugin_path, "wb") as handle:
                handle.write(payload)
            with patch("server_rmm.PLUGINS_DIR", os.path.join(tmp, "artifacts")):
                with patch("server_rmm.AGENT_PLUGINS_DIR", tmp):
                    with patch("server_rmm.POWERSHELL_SOCKS_PLUGIN", plugin_path):
                        handler = FakeServeHandler()
                        handler.server_instance = SimpleNamespace(log=Mock())
                        served = RMMHandler._serve_plugin_tool(
                            handler,
                            "socks.ps1",
                            "12345678-1234-1234-1234-123456789012",
                        )
        self.assertTrue(served)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.body, payload)
        self.assertIn(("Content-Type", "text/plain; charset=utf-8"), handler.headers)

    def test_serves_checked_in_powershell_screenshot_plugin(self):
        payload = b"function global:Invoke-RmmScreenshotPlugin {}"
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = os.path.join(tmp, "screenshot.ps1")
            with open(plugin_path, "wb") as handle:
                handle.write(payload)
            with patch("server_rmm.PLUGINS_DIR", os.path.join(tmp, "artifacts")):
                with patch("server_rmm.AGENT_PLUGINS_DIR", tmp):
                    with patch("server_rmm.POWERSHELL_SCREENSHOT_PLUGIN", plugin_path):
                        handler = FakeServeHandler()
                        handler.server_instance = SimpleNamespace(log=Mock())
                        served = RMMHandler._serve_plugin_tool(
                            handler,
                            "screenshot.ps1",
                            "12345678-1234-1234-1234-123456789012",
                        )
        self.assertTrue(served)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.body, payload)
        self.assertIn(("Content-Type", "text/plain; charset=utf-8"), handler.headers)


class PluginLifecycleTests(unittest.TestCase):
    """Verify server-side plugin lifecycle correlation and redaction."""

    def _server(self):
        server = object.__new__(server_rmm.RMMServer)
        server.sessions = {}
        server.killed_sessions = set()
        server.session_lock = threading.Lock()
        server.tty_lock = threading.Lock()
        server._cli_use_ptk = True
        server.tty_print = Mock()
        server.log = Mock()
        server._record_event = Mock()
        return server

    def test_metadata_rejects_non_plugin_commands(self):
        self.assertEqual(
            server_rmm.plugin_command_metadata("__PE_LOAD__ tool.dll Run secret"),
            {"plugin": "tool.dll", "export": "Run"},
        )
        self.assertEqual(
            server_rmm.plugin_command_metadata("__SCREENSHOT__"),
            {"plugin": "screenshot.dll", "export": "Run"},
        )
        self.assertEqual(
            server_rmm.plugin_command_metadata("__SCREENSHOT__", "powershell"),
            {"plugin": "screenshot.ps1", "export": "Run"},
        )
        self.assertIsNone(server_rmm.plugin_command_metadata("whoami"))

    def test_lifecycle_log_omits_plugin_input(self):
        server = self._server()
        session = server_rmm.Session(
            "12345678-1234-1234-1234-123456789012", "host", "user"
        )
        server.sessions[session.id] = session
        secret_input = "operator-secret"

        server.set_command(
            session.id,
            f"__PE_LOAD__ tool.dll Run {secret_input}",
            "oneshot",
        )
        command, response_type = server.get_command(session.id)
        self.assertEqual((command, response_type), ("__PE_LOAD__ tool.dll Run operator-secret", "execute"))
        server.handle_result(session.id, "plugin result", "output")

        messages = [call.args[0] for call in server.log.call_args_list]
        self.assertTrue(any(message.startswith("Plugin queued:") for message in messages))
        self.assertTrue(any(message.startswith("Plugin dispatched:") for message in messages))
        self.assertTrue(any(message.startswith("Plugin completed:") for message in messages))
        self.assertNotIn(secret_input, "\n".join(messages))


class PivotPluginReadinessTests(unittest.TestCase):
    """Verify pivot start rejects sessions whose agent plugin is unavailable."""

    def _server(self):
        return object.__new__(server_rmm.RMMServer)

    def test_go_requires_plugin_capability(self):
        session = SimpleNamespace(agent_type="go", capabilities=[])
        detail = self._server().pivot_plugin_error(session)
        self.assertIn("socks-plugin", detail)

    def test_go_accepts_built_plugin(self):
        session = SimpleNamespace(agent_type="go", capabilities=["socks-plugin"])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "socks.dll"), "wb") as handle:
                handle.write(b"MZpayload")
            with patch("server_rmm.PLUGINS_DIR", tmp):
                self.assertIsNone(self._server().pivot_plugin_error(session))

    def test_tun_requires_socks_plugin_capability(self):
        session = SimpleNamespace(agent_type="go", capabilities=[])
        detail = self._server().tun_plugin_error(session)
        self.assertIn("socks-plugin", detail)

    def test_tun_accepts_built_transport_and_helper(self):
        session = SimpleNamespace(agent_type="go", capabilities=["socks-plugin"])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "socks.dll"), "wb") as handle:
                handle.write(b"MZpayload")
            helper = os.path.join(tmp, "rmm-tun")
            with open(helper, "wb") as handle:
                handle.write(b"helper")
            os.chmod(helper, 0o700)
            with patch("server_rmm.PLUGINS_DIR", tmp):
                with patch("server_rmm.TUN_HELPER", helper):
                    self.assertIsNone(self._server().tun_plugin_error(session))

    def test_powershell_uses_checked_in_plugin(self):
        session = SimpleNamespace(agent_type="powershell", capabilities=["socks-plugin"])
        with tempfile.NamedTemporaryFile(suffix=".ps1") as handle:
            with patch("server_rmm.POWERSHELL_SOCKS_PLUGIN", handle.name):
                with patch("server_rmm.AGENT_PLUGINS_DIR", os.path.dirname(handle.name)):
                    self.assertIsNone(self._server().pivot_plugin_error(session))

    def test_unknown_agent_type_is_rejected(self):
        session = SimpleNamespace(agent_type="custom", capabilities=[])
        detail = self._server().pivot_plugin_error(session)
        self.assertIn("custom", detail)


if __name__ == "__main__":
    unittest.main()
