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

    def test_workspace_plugin_cache_is_used_for_owned_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_dir = os.path.join(tmp, "workspaces", "alice")
            os.makedirs(workspace_dir)
            path = os.path.join(workspace_dir, "tool.dll")
            with open(path, "wb") as handle:
                handle.write(b"workspace-plugin")
            owner = SimpleNamespace(
                get_session=lambda _session_id: SimpleNamespace(workspace_id="alice")
            )
            with patch("server_rmm.PLUGINS_DIR", tmp):
                resolved = server_rmm.plugin_path_for_session("tool.dll", "session-id", owner)
            self.assertEqual(resolved, os.path.realpath(path))


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

    def test_plugin_command_queues_plugin_owned_dispatch(self):
        handler = self.call_post(
            {"plugin": "tool.dll", "command": "scan", "input": '{"target":"10.0.0.0/8"}'},
        )
        status, body = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(
            body["queued"],
            '__PLUGIN_COMMAND__ tool.dll scan {"target":"10.0.0.0/8"}',
        )
        handler.server_instance.set_command.assert_called_once_with(
            body["session_id"],
            '__PLUGIN_COMMAND__ tool.dll scan {"target":"10.0.0.0/8"}',
            "oneshot",
        )

    def test_plugin_command_rejects_unsafe_command_name(self):
        status, body = self.call_post({"plugin": "tool.dll", "command": "../scan"}).responses[-1]
        self.assertEqual((status, body["error"]), (400, "invalid_plugin_command"))

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
        self.assertEqual(config["routes"], ["10.20.0.0/16"])
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
                "routes": ["10.20.0.0/16"],
                "route": "10.20.0.0/16",
                "tun_address": "198.18.0.1/15",
                "interface": "eth0",
                "mtu": 1400,
                "udp_timeout_seconds": 90,
            },
        )

    def test_start_passes_multiple_routes(self):
        session = _fake_session()
        handler = self._handler(session)
        RMMHandler._handle_api_post(
            handler,
            f"{API_PREFIX}/sessions/abcd1234/tun",
            {
                "device": "tun://rmm0",
                "routes": ["10.20.0.0/16", "10.30.0.0/16"],
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
                "routes": ["10.20.0.0/16", "10.30.0.0/16"],
                "route": "10.20.0.0/16",
                "tun_address": "198.18.0.1/15",
                "interface": "",
                "mtu": 1500,
                "udp_timeout_seconds": 300,
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

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "notes.txt"), "wb") as handle:
                handle.write(b"not a plugin")
            with patch("server_rmm.PLUGINS_DIR", tmp):
                handler = FakeServeHandler()
                served = RMMHandler._serve_plugin_tool(handler, "notes.txt")
        self.assertTrue(served)
        self.assertEqual(handler.responses[-1][0], 404)

    def test_serves_deployed_powershell_pivot_plugin(self):
        payload = b"function global:Start-RmmSocksChannelPlugin {}"
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = os.path.join(tmp, "artifacts")
            os.makedirs(plugin_dir)
            plugin_path = os.path.join(plugin_dir, "socks.ps1")
            with open(plugin_path, "wb") as handle:
                handle.write(payload)
            with patch("server_rmm.PLUGINS_DIR", plugin_dir):
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

    def test_serves_deployed_powershell_screenshot_plugin(self):
        payload = b"function global:Invoke-RmmScreenshotPlugin {}"
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = os.path.join(tmp, "artifacts")
            os.makedirs(plugin_dir)
            plugin_path = os.path.join(plugin_dir, "screenshot.ps1")
            with open(plugin_path, "wb") as handle:
                handle.write(payload)
            with patch("server_rmm.PLUGINS_DIR", plugin_dir):
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
            server_rmm.plugin_command_metadata("__PLUGIN_COMMAND__ tool.dll scan secret"),
            {"plugin": "tool.dll", "command": "scan", "export": "Run"},
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

    def test_tun_requires_tun_plugin_capability(self):
        session = SimpleNamespace(agent_type="go", capabilities=[])
        detail = self._server().tun_plugin_error(session)
        self.assertIn("tun-plugin", detail)

    def test_tun_accepts_built_transport_and_helper(self):
        session = SimpleNamespace(agent_type="go", capabilities=["tun-plugin"])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tun.dll"), "wb") as handle:
                handle.write(b"MZpayload")
            helper = os.path.join(tmp, "rmm-tun")
            with open(helper, "wb") as handle:
                handle.write(b"helper")
            os.chmod(helper, 0o700)
            with patch("server_rmm.PLUGINS_DIR", tmp):
                with patch("server_rmm.TUN_HELPER", helper):
                    self.assertIsNone(self._server().tun_plugin_error(session))

    def test_powershell_uses_deployed_plugin(self):
        session = SimpleNamespace(agent_type="powershell", capabilities=["socks-plugin"])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "socks.ps1"), "wb") as handle:
                handle.write(b"function global:Start-RmmSocksChannelPlugin {}")
            with patch("server_rmm.PLUGINS_DIR", tmp):
                self.assertIsNone(self._server().pivot_plugin_error(session))

    def test_unknown_agent_type_is_rejected(self):
        session = SimpleNamespace(agent_type="custom", capabilities=[])
        detail = self._server().pivot_plugin_error(session)
        self.assertIn("custom", detail)


