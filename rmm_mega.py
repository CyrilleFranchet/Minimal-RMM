"""MEGA.nz upload helper for RMM server (lab use).

Account credentials are read from the environment on the machine running
server_rmm.py — agents never receive MEGA email/password.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

_mega_lock = threading.Lock()
_mega_client = None


class MegaConfigError(Exception):
    """MEGA account is not configured or mega.py-v2 is missing."""


@dataclass(frozen=True)
class MegaConfig:
    email: str
    password: str
    folder: str
    max_bytes: int

    @property
    def configured(self) -> bool:
        return bool(self.email and self.password)

    def public_dict(self) -> dict:
        """Operator-safe view (no password)."""
        email = self.email
        masked = ""
        if email and "@" in email:
            local, domain = email.split("@", 1)
            masked = (local[:1] + "***@" + domain) if local else email
        elif email:
            masked = email[:1] + "***"
        return {
            "configured": self.configured,
            "email": masked or None,
            "folder": self.folder or None,
            "max_bytes": self.max_bytes,
            "library_available": mega_library_available(),
        }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_mega_config() -> MegaConfig:
    folder = os.environ.get("RMM_MEGA_FOLDER", "RMM").strip()
    return MegaConfig(
        email=os.environ.get("RMM_MEGA_EMAIL", "").strip(),
        password=os.environ.get("RMM_MEGA_PASSWORD", "").strip(),
        folder=folder,
        max_bytes=_env_int("RMM_MEGA_MAX_BYTES", 100 * 1024 * 1024),
    )


def mega_library_available() -> bool:
    try:
        import mega  # noqa: F401
        return True
    except ImportError:
        return False


def require_mega_config() -> MegaConfig:
    cfg = load_mega_config()
    if not cfg.configured:
        raise MegaConfigError(
            "MEGA account not configured — set RMM_MEGA_EMAIL and RMM_MEGA_PASSWORD on the server"
        )
    if not mega_library_available():
        raise MegaConfigError(
            "mega.py-v2 is not installed — pip install mega.py-v2 (Python 3.10+)"
        )
    return cfg


def _get_client():
    global _mega_client
    cfg = require_mega_config()
    with _mega_lock:
        if _mega_client is None:
            from mega import Mega

            _mega_client = Mega().login(cfg.email, cfg.password)
        return _mega_client, cfg


def reset_mega_client() -> None:
    """Drop cached session (e.g. after credential change)."""
    global _mega_client
    with _mega_lock:
        _mega_client = None


def _resolve_dest_folder(client, folder_path: str | None):
    if not folder_path or folder_path.strip() in ("", "/"):
        return None
    path = folder_path.strip().strip("/")
    created = client.create_folder(path)
    if isinstance(created, dict) and created:
        return list(created.values())[-1]
    raise MegaConfigError(f"Could not resolve MEGA folder: {folder_path!r}")


def upload_file_to_mega(
    local_path: str,
    *,
    dest_filename: str | None = None,
) -> dict:
    """Upload a local file and return a public MEGA link.

    Returns dict with keys: success, link, remote_path (filename), size, error.
    """
    cfg = require_mega_config()
    size = os.path.getsize(local_path)
    if size > cfg.max_bytes:
        return {
            "success": False,
            "link": None,
            "size": size,
            "error": f"File size {size} exceeds MEGA limit ({cfg.max_bytes} bytes)",
        }

    name = dest_filename or os.path.basename(local_path)
    name = re.sub(r'[<>:"/\\|?*]', "_", name) or "upload.bin"

    try:
        client, cfg = _get_client()
        dest = _resolve_dest_folder(client, cfg.folder)
        upload_resp = client.upload(local_path, dest=dest, dest_filename=name)
        link = client.get_upload_link(upload_resp)
        return {
            "success": True,
            "link": link,
            "size": size,
            "error": None,
        }
    except MegaConfigError:
        raise
    except Exception as exc:
        reset_mega_client()
        return {
            "success": False,
            "link": None,
            "size": size,
            "error": str(exc),
        }
