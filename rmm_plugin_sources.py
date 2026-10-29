"""Persistent repository/branch selections owned by a server or user workspace."""
from __future__ import annotations

from rmm_settings import getenv as settings_getenv

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from rmm_asset_store import AssetError

_LOCK = threading.RLock()
SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CREDENTIAL = re.compile(r"(?:RMM_PLUGIN_GITHUB_TOKEN|RMM_PLUGIN_TOKEN_[A-Z0-9_]+)")
FIELDS = {"label", "repository", "branch", "credential_env"}
MAX_SOURCES = 100


class PluginSourceError(AssetError):
    """Report source configuration errors without disclosing credential values."""


def normalize_source(source_id: str, config: dict) -> dict:
    """Validate a named GitHub repository, branch, and restricted credential reference."""
    if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
        raise PluginSourceError("source ID must contain 1–64 letters, digits, underscores, or hyphens")
    if not isinstance(config, dict) or set(config) - FIELDS:
        raise PluginSourceError("expected label, repository, branch, and credential_env")
    values = {"label": source_id, "branch": "main", "credential_env": "RMM_PLUGIN_GITHUB_TOKEN", **config}
    if not all(isinstance(value, str) for value in values.values()):
        raise PluginSourceError("source configuration fields must be strings")
    values = {key: value.strip() for key, value in values.items()}
    repository = values.get("repository", "")
    if (len(repository) > 201 or not REPOSITORY.fullmatch(repository)
            or any(part in {".", ".."} for part in repository.split("/"))):
        raise PluginSourceError("repository must be owner/repository")
    branch = values["branch"]
    if (not branch or len(branch) > 200 or branch == "@" or branch.startswith(("/", "-"))
            or branch.endswith(("/", ".")) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in branch)
            or any(value in branch for value in ("..", "@{", "//", "~", "^", ":", "?", "*", "[", "\\"))
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))):
        raise PluginSourceError("invalid Git branch name")
    if not CREDENTIAL.fullmatch(values["credential_env"]):
        raise PluginSourceError("credential_env must be RMM_PLUGIN_GITHUB_TOKEN or RMM_PLUGIN_TOKEN_<NAME>")
    if not values["label"] or len(values["label"]) > 100 or any(ord(char) < 32 for char in values["label"]):
        raise PluginSourceError("source label must contain 1–100 printable characters")
    return {"id": source_id, **values}


def public_source(source: dict) -> dict:
    """Add configuration revision and credential presence without exposing the secret."""
    revision = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**source, "revision": revision,
            "credential_configured": bool(settings_getenv(source["credential_env"], "").strip())}


class PluginSourceStore:
    """Persist source settings with owner-only access and atomic thread-safe updates."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / "plugin-sources.json"
        with _LOCK:
            if not self.path.exists():
                source = normalize_source("default", {
                    "label": "Default", "repository": settings_getenv("RMM_PLUGIN_REPOSITORY", "CyrilleFranchet/Minimal-RMM-plugins"),
                    "branch": "main", "credential_env": "RMM_PLUGIN_GITHUB_TOKEN",
                })
                self._write([source])
            self.path.chmod(0o600)

    def _read(self) -> list[dict]:
        """Read and validate saved settings; corruption never resets user configuration."""
        if self.path.stat().st_size > 256 * 1024:
            raise PluginSourceError("plugin source configuration is too large")
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise PluginSourceError("plugin source configuration is invalid") from None
        if not isinstance(rows, list) or len(rows) > MAX_SOURCES:
            raise PluginSourceError("plugin source configuration is invalid")
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or "id" not in row:
                raise PluginSourceError("plugin source configuration is invalid")
            source = normalize_source(row["id"], {key: value for key, value in row.items() if key != "id"})
            if source["id"] in seen:
                raise PluginSourceError("duplicate plugin source ID")
            seen.add(source["id"])
            result.append(source)
        return result

    def _write(self, rows: list[dict]) -> None:
        """Replace the source list atomically with owner-only permissions."""
        fd, temporary = tempfile.mkstemp(prefix="plugin-sources.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def list(self) -> list[dict]:
        """List saved repository/branch pairs and their credential availability."""
        with _LOCK:
            return [public_source(source) for source in self._read()]

    def get(self, source_id: str = "default") -> dict:
        """Resolve one saved source, refusing unknown selections."""
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            raise PluginSourceError("invalid plugin source ID")
        source = next((row for row in self.list() if row["id"] == source_id), None)
        if source is None:
            raise PluginSourceError("plugin source not found")
        return source

    def save(self, source_id: str, config: dict) -> dict:
        """Create or replace one source; its revision changes when settings change."""
        source = normalize_source(source_id, config)
        with _LOCK:
            rows = self._read()
            rows = [row for row in rows if row["id"] != source_id]
            if len(rows) >= MAX_SOURCES:
                raise PluginSourceError("plugin source limit reached")
            rows.append(source)
            self._write(rows)
        return public_source(source)

    def remove(self, source_id: str) -> bool:
        """Delete one configuration without deleting installed plugin files."""
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            raise PluginSourceError("invalid plugin source ID")
        with _LOCK:
            rows = self._read()
            retained = [row for row in rows if row["id"] != source_id]
            if len(retained) == len(rows):
                return False
            self._write(retained)
            return True


def plugin_sources_api(root, method: str, source_id: str | None = None, body=None):
    """Share persistent source CRUD between authenticated server/workspace routes."""
    try:
        store = PluginSourceStore(root)
        if method == "GET" and source_id is None:
            return 200, {"sources": store.list()}
        if method == "POST" and source_id is not None:
            return 200, {"source": store.save(source_id, body)}
        if method == "DELETE" and source_id is not None:
            if store.remove(source_id):
                return 200, {"ok": True, "id": source_id}
            return 404, {"error": "plugin source not found"}
        return 404, {"error": "not_found"}
    except PluginSourceError as exc:
        return 400, {"error": str(exc)}
    except OSError:
        return 500, {"error": "plugin source storage failed"}
