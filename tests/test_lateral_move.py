"""Verify lateral_move and session_uid server flow, REST routing, and redaction."""
import io
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm


def _session(session_id="go-1730000000000000000", agent_type="go"):
    return SimpleNamespace(id=session_id, agent_type=agent_type)


class LateralFlowTests(unittest.TestCase):
    def handshake(self):
        return (f"lateral started: method=psexec host=target target session={self.target_id} "
                f"service=MinimalRmmAgent-0011223344556677 "
                f"binary=minimal-rmm-agent-0011223344556677.exe")

    def call(self, **options):
        return self.server.lateral_move(self.source.id, "psexec", "target", **options)

    def setUp(self):
        self.server = object.__new__(server_rmm.RMMServer)
        self.server.staging_lock = threading.Lock()
        self.server.staging_entries = {}
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
        self.cleanup_output = "lateral cleanup: service deleted; launcher binary removed from ADMIN$"
        self.server.exec_and_wait = Mock(side_effect=self.execute)
        agent_binary = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
        agent_binary.write(b"MZfake-agent-payload-for-staging")
        agent_binary.close()
        self.agent_binary_path = agent_binary.name
        self.addCleanup(os.unlink, self.agent_binary_path)
        patcher = patch.object(server_rmm, "latest_go_agent_binary", return_value=self.agent_binary_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def execute(self, sid, command, timeout=None):
        if command.startswith("__LATERAL_CLEANUP__"):
            return {"body": self.cleanup_output}, "ok"
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
        request = json.loads(commands[0].split("\n", 1)[1])
        self.assertEqual(request["session_id"], self.target_id)
        self.assertEqual(request["workspace_id"], "alice")
        self.assertTrue(request["stage"], "psexec must carry a staging reference")
        probes = [c for c in commands if c.startswith("echo ")]
        self.assertEqual(probes, [])
        self.assertNotIn("source_verify", result)
        self.assertNotIn("target_verify", result)

    def test_psexec_cleanup_runs_after_registration(self):
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "ok")
        self.assertIn("service deleted", result["cleanup"]["output"])
        commands = [c.args[1] for c in self.server.exec_and_wait.call_args_list]
        cleanup_commands = [c for c in commands if c.startswith("__LATERAL_CLEANUP__")]
        self.assertEqual(len(cleanup_commands), 1)
        request = json.loads(cleanup_commands[0].split("\n", 1)[1])
        self.assertEqual(request["host"], "target")
        self.assertEqual(request["service_name"], "MinimalRmmAgent-0011223344556677")
        self.assertEqual(request["binary_name"], "minimal-rmm-agent-0011223344556677.exe")
        self.server._await_session_registration.assert_called_once()
        self.assertGreater(commands.index(cleanup_commands[0]), 0)

    def test_immediate_source_cleanup_does_not_queue_another_command(self):
        self.ack_output += "\n" + self.cleanup_output
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "ok")
        self.assertEqual(result["cleanup"]["performed_by"], "source_agent")
        self.server.exec_and_wait.assert_called_once()

    def test_immediate_cleanup_is_reported_even_without_registration_wait(self):
        self.ack_output += "\n" + self.cleanup_output
        result, _ = self.call(verify=False)
        self.assertTrue(result["pending"])
        self.assertEqual(result["cleanup"]["performed_by"], "source_agent")
        self.server.exec_and_wait.assert_called_once()

    def test_pending_cleanup_uses_existing_owned_retry_path(self):
        self.ack_output += "\nlateral cleanup pending: launcher still running"
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "ok")
        self.assertEqual(self.server.exec_and_wait.call_count, 2)

    def test_cleanup_requires_positive_success_output(self):
        for output in ("Lateral cleanup failed: access denied", "", "unexpected response"):
            with self.subTest(output=output):
                self.cleanup_output = output
                result, _ = self.call()
                self.assertEqual(result["cleanup"]["status"], "failed")
                self.assertEqual(result["cleanup"]["output"], output)

    def test_owned_cleanup_retries_even_without_registration(self):
        self.server._await_session_registration.return_value = False
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "ok")
        self.server.exec_and_wait.reset_mock()
        result, _ = self.call(verify=False)
        self.assertEqual(result["cleanup"]["status"], "skipped")
        cleanup_commands = [c.args[1] for c in self.server.exec_and_wait.call_args_list
                            if c.args[1].startswith("__LATERAL_CLEANUP__")]
        self.assertEqual(cleanup_commands, [])

    def test_failed_worker_keeps_names_for_cleanup_retry(self):
        self.server._await_session_registration.return_value = False
        self.ack_output = self.handshake().replace("lateral started:", "lateral failed:")
        self.ack_output += "\nlauncher service failed: Windows 1066, stage 1\nlateral cleanup pending: file busy"
        result, _ = self.call()
        self.assertEqual(result["launch_status"], "failed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["cleanup"]["status"], "ok")
        self.assertEqual(self.server.exec_and_wait.call_count, 2)

    def test_failed_worker_reports_immediate_cleanup_without_registration(self):
        self.server._await_session_registration.return_value = False
        self.ack_output = self.handshake().replace("lateral started:", "lateral failed:")
        self.ack_output += "\nlauncher service failed: Windows 1066, stage 1\n" + self.cleanup_output
        result, _ = self.call()
        self.assertEqual(result["launch_status"], "failed")
        self.assertEqual(result["cleanup"]["performed_by"], "source_agent")
        self.server.exec_and_wait.assert_called_once()

    def test_psexec_without_staged_build_is_reported(self):
        with patch.object(server_rmm, "latest_go_agent_binary", return_value=None):
            self.assertEqual(self.call(), (None, "agent_payload_unavailable"))
        self.server.exec_and_wait.assert_not_called()

    def test_stage_status_is_surfaced(self):
        stage_ids = []

        def record(stage_id):
            stage_ids.append(stage_id)
            self.server.staging_record_status(stage_id, "stage1 ok: agent installed and started")

        original = self.server.create_staging
        self.server.create_staging = Mock(side_effect=lambda payload: record(original(payload)) or stage_ids[-1])
        result, _ = self.call()
        self.assertEqual(result["stage_status"], "stage1 ok: agent installed and started")

    def test_launch_acknowledgement_without_new_agent_is_not_success(self):
        self.server._await_session_registration.return_value = False
        result, _ = self.call()
        self.assertFalse(result["ok"])
        self.assertFalse(result["registered"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["launch_status"], "acknowledged")

    def test_registration_needs_no_source_or_target_execution_probe(self):
        result, _ = self.call()
        self.assertTrue(result["verified"])
        commands = [c.args[1] for c in self.server.exec_and_wait.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(c.startswith(("__LATERAL__\n", "__LATERAL_CLEANUP__\n")) for c in commands))
        self.assertNotIn("source_responsive", result)
        self.assertNotIn("target_responsive", result)

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

    def test_invalid_method_and_host(self):
        self.assertEqual(self.server.lateral_move(self.source.id, "ssh", "target"), (None, "invalid_method"))
        self.assertEqual(self.server.lateral_move(self.source.id, "winrm", ""), (None, "invalid_host"))

    def test_removed_method_is_rejected_before_dispatch(self):
        self.assertEqual(self.server.lateral_move(self.source.id, "smbexec", "target"), (None, "invalid_method"))
        self.server.exec_and_wait.assert_not_called()
        self.server._new_deployment_id.assert_not_called()

    def test_shared_tool_and_ai_schema_reject_removed_method(self):
        import json
        import rmm_tools
        client = Mock()
        with patch.object(rmm_tools, '_resolve_session_id', return_value=(self.source.id, {})):
            result = json.loads(rmm_tools.tool_lateral_move(client, self.source.id, 'smbexec', 'target'))
        self.assertEqual(result['error'], 'invalid_method')
        client.lateral_move.assert_not_called()
        definition = next(tool['function'] for tool in rmm_tools.OPENAI_TOOLS if tool['function']['name'] == 'lateral_move')
        self.assertEqual(definition['parameters']['properties']['method']['enum'], ['psexec', 'wmiexec', 'winrm'])

    def test_fractional_timeout_reserves_response_budget(self):
        self.call(exec_timeout=12.5)
        call = self.server.exec_and_wait.call_args_list[0]
        request = __import__('json').loads(call.args[1].split("\n", 1)[1])
        self.assertEqual(request["timeout_seconds"], 6.25)
        self.assertEqual(call.kwargs["timeout"], 12.5)

    def test_worker_budget_includes_beacon_sleep_and_jitter(self):
        self.source.sleep_seconds = 60
        self.source.jitter_percent = 30
        self.call(exec_timeout=300)
        call = self.server.exec_and_wait.call_args_list[0]
        request = json.loads(call.args[1].split("\n", 1)[1])
        self.assertEqual(request["timeout_seconds"], 207)
        self.assertEqual(call.kwargs["timeout"], 300)

    def test_uid_reports_output(self):
        self.server.exec_and_wait = Mock(return_value=({"body": "agent user: LAB\\alice\nnetwork identity: none"}, "ok"))
        result, status = self.server.session_uid(self.source.id)
        self.assertEqual(status, "ok")
        self.assertIn("agent user:", result["output"])


class StagingStoreTests(unittest.TestCase):
    def setUp(self):
        self.server = object.__new__(server_rmm.RMMServer)
        self.server.staging_lock = threading.Lock()
        self.server.staging_entries = {}

    def test_mask_never_exposes_an_executable_and_is_symmetric(self):
        payload = b"MZ\x90\x00" + b"stage2 payload bytes" * 64
        staging_id = "0123456789abcdef0123456789abcdef"
        masked = server_rmm.staging_mask(payload, staging_id)
        self.assertTrue(masked.startswith(b"MZ") is False or masked == payload)
        self.assertFalse(masked.startswith(b"MZ"))
        self.assertEqual(server_rmm.staging_mask(masked, staging_id), payload)
        self.assertNotEqual(server_rmm.staging_mask(masked, "ffffffffffffffffffffffffffffffff"), payload)

    def test_fetch_is_single_use_and_status_survives(self):
        staging_id = self.server.create_staging(b"MZpayload")
        self.assertIsNotNone(self.server.staging_payload(staging_id))
        self.assertIsNone(self.server.staging_payload(staging_id), "staged payloads are single-use")
        self.assertTrue(self.server.staging_record_status(staging_id, "stage1 ok: agent installed"))
        self.assertEqual(self.server.staging_status(staging_id), "stage1 ok: agent installed")

    def test_staged_build_ttl_and_operator_status_view(self):
        # Staged ZIP builds are deployed by hand: the staging entry must live
        # for the 24 hours the ZIP README promises, not the psexec TTL.
        staging_id = self.server.create_staging(b"MZpayload", ttl_seconds=server_rmm.STAGING_BUILD_TTL_SECONDS)
        entry = self.server.staging_entries[staging_id]
        self.assertAlmostEqual(entry["expires"] - entry["created"],
                               server_rmm.STAGING_BUILD_TTL_SECONDS, delta=2)
        self.assertTrue(self.server.staging_record_status(staging_id, "stage1 ok: agent installed and started"))
        info = self.server.staging_info(staging_id)
        self.assertEqual(info["staging_id"], staging_id)
        self.assertEqual(info["status"], "stage1 ok: agent installed and started")
        self.assertFalse(info["fetched"])
        self.assertFalse(info["expired"])
        self.assertNotIn("payload", info)
        self.assertIsNone(self.server.staging_info("unknown00000000"))

    def test_operator_stage_status_endpoint(self):
        staging_id = self.server.create_staging(b"MZpayload", ttl_seconds=server_rmm.STAGING_BUILD_TTL_SECONDS)
        self.server.staging_record_status(staging_id, "stage1 failed: stage2 download failed: HTTP 404")
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["agent", "go", "stage", staging_id])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = self.server
        handler._handle_api_get(f"/api/v1/agent/go/stage/{staging_id}", {})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "stage1 failed: stage2 download failed: HTTP 404")
        self.assertFalse(payload["fetched"])
        handler._api_path_parts = Mock(return_value=["agent", "go", "stage", "unknown00000000"])
        handler._handle_api_get("/api/v1/agent/go/stage/unknown00000000", {})
        self.assertEqual(handler._json.call_args.args[0], 404)

    def test_unknown_and_expired_entries_are_rejected(self):
        self.assertIsNone(self.server.staging_payload("unknown00000000"))
        self.assertFalse(self.server.staging_record_status("unknown00000000", "x"))
        staging_id = self.server.create_staging(b"MZpayload")
        self.server.staging_entries[staging_id]["expires"] = 0.0
        self.assertIsNone(self.server.staging_payload(staging_id))
        self.assertFalse(self.server.staging_record_status(staging_id, "late status"))

    def test_expired_entries_are_purged_on_create(self):
        first = self.server.create_staging(b"MZone")
        self.server.staging_entries[first]["expires"] = 0.0
        second = self.server.create_staging(b"MZtwo")
        self.assertNotIn(first, self.server.staging_entries)
        self.assertIn(second, self.server.staging_entries)

    def test_serve_stage_tool_get_then_not_found(self):
        staging_id = self.server.create_staging(b"MZpayload")

        def handler_for(method):
            handler = object.__new__(server_rmm.RMMHandler)
            handler.command = method
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler._safe_write = Mock()
            handler._respond = Mock()
            handler.headers = {}
            handler.rfile = io.BytesIO(b"")
            handler.server_instance = self.server
            return handler

        handler = handler_for("GET")
        self.assertTrue(handler._serve_stage_tool(staging_id))
        served = handler._safe_write.call_args.args[0]
        self.assertFalse(served.startswith(b"MZ"))
        self.assertEqual(server_rmm.staging_mask(served, staging_id), b"MZpayload")
        self.assertEqual(handler.send_response.call_args.args[0], 200)
        again = handler_for("GET")
        self.assertTrue(again._serve_stage_tool(staging_id))
        self.assertEqual(again._respond.call_args.args[0], 404)

    def test_serve_stage_tool_records_status(self):
        staging_id = self.server.create_staging(b"MZpayload")
        handler = object.__new__(server_rmm.RMMHandler)
        handler.command = "POST"
        handler._respond = Mock()
        handler.headers = {"Content-Length": "16"}
        handler.rfile = io.BytesIO(b"stage1 failed: x")
        handler.server_instance = self.server
        self.assertTrue(handler._serve_stage_tool(staging_id))
        self.assertEqual(handler._respond.call_args.args[0], 200)
        self.assertEqual(self.server.staging_status(staging_id), "stage1 failed: x")


