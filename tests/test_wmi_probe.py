"""Verify the wmi_probe server flow, REST routing, and audit redaction."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm


def _session(session_id="go-1730000000000000000", agent_type="go"):
    return SimpleNamespace(id=session_id, agent_type=agent_type)


class WmiProbeMethodTests(unittest.TestCase):
    def setUp(self):
        self.server = object.__new__(server_rmm.RMMServer)
        self.session = SimpleNamespace(id="go-source-session", agent_type="go", workspace_id="alice", hostname="source")
        self.server.resolve_session = Mock(return_value=self.session)
        self.server.record_operator_action = Mock()
        self.ack_status = "ok"
        self.ack_output = "wmi probe host=10.0.0.8 mode=;\nverdict no-repro"
        self.server.exec_and_wait = Mock(side_effect=self.execute)

    def execute(self, sid, command, timeout=None):
        return {"body": self.ack_output}, self.ack_status

    def call(self, **options):
        return self.server.wmi_probe(self.session.id, "10.0.0.8", **options)

    def test_wire_command_and_redacted_audit(self):
        result, status = self.call(mode="switch", b_domain="LAB", b_user="svc-b", b_password="s3cret")
        self.assertEqual(status, "ok")
        self.assertTrue(result["ok"])
        command = self.server.exec_and_wait.call_args.args[1]
        self.assertTrue(command.startswith("__WMI_PROBE__ 10.0.0.8 switch\n"))
        payload = json.loads(command.split("\n", 1)[1])
        self.assertEqual(payload, {"domain": "LAB", "user": "svc-b", "password": "s3cret"})
        audit = self.server.record_operator_action.call_args
        label = audit.args[1]
        self.assertIn("b_identity=LAB\\svc-b", label)
        self.assertNotIn("s3cret", label)
        self.assertEqual(audit.args[2], "wmi_probe")

    def test_default_mode_omits_body(self):
        result, status = self.call()
        self.assertEqual(status, "ok")
        command = self.server.exec_and_wait.call_args.args[1]
        self.assertEqual(command, "__WMI_PROBE__ 10.0.0.8")
        self.assertNotIn("b_identity", self.server.record_operator_action.call_args.args[1])

    def test_domain_user_split_and_rejections(self):
        result, status = self.call(mode="switch", b_user="LAB\\svc-b", b_password="pw")
        self.assertEqual(status, "ok")
        payload = json.loads(self.server.exec_and_wait.call_args.args[1].split("\n", 1)[1])
        self.assertEqual(payload["domain"], "LAB")
        self.assertEqual(payload["user"], "svc-b")
        self.server.exec_and_wait.reset_mock()
        for status_name, kwargs in (
            ("invalid_identity_method", {"mode": "held", "b_user": "svc-b", "b_password": "pw"}),
            ("invalid_identity_method", {"b_user": "svc-b", "b_password": "pw"}),
            ("invalid_identity", {"mode": "switch", "b_domain": "LAB", "b_password": "pw"}),
            ("invalid_identity", {"mode": "switch", "b_domain": "LAB", "b_user": "LAB\\svc-b", "b_password": "pw"}),
            ("invalid_identity", {"mode": "switch", "b_user": "LAB\\", "b_password": "pw"}),
            ("invalid_mode", {"mode": "continuous"}),
        ):
            with self.subTest(status=status_name, kwargs=kwargs):
                _, status = self.server.wmi_probe(self.session.id, "10.0.0.8", **kwargs)
                self.assertEqual(status, status_name)
                self.server.exec_and_wait.assert_not_called()
                self.server.exec_and_wait.reset_mock()

    def test_agent_type_gate_and_failures(self):
        self.server.resolve_session = Mock(return_value=_session(agent_type="powershell"))
        _, status = self.call()
        self.assertEqual(status, "agent_type_unsupported")
        self.server.resolve_session = Mock(return_value=None)
        _, status = self.call()
        self.assertEqual(status, "not_found")
        self.server.resolve_session = Mock(return_value=self.session)
        self.ack_status = "timeout"
        _, status = self.call()
        self.assertEqual(status, "timeout")


class WmiProbeRouteTests(unittest.TestCase):
    def _handler(self, endpoint, result, status="ok"):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["sessions", "abc", endpoint])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(wmi_probe=Mock(return_value=(result, status)))
        return handler

    def _run(self, handler, body):
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="full-session-id")):
            handler._handle_api_post(f"/api/v1/sessions/abc/{handler._api_path_parts()[-1]}", body)

    def test_ok_and_parameter_passthrough(self):
        handler = self._handler("wmi-probe", {"ok": True, "output": "wmi probe host=..."})
        self._run(handler, {"host": "ws01", "mode": "switch"})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        handler.server_instance.wmi_probe.assert_called_once_with(
            "full-session-id", "ws01", "switch", "", "", "", timeout=120.0,
        )

    def test_b_identity_cred_resolution(self):
        handler = self._handler("wmi-probe", {"ok": True})
        cred = {"domain": "LAB", "user": "bob", "password": "pw2"}
        with patch.object(server_rmm._rmm_creds, "creds_resolve", return_value=cred) as resolve, \
                patch.object(server_rmm, "plugin_directory", return_value="/tmp/plugindir"):
            self._run(handler, {"host": "ws02", "mode": "switch", "b_cred_id": "cred-3f0cfe46"})
        resolve.assert_called_once_with("/tmp/plugindir", "cred-3f0cfe46")
        handler.server_instance.wmi_probe.assert_called_once_with(
            "full-session-id", "ws02", "switch", "LAB", "bob", "pw2", timeout=120.0,
        )

    def test_b_identity_cred_not_found(self):
        handler = self._handler("wmi-probe", {"ok": True})
        with patch.object(server_rmm._rmm_creds, "creds_resolve", return_value=None), \
                patch.object(server_rmm, "plugin_directory", return_value="/tmp/plugindir"):
            self._run(handler, {"host": "ws02", "mode": "switch", "b_cred_id": "cred-1"})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 404)
        self.assertEqual(payload["error"], "cred_not_found")
        handler.server_instance.wmi_probe.assert_not_called()

    def test_b_identity_cred_and_fields_rejected(self):
        handler = self._handler("wmi-probe", {"ok": True})
        self._run(handler, {"host": "ws01", "mode": "switch", "b_cred_id": "cred-1", "b_user": "bob"})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid_identity")
        handler.server_instance.wmi_probe.assert_not_called()

    def test_status_codes(self):
        cases = [
            ("agent_type_unsupported", 409),
            ("session_busy", 409),
            ("invalid_host", 400),
            ("invalid_mode", 400),
            ("invalid_identity", 400),
            ("invalid_identity_method", 400),
            ("timeout", 408),
            ("session_killed", 410),
            ("server_stopped", 503),
        ]
        for status, expected_code in cases:
            with self.subTest(status=status):
                handler = self._handler("wmi-probe", None, status)
                self._run(handler, {"host": "ws01"})
                code, payload = handler._json.call_args.args
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["error"], status)

    def test_rejects_invalid_options(self):
        handler = self._handler("wmi-probe", {"ok": True})
        self._run(handler, {"host": "ws01", "timeout": 0})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid_wmi_probe_options")
        handler.server_instance.wmi_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
