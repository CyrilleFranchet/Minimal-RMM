"""Extended identity inspection routing and operator-surface contracts."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import rmm_cli
import rmm_tools
import server_rmm
from scripts import check_operator_parity
from workspace_service import operator_proxy_timeout

import test_cli_dispatch
import test_cli_slash_commands


class UidxServerTests(unittest.TestCase):
    def setUp(self):
        self.server = object.__new__(server_rmm.RMMServer)
        self.session = SimpleNamespace(id="go-session", agent_type="go")
        self.server.resolve_session = Mock(return_value=self.session)
        self.server.record_operator_action = Mock()
        self.server.exec_and_wait = Mock(return_value=({"body": "identity split: true"}, "ok"))

    def test_service_queues_command_and_audits(self):
        result, status = self.server.session_uidx("prefix", timeout=25)
        self.assertEqual(status, "ok")
        self.assertEqual(result, {
            "ok": True,
            "session_id": "go-session",
            "output": "identity split: true",
        })
        self.server.exec_and_wait.assert_called_once_with("go-session", "__UIDX__", timeout=25)
        self.server.record_operator_action.assert_called_once_with(self.session, "__UIDX__", "uidx")

    def test_service_rejects_unavailable_sessions_and_preserves_wait_status(self):
        self.server.resolve_session.return_value = None
        self.assertEqual(self.server.session_uidx("missing"), (None, "not_found"))
        self.server.resolve_session.return_value = SimpleNamespace(id="ps-session", agent_type="powershell")
        self.assertEqual(self.server.session_uidx("ps"), (None, "agent_type_unsupported"))
        self.server.resolve_session.return_value = self.session
        for status in ("timeout", "session_busy", "server_stopped", "session_killed"):
            with self.subTest(status=status):
                self.server.exec_and_wait.return_value = None, status
                self.assertEqual(self.server.session_uidx("prefix"), (None, status))

    def route(self, body, status="ok"):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["sessions", "prefix", "uidx"])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(
            session_uidx=Mock(return_value=({"ok": True}, status)),
        )
        with patch.object(server_rmm, "_resolve_api_session", return_value=self.session):
            handler._handle_api_post("/api/v1/sessions/prefix/uidx", body)
        return handler

    def test_rest_route_validates_timeout_and_maps_status(self):
        handler = self.route({"timeout": 25})
        handler.server_instance.session_uidx.assert_called_once_with("go-session", timeout=25)
        handler._json.assert_called_once_with(200, {"ok": True})

        for value in (0, 3601, "bad", float("nan"), float("inf")):
            with self.subTest(timeout=value):
                handler = self.route({"timeout": value})
                handler._json.assert_called_once_with(400, {"error": "invalid_uidx_options"})
                handler.server_instance.session_uidx.assert_not_called()

        for status, code in (("timeout", 408), ("agent_type_unsupported", 409),
                             ("session_busy", 409), ("session_killed", 410),
                             ("server_stopped", 503)):
            with self.subTest(status=status):
                handler = self.route({}, status)
                self.assertEqual(handler._json.call_args.args[0], code)


class UidxOperatorSurfaceTests(unittest.TestCase):
    def test_api_client_preserves_timeout_and_wait_margin(self):
        client = rmm_cli.RmmApiClient("http://example.test", "token")
        client.request = Mock(return_value=(200, {}))
        client.session_uidx("session", timeout=240)
        client.request.assert_called_once_with(
            "POST", "/api/v1/sessions/session/uidx", {"timeout": 240}, timeout=245,
        )

    def test_shared_tool_resolves_session_and_maps_results(self):
        client = Mock()
        client.list_sessions.return_value = (200, {
            "sessions": [{"id": "session", "hostname": "host"}],
        })
        client.session_uidx.return_value = (200, {"ok": True, "output": "identity split: false"})
        result = json.loads(rmm_tools.tool_session_uidx(client, "host", timeout=30))
        self.assertEqual(result["output"], "identity split: false")
        client.session_uidx.assert_called_once_with("session", timeout=30)

        client.session_uidx.return_value = (408, {})
        result = json.loads(rmm_tools.tool_session_uidx(client, "host"))
        self.assertEqual(result["command"], "__UIDX__")
        self.assertFalse(result["ok"])

    def test_ai_schema_and_parity_registries_include_uidx(self):
        schema = next(
            entry["function"] for entry in rmm_tools.OPENAI_TOOLS
            if entry["function"]["name"] == "session_uidx"
        )
        self.assertEqual(schema["parameters"]["required"], ["session_ref"])
        self.assertEqual(
            set(schema["parameters"]["properties"]), {"session_ref", "timeout"},
        )
        self.assertIn("session_uidx", check_operator_parity.REQUIRED_MCP_TOOLS)
        self.assertEqual(
            check_operator_parity.TOOL_CLIENT_METHOD["session_uidx"], "session_uidx",
        )
        self.assertEqual(check_operator_parity.WEB_SHELL_META["uidx"], "session_uidx")

    def test_direct_cli_dispatches_uidx(self):
        client = Mock()
        client.session_uidx.return_value = (200, {"ok": True, "output": "report"})
        output, _ = test_cli_dispatch.CliDispatchTests().invoke(
            ["uidx", "--session", "session", "--timeout", "25", "--json"], client,
        )
        self.assertEqual(json.loads(output)["output"], "report")
        client.session_uidx.assert_called_once_with("session", timeout=25.0)

    def test_interactive_uidx_is_local_and_validates_arguments(self):
        original = rmm_cli.run_interactive

        def run(client, state):
            client.session_uidx.return_value = (200, {"ok": True, "output": "report"})
            return original(client, state)

        with patch.object(rmm_cli, "run_interactive", side_effect=run):
            client, _, warn = test_cli_slash_commands.SlashCommandTests().run_lines([
                "/uidx", "/uidx extra", "uidx",
            ])
        client.session_uidx.assert_called_once_with("session-id")
        warn.assert_called_once_with("Usage: /uidx")
        client.queue_command.assert_called_once_with("session-id", "uidx")

    def test_workspace_proxy_preserves_uidx_wait_budget(self):
        path = "/api/v1/sessions/session/uidx"
        self.assertEqual(operator_proxy_timeout(path, {}), 150)
        self.assertEqual(operator_proxy_timeout(path, {"timeout": 900}), 930)
        for value in ("bad", None, float("nan"), float("inf"), 999999):
            with self.subTest(timeout=value):
                self.assertEqual(operator_proxy_timeout(path, {"timeout": value}), 150)


if __name__ == "__main__":
    unittest.main()
