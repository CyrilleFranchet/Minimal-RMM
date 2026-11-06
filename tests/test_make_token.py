"""Verify make_token/rev2self server flow, REST routing, and secret redaction."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm


def _session(session_id="go-1730000000000000000", agent_type="go"):
    return SimpleNamespace(id=session_id, agent_type=agent_type)


class MakeTokenFlowTests(unittest.TestCase):
    def _server(self):
        return object.__new__(server_rmm.RMMServer)

    def test_rejects_non_go_and_unknown_sessions(self):
        server = self._server()
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session(agent_type="powershell")):
            self.assertEqual(server.make_token("prefix1", "", "alice", "pw"), (None, "agent_type_unsupported"))
            self.assertEqual(server.rev2self("prefix1"), (None, "agent_type_unsupported"))
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=None):
            self.assertEqual(server.make_token("missing1", "", "alice", "pw"), (None, "not_found"))
            self.assertEqual(server.rev2self("missing1"), (None, "not_found"))

    def test_rejects_missing_user(self):
        server = self._server()
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session()):
            self.assertEqual(server.make_token("prefix1", "", "  ", "pw"), (None, "invalid_credentials"))

    def test_success_never_records_the_password(self):
        server = self._server()
        recorded = []
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session()), \
                patch.object(server_rmm.RMMServer, "exec_and_wait",
                             return_value=({"body": "token created: LAB\\alice; spawned commands now use this network identity"}, "ok")), \
                patch.object(server_rmm.RMMServer, "record_operator_action",
                             side_effect=lambda session, command, action: recorded.append((session.id, command, action))):
            payload, status = server.make_token("prefix1", "LAB", "alice", "s3cret-pw")
        self.assertEqual(status, "ok")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["identity"], "LAB\\alice")
        self.assertIn("token created", payload["output"])
        session_id, command, action = recorded[0]
        self.assertEqual(action, "make_token")
        self.assertEqual(command, "__MAKE_TOKEN__ LAB\\alice")
        self.assertNotIn("s3cret-pw", command)
        self.assertNotIn("s3cret-pw", payload["output"])

    def test_queued_command_carries_password_but_events_do_not(self):
        server = self._server()
        beacon_commands = []
        recorded = []

        def fake_exec(session_id, command, timeout=None):
            beacon_commands.append(command)
            return {"body": "token created: LAB\\alice"}, "ok"

        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session()), \
                patch.object(server_rmm.RMMServer, "exec_and_wait", side_effect=fake_exec), \
                patch.object(server_rmm.RMMServer, "record_operator_action",
                             side_effect=lambda session, command, action: recorded.append(command)):
            server.make_token("prefix1", "LAB", "alice", "s3cret-pw", timeout=5)
        # The beacon command must carry the credentials for the agent...
        self.assertEqual(len(beacon_commands), 1)
        self.assertTrue(beacon_commands[0].startswith("__MAKE_TOKEN__\n"))
        self.assertIn("s3cret-pw", beacon_commands[0])
        # ...while operator events record only the identity label.
        self.assertEqual(recorded, ["__MAKE_TOKEN__ LAB\\alice"])

    def test_combined_identity_is_split_and_agent_failure_is_reported(self):
        server = self._server()
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session()), \
                patch.object(server_rmm.RMMServer, "exec_and_wait",
                             return_value=({"body": "Make token failed: LogonUserW failed: The system cannot find the file specified"}, "ok")), \
                patch.object(server_rmm.RMMServer, "record_operator_action", Mock()):
            payload, status = server.make_token("prefix1", "", "LAB\\alice", "pw")
        self.assertEqual(status, "ok")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["identity"], "LAB\\alice")

    def test_timeout_and_busy_passthrough(self):
        server = self._server()
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session()), \
                patch.object(server_rmm.RMMServer, "exec_and_wait", return_value=(None, "timeout")), \
                patch.object(server_rmm.RMMServer, "record_operator_action", Mock()):
            self.assertEqual(server.make_token("prefix1", "", "alice", "pw"), (None, "timeout"))
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session()), \
                patch.object(server_rmm.RMMServer, "exec_and_wait", return_value=(None, "session_busy")), \
                patch.object(server_rmm.RMMServer, "record_operator_action", Mock()):
            self.assertEqual(server.rev2self("prefix1"), (None, "session_busy"))

    def test_rev2self_reports_agent_output(self):
        server = self._server()
        recorded = []
        with patch.object(server_rmm.RMMServer, "resolve_session", return_value=_session()), \
                patch.object(server_rmm.RMMServer, "exec_and_wait",
                             return_value=({"body": "token reverted: LAB\\alice; spawned commands use the agent identity"}, "ok")), \
                patch.object(server_rmm.RMMServer, "record_operator_action",
                             side_effect=lambda session, command, action: recorded.append((session.id, command, action))):
            payload, status = server.rev2self("prefix1")
        self.assertEqual(status, "ok")
        self.assertTrue(payload["ok"])
        self.assertIn("token reverted", payload["output"])
        self.assertEqual(recorded[0][1], "__REV2SELF__")
        self.assertEqual(recorded[0][2], "rev2self")


class IdentityHttpRoutingTests(unittest.TestCase):
    def _handler(self, method, result, status="ok"):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["sessions", "abc", method])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(
            make_token=Mock(return_value=(result, status)),
            rev2self=Mock(return_value=(result, status)),
        )
        return handler

    def _run(self, handler, body):
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="full-session-id")):
            handler._handle_api_post(f"/api/v1/sessions/abc/{handler._api_path_parts()[-1]}", body)

    def test_make_token_ok_and_split_identity(self):
        handler = self._handler("make_token", {"ok": True, "identity": "LAB\\alice", "output": "token created"})
        self._run(handler, {"user": "LAB\\alice", "password": "pw"})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        call = handler.server_instance.make_token.call_args
        self.assertEqual(call.args[0], "full-session-id")
        # The handler forwards the raw identity; RMMServer.make_token splits it.
        self.assertEqual(call.args[1], "")
        self.assertEqual(call.args[2], "LAB\\alice")
        self.assertEqual(call.args[3], "pw")
        self.assertNotIn("pw", payload)

    def test_make_token_status_codes(self):
        cases = [
            ("agent_type_unsupported", 409),
            ("session_busy", 409),
            ("invalid_credentials", 400),
            ("timeout", 408),
            ("session_killed", 410),
            ("server_stopped", 503),
        ]
        for status, expected_code in cases:
            with self.subTest(status=status):
                handler = self._handler("make_token", None, status)
                self._run(handler, {"user": "alice", "password": "pw"})
                code, payload = handler._json.call_args.args
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["error"], status)

    def test_make_token_rejects_invalid_timeout(self):
        handler = self._handler("make_token", {"ok": True})
        self._run(handler, {"user": "alice", "password": "pw", "timeout": 0})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid_make_token_options")
        handler.server_instance.make_token.assert_not_called()

    def test_rev2self_ok_and_status_codes(self):
        handler = self._handler("rev2self", {"ok": True, "output": "token reverted"})
        self._run(handler, {})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        for status, expected_code in (("agent_type_unsupported", 409), ("timeout", 408)):
            with self.subTest(status=status):
                handler = self._handler("rev2self", None, status)
                self._run(handler, {})
                self.assertEqual(handler._json.call_args.args[0], expected_code)

    def test_rev2self_rejects_invalid_timeout(self):
        handler = self._handler("rev2self", {"ok": True})
        self._run(handler, {"timeout": "abc"})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid_rev2self_options")




class RevertFailureTests(unittest.TestCase):
    def test_failure_is_preserved_through_server_and_shared_tool(self):
        import json
        import rmm_tools
        server = object.__new__(server_rmm.RMMServer)
        with patch.object(server_rmm.RMMServer, 'resolve_session', return_value=_session()), \
                patch.object(server_rmm.RMMServer, 'record_operator_action'), \
                patch.object(server_rmm.RMMServer, 'exec_and_wait', return_value=(
                    {'body': 'Rev2self failed: network identity commands require the Windows agent build'}, 'ok')):
            payload, status = server.rev2self('prefix1')
        self.assertEqual(status, 'ok')
        self.assertFalse(payload['ok'])
        client = Mock()
        client.rev2self.return_value = (200, payload)
        with patch.object(rmm_tools, '_resolve_session_id', return_value=('session-id', {})):
            result = json.loads(rmm_tools.tool_rev2self(client, 'prefix1'))
        self.assertFalse(result['ok'])


if __name__ == "__main__":
    unittest.main()
