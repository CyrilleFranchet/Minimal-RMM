"""Verify lateral_move and session_uid server flow, REST routing, and redaction."""
import hashlib
import hmac
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
        self.server.log = Mock()
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
        # Durable deployment records must not touch the repository's log
        # directory during unit tests, and every test needs a fresh store.
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as records_file:
            records_file.write(b"{}")
            self.deployment_records_path = records_file.name
        self.addCleanup(os.unlink, self.deployment_records_path)
        records_patcher = patch.object(server_rmm, "LATERAL_DEPLOYMENT_FILE", self.deployment_records_path)
        records_patcher.start()
        self.addCleanup(records_patcher.stop)
        beacon_secret_patcher = patch.object(server_rmm, "BEACON_SECRET", "fixture-beacon-secret")
        beacon_secret_patcher.start()
        self.addCleanup(beacon_secret_patcher.stop)

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

    def test_cleanup_retry_carries_signed_deployment_ownership(self):
        self.ack_output += "\nlateral cleanup pending: launcher still running"
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "ok")
        self.assertEqual(result["deployment_id"], self.target_id)
        cleanup = [c.args[1] for c in self.server.exec_and_wait.call_args_list
                   if c.args[1].startswith("__LATERAL_CLEANUP__")][-1]
        request = json.loads(cleanup.split("\n", 1)[1])
        self.assertEqual(request["deployment_id"], self.target_id)
        self.assertEqual(request["host"], "target")
        self.assertEqual(request["service_name"], "MinimalRmmAgent-0011223344556677")
        self.assertEqual(request["binary_name"], "minimal-rmm-agent-0011223344556677.exe")
        # The agent verifies an HMAC over the deployment identity and its exact
        # artifacts keyed by the shared beacon secret; pin that wire contract.
        message = "\n".join(["lateral-cleanup-v1", self.target_id, "target",
                             "MinimalRmmAgent-0011223344556677",
                             "minimal-rmm-agent-0011223344556677.exe"]).encode("utf-8")
        expected = hmac.new(b"fixture-beacon-secret", message, hashlib.sha256).hexdigest()
        self.assertEqual(request["ownership_tag"], expected)

    def test_ownership_tag_binds_the_deployment_only(self):
        tag = server_rmm.lateral_cleanup_ownership_tag(
            self.target_id, "target", "MinimalRmmAgent-0011223344556677",
            "minimal-rmm-agent-0011223344556677.exe")
        self.assertNotEqual(tag, server_rmm.lateral_cleanup_ownership_tag(
            "another-deployment-00001", "target", "MinimalRmmAgent-0011223344556677",
            "minimal-rmm-agent-0011223344556677.exe"), "another deployment must never reuse a tag")
        self.assertNotEqual(tag, server_rmm.lateral_cleanup_ownership_tag(
            self.target_id, "target", "MinimalRmmAgent-ffeeddccbbaa99887766",
            "minimal-rmm-agent-0011223344556677.exe"), "another service must never be removable")
        self.assertNotEqual(tag, server_rmm.lateral_cleanup_ownership_tag(
            self.target_id, "target", "MinimalRmmAgent-0011223344556677",
            "other-agent-0011223344556677.exe"), "another binary must never be removable")

    def test_deployment_record_tracks_artifacts_and_cleanup_state(self):
        result, _ = self.call()
        records = self.server.lateral_deployment_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["deployment_id"], self.target_id)
        self.assertEqual(record["workspace_id"], "alice")
        self.assertEqual(record["source_session_id"], self.source.id)
        self.assertEqual(record["host"], "target")
        self.assertEqual(record["artifacts"], {"service_name": "MinimalRmmAgent-0011223344556677",
                                              "binary_name": "minimal-rmm-agent-0011223344556677.exe"})
        self.assertEqual(record["deployment"]["status"], "acknowledged")
        self.assertEqual(record["cleanup"]["status"], "ok")
        self.assertEqual(record["cleanup"]["performed_by"], "cleanup_worker")
        self.assertEqual(record["cleanup"]["attempts"], 1)
        # The record survives on disk, so a server restart can recover it.
        with open(self.deployment_records_path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)[self.target_id]["cleanup"]["status"], "ok")

    def test_cleanup_retry_recovers_after_source_restart(self):
        self.ack_output += "\nlateral cleanup pending: launcher port busy"
        self.cleanup_output = "lateral cleanup failed: remove launcher binary on target (denied)"
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "failed")
        restarted = object.__new__(server_rmm.RMMServer)
        restarted.log = Mock()
        restarted.resolve_session = Mock(return_value=self.source)
        restarted.record_operator_action = Mock()
        restarted.exec_and_wait = Mock(return_value=(
            {"body": "lateral cleanup: service deleted; launcher binary removed from ADMIN$"}, "ok"))
        outcome, status = restarted.retry_lateral_cleanup(self.target_id)
        self.assertEqual(status, "ok")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["deployment"]["host"], "target")
        # The restarted server loaded the record from disk and sent exactly
        # one signed cleanup command for the recorded artifacts.
        command = restarted.exec_and_wait.call_args.args[1]
        self.assertTrue(command.startswith("__LATERAL_CLEANUP__"))
        request = json.loads(command.split("\n", 1)[1])
        self.assertEqual(request["service_name"], "MinimalRmmAgent-0011223344556677")
        self.assertEqual(request["binary_name"], "minimal-rmm-agent-0011223344556677.exe")
        self.assertEqual(request["deployment_id"], self.target_id)
        record = restarted.lateral_deployment_records()[0]
        self.assertEqual(record["cleanup"]["status"], "ok")
        self.assertEqual(record["cleanup"]["attempts"], 2)
        restarted.exec_and_wait.assert_called_once()

    def test_duplicate_retry_after_success_is_idempotent(self):
        self.ack_output += "\nlateral cleanup pending: launcher still running"
        self.call()
        self.server.exec_and_wait.reset_mock()
        outcome, status = self.server.retry_lateral_cleanup(self.target_id)
        self.assertEqual(status, "ok")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["cleanup"]["status"], "ok")
        self.server.exec_and_wait.assert_not_called()
        record = self.server.lateral_deployment_records()[0]
        self.assertEqual(record["cleanup"]["attempts"], 1, "an idempotent retry dispatches nothing")

    def test_retry_accepts_alternate_executor_of_the_same_workspace(self):
        self.ack_output += "\nlateral cleanup pending: launcher still running"
        self.cleanup_output = "lateral cleanup failed: remove launcher binary on target (denied)"
        self.call()
        alternate = SimpleNamespace(id="go-alternate-session", agent_type="go", workspace_id="alice")
        self.server.resolve_session = Mock(return_value=alternate)
        self.server.exec_and_wait = Mock(return_value=(
            {"body": "lateral cleanup: service not found or already removed; launcher binary not found on ADMIN$"},
            "ok"))
        outcome, status = self.server.retry_lateral_cleanup(self.target_id, "go-alternate")
        self.assertEqual(status, "ok")
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.server.exec_and_wait.call_args.args[0], "go-alternate-session")

    def test_unacknowledged_deployment_names_stay_untrackable(self):
        self.ack_status = "timeout"
        self.ack_output = ""
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "skipped")
        self.assertEqual(result["cleanup"]["record_status"], "untracked")
        record = self.server.lateral_deployment_records()[0]
        self.assertEqual(record["artifacts"], {})
        self.assertEqual(record["cleanup"]["status"], "untracked")
        outcome, status = self.server.retry_lateral_cleanup(self.target_id)
        self.assertEqual(status, "untracked")
        self.assertFalse(outcome["ok"])
        self.server.exec_and_wait.assert_called_once()

    def test_retry_listing_is_workspace_scoped(self):
        self.call()
        self.assertEqual([row["deployment_id"] for row in self.server.lateral_deployment_records()],
                         [self.target_id])
        self.assertEqual(self.server.lateral_deployment_records(workspace_id="alice"),
                         self.server.lateral_deployment_records())
        self.assertEqual(self.server.lateral_deployment_records(workspace_id="bob"), [])
        # A foreign workspace cannot even see that the deployment exists.
        self.assertEqual(self.server.retry_lateral_cleanup(self.target_id, workspace_id="bob"),
                         (None, "deployment_not_found"))

    def test_retry_rejects_malformed_identifiers(self):
        self.assertEqual(self.server.retry_lateral_cleanup("../pwn"), (None, "invalid_deployment"))
        self.assertEqual(self.server.retry_lateral_cleanup("short"), (None, "invalid_deployment"))
        self.assertEqual(self.server.retry_lateral_cleanup("0123456789abcdef0123456789abcdef"),
                         (None, "deployment_not_found"))

    def test_skipped_cleanup_call_records_a_recoverable_state(self):
        self.ack_output += "\nlateral cleanup pending: launcher still running"
        result, _ = self.call(verify=False)
        self.assertEqual(result["cleanup"]["status"], "skipped")
        self.assertEqual(result["cleanup"]["record_status"], "pending")
        record = self.server.lateral_deployment_records()[0]
        self.assertEqual(record["cleanup"]["status"], "pending")
        self.assertIn("verification disabled", record["cleanup"]["detail"])
        # The persisted pending state keeps the operation recoverable later.
        self.server.exec_and_wait = Mock(return_value=({"body": self.cleanup_output}, "ok"))
        outcome, status = self.server.retry_lateral_cleanup(self.target_id)
        self.assertEqual(status, "ok")
        self.assertTrue(outcome["ok"])

    def test_cleanup_response_status_and_record_status_travel_together(self):
        # An immediate source-agent cleanup is ok in both views.
        self.ack_output += "\n" + self.cleanup_output
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "ok")
        self.assertEqual(result["cleanup"]["record_status"], "ok")
        # A transport failure during the retry is a delivery failure in the
        # response and a durable failed state in the record.
        full_output = self.handshake() + "\nlateral cleanup pending: still running"
        self.ack_output = full_output
        self.ack_status = "ok"

        def execute(sid, command, timeout=None):
            if command.startswith("__LATERAL_CLEANUP__"):
                return None, "timeout"
            return {"body": full_output}, "ok"

        self.server.exec_and_wait = Mock(side_effect=execute)
        result, _ = self.call()
        self.assertEqual(result["cleanup"]["status"], "timeout")
        self.assertEqual(result["cleanup"]["record_status"], "failed")
        record = self.server.lateral_deployment_records()[0]
        self.assertEqual(record["cleanup"]["status"], "failed")
        self.assertIn("dispatch ended with timeout", record["cleanup"]["detail"])

    def test_corrupt_record_store_is_preserved_not_replaced(self):
        corrupt = b"{ not valid json"
        with open(self.deployment_records_path, "wb") as handle:
            handle.write(corrupt)
        restarted = object.__new__(server_rmm.RMMServer)
        restarted.log = Mock()
        restarted.record_lateral_deployment("srv-record-target-1", "alice",
                                            self.source.id, "psexec", "ws01")
        # The damaged file rotated aside instead of being overwritten.
        directory = os.path.dirname(self.deployment_records_path)
        base = os.path.basename(self.deployment_records_path)
        backups = [name for name in os.listdir(directory)
                   if name.startswith(base + ".corrupt-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(directory, backups[0]), "rb") as handle:
            self.assertEqual(handle.read(), corrupt)
        # A fresh store takes over without losing the new record.
        with open(self.deployment_records_path, "r", encoding="utf-8") as handle:
            self.assertEqual(list(json.load(handle)), ["srv-record-target-1"])
        self.assertEqual([row["deployment_id"] for row in restarted.lateral_deployment_records()],
                         ["srv-record-target-1"])
        messages = " ".join(str(call.args[0]) for call in restarted.log.call_args_list if call.args)
        self.assertIn("preserved", messages)

    def test_unreadable_record_store_disables_persistence(self):
        with open(self.deployment_records_path, "wb") as handle:
            handle.write(b"legacy-bytes")
        fresh = object.__new__(server_rmm.RMMServer)
        fresh.log = Mock()
        with patch.object(server_rmm.RMMServer, "_read_lateral_deployment_store",
                          side_effect=PermissionError(13, "Permission denied")):
            fresh.record_lateral_deployment("srv-record-target-2", "alice",
                                            self.source.id, "psexec", "ws01")
            fresh.record_lateral_deployment("srv-record-target-3", "alice",
                                            self.source.id, "psexec", "ws02")
        # The unreadable file is untouched, and records stay in memory only.
        with open(self.deployment_records_path, "rb") as handle:
            self.assertEqual(handle.read(), b"legacy-bytes")
        self.assertEqual([row["deployment_id"] for row in fresh.lateral_deployment_records()],
                         ["srv-record-target-2", "srv-record-target-3"])
        warnings = [call for call in fresh.log.call_args_list
                    if call.args and "stay in memory only" in str(call.args[0])]
        self.assertEqual(len(warnings), 1, "the preservation warning is logged once per server run")

    def test_undecodable_record_store_is_preserved_not_replaced(self):
        raw = b"\xff\xfe broken bytes \x81"
        with open(self.deployment_records_path, "wb") as handle:
            handle.write(raw)
        restarted = object.__new__(server_rmm.RMMServer)
        restarted.log = Mock()
        restarted.record_lateral_deployment("srv-record-target-4", "alice",
                                            self.source.id, "psexec", "ws01")
        # The undecodable file rotated aside with its bytes intact.
        directory = os.path.dirname(self.deployment_records_path)
        base = os.path.basename(self.deployment_records_path)
        backups = [name for name in os.listdir(directory) if name.startswith(base + ".corrupt-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(directory, backups[0]), "rb") as handle:
            self.assertEqual(handle.read(), raw)
        # A fresh store takes over without losing the new record.
        with open(self.deployment_records_path, "r", encoding="utf-8") as handle:
            self.assertEqual(list(json.load(handle)), ["srv-record-target-4"])
        self.assertEqual([row["deployment_id"] for row in restarted.lateral_deployment_records()],
                         ["srv-record-target-4"])
        messages = " ".join(str(call.args[0]) for call in restarted.log.call_args_list if call.args)
        self.assertIn("undecodable", messages)

    def test_malformed_nested_records_are_preserved_and_valid_ones_carry_over(self):
        valid = {
            "deployment_id": "srv-record-target-9",
            "workspace_id": "alice", "source_session_id": self.source.id,
            "method": "psexec", "host": "ws02", "debug": False,
            "created": "2026-11-23T13:00:00",
            "deployment": {"status": "acknowledged", "updated": "2026-11-23T13:00:01"},
            "artifacts": {"service_name": "MinimalRmmAgent-0011223344556677",
                          "binary_name": "minimal-rmm-agent-0011223344556677.exe"},
            "cleanup": {"status": "pending", "attempts": 1, "detail": "launcher busy",
                        "performed_by": None, "updated": "2026-11-23T13:00:02"},
        }
        wrong_section_type = {"deployment_id": "srv-record-target-8",
                             "workspace_id": "alice", "host": "ws03", "cleanup": "broken"}
        wrong_field_type = {"deployment_id": "srv-record-target-7",
                            "workspace_id": "alice", "host": "ws04", "cleanup": {"attempts": "two"}}
        original = {"srv-record-target-9": valid,
                    "srv-record-target-8": wrong_section_type,
                    "srv-record-target-7": wrong_field_type}
        with open(self.deployment_records_path, "w", encoding="utf-8") as handle:
            json.dump(original, handle)
        restarted = object.__new__(server_rmm.RMMServer)
        restarted.log = Mock()
        restarted.resolve_session = Mock(return_value=self.source)
        restarted.record_operator_action = Mock()
        restarted.exec_and_wait = Mock(return_value=({"body": self.cleanup_output}, "ok"))
        # The recoverable cleanup keeps working: a malformed sibling record
        # cannot break consumers of the valid one.
        outcome, status = restarted.retry_lateral_cleanup("srv-record-target-9")
        self.assertEqual(status, "ok")
        self.assertTrue(outcome["ok"])
        # The damaged entries are preserved in a rotation backup...
        directory = os.path.dirname(self.deployment_records_path)
        base = os.path.basename(self.deployment_records_path)
        backups = [name for name in os.listdir(directory) if name.startswith(base + ".corrupt-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(directory, backups[0]), "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), original)
        # ...and the live store carries only the valid record, immediately,
        # so a restart does not re-rotate the same damage.
        with open(self.deployment_records_path, "r", encoding="utf-8") as handle:
            live = json.load(handle)
        self.assertEqual(list(live), ["srv-record-target-9"])
        self.assertEqual(live["srv-record-target-9"]["cleanup"]["status"], "ok")
        messages = " ".join(str(call.args[0]) for call in restarted.log.call_args_list if call.args)
        self.assertIn("malformed record", messages)
        self.assertIn("srv-record-target-8", messages)
        self.assertIn("srv-record-target-7", messages)

    def test_retention_bounds_memory_and_history_consistently(self):
        import datetime as datetime_module
        counter = {"seconds": 0}

        def fake_now():
            counter["seconds"] += 1
            return datetime_module.datetime(2026, 11, 23, 12, 0, counter["seconds"])

        with patch.object(server_rmm, "LATERAL_DEPLOYMENT_LIMIT", 3), \
                patch.object(server_rmm, "datetime", SimpleNamespace(now=fake_now)):
            deployment_ids = [f"srv-record-target-{index:08d}" for index in range(5)]
            for deployment_id in deployment_ids:
                self.server.record_lateral_deployment(deployment_id, "alice",
                                                      self.source.id, "psexec", "ws01")
                self.server.update_lateral_deployment_result(
                    deployment_id, "acknowledged",
                    "MinimalRmmAgent-0011223344556677", "minimal-rmm-agent-0011223344556677.exe")
                self.server.update_lateral_cleanup(deployment_id, "ok", detail="lateral cleanup: done")
            # The finished records drop off oldest first, in memory and on disk,
            # so listings match what a restart will load.
            rows = self.server.lateral_deployment_records()
            self.assertEqual([row["deployment_id"] for row in rows],
                             list(reversed(deployment_ids[-3:])))
            with open(self.deployment_records_path, "r", encoding="utf-8") as handle:
                self.assertEqual(sorted(json.load(handle)), sorted(deployment_ids[-3:]))
            # Unfinished cleanups are never pruned, so recovery outlives the limit.
            pending_id = "srv-record-target-p"
            self.server.record_lateral_deployment(pending_id, "alice", self.source.id, "psexec", "ws02")
            self.server.update_lateral_deployment_result(
                pending_id, "acknowledged",
                "MinimalRmmAgent-0011223344556677", "minimal-rmm-agent-0011223344556677.exe")
            self.server.update_lateral_cleanup(pending_id, "pending", detail="busy")
            rows = self.server.lateral_deployment_records()
            self.assertEqual([row["deployment_id"] for row in rows],
                             [pending_id] + list(reversed(deployment_ids[-2:])))
            with open(self.deployment_records_path, "r", encoding="utf-8") as handle:
                self.assertEqual(sorted(json.load(handle)),
                                 sorted([pending_id] + deployment_ids[-2:]))

    def test_psexec_without_staged_build_is_reported(self):
        with patch.object(server_rmm, "latest_go_agent_binary", return_value=None):
            self.assertEqual(self.call(), (None, "agent_payload_unavailable"))
        self.server.exec_and_wait.assert_not_called()

    def test_psexec_selects_an_artifact_bound_to_the_source_workspace(self):
        self.call()
        self.assertEqual(server_rmm.latest_go_agent_binary.call_args.kwargs, {"workspace_id": "alice"})

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

    def _capture_execute(self):
        captured = []
        original = self.execute

        def capture(sid, command, timeout=None):
            captured.append(command)
            return original(sid, command, timeout=timeout)

        self.server.exec_and_wait = Mock(side_effect=capture)
        return captured

    def test_explicit_identity_travels_to_the_wmi_agent_payload(self):
        captured = self._capture_execute()
        self.ack_output = f"lateral started: method=wmiexec host=target target session={self.target_id}"
        result, status = self.server.lateral_move(
            self.source.id, "wmiexec", "target", verify=False,
            identity_domain="LAB", identity_user="alice", identity_password="s3cret pw")
        self.assertEqual(status, "ok")
        body = json.loads(captured[0].split("\n", 1)[1])
        self.assertEqual(body["identity"], {"domain": "LAB", "user": "alice", "password": "s3cret pw"})
        # The audit records the identity label only: no domain pair with the
        # password, and no password material anywhere near the audit text.
        audit = self.server.record_operator_action.call_args.args[1]
        self.assertIn("identity=LAB\\alice", audit)
        self.assertIn("password=", audit)
        self.assertIn('password="s3cret pw"', audit)
        self.assertEqual(result["launch_status"], "acknowledged")

    def test_explicit_identity_splits_a_combined_identity(self):
        captured = self._capture_execute()
        self.ack_output = f"lateral started: method=winrm host=target target session={self.target_id}"
        _, status = self.server.lateral_move(
            self.source.id, "winrm", "target", verify=False,
            identity_user="LAB\\bob", identity_password="pw")
        self.assertEqual(status, "ok")
        body = json.loads(captured[0].split("\n", 1)[1])
        self.assertEqual(body["identity"]["domain"], "LAB")
        self.assertEqual(body["identity"]["user"], "bob")

    def test_explicit_identity_requires_wmiexec_or_winrm(self):
        for method in ("psexec",):
            _, status = self.server.lateral_move(
                self.source.id, method, "target", identity_user="alice", identity_password="pw")
            self.assertEqual(status, "invalid_identity_method")
        self.server.exec_and_wait.assert_not_called()

    def test_explicit_identity_requires_a_user(self):
        _, status = self.server.lateral_move(
            self.source.id, "wmiexec", "target", identity_domain="LAB", identity_password="pw")
        self.assertEqual(status, "invalid_identity")
        self.server.exec_and_wait.assert_not_called()

    def test_default_lateral_payload_stays_identity_free(self):
        captured = self._capture_execute()
        self.ack_output = f"lateral started: method=wmiexec host=target target session={self.target_id}"
        _, status = self.server.lateral_move(self.source.id, "wmiexec", "target", verify=False)
        self.assertEqual(status, "ok")
        body = json.loads(captured[0].split("\n", 1)[1])
        self.assertNotIn("identity", body)


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

    def test_cleanup_retry_tools_and_ai_schema(self):
        import rmm_tools
        client = Mock()
        client.list_lateral_deployments.return_value = (
            200, {"count": 1, "deployments": [{
                "deployment_id": self.target_id, "workspace_id": "alice", "host": "target",
                "created": "2026-11-23 10:00:00",
                "deployment": {"status": "acknowledged"},
                "artifacts": {"service_name": "MinimalRmmAgent-0011223344556677",
                              "binary_name": "minimal-rmm-agent-0011223344556677.exe"},
                "cleanup": {"status": "failed", "attempts": 1}}]})
        listed = json.loads(rmm_tools.tool_list_lateral_deployments(client))
        self.assertTrue(listed["ok"])
        self.assertTrue(listed["deployments"][0]["retriable"])
        client.list_lateral_deployments.assert_called_once_with()
        handlers = rmm_tools.TOOL_HANDLERS
        client.retry_lateral_cleanup.return_value = (
            200, {"ok": True, "deployment": {},
                  "cleanup": {"status": "ok", "attempts": 2, "output": "lateral cleanup: service deleted"}})
        retried = json.loads(handlers["retry_lateral_cleanup"](
            client, {"deployment_id": self.target_id, "session_ref": "go-alternate"}))
        self.assertTrue(retried["ok"])
        client.retry_lateral_cleanup.assert_called_once_with(self.target_id, session_id="go-alternate")
        schema_names = {tool["function"]["name"] for tool in rmm_tools.OPENAI_TOOLS}
        self.assertIn("list_lateral_deployments", schema_names)
        self.assertIn("retry_lateral_cleanup", schema_names)
        # A missing deployment id is rejected without contacting the server.
        client.reset_mock()
        invalid = json.loads(rmm_tools.tool_retry_lateral_cleanup(client, "  "))
        self.assertEqual(invalid["error"], "invalid_deployment")
        client.retry_lateral_cleanup.assert_not_called()

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

    def test_winrm_https_is_queued_and_audited(self):
        self.ack_output = f"lateral started: method=winrm host=target target session={self.target_id}"
        result, status = self.server.lateral_move(self.source.id, "winrm", "target", https=True)
        self.assertEqual(status, "ok")
        self.assertTrue(result["ok"])
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertIs(request["https"], True)
        self.assertEqual(request["method"], "winrm_https_loader_independent", "older agents must reject the loader chain instead of ignoring it")
        self.assertIn("-s", self.server.record_operator_action.call_args.args[1])

    def test_wmiexec_and_winrm_deploy_server_staged_artifacts(self):
        for method, wire in (("wmiexec", "wmiexec_loader"), ("winrm", "winrm_loader_independent")):
            with self.subTest(method=method):
                self.ack_output = f"lateral started: method={method} host=target target session={self.target_id} loader=csharp"
                self.server.exec_and_wait.reset_mock()
                result, status = self.server.lateral_move(self.source.id, method, "target")
                self.assertEqual(status, "ok")
                self.assertTrue(result["ok"])
                self.assertEqual(result["loader"], "csharp")
                if method == "winrm":
                    self.assertTrue(result["independent"], "winrm deployments are independent by default")
                else:
                    self.assertNotIn("independent", result, "independent is winrm-scoped")
                request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
                self.assertEqual(request["method"], wire,
                                 "loader deployments must use wired names older agents reject")
                self.assertEqual(request["loader"], "csharp")
                self.assertTrue(request["stage"],
                                "loader deployments must carry a staging reference")
                self.assertEqual(request["payload_sha256"],
                                 hashlib.sha256(b"MZfake-agent-payload-for-staging").hexdigest())
                self.assertIn("loader=csharp", self.server.record_operator_action.call_args.args[1])

    def test_winrm_tethered_optout_and_off_method_scoping(self):
        self.ack_output = f"lateral started: method=winrm host=target target session={self.target_id} loader=csharp"
        result, _ = self.server.lateral_move(self.source.id, "winrm", "target", independent=False)
        self.assertFalse(result["independent"])
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertEqual(request["method"], "winrm_loader",
                         "the tethered opt-out must use the held-shell wire name")
        self.assertIn("--tethered", self.server.record_operator_action.call_args.args[1])
        self.server.exec_and_wait.reset_mock()
        # Non-winrm methods ignore the independent option instead of failing.
        result, status = self.server.lateral_move(self.source.id, "wmiexec", "target")
        self.assertEqual(status, "ok")
        self.assertNotIn("independent", result)
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertEqual(request["method"], "wmiexec_loader")
        self.server.exec_and_wait.reset_mock()
        result, status = self.server.lateral_move(self.source.id, "psexec", "target", independent=True)
        self.assertEqual(status, "ok")
        self.assertNotIn("independent", result)

    def test_loader_deployments_require_the_workspace_artifact(self):
        for method in ("wmiexec", "winrm"):
            with self.subTest(method=method):
                with patch.object(server_rmm, "latest_go_agent_binary", return_value=None):
                    self.assertEqual(self.server.lateral_move(self.source.id, method, "target"),
                                      (None, "agent_payload_unavailable"))
        self.server.exec_and_wait.assert_not_called()

    def test_loader_worker_budget_includes_beacon_sleep_and_jitter(self):
        self.source.sleep_seconds = 60
        self.source.jitter_percent = 30
        self.server.lateral_move(self.source.id, "winrm", "target", exec_timeout=300)
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertEqual(request["timeout_seconds"], 207)
        self.assertEqual(self.server.exec_and_wait.call_args.kwargs["timeout"], 300)

    def test_powershell_loader_is_reserved_and_unknown_loaders_fail(self):
        self.assertEqual(self.server.lateral_move(self.source.id, "winrm", "target", loader="powershell"),
                         (None, "loader_reserved"))
        for value in ["rubylang", 3, None]:
            self.assertEqual(self.server.lateral_move(self.source.id, "winrm", "target", loader=value),
                             (None, "invalid_loader"))
        self.server.exec_and_wait.assert_not_called()
        import rmm_cli
        import rmm_tools
        client = rmm_cli.RmmApiClient("http://fixture", "fixture-token")
        client.request = Mock(return_value=(200, {"ok": True}))
        with self.assertRaises(ValueError):
            client.lateral_move("source", "winrm", "target", loader="rust")
        client.lateral_move("source", "winrm", "target", loader=" powershell ".strip())
        self.assertEqual(client.request.call_args.args[2]["loader"], "powershell")
        with patch.object(rmm_tools, "_resolve_session_id", return_value=("source", {})):
            reserved = json.loads(rmm_tools.tool_lateral_move(client, "source", "winrm", "target", loader="powershell"))
            self.assertEqual(reserved["error"], "loader_reserved")
            invalid = json.loads(rmm_tools.tool_lateral_move(client, "source", "winrm", "target", loader="rust"))
            self.assertEqual(invalid["error"], "invalid_loader")
            defaulted = json.loads(rmm_tools.tool_lateral_move(client, "source", "winrm", "target"))
            self.assertEqual(defaulted["ok"], True)
            self.assertEqual(client.request.call_args.args[2]["loader"], "csharp")

    def test_certificate_bypass_is_explicit_and_requires_https(self):
        self.server.lateral_move(self.source.id, "winrm", "target", https=True, insecure=True)
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertIs(request["insecure"], True)
        self.assertEqual(request["method"], "winrm_https_insecure_loader_independent")
        self.assertIn("-k", self.server.record_operator_action.call_args.args[1])
        self.server.exec_and_wait.reset_mock()
        for value in [True, "true", 1, None]:
            result = self.server.lateral_move(self.source.id, "winrm", "target", insecure=value)
            self.assertEqual(result, (None, "invalid_insecure"))
        self.server.exec_and_wait.assert_not_called()
        import rmm_cli
        import rmm_tools
        client = rmm_cli.RmmApiClient("http://fixture", "fixture-token")
        client.request = Mock(return_value=(200, {}))
        client.lateral_move("source", "winrm", "target", https=True, insecure=True)
        self.assertIs(client.request.call_args.args[2]["insecure"], True)
        with self.assertRaises(ValueError): client.lateral_move("source", "winrm", "target", insecure=True)
        with patch.object(rmm_tools, "_resolve_session_id", return_value=("source", {})):
            result = json.loads(rmm_tools.tool_lateral_move(client, "source", "winrm", "target", insecure=True))
            self.assertEqual(result["error"], "invalid_insecure")

    def test_winrm_independent_deploys_with_local_wmi_restage(self):
        self.ack_output = f"lateral started: method=winrm host=target target session={self.target_id} loader=csharp"
        result, status = self.server.lateral_move(self.source.id, "winrm", "target", independent=True)
        self.assertEqual(status, "ok")
        self.assertTrue(result["ok"])
        self.assertTrue(result["independent"])
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertEqual(request["method"], "winrm_loader_independent",
                         "independent deployments must carry a wire name older agents reject")
        self.assertNotIn("--tethered", self.server.record_operator_action.call_args.args[1])

    def test_winrm_https_independent_wire_composition(self):
        self.server.lateral_move(self.source.id, "winrm", "target", https=True, independent=True)
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertEqual(request["method"], "winrm_https_loader_independent")
        self.server.exec_and_wait.reset_mock()
        self.server.lateral_move(self.source.id, "winrm", "target", https=True, insecure=True, independent=True)
        request = json.loads(self.server.exec_and_wait.call_args_list[0].args[1].split("\n", 1)[1])
        self.assertEqual(request["method"], "winrm_https_insecure_loader_independent")

    def test_independent_is_winrm_scoped_and_requires_booleans(self):
        for value in ("true", 1, None):
            self.assertEqual(self.server.lateral_move(self.source.id, "winrm", "target", independent=value),
                             (None, "invalid_independent"))
        self.server.exec_and_wait.assert_not_called()
        import rmm_cli
        import rmm_tools
        client = rmm_cli.RmmApiClient("http://fixture", "fixture-token")
        client.request = Mock(return_value=(200, {"ok": True}))
        with self.assertRaises(ValueError):
            client.lateral_move("source", "winrm", "target", independent="true")
        # The default is independent; the winrm-scoped option forwards, and
        # the tethered opt-out forwards false.
        client.lateral_move("source", "winrm", "target", independent=False)
        self.assertIs(client.request.call_args.args[2]["independent"], False)
        client.lateral_move("source", "winrm", "target")
        self.assertIs(client.request.call_args.args[2]["independent"], True)
        # Non-winrm methods scope the option away instead of failing.
        client.lateral_move("source", "wmiexec", "target", independent=True)
        self.assertNotIn("independent", client.request.call_args.args[2])
        with patch.object(rmm_tools, "_resolve_session_id", return_value=("source", {})):
            forwarded = json.loads(rmm_tools.tool_lateral_move(client, "source", "winrm", "target"))
            self.assertEqual(forwarded["ok"], True)
            self.assertIs(client.request.call_args.args[2]["independent"], True)
            scoped = json.loads(rmm_tools.tool_lateral_move(client, "source", "wmiexec", "target", independent=True))
            self.assertEqual(scoped["ok"], True)
            self.assertNotIn("independent", client.request.call_args.args[2])
            for value in ["true", 0, None]:
                invalid = json.loads(rmm_tools.tool_lateral_move(client, "source", "winrm", "target", independent=value))
                self.assertEqual(invalid["error"], "invalid_independent")

    def test_invalid_https_never_stages_or_dispatches(self):
        for method, value in [("psexec", True), ("wmiexec", True), ("winrm", "true"), ("winrm", 1), ("winrm", None)]:
            with self.subTest(method=method, value=value):
                self.assertEqual(self.server.lateral_move(self.source.id, method, "target", https=value), (None, "invalid_https"))
        self.server.exec_and_wait.assert_not_called()
        self.server._new_deployment_id.assert_not_called()

    def test_https_client_and_tool_forward_and_validate(self):
        import rmm_cli
        import rmm_tools
        client = rmm_cli.RmmApiClient("http://fixture", "fixture-token")
        client.request = Mock(return_value=(200, {"ok": True}))
        client.lateral_move("source", "winrm", "target", https=True)
        self.assertIs(client.request.call_args.args[2]["https"], True)
        client.lateral_move("source", " WinRM ", "target", https=True)
        self.assertIs(client.request.call_args.args[2]["https"], True)
        for method, value in [("psexec", True), ("winrm", "false"), ("winrm", 0)]:
            with self.assertRaises(ValueError):
                client.lateral_move("source", method, "target", https=value)
        mocked = Mock()
        mocked.lateral_move.return_value = (200, {"ok": True})
        with patch.object(rmm_tools, "_resolve_session_id", return_value=("source", {})):
            rmm_tools.TOOL_HANDLERS["lateral_move"](mocked, {"session_ref": "source", "method": "winrm", "host": "target", "https": True})
            self.assertIs(mocked.lateral_move.call_args.kwargs["https"], True)
            for value in ["false", 0, None]:
                result = json.loads(rmm_tools.tool_lateral_move(mocked, "source", "winrm", "target", https=value))
                self.assertEqual(result["error"], "invalid_https")


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
    def test_https_route_forwards_boolean_and_rejects_strings(self):
        handler = self._handler("lateral", {"ok": True})
        self._run(handler, {"method": "winrm", "host": "target", "https": True})
        self.assertIs(handler.server_instance.lateral_move.call_args.kwargs["https"], True)
        for value in ["true", "false", 1, None]:
            handler = self._handler("lateral", {"ok": True})
            self._run(handler, {"method": "winrm", "host": "target", "https": value})
            self.assertEqual(handler._json.call_args.args[0], 400)
            handler.server_instance.lateral_move.assert_not_called()

    def test_insecure_route_validates_and_forwards(self):
        handler = self._handler("lateral", {"ok": True})
        self._run(handler, {"method": "winrm", "host": "target", "https": True, "insecure": True})
        self.assertIs(handler.server_instance.lateral_move.call_args.kwargs["insecure"], True)
        for value in ["true", 1, None]:
            handler = self._handler("lateral", {"ok": True})
            self._run(handler, {"method": "winrm", "host": "target", "https": True, "insecure": value})
            self.assertEqual(handler._json.call_args.args[0], 400)
            handler.server_instance.lateral_move.assert_not_called()

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
            loader="csharp", independent=True,
            identity_domain="", identity_user="", identity_password="",
        )

    def test_lateral_identity_passthrough(self):
        handler = self._handler("lateral", {"ok": True, "target_session_id": "go-x-lateral-1", "verified": True})
        self._run(handler, {"method": "wmiexec", "host": "ws01",
                            "identity_domain": "LAB", "identity_user": "alice", "identity_password": "pw"})
        handler.server_instance.lateral_move.assert_called_once_with(
            "full-session-id", "wmiexec", "ws01",
            verify=True, exec_timeout=300.0, register_timeout=60.0, verify_timeout=120.0,
            loader="csharp", independent=True,
            identity_domain="LAB", identity_user="alice", identity_password="pw",
        )
        handler = self._handler("lateral", {"ok": True})
        cred = {"domain": "LAB", "user": "bob", "password": "pw2"}
        with patch.object(server_rmm._rmm_creds, "creds_resolve", return_value=cred) as resolve, \
                patch.object(server_rmm, "plugin_directory", return_value="/tmp/plugindir"):
            self._run(handler, {"method": "winrm", "host": "ws02", "identity_cred_id": "cred-3f0cfe46"})
        resolve.assert_called_once_with("/tmp/plugindir", "cred-3f0cfe46")
        handler.server_instance.lateral_move.assert_called_once_with(
            "full-session-id", "winrm", "ws02",
            verify=True, exec_timeout=300.0, register_timeout=60.0, verify_timeout=120.0,
            loader="csharp", independent=True,
            identity_domain="LAB", identity_user="bob", identity_password="pw2",
        )

    def test_lateral_status_codes(self):
        cases = [
            ("agent_type_unsupported", 409),
            ("session_busy", 409),
            ("invalid_method", 400),
            ("invalid_host", 400),
            ("invalid_https", 400),
            ("invalid_insecure", 400),
            ("invalid_loader", 400),
            ("loader_reserved", 400),
            ("invalid_independent", 400),
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

    def test_lateral_deployments_route_lists_records(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(return_value=["lateral", "deployments"])
        handler._workspace_scope = Mock(return_value="alice")
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(
            lateral_deployment_records=Mock(return_value=[
                {"deployment_id": "srv-target-session-12345678", "workspace_id": "alice",
                 "cleanup": {"status": "pending", "attempts": 1}}]))
        handler._handle_api_get("/api/v1/lateral/deployments", {})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["deployments"][0]["deployment_id"], "srv-target-session-12345678")
        handler.server_instance.lateral_deployment_records.assert_called_once_with(workspace_id="alice")

    def test_lateral_cleanup_retry_route_status_codes(self):
        cases = [
            ("invalid_deployment", 400),
            ("deployment_not_found", 404),
            ("session_not_found", 404),
            ("untracked", 409),
            ("session_busy", 409),
            ("timeout", 408),
            ("session_killed", 410),
            ("server_stopped", 503),
        ]
        record = {"deployment_id": "srv-target-session-12345678", "cleanup": {"status": "failed"}}
        cleanup_result = {"ok": False, "deployment": record,
                          "detail": "the deployment never acknowledged its artifact names"}
        for status, expected_code in cases:
            with self.subTest(status=status):
                handler = object.__new__(server_rmm.RMMHandler)
                handler._api_authorized = Mock(return_value=True)
                handler._api_path_parts = Mock(
                    return_value=["lateral", "deployments", "srv-target-session-12345678", "cleanup"])
                handler._workspace_scope = Mock(return_value=None)
                handler._json = Mock()
                handler.server_instance = SimpleNamespace(
                    retry_lateral_cleanup=Mock(return_value=(cleanup_result, status)))
                handler._handle_api_post(
                    "/api/v1/lateral/deployments/srv-target-session-12345678/cleanup", {})
                code, payload = handler._json.call_args.args
                self.assertEqual(code, expected_code)
                handler.server_instance.retry_lateral_cleanup.assert_called_once_with(
                    "srv-target-session-12345678", "", workspace_id=None)
        # A completed retry returns the record without an error status.
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(
            return_value=["lateral", "deployments", "srv-target-session-12345678", "cleanup"])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(
            retry_lateral_cleanup=Mock(return_value=(
                {"ok": True, "deployment": record,
                 "cleanup": {"status": "ok", "output": "lateral cleanup: service deleted"}}, "ok")))
        handler._handle_api_post(
            "/api/v1/lateral/deployments/srv-target-session-12345678/cleanup", {})
        code, payload = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_lateral_cleanup_retry_route_validates_executor(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._api_path_parts = Mock(
            return_value=["lateral", "deployments", "srv-target-session-12345678", "cleanup"])
        handler._workspace_scope = Mock(return_value=None)
        handler._json = Mock()
        handler.server_instance = SimpleNamespace(retry_lateral_cleanup=Mock())
        handler._handle_api_post(
            "/api/v1/lateral/deployments/srv-target-session-12345678/cleanup",
            {"session_id": ["not", "a", "string"]})
        self.assertEqual(handler._json.call_args.args[0], 400)
        handler.server_instance.retry_lateral_cleanup.assert_not_called()

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
