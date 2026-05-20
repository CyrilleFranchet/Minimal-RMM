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
import getpass
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

DEFAULT_URL = os.environ.get("RMM_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
DEFAULT_TOKEN = os.environ.get("RMM_API_TOKEN", "").strip()
STATE_FILE = os.path.expanduser("~/.rmm_cli_state.json")
HISTORY_FILE = os.path.expanduser("~/.rmm_cli_history")


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
        except TimeoutError:
            return 0, {"error": "timeout", "detail": "request timed out"}

    def health(self):
        return self.request("GET", "/api/v1/health")

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

    def queue_screenshot(self, session_id: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/screenshot",
            {},
        )


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


def say(msg: str = "", *, err: bool = False):
    stream = sys.stderr if err else sys.stdout
    print(msg, file=stream)
    stream.flush()


def die(msg: str, code: int = 1):
    say(msg, err=True)
    sys.exit(code)


def warn(msg: str):
    say(msg, err=True)


def ensure_client(url: str, token: str, *, prompt_token: bool = False) -> tuple:
    """Verify server reachability and API auth; optionally prompt for token."""
    client = RmmApiClient(url, token)
    code, data = client.health()

    if code == 0:
        detail = data.get("detail") or data.get("error") or "unknown error"
        die(
            f"Cannot connect to {url}\n"
            f"  {detail}\n"
            f"  Is the server running? Check --url / RMM_SERVER_URL (default {DEFAULT_URL})."
        )

    if code == 401 and prompt_token and sys.stdin.isatty():
        if not client.token:
            client.token = getpass.getpass("API token (RMM_API_TOKEN): ").strip()
        else:
            client.token = getpass.getpass("Invalid token. Enter API token: ").strip()
        code, data = client.health()

    if code == 401:
        die(
            "API authentication failed (401).\n"
            "  Set RMM_API_TOKEN or run: export RMM_API_TOKEN='your-token'\n"
            "  Must match the token used when starting server_rmm.py"
        )

    if code != 200:
        die(f"Server returned HTTP {code}: {data}")

    return client, data


def print_event(ev: dict, json_mode: bool = False):
    if json_mode:
        print_json(ev)
        return
    cmd = ev.get("command") or ""
    suffix = f" » {cmd}" if cmd else ""
    say(f"\n[{ev['id']}] {ev.get('type', 'output')}{suffix}")
    if ev.get("artifact"):
        say(f"  artifact: {ev['artifact']}")
    if ev.get("artifact_url"):
        say(f"  url: {ev['artifact_url']}")
    body = ev.get("body", "")
    if body:
        say(body.rstrip())
    say("-" * 60)


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
    print(f"{'ID':<10} {'User':<15} {'Host':<20} {'Sleep':<8} {'Jitter':<8} Last seen")
    print("-" * 85)
    for s in sessions:
        print(
            f"{s['id'][:8]:<10} {s['username']:<15} {s['hostname']:<20} "
            f"{s['sleep_seconds']:<8} {s['jitter_percent']:<7}% "
            f"{s['last_seen']}"
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
    print(f"Sleep set to {args.seconds}s (effective on next beacon)")


def cmd_config_set_jitter(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.patch_config(sid, jitter_percent=args.percent)
    if code != 200:
        die(f"config failed ({code}): {data}")
    print(f"Jitter set to {args.percent}%")


def cmd_download(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_download(sid, args.remote_path)
    if code != 200:
        die(f"download queue failed ({code}): {data}")
    print("Download queued — check server RMM_logs/downloads/ or poll events")


def cmd_upload(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    if not os.path.isfile(args.local):
        die(f"Local file not found: {args.local}")
    code, data = client.upload_file(sid, args.local, args.remote)
    if code != 200:
        die(f"upload failed ({code}): {data}")
    print(f"Upload queued: {args.local} -> {args.remote}")


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


def _prompt(state: dict) -> str:
    sid = current_session(state)
    tag = f"[{sid[:8]}] " if sid else ""
    return f"RMM {tag}> "


def _show_help():
    print("""
Session:  list | use <id> | info | background | kill [id]
Beacon:   set_sleep <s> | set_jitter <%> | show_config
Remote:   <command>  (queue) | exec <command>  (wait) | persist <cmd> | stop
Files:    download <remote> | upload <local> <remote> | screenshot
Other:    events [since] | health | help | quit

Tip: output from the agent streams above while a session is selected.
""")


def _event_poller(client: RmmApiClient, state: dict, json_mode: bool):
    while state.get("interactive_running"):
        sid = current_session(state)
        if sid:
            since = state.get("last_event_id", 0)
            code, data = client.get_events(sid, since=since, limit=100)
            if code == 200:
                for ev in data.get("events", []):
                    if ev["id"] > since:
                        print_event(ev, json_mode)
                        state["last_event_id"] = ev["id"]
            elif code == 401:
                state["interactive_running"] = False
                break
        time.sleep(2)


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
    data = start_data or {}

    if readline and os.path.exists(HISTORY_FILE):
        try:
            readline.read_history_file(HISTORY_FILE)
        except OSError:
            pass
        readline.set_history_length(2000)

    state["interactive_running"] = True
    state.setdefault("last_event_id", 0)
    poller = threading.Thread(
        target=_event_poller, args=(client, state, json_mode), daemon=True
    )
    poller.start()

    say(f"Connected to {client.base_url} ({data.get('sessions', 0)} session(s)).")
    if data.get("sessions", 0) == 0:
        say("  No agents connected — start client_rmm.ps1 on a target (RMM_BASE_URL + RMM_BEACON_SECRET).")
    say("Type 'list' or 'help'. Ctrl+C clears the line; 'quit' exits.\n")
    _show_help()

    try:
        while True:
            try:
                line = input(_prompt(state))
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
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
                os.system("clear" if os.name != "nt" else "cls")
                continue
            if cmd == "health":
                code, hdata = client.health()
                print(f"ok ({hdata.get('sessions', 0)} sessions)" if code == 200 else warn(f"health {code}"))
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
                print(f"{'ID':<10} {'User':<15} {'Host':<20} {'Sleep':<8} {'Jitter':<8}")
                print("-" * 65)
                for s in sessions:
                    print(
                        f"{s['id'][:8]:<10} {s['username']:<15} {s['hostname']:<20} "
                        f"{s['sleep_seconds']:<8} {s['jitter_percent']:<7}%"
                    )
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
                state["last_event_id"] = 0
                code, sdata = client.get_session(full)
                if code == 200:
                    s = sdata["session"]
                    print(f"Selected {s['username']}@{s['hostname']} [{full[:8]}]")
                continue
            if cmd == "background":
                state.pop("current_session", None)
                save_state(state)
                print("Returned to global view")
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
                    print(f"  {k}: {v}")
                if cmd == "show_config":
                    j = s["jitter_percent"] / 100.0
                    lo = s["sleep_seconds"] * (1 - j)
                    hi = s["sleep_seconds"] * (1 + j)
                    print(f"  effective_range: {lo:.1f}s - {hi:.1f}s")
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
                print(f"Killed {kdata.get('session_id', sid)[:8]}")
                continue
            if cmd == "set_sleep":
                if not rest:
                    warn("Usage: set_sleep <seconds>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.patch_config(sid, sleep_seconds=int(rest[0]))
                print(f"Sleep -> {rest[0]}s" if code == 200 else warn(f"failed ({code})"))
                continue
            if cmd == "set_jitter":
                if not rest:
                    warn("Usage: set_jitter <percent>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.patch_config(sid, jitter_percent=int(rest[0]))
                print(f"Jitter -> {rest[0]}%" if code == 200 else warn(f"failed ({code})"))
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
                for ev in edata.get("events", []):
                    print_event(ev, json_mode)
                    state["last_event_id"] = max(state.get("last_event_id", 0), ev["id"])
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
                print("Download queued" if code == 200 else warn(f"failed ({code})"))
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
                print(f"Upload queued -> {remote}" if code == 200 else warn(f"failed ({code})"))
                continue
            if cmd == "screenshot":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_screenshot(sid)
                print("Screenshot queued" if code == 200 else warn(f"failed ({code})"))
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
                print("Queued" if code == 200 else warn(f"failed ({code})"))
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
                    print_event(edata["event"], json_mode)
                    state["last_event_id"] = max(
                        state.get("last_event_id", 0), edata["event"].get("id", 0)
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
                print("Persistent command set" if code == 200 else warn(f"failed ({code})"))
                continue
            if cmd == "stop":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_command(sid, "__STOP__")
                print("Persistent command stopped" if code == 200 else warn(f"failed ({code})"))
                continue

            # Default: remote command (queued), like embedded server CLI
            sid = _active_session(client, state)
            if not sid:
                warn("No session selected. Use: use <id>")
                continue
            command = line.strip()
            code, _ = client.queue_command(sid, command)
            if code == 200:
                say("Command queued (output appears after next agent beacon).")
            else:
                warn(f"queue failed ({code})")

    finally:
        state["interactive_running"] = False
        if readline:
            try:
                readline.write_history_file(HISTORY_FILE)
            except OSError:
                pass
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
    p.add_argument("--url", default=DEFAULT_URL, help=f"Server base URL (default: {DEFAULT_URL})")
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

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    state = load_state()

    if args.command is None or getattr(args, "interactive", False):
        client, data = ensure_client(args.url, args.token or DEFAULT_TOKEN, prompt_token=True)
        run_interactive(client, state, json_mode=getattr(args, "json", False), start_data=data)
        return

    client, _ = ensure_client(args.url, args.token or DEFAULT_TOKEN, prompt_token=False)

    if args.command == "health":
        cmd_health(client, args)
    else:
        args.func(client, state, args)


if __name__ == "__main__":
    main()
