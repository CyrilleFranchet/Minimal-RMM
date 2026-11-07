"""Verify the spawn_session server flow and REST routing without live agents."""
import unittest
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm


def _session(session_id="go-1730000000000000000", agent_type="go"):
    session = SimpleNamespace(id=session_id, agent_type=agent_type)
    return session


class SpawnSessionFlowTests(unittest.TestCase):
    def test_server_selected_go_ids_are_unique_and_skip_live_and_killed_ids(self):
        server = object.__new__(server_rmm.RMMServer)
        server.sessions = {'a' * 32: object()}
        server.killed_sessions = {'b' * 32}
        with patch.object(server_rmm.secrets, 'token_hex', side_effect=['a' * 32, 'b' * 32, 'c' * 32]):
            self.assertEqual(server._new_deployment_id('spawn'), 'c' * 32)
        for kind in ('spawn', 'spawnas', 'lateral'):
            first = server._new_deployment_id(kind)
            second = server._new_deployment_id(kind)
            self.assertRegex(first, r'^[0-9a-f]{32}$')
            self.assertNotEqual(first, second)

    def handshake(self):
        return f"spawned session={self.target_id} pid=42"

    def call(self, **options):
        return self.server.spawn_session(self.source.id, **options)

    def setUp(self):
        self.server = object.__new__(server_rmm.RMMServer)
        self.source = SimpleNamespace(id="go-source-session", agent_type="go", workspace_id="alice", hostname="source")
        self.target_id = "srv-target-session-12345678"
        self.target = SimpleNamespace(id=self.target_id, workspace_id="alice", hostname="target")
        self.server.resolve_session = Mock(return_value=self.source)
        self.server._new_deployment_id = Mock(return_value=self.target_id)
        self.server.get_session = Mock(side_effect=lambda sid: self.source if sid == self.source.id else self.target)
        self.server.record_operator_action = Mock()
        self.server._await_session_registration = Mock(return_value=True)
        self.ack_status = "ok"
        self.ack_output = self.handshake()
        self.server.exec_and_wait = Mock(side_effect=self.execute)

    def execute(self, sid, command, timeout=None):
        return {"body": self.ack_output}, self.ack_status

    def test_registration_confirms_success_without_probes(self):
        result, status = self.call()
        self.assertEqual(status, "ok")
        self.assertTrue(result["ok"])
        self.assertTrue(result["registered"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["workspace_id"], "alice")
        self.assertEqual(result["registered_hostname"], "target")
        commands = [c.args[1] for c in self.server.exec_and_wait.call_args_list]
        request = __import__('json').loads(commands[0].split("\n", 1)[1])
        self.assertEqual(request["session_id"], self.target_id)
        self.assertEqual(request["workspace_id"], "alice")
        probes = [c for c in commands if c.startswith("echo ")]
        self.assertEqual(probes, [])
        self.assertNotIn("source_verify", result)
        self.assertNotIn("target_verify", result)

    def test_spawn_method_and_identity_downgrade_are_reported(self):
        self.ack_output = self.handshake() + "\nspawn method=CreateProcessA identity_degraded=true"
        result, status = self.call()
        self.assertEqual(status, "ok")
        self.assertEqual(result["spawn_method"], "CreateProcessA")
        self.assertTrue(result["identity_degraded"])
        self.assertTrue(result["verified"])

    def test_spawn_failure_diagnostics_survive_a_successful_fallback(self):
        detail = {"active_token": True, "credentials_available": True, "token_type": "primary",
                  "process_identity": "LAB\\operator", "password": "must-not-be-projected",
                  "process_privileges": {"SeImpersonatePrivilege": "absent", "unknown": "enabled"},
                  "attempts": [{"method": "CreateProcessAsUser", "success": False, "error_code": 1314, "error": "privilege not held"},
                               {"method": "CreateProcessA", "success": True}]}
        self.ack_output = self.handshake() + "\nspawn diagnostics=" + json.dumps(detail)
        result, _ = self.call()
        self.assertEqual(result["spawn_diagnostics"]["attempts"][0]["error_code"], 1314)
        self.assertTrue(result["spawn_diagnostics"]["attempts"][1]["success"])
        self.assertNotIn("password", result["spawn_diagnostics"])
        self.assertEqual(result["spawn_diagnostics"]["process_privileges"], {"SeImpersonatePrivilege": "absent"})

    def test_malformed_spawn_diagnostics_do_not_break_registration(self):
        for detail in ([], {"attempts": [{}]}, {"attempts": [{"method": [], "success": False}]},
                       {"attempts": [{"method": "CreateProcessA", "success": True, "error_code": True}]}):
            self.ack_output = self.handshake() + "\nspawn diagnostics=" + json.dumps(detail)
            result, _ = self.call()
            self.assertTrue(result["registered"])
            self.assertNotIn("spawn_diagnostics", result)

    def test_launch_acknowledgement_without_new_agent_is_not_success(self):
        self.server._await_session_registration.return_value = False
        result, _ = self.call()
        self.assertFalse(result["ok"])
        self.assertFalse(result["registered"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["launch_status"], "acknowledged")

    def test_registration_needs_no_parent_or_target_execution_probe(self):
        result, _ = self.call()
        self.assertTrue(result["verified"])
        self.server.exec_and_wait.assert_called_once()
        self.assertNotIn("parent_responsive", result)
        self.assertNotIn("spawned_responsive", result)

    def test_registration_proves_success_even_after_ack_timeout(self):
        self.ack_status = "timeout"
        self.ack_output = ""
        result, _ = self.call()
        self.assertTrue(result["registered"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["launch_status"], "inconclusive")
        self.assertEqual(result["submission_status"], "timeout")

    def test_missing_registration_after_timeout_preserves_expected_identity(self):
        self.ack_status = "timeout"
        self.ack_output = ""
        self.server._await_session_registration.return_value = False
        result, _ = self.call()
        self.assertFalse(result["ok"])
        self.assertIn(self.target_id, str(result))

    def test_wrong_workspace_registration_is_rejected(self):
        self.target.workspace_id = "bob"
        result, _ = self.call()
        self.assertFalse(result["registered"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])

    def test_without_verification_only_request_acceptance_is_reported(self):
        result, _ = self.call(verify=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["pending"])
        self.assertIsNone(result["registered"])
        self.server._await_session_registration.assert_not_called()
        self.assertEqual(self.server.exec_and_wait.call_count, 1)

    def test_busy_and_unknown_sessions_do_not_attempt_registration(self):
        self.ack_status = "session_busy"
        self.assertEqual(self.call(), (None, "session_busy"))
        self.server._await_session_registration.assert_not_called()
        self.server.resolve_session.return_value = None
        self.assertEqual(self.call(), (None, "not_found"))
        self.server.resolve_session.return_value = SimpleNamespace(id="ps-session", agent_type="powershell")
        self.assertEqual(self.call(), (None, "agent_type_unsupported"))

    def test_failure_output_stays_unconfirmed_without_registration(self):
        self.ack_output = "Agent failed: test error"
        self.server._await_session_registration.return_value = False
        result, _ = self.call()
        self.assertFalse(result["ok"])
        self.assertEqual(result["launch_status"], "inconclusive")


class SpawnHttpRoutingTests(unittest.TestCase):
    def _handler(self, spawn_result, spawn_status="ok"):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["sessions", "abc", "spawn"])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(
            spawn_session=Mock(return_value=(spawn_result, spawn_status)),
        )
        return handler

    def _resolve(self, handler):
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="full-session-id")):
            handler._handle_api_post("/api/v1/sessions/abc/spawn", {})

    def test_ok_response_passes_payload_through(self):
        handler = self._handler({"ok": True, "verified": True}, "ok")
        self._resolve(handler)
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertTrue(payload["verified"])
        handler.server_instance.spawn_session.assert_called_once_with(
            "full-session-id", verify=True, spawn_timeout=120.0,
            register_timeout=60.0, verify_timeout=120.0,
        )

    def test_status_codes_follow_exec_conventions(self):
        cases = [
            ("agent_type_unsupported", 409),
            ("session_busy", 409),
            ("timeout", 408),
            ("session_killed", 410),
            ("server_stopped", 503),
            ("spawn_result_invalid", 502),
        ]
        for status, expected_code in cases:
            with self.subTest(status=status):
                handler = self._handler(None, status)
                self._resolve(handler)
                code, payload = handler._json.call_args.args
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["error"], status)

    def test_rejects_invalid_options(self):
        handler = self._handler(None, "ok")
        handler._api_path_parts = Mock(return_value=["sessions", "abc", "spawn"])
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="full-session-id")):
            handler._handle_api_post("/api/v1/sessions/abc/spawn", {"spawn_timeout": 0})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid_spawn_options")
        handler.server_instance.spawn_session.assert_not_called()

    def test_spawn_result_regex_only_accepts_agent_handshake(self):
        for body, expected in [
            ("spawned session=go-spawn-child-1 pid=4242", "go-spawn-child-1"),
            ("Spawn failed: resolve agent executable: boom", None),
            ("spawned session=../pwn pid=1", None),
        ]:
            with self.subTest(body=body):
                match = server_rmm.SPAWN_RESULT_RE.search(body)
                self.assertEqual(match.group("session_id") if match else None, expected)


if __name__ == "__main__":
    unittest.main()
