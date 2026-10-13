"""Persistent operator asset storage for profiles, plugins, skills, and rclone files.

The store is deliberately filesystem based.  In workspace mode its root lives
inside the operator's Exegol container.  The control plane may keep a runtime
copy of an uploaded agent plugin, but the workspace remains the source of
truth and can republish that copy after a restart.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


ASSET_KINDS = {"plugins", "skills", "rclone"}
ASSET_KIND_ALIASES = {
    "plugin": "plugins",
    "plugins": "plugins",
    "skill": "skills",
    "skills": "skills",
    "rclone": "rclone",
}
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")
PLUGIN_SUFFIXES = {".dll", ".ps1"}
SKILL_SUFFIXES = {".md"}
RCLONE_SUFFIXES = {".json"}
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class AssetError(ValueError):
    """Raised when an operator asset cannot be downloaded or validated."""


def normalize_asset_kind(kind: str) -> str:
    """Return the canonical asset kind or raise ``AssetError``."""
    canonical = ASSET_KIND_ALIASES.get(str(kind or "").strip().lower())
    if canonical not in ASSET_KINDS:
        raise AssetError("asset kind must be plugins, skills, or rclone")
    return canonical


def _max_bytes() -> int:
    """Return the bounded download size for remote assets."""
    raw = os.environ.get("RMM_ASSET_MAX_BYTES", str(DEFAULT_MAX_BYTES)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return max(1024, min(value, 512 * 1024 * 1024))


def _safe_name(value: str, *, default: str = "") -> str:
    """Normalize one asset basename and reject path traversal."""
    name = os.path.basename(str(value or "").replace("\\", "/")).strip().strip(".")
    if not name or name in {".", ".."} or ".." in name:
        return default
    cleaned = "".join(char for char in name if char.isalnum() or char in "._- ")
    cleaned = cleaned.strip()[:200]
    return cleaned if ASSET_NAME_RE.fullmatch(cleaned) else default


def _display_url(url: str) -> str:
    """Remove URL credentials and query fragments before storing display metadata."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return ""


