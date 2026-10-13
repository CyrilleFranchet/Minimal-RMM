"""SQLite-backed operator WebUI state for server and workspace deployments."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any


MAX_UI_STATE_BYTES = 2 * 1024 * 1024

# The API token is deliberately absent. It is needed to authenticate the first
# request that loads this state and must remain a login credential in the UI.
UI_SESSION_KEYS = frozenset(
    {
        "rmm_sidebar_width",
        "rmm_agent_gen_prefs",
        "rmm_go_agent_prefs",
        "rmm_ai_panel_open",
        "rmm_ai_provider_state",
        "rmm_ai_provider",
        "rmm_exegol_mcp_enabled",
        "rmm_exegol_mcp_url",
        "rmm_ai_skills_enabled",
        "rmm_shell_history",
        "rmm_event_cursors",
        "rmm_openai_api_key",
        "rmm_anthropic_api_key",
        "rmm_mistral_api_key",
        "rmm_exegol_mcp_token",
    }
)
UI_LOCAL_KEYS = frozenset({"rmm_theme"})


class UiStateError(ValueError):
    """Raised when persisted operator WebUI state is invalid or too large."""


def normalize_ui_state(payload: Any) -> dict[str, dict[str, str]]:
    """Return the allowlisted JSON storage shape used by the UI state API."""
    if not isinstance(payload, dict):
        raise UiStateError("UI state must be an object")
    storage = payload.get("storage", payload)
    if not isinstance(storage, dict):
        raise UiStateError("UI state storage must be an object")

    session = storage.get("session", {})
    local = storage.get("local", {})
    if not isinstance(session, dict) or not isinstance(local, dict):
        raise UiStateError("UI state session and local values must be objects")

    normalized = {
        "session": {
            key: value
            for key, value in session.items()
            if key in UI_SESSION_KEYS and isinstance(value, str)
        },
        "local": {
            key: value
            for key, value in local.items()
            if key in UI_LOCAL_KEYS and isinstance(value, str)
        },
    }
    if normalized["local"].get("rmm_theme") not in {None, "dark", "light"}:
        normalized["local"].pop("rmm_theme", None)

    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_UI_STATE_BYTES:
        raise UiStateError("UI state exceeds the configured size limit")
    return normalized


class UiStateStore:
    """Persist one or more operator UI state namespaces in SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _init_schema(self) -> None:
        """Create the UI state table when the deployment starts."""
        with self._lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS ui_state (
                    namespace TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._db.commit()

    @staticmethod
    def _namespace(namespace: str) -> str:
        """Validate one state namespace used as a SQLite key."""
        value = str(namespace or "").strip()
        if not value or len(value) > 128:
            raise UiStateError("UI state namespace is invalid")
        return value

    def get(self, namespace: str) -> dict[str, dict[str, str]] | None:
        """Load one normalized namespace, or ``None`` when it is not stored."""
        key = self._namespace(namespace)
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM ui_state WHERE namespace=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UiStateError("stored UI state is invalid") from exc
        return normalize_ui_state(payload)

    def set(self, namespace: str, payload: Any) -> dict[str, Any]:
        """Normalize and atomically persist one UI state namespace."""
        key = self._namespace(namespace)
        normalized = normalize_ui_state(payload)
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT INTO ui_state(namespace,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(namespace) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (key, encoded, updated_at),
            )
            self._db.commit()
        return {"state": normalized, "updated_at": updated_at}

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            self._db.close()
