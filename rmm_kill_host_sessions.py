#!/usr/bin/env python3
"""
Kill all active RMM sessions matching a hostname via the operator API.

Environment:
  RMM_SERVER_URL or RMM_BASE_URL  (default http://127.0.0.1:8080)
  RMM_API_TOKEN                     (required if the server uses --token)

Example:
  export RMM_API_TOKEN=your-token
  export RMM_SERVER_URL=http://127.0.0.1:8080
  python rmm_kill_host_sessions.py --hostname CFRANCHETZIA110
"""

from __future__ import annotations

import argparse
import sys

from rmm_cli import DEFAULT_TOKEN, RmmApiClient, _default_server_url


def find_sessions_by_hostname(sessions: list[dict], hostname: str) -> list[dict]:
    want = hostname.strip().upper()
    return [
        s
        for s in sessions
        if (s.get("hostname") or "").strip().upper() == want
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kill all RMM agent sessions for a given hostname."
    )
    parser.add_argument(
        "--hostname",
        "-H",
        default="CFRANCHETZIA110",
        help="Agent hostname to match (case-insensitive)",
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
    args = parser.parse_args()

    if not args.token:
        print(
            "Missing API token: set RMM_API_TOKEN or pass --token",
            file=sys.stderr,
        )
        return 1

    client = RmmApiClient(args.url.rstrip("/"), args.token)

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

    matches = find_sessions_by_hostname(data.get("sessions", []), args.hostname)
    if not matches:
        hosts = sorted({(s.get("hostname") or "?") for s in data.get("sessions", [])})
        print(
            f"No session with hostname {args.hostname!r}. Active hosts: "
            f"{', '.join(hosts) or '(none)'}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Killing {len(matches)} session(s) for hostname {args.hostname!r}…",
        flush=True,
    )

    failures = 0
    for session in matches:
        sid = session["id"]
        label = (
            f"{session.get('username')}@{session.get('hostname')} "
            f"[{sid[:8]}] status={session.get('beacon_status')}"
        )
        code, kdata = client.kill_session(sid)
        if code == 200:
            print(f"  killed: {label}", flush=True)
        else:
            err = kdata.get("error") or kdata.get("detail") or f"HTTP {code}"
            print(f"  failed: {label} — {err}", file=sys.stderr)
            failures += 1

    print(
        f"Done: {len(matches) - failures}/{len(matches)} killed.",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
