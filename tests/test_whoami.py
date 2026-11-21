"""Read-only identity report routing, failures, and option validation."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm


class WhoamiTests(unittest.TestCase):
    def setUp(self):
        self.server = object.__new__(server_rmm.RMMServer)
        self.session = SimpleNamespace(id="go-session", agent_type="go")
        self.server.resolve_session = Mock(return_value=self.session)
        self.server.record_operator_action = Mock()
        self.server.exec_and_wait = Mock(return_value=({"body": "Process report"}, "ok"))

    def test_ticket_opt_in_and_audit(self):
        for tickets in (False, True):
            with self.subTest(tickets=tickets):
                result, status = self.server.session_whoami("prefix", tickets=tickets)
                self.assertEqual(status, "ok")
                self.assertTrue(result["ok"])
                command = "__WHOAMI__" + (" --tickets" if tickets else "")
                self.server.exec_and_wait.assert_called_with("go-session", command, timeout=120.0)
                self.server.record_operator_action.assert_called_with(self.session, command, "whoami")

    def test_unavailable_and_unsupported(self):
        self.server.resolve_session.return_value = None
        self.assertEqual(self.server.session_whoami("missing"), (None, "not_found"))
        self.server.resolve_session.return_value = SimpleNamespace(agent_type="powershell")
        self.assertEqual(self.server.session_whoami("ps"), (None, "agent_type_unsupported"))
        self.server.exec_and_wait.assert_not_called()

    def test_errors_are_not_success(self):
        for text in ("", "Whoami failed: access denied"):
            self.server.exec_and_wait.return_value = {"body": text}, "ok"
            self.assertFalse(self.server.session_whoami("prefix")[0]["ok"])
        for status in ("timeout", "session_busy", "server_stopped", "session_killed"):
            self.server.exec_and_wait.return_value = None, status
            self.assertEqual(self.server.session_whoami("prefix"), (None, status))

    def route(self, body, status="ok"):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["sessions", "prefix", "whoami"])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(session_whoami=Mock(return_value=({"ok": True}, status)))
        with patch.object(server_rmm, "_resolve_api_session", return_value=self.session):
            handler._handle_api_post("/api/v1/sessions/prefix/whoami", body)
        return handler

    def test_rejects_invalid_options_before_queue(self):
        for body in (None, [], "invalid", {"timeout": True},
                     {"tickets": "false"}, {"tickets": 1}, {"timeout": 0},
                     {"timeout": float("nan")}, {"timeout": float("inf")}, {"extra": True}):
            with self.subTest(body=body):
                handler = self.route(body)
                self.assertEqual(handler._json.call_args.args[0], 400)
                handler.server_instance.session_whoami.assert_not_called()

    def test_resolves_scope_and_maps_status(self):
        handler = self.route({"tickets": True, "timeout": 25})
        handler.server_instance.session_whoami.assert_called_once_with("go-session", timeout=25, tickets=True)
        self.assertEqual(handler._json.call_args.args[0], 200)
        for status, code in (("timeout", 408), ("session_busy", 409),
                             ("session_killed", 410), ("server_stopped", 503)):
            self.assertEqual(self.route({}, status)._json.call_args.args[0], code)