class LateralHttpRoutingTests(unittest.TestCase):
    def _handler(self, endpoint, result, status="ok"):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["sessions", "abc", endpoint])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(
            lateral_move=Mock(return_value=(result, status)),
            session_uid=Mock(return_value=(result, status)),
        )
        return handler

    def _run(self, handler, body):
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="full-session-id")):
            handler._handle_api_post(f"/api/v1/sessions/abc/{handler._api_path_parts()[-1]}", body)

    def test_lateral_ok_and_parameter_passthrough(self):
        handler = self._handler("lateral", {"ok": True, "target_session_id": "go-x-lateral-1", "verified": True})
        self._run(handler, {"method": "psexec", "host": "ws01"})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertTrue(payload["verified"])
        handler.server_instance.lateral_move.assert_called_once_with(
            "full-session-id", "psexec", "ws01",
            verify=True, exec_timeout=300.0, register_timeout=60.0, verify_timeout=120.0,
        )

    def test_lateral_status_codes(self):
        cases = [
            ("agent_type_unsupported", 409),
            ("session_busy", 409),
            ("invalid_method", 400),
            ("invalid_host", 400),
            ("agent_payload_unavailable", 503),
            ("debug_payload_unavailable", 503),
            ("timeout", 408),
            ("session_killed", 410),
            ("server_stopped", 503),
        ]
        for status, expected_code in cases:
            with self.subTest(status=status):
                handler = self._handler("lateral", None, status)
                self._run(handler, {"method": "psexec", "host": "ws01"})
                code, payload = handler._json.call_args.args
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["error"], status)

    def test_lateral_rejects_invalid_options(self):
        handler = self._handler("lateral", {"ok": True})
        self._run(handler, {"method": "psexec", "host": "ws01", "exec_timeout": 0})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid_lateral_options")
        handler.server_instance.lateral_move.assert_not_called()

    def test_uid_ok_and_status_codes(self):
        handler = self._handler("uid", {"ok": True, "output": "agent user: LAB\\alice"})
        self._run(handler, {})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertIn("agent user", payload["output"])
        for status, expected_code in (("agent_type_unsupported", 409), ("timeout", 408)):
            with self.subTest(status=status):
                handler = self._handler("uid", None, status)
                self._run(handler, {})
                self.assertEqual(handler._json.call_args.args[0], expected_code)

    def test_uid_rejects_invalid_timeout(self):
        handler = self._handler("uid", {"ok": True})
        self._run(handler, {"timeout": "abc"})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid_uid_options")

    def test_lateral_result_regex_only_accepts_agent_handshake(self):
        for body, expected in [
            ("lateral started: method=psexec host=ws01 target session=go-src-lateral-1730000000000000000", "go-src-lateral-1730000000000000000"),
            ("Lateral failed: stage agent on ws01: access is denied", None),
            ("lateral started: method=psexec host=ws01 target session=../pwn", None),
        ]:
            with self.subTest(body=body):
                match = server_rmm.LATERAL_RESULT_RE.search(body)
                self.assertEqual(match.group("session_id") if match else None, expected)

    def test_psexec_deployment_names_are_parsed_for_cleanup(self):
        body = ("lateral started: method=psexec host=ws01 target session=go-x-lateral-1 "
                "service=MinimalRmmAgent-0011223344556677 binary=minimal-rmm-agent-0011223344556677.exe")
        service = server_rmm.LATERAL_SERVICE_RE.search(body)
        binary = server_rmm.LATERAL_BINARY_RE.search(body)
        self.assertEqual(service.group("service_name"), "MinimalRmmAgent-0011223344556677")
        self.assertEqual(binary.group("binary_name"), "minimal-rmm-agent-0011223344556677.exe")
        self.assertIsNone(server_rmm.LATERAL_SERVICE_RE.search("lateral started: method=wmiexec host=ws01 target session=go-x-1"))




if __name__ == "__main__":
    unittest.main()
