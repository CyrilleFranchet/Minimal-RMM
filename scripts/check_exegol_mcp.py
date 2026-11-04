#!/usr/bin/env python3
"""
Validate a live Exegol MCP endpoint independently of any RMM feature.

This is a deployment/development diagnostic, not an operator capability: it
is run by hand, directly by whoever holds the Exegol MCP credentials, and is
therefore not subject to the REST/CLI/MCP operator parity rule in
docs/mcp-parity.md (no agent session ever reaches this script). See
docs/exegol-mcp-verification.md.

Run: make check-exegol-mcp
     python3 scripts/check_exegol_mcp.py [--container NAME] [--no-probe] [--json]
                                          [--config PATH] [--data-dir PATH]

RMM_EXEGOL_MCP_URL/RMM_EXEGOL_MCP_TOKEN/RMM_EXEGOL_CONTAINER are workspace
settings (see rmm_settings.FIELDS): an operator can save them from the
workspace Config dialog instead of exporting environment variables. This
script loads the same persisted workspace settings.json workspace_service.py
reads at startup (bootstrap("workspace")) before resolving those keys, so it
reflects what the running workspace actually uses. A real environment
variable still overrides the saved value, matching rmm_settings.getenv's own
precedence. --config/--data-dir select the settings file exactly as they do
for workspace_service.py.

It never starts, stops, or modifies anything in the target container —
only `list_exegol_containers` (read-only) and a one-line `printf` identity
probe through `execute_command_in_container`, the same probe
rmm_exegol_runtime.py itself uses before ever touching bloodhound-ce.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rmm_exegol_runtime import (  # noqa: E402
    EXEC_TOOL, LIST_TOOL, discovery_candidates, execution_arguments,
    output_strings, runtime_failure_code,
)
from rmm_mcp_client import McpHttpSession  # noqa: E402
from rmm_settings import bootstrap, getenv  # noqa: E402


def validated_url():
    """Validate RMM_EXEGOL_MCP_URL without requiring any BloodHound setting."""
    url = getenv('RMM_EXEGOL_MCP_URL', '').strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise SystemExit(f"RMM_EXEGOL_MCP_URL is missing or invalid: {url!r}")
    return url


def validated_container(container):
    """Match the same safe pattern rmm_exegol_runtime.configuration() enforces."""
    if container and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container):
        raise SystemExit(f"Invalid container name: {container!r}")
    return container or None


async def probe_identity(session, container):
    """Run the exact identity probe rmm_exegol_runtime.py runs before bloodhound-ce."""
    nonce = secrets.token_hex(24)
    hostname = socket.gethostname()
    command = f"printf 'RMM_CHECK_ID=%s:%s\\n' {nonce!r} \"$(hostname)\""
    arguments = execution_arguments(session.openai_tools, container, command)
    result = await session.call_tool(EXEC_TOOL, arguments)
    lines = [line.strip() for text in output_strings(result) for line in text.splitlines()]
    expected = f"RMM_CHECK_ID={nonce}:{hostname}"
    return expected in lines, lines


async def run(args):
    url = validated_url()
    token = getenv('RMM_EXEGOL_MCP_TOKEN', '').strip() or None
    container = validated_container(args.container or getenv('RMM_EXEGOL_CONTAINER', '').strip())
    report = {'url': url, 'ok': False}

    try:
        async with McpHttpSession(url, token) as session:
            report['mcp_connected'] = True
            tool_names = sorted(row.get('function', {}).get('name', '') for row in session.openai_tools)
            report['tools'] = tool_names
            report['required_tools_present'] = {
                LIST_TOOL: LIST_TOOL in tool_names,
                EXEC_TOOL: EXEC_TOOL in tool_names,
            }

            list_matches = [row for row in session.openai_tools if row.get('function', {}).get('name') == LIST_TOOL]
            if len(list_matches) != 1 or list_matches[0]['function'].get('parameters', {}).get('required', []):
                report['error'] = 'exegol_discovery_tool_unsupported'
                return report

            raw = await asyncio.wait_for(session.call_tool(LIST_TOOL, {}), timeout=10)
            candidates = discovery_candidates(raw)
            report['running_containers'] = candidates

            target = container or (candidates[0] if len(candidates) == 1 else None)
            if args.no_probe or not target:
                report['ok'] = bool(candidates)
                if not target:
                    report['note'] = ('no running container to probe; pass --container, '
                                       'or exactly one must be running' if candidates else 'no running containers found')
                return report

            report['probed_container'] = target
            matched, lines = await probe_identity(session, target)
            report['identity_confirmed'] = matched
            if not matched:
                report['probe_output'] = lines[:20]
            report['ok'] = matched
            return report
    except Exception as exc:  # noqa: BLE001 - this is a human-facing diagnostic, not a server path
        report['error'] = runtime_failure_code(exc)
        report['exception'] = f"{type(exc).__name__}: {exc}"
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--container', help='Override RMM_EXEGOL_CONTAINER for the identity probe')
    parser.add_argument('--no-probe', action='store_true', help='List tools/containers only; skip the exec round trip')
    parser.add_argument('--json', action='store_true', help='Print the machine-readable report instead of a summary')
    parser.add_argument('--config', help='Workspace settings file (same meaning as workspace_service.py --config)')
    parser.add_argument('--data-dir', help='Workspace data directory (same meaning as workspace_service.py --data-dir)')
    args = parser.parse_args()

    # Load the same persisted workspace settings.json workspace_service.py reads at
    # startup, so RMM_EXEGOL_MCP_URL/_TOKEN/RMM_EXEGOL_CONTAINER saved from the Config
    # dialog are picked up here too, not just real environment variables.
    bootstrap_argv = []
    if args.config:
        bootstrap_argv += ['--config', args.config]
    if args.data_dir:
        bootstrap_argv += ['--data-dir', args.data_dir]
    bootstrap('workspace', bootstrap_argv)

    report = asyncio.run(run(args))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"MCP endpoint:        {report.get('url')}")
        print(f"MCP connected:       {'yes' if report.get('mcp_connected') else 'no'}")
        if report.get('tools') is not None:
            print(f"Tools advertised:    {', '.join(report['tools']) or '(none)'}")
            for name, present in report.get('required_tools_present', {}).items():
                print(f"  required: {name:<30} {'present' if present else 'MISSING'}")
        if 'running_containers' in report:
            print(f"Running containers:  {', '.join(report['running_containers']) or '(none)'}")
        if 'probed_container' in report:
            print(f"Identity probe on:   {report['probed_container']} -> "
                  f"{'confirmed' if report.get('identity_confirmed') else 'FAILED'}")
            if not report.get('identity_confirmed'):
                print(f"  exec reply lines:  {report.get('probe_output')}")
        if 'note' in report:
            print(f"Note:                {report['note']}")
        if 'error' in report:
            print(f"Error:               {report['error']}")
            if 'exception' in report:
                print(f"  ({report['exception']})")
        print(f"\nResult: {'OK' if report.get('ok') else 'FAILED'}")

    sys.exit(0 if report.get('ok') else 1)


if __name__ == '__main__':
    main()
