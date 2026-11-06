"""Persist AI job status while execution remains owned by the server process."""

import json
import builtins
import os
import re
import sqlite3
import threading
import uuid
import urllib.error
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


def _failure_diagnostics(error):
    """Extract bounded exception metadata without messages, locals, or URLs."""
    chain = []
    seen = set()
    pending = [error]
    while pending and len(chain) < 8:
        error = pending.pop(0)
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        name = type(error).__name__
        item = {"type": name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"}
        if isinstance(error, urllib.error.HTTPError):
            code = error.code
            if type(code) is int and 100 <= code <= 599:
                item["http_status"] = code
        # Line numbers locate failures without exposing paths or source text.
        frames = []
        frame = error.__traceback__
        while frame is not None:
            frames.append(frame.tb_lineno)
            frame = frame.tb_next
        item["traceback_lines"] = frames[-12:]
        chain.append(item)
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if isinstance(error, getattr(builtins, "BaseExceptionGroup", ())):
            pending.extend(error.exceptions[:8])
        if cause is not None:
            pending.append(cause)
    return {"exception_chain": chain}


# Safe, provider-agnostic classification of a failed AI call. Codes are
# mapped to a bounded label and a fixed, credential-free message — never the
# provider's own response text, which `_run` deliberately discards (it may
# contain the request, the API key, or account details).
_CLASSIFIED_ERRORS = {
    "ai_provider_authentication_failed": (
        "AI provider rejected the request (HTTP 401/403): the configured API "
        "key is missing, invalid, or revoked. Check the provider key and retry."
    ),
    "ai_provider_rate_limited_or_quota_exceeded": (
        "AI provider returned a rate-limit or quota error (HTTP 429). Wait "
        "before retrying, or check the provider account's usage/billing."
    ),
    "ai_provider_unavailable": (
        "AI provider returned a server error (HTTP 5xx). Retry later."
    ),
}
_GENERIC_FAILURE_MESSAGE = (
    "AI execution failed. Inspect job result diagnostics and session events "
    "before retrying."
)


def _classify_failure(error):
    """Map the first HTTP status found in the exception chain to a safe label.

    Returns one of the `_CLASSIFIED_ERRORS` keys, or None when no HTTP status
    is found (a non-HTTP failure, or one whose cause/context chain was lost).
    """
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 8:
        seen.add(id(error))
        if isinstance(error, urllib.error.HTTPError):
            code = error.code
            if code in (401, 403):
                return "ai_provider_authentication_failed"
            if code == 429:
                return "ai_provider_rate_limited_or_quota_exceeded"
            if type(code) is int and 500 <= code <= 599:
                return "ai_provider_unavailable"
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return None


def _safe_progress(event):
    """Keep operational counters and labels without tool arguments or results."""
    if not isinstance(event, dict):
        return {}
    clean = {}
    for key in ("phase", "tool", "status", "tool_path", "mcp_status", "exegol_status", "backend", "call_id", "artifact_id", "arguments_artifact_id"):
        value = event.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
            clean[key] = value
    for key in ("round", "tool_count", "step", "context_compactions", "duration_ms", "output_bytes"):
        value = event.get(key)
        ceiling = 2**53 - 1 if key in ("duration_ms", "output_bytes") else 1000000
        if type(value) is int and 0 <= value <= ceiling:
            clean[key] = value
    for key in ('skills', 'active_skills'):
        if isinstance(event.get(key), list):
            clean[key] = [s for s in event[key][:100]
                          if isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", s)]
    if isinstance(event.get('step_summary'), str):
        clean['step_summary'] = ''.join(c for c in event['step_summary'][:500] if c.isprintable())
    if isinstance(event.get('step_checkpoint'), str):
        clean['step_checkpoint'] = ''.join(c for c in event['step_checkpoint'][:2000] if c.isprintable())
    return clean


def _safe_activity(event):
    """Keep bounded operational metadata without command text or tool output."""
    clean = _safe_progress(event)
    # Human-authored model summaries are not a credential filtering boundary.
    for key in ("step_summary", "step_checkpoint"):
        clean.pop(key, None)
    keys = event.get("arguments_keys")
    if isinstance(keys, list):
        clean["arguments_keys"] = [key for key in keys[:50] if isinstance(key, str)
                                  and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", key)]
    diagnostics = event.get("diagnostics")
    if isinstance(diagnostics, dict):
        entries = []
        for item in diagnostics.get("exception_chain", [])[:8]:
            if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", str(item.get("type", ""))):
                continue
            entry = {"type": item["type"]}
            if type(item.get("http_status")) is int and 100 <= item["http_status"] <= 599:
                entry["http_status"] = item["http_status"]
            lines = item.get("traceback_lines", [])
            if isinstance(lines, list):
                entry["traceback_lines"] = [line for line in lines[-12:] if type(line) is int and 0 < line < 10000000]
            entries.append(entry)
        clean["diagnostics"] = {"exception_chain": entries}
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
        self._inflight_scopes = {}
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
            if "activity" not in columns:
                self._db.execute("ALTER TABLE ai_jobs ADD COLUMN activity TEXT NOT NULL DEFAULT '[]'")
            if "activity_total" not in columns:
                self._db.execute("ALTER TABLE ai_jobs ADD COLUMN activity_total INTEGER NOT NULL DEFAULT 0")
            if "evidence_scope" not in columns:
                self._db.execute("ALTER TABLE ai_jobs ADD COLUMN evidence_scope TEXT")
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
        job.pop("evidence_scope", None)
        job["status"] = job.pop("state")
        job["dismissed"] = bool(job["dismissed"])
        job["progress"] = json.loads(job["progress"])
        job["result"] = json.loads(job["result"]) if job["result"] else None
        job["activity"] = json.loads(job["activity"])
        job["activity_omitted"] = max(0, job.pop("activity_total") - len(job["activity"]))
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

    def read_evidence(self, job_id, owner, artifact_id, offset=0, limit=4096):
        """Read one protected evidence page referenced by this owner's job."""
        if (not isinstance(artifact_id, str) or not re.fullmatch(r"context-[a-f0-9]{32}", artifact_id)
                or type(offset) is not int or not 0 <= offset <= 2**31 - 1
                or type(limit) is not int or not 1 <= limit <= 4096):
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM ai_jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
            if row is None or not row["evidence_scope"]:
                return None
            job = self._public(row)
            references = {event.get(key) for event in job["activity"]
                          for key in ("artifact_id", "arguments_artifact_id")}
            manifest = (job.get("result") or {}).get("context_artifacts", {})
            if isinstance(manifest, dict):
                references.update(manifest)
            if artifact_id not in references:
                return None
            from rmm_ai_context import ContextWindow
            window = ContextWindow(scope=row["evidence_scope"])
            page = window.read_page(artifact_id, offset, limit)
            if page is None:
                return None
            return {"artifact_id": artifact_id, **page,
                    "offset": offset, "next_offset": offset + limit if offset + limit < page["characters"] else None}

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

    def cancel(self, job_id, owner):
        """Stop an owner's job at its next checkpoint, retaining terminal history."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE ai_jobs SET state='interrupted',updated_at=?,completed_at=?,error=? "
                "WHERE id=? AND owner=? AND state IN ('queued','running')",
                (_now(), _now(), "Stopped by operator; already dispatched actions may continue.", job_id, owner),
            )
            return self.get(job_id, owner)

    def interrupt_session(self, session_id):
        """Interrupt all active jobs bound to a deleted session, retaining audit state."""
        if not session_id:
            return
        with self._lock, self._db:
            self._db.execute(
                "UPDATE ai_jobs SET state='interrupted',updated_at=?,completed_at=?,error=? "
                "WHERE session_id=? AND state IN ('queued','running')",
                (_now(), _now(), "Session killed; execution was not replayed.", session_id),
            )

    def _check_active(self, job_id):
        """Reject checkpoints for shutdown or interrupted jobs while holding the lock."""
        if self._closed:
            raise JobInterrupted("Server is stopping")
        row = self._db.execute("SELECT state FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["state"] not in ("queued", "running"):
            raise JobInterrupted("Job was interrupted")

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
            if (owner, session_id) in self._inflight_scopes:
                raise JobConflictError("Session still has an executing or stopping AI job")
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
            scope = (owner, session_id)
            self._inflight_scopes[scope] = job_id
            worker = threading.Thread(target=self._run, args=(job_id, run, scope), daemon=True, name=f"ai-job-{job_id[:8]}")
            self._threads.add(worker)
            try:
                worker.start()
            except RuntimeError:
                self._threads.discard(worker)
                self._active -= 1
                self._inflight_scopes.pop(scope, None)
                self._finish(job_id, "failed", error="Could not start job worker.")
                raise
            return self.get(job_id, owner), True

    def _progress(self, job_id, event):
        """Persist observations, including a canceled call's late response, then stop."""
        with self._lock:
            row = self._db.execute("SELECT state,activity FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
            activity = json.loads(row["activity"]) if row is not None else []
            call_id = event.get("call_id")
            prior = [item for item in activity if call_id and item.get("call_id") == call_id]
            late_observation = (row is not None and row["state"] == "interrupted"
                                and event.get("phase") == "tool"
                                and event.get("status") in ("completed", "outcome_unknown")
                                and prior and prior[-1].get("status") == "running"
                                and prior[-1].get("tool") == event.get("tool"))
            if not late_observation:
                self._check_active(job_id)
            timestamp = _now()
            scope = event.get("context_scope")
            if isinstance(scope, str) and re.fullmatch(r"[a-f0-9]{64}", scope):
                with self._db:
                    self._db.execute("UPDATE ai_jobs SET evidence_scope=? WHERE id=?", (scope, job_id))
            activity.append({**_safe_activity(event), "timestamp": timestamp})
            with self._db:
                self._db.execute(
                    "UPDATE ai_jobs SET progress=?, activity=?, activity_total=activity_total+1, updated_at=? WHERE id=? AND state=?",
                    (json.dumps(_safe_progress(event)), json.dumps(activity[-500:]), timestamp, job_id, row["state"]),
                )
            if late_observation:
                raise JobInterrupted("Job was interrupted; the dispatched call's final observation was retained.")

    def _finish(self, job_id, state, result=None, error=None):
        """Record terminal state without overwriting an interrupted job."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE ai_jobs SET state=?,result=?,error=?,updated_at=?,completed_at=? "
                "WHERE id=? AND state IN ('queued','running')",
                (state, json.dumps(result) if result is not None else None, error, _now(), _now(), job_id),
            )

    def _run(self, job_id, run, scope):
        """Execute an admitted job while respecting bounded concurrency."""
        acquired = False
        try:
            while not acquired:
                with self._lock:
                    self._check_active(job_id)
                acquired = self._slots.acquire(timeout=0.1)
            with self._lock:
                self._check_active(job_id)
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
        except Exception as exc:
            # Provider/tool exceptions may contain credentials or raw requests;
            # only a bounded, fixed-message classification of the HTTP status
            # (not the provider's own text) is ever exposed.
            classification = _classify_failure(exc)
            self._finish(
                job_id, "failed",
                result={"ok": False, "diagnostics": _failure_diagnostics(exc)},
                error=_CLASSIFIED_ERRORS.get(classification, _GENERIC_FAILURE_MESSAGE),
            )
        finally:
            if acquired:
                self._slots.release()
            with self._lock:
                self._active -= 1
                self._inflight_scopes.pop(scope, None)
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