def _filename_from_url(url: str) -> str:
    """Derive a safe basename from a remote URL path."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return ""
    return _safe_name(urllib.parse.unquote(Path(path).name))


def validate_asset_name(kind: str, name: str) -> str:
    """Validate an asset name and its kind-specific extension."""
    kind = normalize_asset_kind(kind)
    safe = _safe_name(name)
    if not safe:
        raise AssetError("invalid asset name")
    suffix = Path(safe).suffix.lower()
    allowed = {
        "plugins": PLUGIN_SUFFIXES,
        "skills": SKILL_SUFFIXES,
        "rclone": RCLONE_SUFFIXES,
    }[kind]
    if suffix not in allowed:
        expected = ", ".join(sorted(allowed))
        raise AssetError(f"{kind} assets must use one of: {expected}")
    return safe


def _sha256(data: bytes) -> str:
    """Return the SHA-256 digest for asset bytes."""
    return hashlib.sha256(data).hexdigest()


def decode_asset_content(encoded: str, *, max_bytes: int | None = None) -> bytes:
    """Decode one bounded base64 asset upload from an operator request."""
    if not isinstance(encoded, str) or not encoded.strip():
        raise AssetError("asset content is missing")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise AssetError("asset content is not valid base64") from exc
    limit = max_bytes if max_bytes is not None else _max_bytes()
    if not content:
        raise AssetError("asset body is empty")
    if len(content) > limit:
        raise AssetError(f"asset exceeds the {limit}-byte upload limit")
    return content


def download_asset(url: str, *, max_bytes: int | None = None, timeout: float = 30.0) -> bytes:
    """Download one HTTP(S) asset with a bounded response body."""
    raw_url = str(url or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError as exc:
        raise AssetError("invalid asset URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AssetError("asset URL must use http or https")
    limit = max_bytes if max_bytes is not None else _max_bytes()
    request = urllib.request.Request(
        raw_url,
        headers={"Accept": "application/octet-stream, text/plain, application/json", "User-Agent": "Minimal-RMM/asset-store"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    if int(declared) > limit:
                        raise AssetError(f"asset exceeds the {limit}-byte download limit")
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise AssetError(f"asset exceeds the {limit}-byte download limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except AssetError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AssetError(f"asset download failed: {exc}") from exc


class AssetStore:
    """Store downloaded operator assets and their enabled state atomically."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.metadata_path = self.root / "metadata.json"
        self._lock = threading.RLock()
        for kind in ASSET_KINDS:
            (self.root / kind).mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def _read_metadata(self) -> dict[str, dict[str, Any]]:
        """Read metadata, returning an empty map for a missing or invalid file."""
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(name): dict(value)
            for name, value in payload.items()
            if isinstance(name, str) and isinstance(value, dict)
        }

    def _write_metadata(self, metadata: dict[str, dict[str, Any]]) -> None:
        """Atomically persist asset metadata with owner-only permissions."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="metadata.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.metadata_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _path(self, kind: str, name: str) -> Path:
        """Resolve one asset path below its canonical kind directory."""
        kind = normalize_asset_kind(kind)
        name = validate_asset_name(kind, name)
        directory = (self.root / kind).resolve()
        path = (directory / name).resolve()
        if path.parent != directory:
            raise AssetError("asset path escapes store")
        return path

    def add_bytes(
        self,
        kind: str,
        name: str,
        content: bytes,
        *,
        source_url: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Atomically write an asset and return its public metadata."""
        kind = normalize_asset_kind(kind)
        name = validate_asset_name(kind, name)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise AssetError("asset body is empty")
        if len(content) > _max_bytes():
            raise AssetError("asset exceeds the configured download limit")
        path = self._path(kind, name)
        with self._lock:
            fd, temporary = tempfile.mkstemp(prefix=f"{name}.", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(bytes(content))
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
                metadata = self._read_metadata()
                previous = metadata.get(f"{kind}/{name}", {})
                row = {
                    "kind": kind,
                    "name": name,
                    "enabled": bool(enabled if "enabled" not in previous else previous["enabled"]),
                    "size": len(content),
                    "sha256": _sha256(bytes(content)),
                    "source_url": _display_url(source_url) if source_url else previous.get("source_url", ""),
                    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                }
                metadata[f"{kind}/{name}"] = row
                self._write_metadata(metadata)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return row

    def add_url(
        self,
        kind: str,
        url: str,
        *,
        name: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Download and persist an asset from an HTTP(S) URL."""
        kind = normalize_asset_kind(kind)
        candidate = _safe_name(name) or _filename_from_url(url)
        if not candidate:
            raise AssetError("asset URL must contain a filename or name is required")
        content = download_asset(url)
        return self.add_bytes(kind, candidate, content, source_url=url, enabled=enabled)

    def list(self, kind: str | None = None, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        """List asset metadata whose files still exist."""
        kinds = [normalize_asset_kind(kind)] if kind else sorted(ASSET_KINDS)
        with self._lock:
            metadata = self._read_metadata()
        rows: list[dict[str, Any]] = []
        for key, row in sorted(metadata.items()):
            if row.get("kind") not in kinds:
                continue
            if not include_disabled and not row.get("enabled", True):
                continue
            name = str(row.get("name") or "")
            try:
                path = self._path(str(row.get("kind")), name)
            except AssetError:
                continue
            if not path.is_file():
                continue
            item = dict(row)
            item["size"] = path.stat().st_size
            rows.append(item)
        return rows

    def set_enabled(self, kind: str, name: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable one managed asset."""
        kind = normalize_asset_kind(kind)
        name = validate_asset_name(kind, name)
        key = f"{kind}/{name}"
        with self._lock:
            metadata = self._read_metadata()
            row = metadata.get(key)
            if not row:
                raise AssetError("asset not found")
            row = dict(row)
            row["enabled"] = bool(enabled)
            metadata[key] = row
            self._write_metadata(metadata)
        return row

    def set_flag(self, kind: str, name: str, enabled: bool) -> dict[str, Any]:
        """Persist an enabled flag for an asset that may be built in."""
        kind = normalize_asset_kind(kind)
        safe = _safe_name(name)
        if not safe:
            raise AssetError("invalid asset name")
        key = f"{kind}/{safe}"
        with self._lock:
            metadata = self._read_metadata()
            row = dict(metadata.get(key) or {"kind": kind, "name": safe})
            row["enabled"] = bool(enabled)
            row["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            metadata[key] = row
            self._write_metadata(metadata)
        return row

    def enabled(self, kind: str, name: str, default: bool = True) -> bool:
        """Return an explicit enabled flag or the supplied default."""
        key = f"{normalize_asset_kind(kind)}/{_safe_name(name)}"
        with self._lock:
            row = self._read_metadata().get(key)
        return bool(row["enabled"]) if row and "enabled" in row else bool(default)

    def flags(self, kind: str) -> dict[str, bool]:
        """Return explicit enabled flags, including flags for built-in assets."""
        kind = normalize_asset_kind(kind)
        with self._lock:
            metadata = self._read_metadata()
        return {
            str(row.get("name")): bool(row["enabled"])
            for key, row in metadata.items()
            if key.startswith(f"{kind}/") and isinstance(row, dict) and "enabled" in row
        }

    def remove(self, kind: str, name: str) -> bool:
        """Delete one managed asset and its metadata."""
        kind = normalize_asset_kind(kind)
        name = _safe_name(name)
        if not name:
            return False
        path = self._path(kind, name)
        key = f"{kind}/{name}"
        with self._lock:
            existed = path.is_file()
            if existed:
                path.unlink()
            metadata = self._read_metadata()
            metadata.pop(key, None)
            self._write_metadata(metadata)
        return existed

    def read(self, kind: str, name: str, *, enabled_only: bool = True) -> bytes:
        """Read one asset, enforcing its enabled state when requested."""
        kind = normalize_asset_kind(kind)
        name = validate_asset_name(kind, name)
        if enabled_only and not self.enabled(kind, name):
            raise AssetError("asset is disabled")
        path = self._path(kind, name)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise AssetError("asset not found") from exc
