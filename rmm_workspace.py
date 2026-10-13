"""Workspace authentication and transient event delivery for Minimal-RMM.

The central server uses this module when ``RMM_STORAGE_MODE=workspace``. A
workspace is an operator-owned Exegol container or sidecar. The central
server keeps only bounded, transient event queues for each workspace; the
workspace service persists transcripts and artifacts locally.
"""

from __future__ import annotations

from collections import deque
import copy
import hmac
import json
import os
import re
import threading
from typing import Any


WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_WORKSPACE_EVENT_LIMIT = 10_000


def valid_workspace_id(value: str) -> bool:
    """Return whether a workspace identifier is safe for headers and storage keys."""
    return bool(WORKSPACE_ID_RE.fullmatch(str(value or "").strip()))


def _workspace_event_limit() -> int:
    """Read the bounded central event queue size from the environment."""
    raw = os.environ.get("RMM_WORKSPACE_EVENT_LIMIT", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_WORKSPACE_EVENT_LIMIT
    except ValueError:
        value = DEFAULT_WORKSPACE_EVENT_LIMIT
    return max(100, min(100_000, value))


class WorkspaceRegistry:
    """Authenticate workspace IDs and their operator-scoped bearer tokens.

    The registry is configured from ``RMM_WORKSPACES``. It accepts either a
    JSON object mapping IDs to tokens or a JSON list of ``{"id", "token"}``
    records. Tokens stay in process memory and are never returned by an API.
    """

    def __init__(self, workspaces: dict[str, str] | None = None):
        self._tokens = {
            workspace_id: token
            for workspace_id, token in (workspaces or {}).items()
            if valid_workspace_id(workspace_id) and str(token or "").strip()
        }

    @classmethod
    def from_environment(cls) -> "WorkspaceRegistry":
        """Build a registry from ``RMM_WORKSPACES`` or singular workspace variables."""
        raw = os.environ.get("RMM_WORKSPACES", "").strip()
        values: dict[str, str] = {}
        if raw:
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                values = {
                    str(key).strip(): str(value).strip()
                    for key, value in loaded.items()
                }
            elif isinstance(loaded, list):
                for item in loaded:
                    if not isinstance(item, dict):
                        continue
                    workspace_id = str(item.get("id") or item.get("workspace_id") or "").strip()
                    token = str(item.get("token") or "").strip()
                    if workspace_id and token:
                        values[workspace_id] = token

        workspace_id = os.environ.get("RMM_WORKSPACE_ID", "").strip()
        token = os.environ.get("RMM_WORKSPACE_TOKEN", "").strip()
        if workspace_id and token:
            values.setdefault(workspace_id, token)
        return cls(values)

    def authenticate(self, workspace_id: str, token: str) -> bool:
        """Return whether a workspace token matches the configured workspace."""
        workspace_id = str(workspace_id or "").strip()
        expected = self._tokens.get(workspace_id)
        if not expected:
            return False
        return hmac.compare_digest(str(token or "").encode(), expected.encode())

    def contains(self, workspace_id: str) -> bool:
        """Return whether a workspace ID is configured."""
        return str(workspace_id or "").strip() in self._tokens

    def ids(self) -> list[str]:
        """Return configured workspace IDs without exposing their tokens."""
        return sorted(self._tokens)


class WorkspaceEventBroker:
    """Bounded, thread-safe event queues keyed by workspace ID.

    Cursors are process-local and monotonically increasing. A workspace must
    persist its cursor locally and poll often enough to stay within the queue
    limit. If an old cursor has fallen out of the queue, ``read`` reports
    ``reset_required`` so the workspace can alert the operator instead of
    silently presenting an incomplete transcript.
    """

    def __init__(self, max_events: int | None = None):
        self.max_events = max_events or _workspace_event_limit()
        self._lock = threading.Lock()
        self._cursor = 0
        self._queues: dict[str, deque[dict[str, Any]]] = {}

    def publish(self, workspace_id: str, session_id: str, event: dict[str, Any]) -> int | None:
        """Append one event and return its workspace cursor, or ``None`` if unassigned."""
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id or not isinstance(event, dict):
            return None
        with self._lock:
            self._cursor += 1
            envelope = {
                "cursor": self._cursor,
                "session_id": str(session_id),
                "event": copy.deepcopy(event),
            }
            queue = self._queues.setdefault(workspace_id, deque(maxlen=self.max_events))
            queue.append(envelope)
            return self._cursor

    def read(self, workspace_id: str, since: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read events after a cursor and report cursor loss explicitly."""
        workspace_id = str(workspace_id or "").strip()
        try:
            since = max(0, int(since))
        except (TypeError, ValueError):
            since = 0
        try:
            limit = max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            limit = 100
        with self._lock:
            queue = self._queues.get(workspace_id, deque())
            rows = list(queue)
            oldest = rows[0]["cursor"] if rows else since + 1
            reset_required = bool(rows and since and since < oldest - 1)
            selected = [copy.deepcopy(row) for row in rows if row["cursor"] > since][:limit]
            next_cursor = selected[-1]["cursor"] if selected else since
            return {
                "workspace_id": workspace_id,
                "events": selected,
                "next_cursor": next_cursor,
                "oldest_cursor": oldest,
                "reset_required": reset_required,
            }

    def clear(self, workspace_id: str) -> None:
        """Drop queued events for a workspace during an explicit workspace reset."""
        with self._lock:
            self._queues.pop(str(workspace_id or "").strip(), None)
