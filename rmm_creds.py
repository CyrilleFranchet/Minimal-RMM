"""
Workspace-scoped credential store for Minimal-RMM.

Credentials are stored in {PLUGIN_CACHE_DIR}/workspaces/{workspace_id}/creds.json.
Each entry maps a cred_id to its metadata; passwords are stored server-side only
and are never returned in list responses or logged.

Cred IDs have the format "cred-XXXXXXXX" (8 lowercase hex chars).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from functools import wraps
from typing import Any

_lock = threading.Lock()


def _cred_id() -> str:
    return "cred-" + secrets.token_hex(4)


def _creds_path(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, "creds.json")


def _load(workspace_dir: str) -> dict:
    path = _creds_path(workspace_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if not isinstance(data, dict) or any(
                not isinstance(row, dict) or row.get("id") != key or
                any(not isinstance(row.get(field), str) for field in ("label", "domain", "user", "password"))
                for key, row in data.items()
            ):
                raise ValueError("invalid credential store")
            return data
    except FileNotFoundError:
        pass
    return {}


def _save(workspace_dir: str, creds: dict) -> None:
    path = _creds_path(workspace_dir)
    tmp = path + ".tmp"
    os.makedirs(workspace_dir, mode=0o700, exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(creds, fh, indent=2)
    os.replace(tmp, path)


def _store_operation(operation):
    """Report storage failures without exposing secret data or replacing corrupt files."""
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, ValueError):
            return {"ok": False, "error": "credential_store_unavailable"}
    return guarded


@_store_operation
def creds_add(workspace_dir: str, domain: str, user: str, password: str) -> dict[str, Any]:
    """
    Add a credential entry and return its ID and label.
    The password is stored but never returned in subsequent list calls.
    """
    domain = (domain or "").strip()
    user = (user or "").strip()
    if not user:
        return {"ok": False, "error": "user is required"}
    # Split combined DOMAIN\\user if domain is empty
    if not domain and "\\" in user:
        parts = user.split("\\", 1)
        domain = parts[0].strip()
        user = parts[1].strip()
    if not user:
        return {"ok": False, "error": "user is required after splitting domain"}
    label = f"{domain}\\{user}" if domain else user
    with _lock:
        creds = _load(workspace_dir)
        cred_id = _cred_id()
        # Ensure uniqueness (astronomically unlikely to collide but be safe)
        while cred_id in creds:
            cred_id = _cred_id()
        creds[cred_id] = {
            "id": cred_id,
            "label": label,
            "domain": domain,
            "user": user,
            "password": password or "",
        }
        _save(workspace_dir, creds)
    return {"ok": True, "cred_id": cred_id, "label": label}


@_store_operation
def creds_del(workspace_dir: str, cred_id: str) -> dict[str, Any]:
    """Remove a credential entry by ID."""
    cred_id = (cred_id or "").strip()
    if not cred_id:
        return {"ok": False, "error": "cred_id is required"}
    with _lock:
        creds = _load(workspace_dir)
        if cred_id not in creds:
            return {"ok": False, "error": "cred_not_found", "cred_id": cred_id}
        del creds[cred_id]
        _save(workspace_dir, creds)
    return {"ok": True, "cred_id": cred_id}


@_store_operation
def creds_list(workspace_dir: str) -> dict[str, Any]:
    """List all stored credentials. Passwords are omitted."""
    with _lock:
        creds = _load(workspace_dir)
    items = [
        {"cred_id": v["id"], "label": v["label"]}
        for v in creds.values()
        if isinstance(v, dict)
    ]
    return {"ok": True, "creds": items}


def creds_resolve(workspace_dir: str, cred_id: str) -> dict[str, str] | None:
    """
    Resolve a cred_id to its full record (including password) for internal use.
    Returns None if the ID does not exist.
    NEVER log or return this value to the operator — it contains the password.
    """
    cred_id = (cred_id or "").strip()
    if not cred_id:
        return None
    with _lock:
        creds = _load(workspace_dir)
    entry = creds.get(cred_id)
    if not isinstance(entry, dict):
        return None
    return {
        "domain": entry.get("domain", ""),
        "user": entry.get("user", ""),
        "password": entry.get("password", ""),
        "label": entry.get("label", ""),
    }
