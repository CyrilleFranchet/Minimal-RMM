#!/usr/bin/env python3
"""
Verify operator surface alignment across MCP, web AI tools, API client, and web shell.

Run: make check-parity
See: docs/mcp-parity.md
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Canonical MCP / OPENAI_TOOLS / TOOL_HANDLERS tool names.
REQUIRED_MCP_TOOLS = frozenset({
    "health",
    "list_sessions",
    "get_session",
    "exec_command",
    "queue_command",
    "patch_config",
    "get_events",
    "kill_session",
    "queue_download",
    "queue_exfil",
    "get_rclone_config",
    "queue_screenshot",
    "queue_upload",
    "list_socks",
    "start_socks",
    "stop_socks",
    "queue_persistent",
    "stop_persistent",
    "list_history",
    "get_history_session",
    "get_history_events",
    "delete_history",
    "clear_history",
    "list_session_downloads",
    "get_agent_script",
    "queue_keylog",
    "install_persistence",
    "remove_persistence",
})

# MCP tool -> RmmApiClient method (wrappers may share queue_command).
TOOL_CLIENT_METHOD: dict[str, str] = {
    "health": "health",
    "list_sessions": "list_sessions",
    "get_session": "get_session",
    "exec_command": "exec_command",
    "queue_command": "queue_command",
    "patch_config": "patch_config",
    "get_events": "get_events",
    "kill_session": "kill_session",
    "queue_download": "queue_download",
    "queue_exfil": "queue_exfil",
    "get_rclone_config": "get_rclone_config",
    "queue_screenshot": "queue_screenshot",
    "queue_upload": "upload_file",
    "list_socks": "list_socks",
    "start_socks": "start_socks",
    "stop_socks": "stop_socks",
    "queue_persistent": "queue_command",
    "stop_persistent": "queue_command",
    "list_history": "list_history",
    "get_history_session": "get_history_session",
    "get_history_events": "get_history_events",
    "delete_history": "delete_history",
    "clear_history": "clear_history",
    "list_session_downloads": "list_session_downloads",
    "get_agent_script": "get_agent_script",
    "queue_keylog": "queue_command",
    "install_persistence": "queue_command",
    "remove_persistence": "queue_command",
}

# Web shell meta verbs -> MCP tool (web/app.js dispatchShellMetaCommand).
WEB_SHELL_META: dict[str, str] = {
    "download": "queue_download",
    "exfil": "queue_exfil",
    "screenshot": "queue_screenshot",
}


def _load_module(name: str, path: Path):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def mcp_tools_from_server() -> set[str]:
    text = (ROOT / "mcp_rmm_server.py").read_text(encoding="utf-8")
    return set(re.findall(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)\s*\(", text))


def openai_tool_names(tools_mod) -> set[str]:
    names: set[str] = set()
    for entry in tools_mod.OPENAI_TOOLS:
        name = entry.get("function", {}).get("name")
        if name:
            names.add(name)
    return names


def client_methods() -> set[str]:
    cli = _load_module("rmm_cli_parity", ROOT / "rmm_cli.py")
    return {
        name
        for name, member in inspect.getmembers(cli.RmmApiClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def web_shell_meta_commands() -> set[str]:
    text = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    match = re.search(
        r'const SHELL_META_COMMANDS = \[(.*?)\];',
        text,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("SHELL_META_COMMANDS not found in web/app.js")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def main() -> int:
    errors: list[str] = []

    tools_mod = _load_module("rmm_tools_parity", ROOT / "rmm_tools.py")
    handler_tools = set(tools_mod.TOOL_HANDLERS)
    openai_tools = openai_tool_names(tools_mod)
    mcp_tools = mcp_tools_from_server()
    api_methods = client_methods()
    shell_meta = web_shell_meta_commands()

    for label, actual, expected in (
        ("TOOL_HANDLERS", handler_tools, REQUIRED_MCP_TOOLS),
        ("OPENAI_TOOLS", openai_tools, REQUIRED_MCP_TOOLS),
        ("mcp_rmm_server.py", mcp_tools, REQUIRED_MCP_TOOLS),
    ):
        missing = expected - actual
        extra = actual - expected
        if missing:
            errors.append(f"{label} missing tools: {sorted(missing)}")
        if extra:
            errors.append(f"{label} unexpected tools (update scripts/check_operator_parity.py): {sorted(extra)}")

    for tool, method in TOOL_CLIENT_METHOD.items():
        if method not in api_methods:
            errors.append(f"RmmApiClient missing method {method!r} required by MCP tool {tool!r}")

    for verb, mcp_tool in WEB_SHELL_META.items():
        if verb not in shell_meta:
            errors.append(f"web shell missing meta command {verb!r} (expected in SHELL_META_COMMANDS)")
        if mcp_tool not in REQUIRED_MCP_TOOLS:
            errors.append(f"web shell meta {verb!r} maps to unknown MCP tool {mcp_tool!r}")

    for verb in shell_meta:
        if verb not in WEB_SHELL_META:
            errors.append(
                f"web shell meta command {verb!r} has no entry in WEB_SHELL_META "
                f"(update scripts/check_operator_parity.py and docs/mcp-parity.md)"
            )

    if errors:
        print("Operator parity check FAILED:\n", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print("\nSee docs/mcp-parity.md", file=sys.stderr)
        return 1

    print(
        f"Operator parity OK: {len(REQUIRED_MCP_TOOLS)} MCP tools, "
        f"{len(WEB_SHELL_META)} web shell meta commands"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
