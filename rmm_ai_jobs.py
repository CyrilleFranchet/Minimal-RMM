"""Persist AI job status while execution remains owned by the server process."""

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


class JobCapacityError(RuntimeError):
    """The server's bounded job admission limit has been reached."""


class JobClosedError(RuntimeError):
    """The server no longer accepts AI jobs."""


class JobConflictError(RuntimeError):
    """A request conflicts with an existing job or active session."""


class JobInterrupted(RuntimeError):
    """Execution must stop at its next cooperative checkpoint."""


def _now():
    """Return an ISO 8601 UTC timestamp for persisted job transitions."""
    return datetime.now(timezone.utc).isoformat()


def _safe_progress(event):
    """Keep operational counters and labels without tool arguments or results."""
    if not isinstance(event, dict):
        return {}
    clean = {}
    for key in ("phase", "tool", "status"):
        value = event.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
            clean[key] = value
    for key in ("round", "tool_count"):
        value = event.get(key)
        if type(value) is int and 0 <= value <= 1000000:
            clean[key] = value
    return clean


class AIJobManager:
    """Bound background execution and retain scoped status/results in SQLite.

    Only one instance should own a database. Restarted jobs are interrupted,
    never replayed: an interrupted tool may already have produced side effects.
    Request credentials and runner closures are kept exclusively in memory.
    """

    def __init__(self, path, max_workers=4, max_pending=16):
        if max_workers < 1 or max_pending < 0:
            raise ValueError("invalid job capacity")
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(db_path, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._slots = threading.BoundedSemaphore(max_workers)
        self._capacity = max_workers + max_pending
        self._active = 0
        self._closed = False
        self._threads = set()
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS ai_jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT,
                request_id TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                started_at TEXT, completed_at TEXT,
                progress TEXT NOT NULL DEFAULT '{}', result TEXT,
                error TEXT, dismissed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner, request_id))""")
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(ai_jobs)")}
            if "dismissed" not in columns:
                self._db.execute("ALTER TABLE ai_jobs ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            self._db.execute(
                "UPDATE ai_jobs SET state='interrupted', updated_at=?, completed_at=?, error=? "
                "WHERE state IN ('queued','running')",
                (_now(), _now(), "Server restarted; execution was not replayed."),
            )

    def _public(self, row):
        """Return job metadata without its internal owner scope."""
        if row is None:
            return None
        job = dict(row)
        job.pop("owner", None)
        job["status"] = job.pop("state")
        job["dismissed"] = bool(job["dismissed"])
        job["progress"] = json.loads(job["progress"])
        job["result"] = json.loads(job["result"]) if job["result"] else None
        return job

    def get(self, job_id, owner):
        """Read a job only within the authenticated owner's scope."""
        with self._lock:
            return self._public(self._db.execute(
                "SELECT * FROM ai_jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone())

    def list(self, owner, session_id=None, limit=50):
        """List recent jobs for an owner, optionally restricted to one session."""
        limit = max(1, min(int(limit), 100))
        with self._lock:
            if session_id is None:
                rows = self._db.execute(
                    "SELECT * FROM ai_jobs WHERE owner=? ORDER BY created_at DESC LIMIT ?",
                    (owner, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM ai_jobs WHERE owner=? AND session_id IS ? ORDER BY created_at DESC LIMIT ?",
                    (owner, session_id or None, limit),
                ).fetchall()
            return [self._public(row) for row in rows]

    def dismiss(self, owner, session_id):
        """Hide completed session jobs after chat reset while retaining history."""
        session_id = session_id or None
        with self._lock:
            if self._db.execute(
                "SELECT 1 FROM ai_jobs WHERE owner=? AND session_id IS ? AND state IN ('queued','running')",
                (owner, session_id),
            ).fetchone():
                raise JobConflictError("Session already has an active AI job")
            with self._db:
                self._db.execute(
                    "UPDATE ai_jobs SET dismissed=1 WHERE owner=? AND session_id IS ?",
                    (owner, session_id),
                )

    def submit(self, *, owner, session_id, request_id, run):
        """Durably admit a job before starting its in-memory runner.

        Repeating an owner's request ID returns its existing job, including a
        completed one. Reusing that ID for a different session is rejected.
        """
        if not isinstance(owner, str) or not owner or not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("owner and request_id are required")
        session_id = session_id or None
        with self._lock:
            prior = self._db.execute(
                "SELECT * FROM ai_jobs WHERE owner=? AND request_id=?", (owner, request_id)
            ).fetchone()
            if prior is not None:
                if prior["session_id"] != session_id:
                    raise JobConflictError("Request ID belongs to a different session")
                return self._public(prior), False
            if self._closed:
                raise JobClosedError("Server is stopping")
            if self._db.execute(
                "SELECT 1 FROM ai_jobs WHERE owner=? AND session_id IS ? AND state IN ('queued','running')",
                (owner, session_id),
            ).fetchone():
                raise JobConflictError("Session already has an active AI job")
            if self._active >= self._capacity:
                raise JobCapacityError("AI job capacity reached")
            job_id = uuid.uuid4().hex
            timestamp = _now()
            with self._db:
                self._db.execute(
                    "INSERT INTO ai_jobs (id,owner,session_id,request_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (job_id, owner, session_id, request_id, "queued", timestamp, timestamp),
                )
            self._active += 1
            worker = threading.Thread(target=self._run, args=(job_id, run), daemon=True, name=f"ai-job-{job_id[:8]}")
            self._threads.add(worker)
            try:
                worker.start()
            except RuntimeError:
                self._threads.discard(worker)
                self._active -= 1
                self._finish(job_id, "failed", error="Could not start job worker.")
                raise
            return self.get(job_id, owner), True

    def _progress(self, job_id, event):
        """Persist safe progress and stop a worker after server shutdown."""
        with self._lock:
            if self._closed:
                raise JobInterrupted("Server is stopping")
            with self._db:
                self._db.execute(
                    "UPDATE ai_jobs SET progress=?, updated_at=? WHERE id=? AND state='running'",
                    (json.dumps(_safe_progress(event)), _now(), job_id),
                )

    def _finish(self, job_id, state, result=None, error=None):
        """Record terminal state without overwriting an interrupted job."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE ai_jobs SET state=?,result=?,error=?,updated_at=?,completed_at=? "
                "WHERE id=? AND state IN ('queued','running')",
                (state, json.dumps(result) if result is not None else None, error, _now(), _now(), job_id),
            )

    def _run(self, job_id, run):
        """Execute an admitted job while respecting bounded concurrency."""
        acquired = False
        try:
            while not acquired:
                with self._lock:
                    if self._closed:
                        raise JobInterrupted("Server is stopping")
                acquired = self._slots.acquire(timeout=0.1)
            with self._lock:
                if self._closed:
                    raise JobInterrupted("Server is stopping")
                with self._db:
                    self._db.execute(
                        "UPDATE ai_jobs SET state='running',started_at=?,updated_at=? WHERE id=?",
                        (_now(), _now(), job_id),
                    )
            result = run(lambda event: self._progress(job_id, event))
            if not isinstance(result, dict):
                raise ValueError("AI runner returned invalid result")
            self._finish(job_id, "succeeded" if result.get("ok") else "failed", result=result)
        except JobInterrupted:
            self._finish(job_id, "interrupted", error="Server stopped; execution was not replayed.")
        except Exception:
            # Provider/tool exceptions may contain credentials or raw requests.
            self._finish(job_id, "failed", error="AI execution failed. Inspect server diagnostics and session events before retrying.")
        finally:
            if acquired:
                self._slots.release()
            with self._lock:
                self._active -= 1
                self._threads.discard(threading.current_thread())

    def close(self):
        """Stop admission and mark unfinished jobs interrupted without replay."""
        with self._lock:
            self._closed = True
            with self._db:
                self._db.execute(
                    "UPDATE ai_jobs SET state='interrupted', updated_at=?,completed_at=?,error=? "
                    "WHERE state IN ('queued','running')",
                    (_now(), _now(), "Server stopped; execution was not replayed."),
                )
