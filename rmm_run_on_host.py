#!/usr/bin/env python3
"""
Target a registered agent by hostname via the RMM operator API:
set beacon sleep, then run a fixed list of recon commands and print results.

Environment:
  RMM_SERVER_URL or RMM_BASE_URL  (default http://127.0.0.1:8080)
  RMM_API_TOKEN                     (required if the server uses --token)

Example:
  export RMM_API_TOKEN=your-token
  export RMM_SERVER_URL=http://127.0.0.1:8080
  python rmm_run_on_host.py --hostname CFRANCHETZIA110 --sleep 5
"""

from __future__ import annotations

import argparse
import sys
import time

from rmm_cli import DEFAULT_TOKEN, RmmApiClient, _default_server_url

DEFAULT_COMMANDS = [
    "whoami",
    "systeminfo",
    "net share",
    "net view",
    "net user",
    "net user /domain",
    "net localgroup",
    "net group /domain",
]


def find_session_by_hostname(sessions: list[dict], hostname: str) -> dict | None:
    want = hostname.strip().upper()
    for s in sessions:
        if (s.get("hostname") or "").strip().upper() == want:
            return s
    return None


def wait_for_beacon(
    client: RmmApiClient,
    session_id: str,
    *,
    previous_last_seen: str | None,
    timeout: float,
    poll_interval: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, data = client.get_session(session_id)
        if code != 200:
            return False
        last = data.get("session", {}).get("last_seen")
        if last and last != previous_last_seen:
            return True
        time.sleep(poll_interval)
    return False


def print_banner(title: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n{title}\n{line}", flush=True)


def print_event_result(command: str, event: dict | None, *, error: str | None = None) -> None:
    print_banner(f"$ {command}")
    if error:
        print(f"[error] {error}", flush=True)
        return
    if not event:
        print("(no event returned)", flush=True)
        return
    body = (event.get("body") or "").strip()
    if body:
        print(body, flush=True)
    else:
        print("(empty output)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set beacon sleep and run recon commands on an RMM agent by hostname."
    )
    parser.add_argument(
        "--hostname",
        "-H",
        default="CFRANCHETZIA110",
        help="Agent hostname to match (case-insensitive)",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=5,
        metavar="SECONDS",
        help="Beacon sleep interval to apply (1-3600)",
    )
    parser.add_argument(
        "--url",
        default=_default_server_url(),
        help="RMM server base URL",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="Operator API token (or RMM_API_TOKEN)",
    )
    parser.add_argument(
        "--exec-timeout",
        type=float,
        default=180,
        help="Per-command exec wait timeout in seconds",
    )
    parser.add_argument(
        "--commands",
        nargs="*",
        default=None,
        help="Override default command list",
    )
    args = parser.parse_args()

    if not args.token:
        print(
            "Missing API token: set RMM_API_TOKEN or pass --token",
            file=sys.stderr,
        )
        return 1

    if not 1 <= args.sleep <= 3600:
        print("--sleep must be between 1 and 3600", file=sys.stderr)
        return 1

    client = RmmApiClient(args.url.rstrip("/"), args.token)
    commands = args.commands if args.commands else DEFAULT_COMMANDS

    code, data = client.health()
    if code == 401:
        print("API authentication failed (401)", file=sys.stderr)
        return 1
    if code != 200:
        print(f"Cannot reach server ({code}): {data}", file=sys.stderr)
        return 1

    code, data = client.list_sessions()
    if code != 200:
        print(f"list sessions failed ({code}): {data}", file=sys.stderr)
        return 1

    session = find_session_by_hostname(data.get("sessions", []), args.hostname)
    if not session:
        hosts = [s.get("hostname", "?") for s in data.get("sessions", [])]
        print(
            f"No session with hostname {args.hostname!r}. Active: {', '.join(hosts) or '(none)'}",
            file=sys.stderr,
        )
        return 1

    sid = session["id"]
    print(
        f"Target: {session.get('username')}@{session.get('hostname')} "
        f"[{sid[:8]}] status={session.get('beacon_status')}",
        flush=True,
    )

    old_sleep = int(session.get("sleep_seconds") or 60)
    old_jitter = int(session.get("jitter_percent") or 0)
    last_seen_before = session.get("last_seen")

    print(f"Setting sleep to {args.sleep}s (was {old_sleep}s, jitter {old_jitter}%)…", flush=True)
    code, cfg_data = client.patch_config(sid, sleep_seconds=args.sleep)
    if code != 200:
        print(f"patch config failed ({code}): {cfg_data}", file=sys.stderr)
        return 1

    # Agent receives __CONFIG__ on the next beacon when the command queue is empty.
    config_wait = old_sleep + (old_sleep * old_jitter / 100.0) + 25
    print(
        f"Waiting up to {int(config_wait)}s for agent to beacon and apply config…",
        flush=True,
    )
    if not wait_for_beacon(
        client,
        sid,
        previous_last_seen=last_seen_before,
        timeout=config_wait,
    ):
        print(
            "Warning: no new beacon observed yet; commands may still run on old interval.",
            file=sys.stderr,
        )

    failures = 0
    for cmd in commands:
        code, edata = client.exec_command(sid, cmd, timeout=args.exec_timeout)
        if code == 408:
            print_event_result(cmd, None, error="timed out waiting for agent result")
            failures += 1
            continue
        if code != 200:
            err = edata.get("error") or edata.get("detail") or f"HTTP {code}"
            print_event_result(cmd, None, error=str(err))
            failures += 1
            continue
        print_event_result(cmd, edata.get("event"))

    print_banner("Done")
    print(f"Commands run: {len(commands)}, failures: {failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
