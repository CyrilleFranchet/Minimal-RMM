"""Exercise scoped, durable, bounded AI job execution without provider calls."""

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from rmm_ai_jobs import AIJobManager, JobCapacityError, JobClosedError, JobConflictError, JobInterrupted


class AIJobManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "jobs.sqlite3"
        self.manager = AIJobManager(self.path, max_workers=1, max_pending=1)
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.manager.close()
        self.directory.cleanup()

    def wait_for(self, job_id, states, owner="operator"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.manager.get(job_id, owner)
            if job["status"] in states:
                return job
            time.sleep(0.005)
        self.fail(f"Job did not reach {states}")

    def blocking_runner(self, progress):
        progress({"phase": "provider", "round": 1})
        self.release.wait(3)
        return {"ok": True, "message": "done"}

    def submit(self, request_id="request", session_id="session", run=None, owner="operator"):
        return self.manager.submit(owner=owner, session_id=session_id, request_id=request_id, run=run or self.blocking_runner)

    def test_worker_continues_without_caller_and_persists_result(self):
        job, created = self.submit()
        self.assertTrue(created)
        self.wait_for(job["id"], {"running"})
        self.release.set()
        final = self.wait_for(job["id"], {"succeeded"})
        self.assertEqual(final["result"]["message"], "done")
        self.assertIsNotNone(final["started_at"])
        self.assertIsNotNone(final["completed_at"])
        self.manager.close()
        reopened = AIJobManager(self.path)
        try:
            self.assertEqual(reopened.get(job["id"], "operator")["result"], final["result"])
        finally:
            reopened.close()

    def test_idempotent_replay_returns_original_job_without_reexecution(self):
        calls = []

        def run(progress):
            calls.append(1)
            return {"ok": True, "message": "done"}

        job, _ = self.submit(run=run)
        self.wait_for(job["id"], {"succeeded"})
        repeated, created = self.submit(run=run)
        self.assertFalse(created)
        self.assertEqual(job["id"], repeated["id"])
        self.assertEqual(calls, [1])
        with self.assertRaises(JobConflictError):
            self.submit(session_id="different", run=run)

    def test_owner_isolation_and_active_session_conflict(self):
        job, _ = self.submit()
        self.assertIsNone(self.manager.get(job["id"], "other"))
        self.assertEqual(self.manager.list("other"), [])
        with self.assertRaises(JobConflictError):
            self.submit(request_id="second")
        other, created = self.submit(owner="other")
        self.assertTrue(created)
        self.assertNotEqual(other["id"], job["id"])
        self.assertEqual(len(self.manager.list("operator", "session")), 1)
        self.assertEqual(self.manager.list("operator", "different"), [])

    def test_capacity_and_concurrency_are_bounded(self):
        first, _ = self.submit()
        self.wait_for(first["id"], {"running"})
        second, _ = self.submit(request_id="second", session_id="second")
        self.assertEqual(second["status"], "queued")
        with self.assertRaises(JobCapacityError):
            self.submit(request_id="third", session_id="third")
        self.release.set()
        self.wait_for(second["id"], {"succeeded"})

    def test_progress_does_not_persist_request_secrets(self):
        secret = "credential-must-stay-in-memory"

        def run(progress):
            progress({"phase": "tool", "tool": "get_session", "round": 2, "arguments": {"token": secret}, "result": secret})
            self.release.wait(3)
            return {"ok": True, "message": "done"}

        job, _ = self.submit(run=run)
        self.wait_for(job["id"], {"running"})
        deadline = time.monotonic() + 3
        while self.manager.get(job["id"], "operator")["progress"].get("round") != 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        progress = self.manager.get(job["id"], "operator")["progress"]
        self.assertEqual(progress, {"phase": "tool", "tool": "get_session", "round": 2})
        self.assertNotIn(secret.encode(), self.path.read_bytes())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_shutdown_interrupts_without_overwriting_state_or_replaying(self):
        checkpoint_rejected = threading.Event()

        def run(progress):
            self.release.wait(3)
            try:
                progress({"phase": "tool"})
            except JobInterrupted:
                checkpoint_rejected.set()
                raise
            return {"ok": True}

        job, _ = self.submit(run=run)
        self.wait_for(job["id"], {"running"})
        queued, _ = self.submit(request_id="second", session_id="second")
        self.manager.close()
        self.release.set()
        self.assertTrue(checkpoint_rejected.wait(2))
        self.assertEqual(self.manager.get(job["id"], "operator")["status"], "interrupted")
        self.assertEqual(self.manager.get(queued["id"], "operator")["status"], "interrupted")
        with self.assertRaises(JobClosedError):
            self.submit(request_id="third", session_id="third")

    def test_session_kill_interrupts_running_and_queued_jobs(self):
        rejected = threading.Event()
        called = threading.Event()

        def run(progress):
            self.release.wait(3)
            try:
                progress({"phase": "tool"})
            except JobInterrupted:
                rejected.set()
                raise
            called.set()
            return {"ok": True}

        first, _ = self.submit(run=run)
        self.wait_for(first["id"], {"running"})
        second, _ = self.submit(owner="other", run=lambda p: called.set())
        self.manager.interrupt_session("session")
        self.assertEqual(self.manager.get(first["id"], "operator")["status"], "interrupted")
        self.assertEqual(self.manager.get(second["id"], "other")["status"], "interrupted")
        self.release.set()
        self.assertTrue(rejected.wait(2))
        for worker in list(self.manager._threads):
            worker.join(2)
        self.assertFalse(called.is_set())
        self.assertIsNone(self.manager.get(first["id"], "operator")["result"])

    def test_session_kill_does_not_interrupt_unrelated_job(self):
        job, _ = self.submit()
        self.manager.interrupt_session("other-session")
        self.release.set()
        self.wait_for(job["id"], {"succeeded"})

    def test_restart_marks_unfinished_jobs_interrupted(self):
        timestamp = "2026-10-30T00:00:00+00:00"
        with sqlite3.connect(self.path) as connection:
            for state in ("queued", "running"):
                connection.execute(
                    "INSERT INTO ai_jobs (id,owner,session_id,request_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (state, "operator", state, state, state, timestamp, timestamp),
                )
        self.manager.close()
        # Simulate abrupt process loss rather than cooperative shutdown.
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE ai_jobs SET state=id,completed_at=NULL,error=NULL")
        self.manager = AIJobManager(self.path)
        for job in self.manager.list("operator"):
            self.assertEqual(job["status"], "interrupted")
            self.assertIn("restarted", job["error"])
            self.assertIsNone(job["result"])

    def test_failure_does_not_expose_exception_credentials(self):
        def run(progress):
            raise RuntimeError("Bearer private-token")

        job, _ = self.submit(run=run)
        final = self.wait_for(job["id"], {"failed"})
        self.assertNotIn("private-token", json.dumps(final))
        self.assertNotIn(b"private-token", self.path.read_bytes())

    def test_dismiss_preserves_history_and_owner_scope(self):
        job, _ = self.submit()
        with self.assertRaises(JobConflictError):
            self.manager.dismiss("operator", "session")
        self.release.set()
        self.wait_for(job["id"], {"succeeded"})
        self.manager.dismiss("other", "session")
        self.assertFalse(self.manager.get(job["id"], "operator")["dismissed"])
        self.manager.dismiss("operator", "session")
        retained = self.manager.get(job["id"], "operator")
        self.assertTrue(retained["dismissed"])
        self.assertEqual(retained["result"]["message"], "done")

    def test_no_session_jobs_are_scoped_and_serialized(self):
        job, _ = self.submit(session_id="")
        self.assertEqual([row["id"] for row in self.manager.list("operator", "")], [job["id"]])
        self.assertEqual(self.manager.list("operator", "session"), [])
        with self.assertRaises(JobConflictError):
            self.submit(request_id="second", session_id=None)
        self.release.set()
        self.wait_for(job["id"], {"succeeded"})
        self.manager.dismiss("operator", "")
        self.assertTrue(self.manager.get(job["id"], "operator")["dismissed"])


if __name__ == "__main__":
    unittest.main()