class PivotInventoryTests(unittest.TestCase):
    """Verify private TUN egress bridges stay out of the SOCKS inventory."""

    def test_server_creates_separate_socks_and_tun_transports(self):
        with patch.object(server_rmm.RMMServer, "_load_persisted_session_configs", return_value={}), \
             patch.object(server_rmm.RMMServer, "_archive_orphaned_sessions_on_startup"):
            server = server_rmm.RMMServer()

        self.assertIsNot(server.socks, server.tun_transport)
        self.assertIs(server.tun.socks_manager, server.tun_transport)

    def test_starting_tun_keeps_public_socks_active(self):
        session = SimpleNamespace(id="session-1")
        server = object.__new__(server_rmm.RMMServer)
        server.get_session = Mock(return_value=session)
        server.tun_plugin_error = Mock(return_value=None)
        relay = SimpleNamespace(status=Mock(return_value={"session_id": session.id}))
        server.tun = SimpleNamespace(start=Mock(return_value=relay))
        server.record_operator_action = Mock()
        server._record_event = Mock()

        result = server.start_tun(
            session.id,
            {"device": "tun://rmm0", "route": "10.20.0.0/16"},
        )

        self.assertEqual(result, {"session_id": session.id})
        server.tun.start.assert_called_once()

    def test_socks_inventory_remains_visible_when_tun_is_active(self):
        server = object.__new__(server_rmm.RMMServer)
        public_session = "public-session"
        server.socks = SimpleNamespace(
            list_relays=lambda: [
                {"session_id": public_session, "port": 1080},
            ]
        )
        server.tun_active = lambda _session_id: True
        server.get_session = lambda session_id: SimpleNamespace(
            hostname=session_id,
            username="user",
            beacon_status=lambda: "online",
        )

        rows = server.list_socks_status()

        self.assertEqual([row["session_id"] for row in rows], [public_session])

    def test_public_socks_stop_does_not_stop_tun(self):
        session = SimpleNamespace(id="session-1")
        server = object.__new__(server_rmm.RMMServer)
        server.socks = SimpleNamespace(stop=Mock(return_value=True))
        server.tun_active = Mock(return_value=True)
        server.get_session = Mock(return_value=session)
        server.record_operator_action = Mock()
        server._record_event = Mock()
        server.workspace_socks_configs = {}

        self.assertTrue(server.stop_socks(session.id))
        server.socks.stop.assert_called_once_with(session.id)
        server.tun_active.assert_not_called()


class ArtifactInventoryTests(unittest.TestCase):
    """Verify global artifact rows retain the originating agent identity."""

    def test_global_artifact_api_route_selects_kind(self):
        server = SimpleNamespace(
            list_artifacts=Mock(return_value=[{"kind": "screenshots", "agent_label": "alice@target-01"}])
        )
        handler = FakeApiHandler(server_instance=server)

        RMMHandler._handle_api_get(
            handler,
            f"{API_PREFIX}/artifacts",
            {"kind": ["screenshots"]},
        )

        status, body = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "screenshots")
        self.assertEqual(body["count"], 1)
        server.list_artifacts.assert_called_once_with("screenshots", workspace_id=None)

    def test_server_artifacts_include_active_agent_metadata(self):
        session_id = "12345678-1234-1234-1234-123456789012"
        session = server_rmm.Session(session_id, "target-01", "alice")
        prefix = server_rmm.safe_session_storage_prefix(session_id)
        download_name = f"{prefix}_report.txt"
        screenshot_name = f"{prefix}_20261013_160000.png"
        session.download_artifacts.append(
            {
                "artifact": download_name,
                "remote_path": "C:/Temp/report.txt",
                "size": 7,
                "received_at": "2026-10-13T16:00:00",
            }
        )
        server = object.__new__(server_rmm.RMMServer)
        server.storage_mode = "server"
        server.sessions = {session_id: session}
        server.session_lock = threading.Lock()
        server.list_session_history = lambda: []

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "downloads"))
            os.makedirs(os.path.join(tmp, "screenshots"))
            with open(os.path.join(tmp, "downloads", download_name), "wb") as handle:
                handle.write(b"report\n")
            with open(os.path.join(tmp, "screenshots", screenshot_name), "wb") as handle:
                handle.write(b"png")
            with patch("server_rmm.LOG_DIR", tmp):
                downloads = server.list_artifacts("downloads")
                screenshots = server.list_artifacts("screenshots")

        self.assertEqual(downloads[0]["agent_label"], "alice@target-01")
        self.assertEqual(downloads[0]["session_id"], session_id)
        self.assertEqual(downloads[0]["remote_path"], "C:/Temp/report.txt")
        self.assertEqual(screenshots[0]["agent_label"], "alice@target-01")
        self.assertEqual(screenshots[0]["session_id"], session_id)


if __name__ == "__main__":
    unittest.main()
