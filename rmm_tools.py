"""
Shared RMM operator tools for MCP and web AI assistant.
Uses the same REST API as rmm_cli.py (RmmApiClient).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rmm_cli import RmmApiClient, _default_server_url
from rmm_settings import getenv as settings_getenv

DEFAULT_RMM_URL = _default_server_url()
DEFAULT_RMM_TOKEN = settings_getenv("RMM_API_TOKEN", "").strip()


def make_client(
    base_url: str | None = None,
    token: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> RmmApiClient:
    resolved_token = DEFAULT_RMM_TOKEN if token is None and extra_headers is None else str(token or "").strip()
    headers = dict(extra_headers or {})
    if not token and extra_headers is None:
        workspace_id = settings_getenv("RMM_WORKSPACE_ID", "").strip()
        workspace_token = settings_getenv("RMM_WORKSPACE_TOKEN", "").strip()
        if workspace_id and workspace_token:
            headers["X-RMM-Workspace"] = workspace_id
            headers["X-RMM-Workspace-Token"] = workspace_token
            if settings_getenv('RMM_WORKSPACE_OPERATOR_RELAY', '') == '1':
                headers['X-RMM-Operator-Relay'] = '1'
    return RmmApiClient(
        (base_url or DEFAULT_RMM_URL).rstrip("/"),
        resolved_token,
        extra_headers=headers,
    )


def _json_result(payload: Any) -> str:
    from rmm_embeddings import compact_embedding_payload
    return json.dumps(compact_embedding_payload(payload), indent=2, default=str)


def _resolve_session_id(client: RmmApiClient, session_ref: str) -> tuple[str | None, dict]:
    """Resolve only an unambiguous reference from the caller's visible sessions."""
    ref = (session_ref or "").strip()
    if not ref:
        return None, {"error": "session_ref required"}

    code, data = client.list_sessions()
    if code != 200:
        return None, {"error": "list_sessions failed", "detail": data}

    sessions = data.get("sessions", [])
    ref_upper = ref.upper()
    exact_ids = [s for s in sessions if s["id"] == ref]
    exact_hosts = [s for s in sessions if (s.get("hostname") or "").upper() == ref_upper]
    prefixes = [s for s in sessions if s["id"].startswith(ref)
                or (s.get("hostname") or "").upper().startswith(ref_upper)]
    matches = exact_ids or exact_hosts or prefixes
    if len(matches) == 1:
        return matches[0]["id"], matches[0]
    if len(matches) > 1:
        return None, {
            "error": "ambiguous_session_ref",
            "session_ref": ref,
            "detail": "Use a full session ID from the visible candidates.",
            "candidates": [{"id": s["id"], "hostname": s.get("hostname")} for s in matches],
        }

    return None, {"error": "session_not_found", "session_ref": ref}


def tool_settings(client, action="show", settings=None):
    """Read redacted settings or save overrides at the connected storage owner."""
    if action not in {"show", "update"} or (action == "update" and not isinstance(settings, dict)):
        return _json_result({"ok": False, "status": 400, "data": {"error": "Use show, or update with a settings object"}})
    code, data = client.settings(settings if action == "update" else None)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_bloodhound(client, action="preflight", run_id="", options=None):
    """Operate the storage-owner-local BloodHound workflow without exposing credentials."""
    code, data = client.bloodhound(action, run_id, options)
    return _json_result({"ok": 200 <= code < 300, "status": code, "data": data})


def tool_embedding_profiles(client, action="list", name="", config=None):
    """Manage secret-free embedding profiles in the selected storage owner."""
    code, data = client.embedding_profiles(action, name, config)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_run_embeddings(client, session_ref, profile, documents):
    """Queue explicit file or directory paths using a previously saved profile."""
    code, data = client.run_embeddings(session_ref, profile, documents)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_embedding_results(client, action="list", name="", session_ref="", event_id=0):
    """Save a selected output event or manage persisted result collections."""
    code, data = client.embedding_results(action, name, session_ref, event_id)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_health(client: RmmApiClient) -> str:
    code, data = client.health(timeout=5)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_list_sessions(client: RmmApiClient) -> str:
    # The API validates either operator or workspace credentials and scopes results.
    code, data = client.list_sessions()
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    sessions = data.get("sessions", [])
    summary = [
        {
            "id": s["id"],
            "id_prefix": s["id"][:8],
            "hostname": s.get("hostname"),
            "username": s.get("username"),
            "beacon_status": s.get("beacon_status"),
            "sleep_seconds": s.get("sleep_seconds"),
            "jitter_percent": s.get("jitter_percent"),
        }
        for s in sessions
    ]
    return _json_result({"ok": True, "count": len(summary), "sessions": summary})


def tool_get_session(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.get_session(sid)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result({"ok": True, "session": data.get("session")})


def tool_exec_command(
    client: RmmApiClient,
    session_ref: str,
    command: str,
    timeout: float = 120,
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.exec_command(sid, command, timeout=timeout)
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": command})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    ev = data.get("event") or {}
    return _json_result({
        "ok": True,
        "session_id": sid,
        "command": command,
        "output": ev.get("body", ""),
        "event": ev,
    })


def tool_queue_command(
    client: RmmApiClient,
    session_ref: str,
    command: str,
    cmd_type: str = "oneshot",
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.queue_command(sid, command, cmd_type=cmd_type)
    return _json_result({
        "ok": code == 200,
        "status": code,
        "session_id": sid,
        "queued": command,
        "data": data,
    })


def tool_patch_config(
    client: RmmApiClient,
    session_ref: str,
    sleep_seconds: int | None = None,
    jitter_percent: int | None = None,
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.patch_config(sid, sleep_seconds=sleep_seconds, jitter_percent=jitter_percent)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_get_events(
    client: RmmApiClient,
    session_ref: str,
    since: int = 0,
    limit: int = 50,
    forward: bool = False,
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.get_events(sid, since=since, limit=limit, forward=forward)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result({"ok": True, "session_id": sid, "events": data.get("events", [])})


def tool_kill_session(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.kill_session(sid)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_queue_download(client: RmmApiClient, session_ref: str, remote_path: str) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.queue_download(sid, remote_path)
    return _json_result({"ok": code == 200, "status": code, "queued": remote_path, "data": data})


def tool_queue_exfil(
    client: RmmApiClient,
    session_ref: str,
    remote_path: str,
    profile: str | None = None,
    dest: str | None = None,
    mode: str = "auto",
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.queue_exfil(sid, remote_path, profile=profile, dest=dest, mode=mode)
    return _json_result({
        "ok": code == 200,
        "status": code,
        "queued": remote_path,
        "data": data,
    })


def tool_get_rclone_config(client: RmmApiClient) -> str:
    code, data = client.get_rclone_config()
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_queue_screenshot(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.queue_screenshot(sid)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_list_plugins(client: RmmApiClient) -> str:
    code, data = client.list_plugins()
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_list_plugin_sources(client: RmmApiClient) -> str:
    """List saved repository/branch configurations without returning credentials."""
    code, data = client.list_plugin_sources()
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_save_plugin_source(client: RmmApiClient, source_id: str, repository: str, branch: str = "main", label: str = "", credential_env: str = "RMM_PLUGIN_GITHUB_TOKEN") -> str:
    """Persist a plugin source and its service-side credential reference."""
    code, data = client.save_plugin_source(source_id, repository, branch, label, credential_env)
    return _json_result({"ok": code in (200, 201), "status": code, "data": data})


def tool_remove_plugin_source(client: RmmApiClient, source_id: str) -> str:
    """Remove source configuration while preserving installed plugin files."""
    code, data = client.remove_plugin_source(source_id)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_list_plugin_release(client: RmmApiClient, tag: str = "latest", source: str = "default") -> str:
    """List compiled plugins and resolve the exact tag for a GitHub release."""
    code, data = client.list_plugin_release(tag, source)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_install_plugin_release(client: RmmApiClient, name: str, tag: str, sha256: str = "", source: str = "default", source_revision: str = "") -> str:
    """Install a verified plugin from an exact release without executing it."""
    if not tag or tag == "latest":
        return _json_result({"ok": False, "error": "exact release tag required; list_plugin_release resolves latest"})
    code, data = client.install_plugin_release(name, tag, sha256, source, source_revision)
    return _json_result({"ok": code in (200, 201), "status": code, "data": data})


def tool_list_assets(client: RmmApiClient, kind: str = "") -> str:
    """List operator-managed URL-installed assets."""
    code, data = client.list_assets(kind or None)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_add_asset(
    client: RmmApiClient,
    kind: str,
    url: str,
    name: str = "",
    enabled: bool = True,
) -> str:
    """Download and install one operator asset from a URL."""
    code, data = client.add_asset(kind, url, name or None, enabled)
    return _json_result({"ok": code in (200, 201), "status": code, "data": data})


def tool_set_asset_enabled(client: RmmApiClient, kind: str, name: str, enabled: bool) -> str:
    """Enable or disable one managed operator asset."""
    code, data = client.set_asset_enabled(kind, name, enabled)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_remove_asset(client: RmmApiClient, kind: str, name: str) -> str:
    """Remove one managed operator asset."""
    code, data = client.remove_asset(kind, name)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_profile_status(client: RmmApiClient) -> str:
    """Return the active RMM-Profile and beacon URL."""
    code, data = client.profile_status()
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_start_profile(client: RmmApiClient, name: str = "cloudflared", config: dict | None = None) -> str:
    """Start one installed RMM-Profile connector."""
    code, data = client.start_profile(name, config)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_stop_profile(client: RmmApiClient) -> str:
    """Stop the active RMM-Profile connector."""
    code, data = client.stop_profile()
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_queue_pe_load(
    client: RmmApiClient,
    session_ref: str,
    plugin: str,
    export: str = "Run",
    plugin_input: str = "",
    plugin_command: str = "",
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.queue_pe_load(
        sid,
        plugin,
        export=export,
        plugin_input=plugin_input,
        command=plugin_command or None,
    )
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_queue_upload(
    client: RmmApiClient,
    session_ref: str,
    local_path: str,
    remote_path: str,
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    local = Path(local_path).expanduser()
    if not local_path.startswith("artifact:") and not local.is_file():
        return _json_result({"ok": False, "error": "local_file_not_found", "path": str(local)})
    code, data = client.upload_file(sid, local_path if local_path.startswith("artifact:") else str(local), remote_path)
    return _json_result({
        "ok": code == 200,
        "status": code,
        "session_id": sid,
        "local_path": str(local),
        "remote_path": remote_path,
        "delivery_state": "queued" if code == 200 else "not_confirmed",
        "note": "Queuing does not confirm delivery. Wait for a new successful agent upload result matching this session and destination via get_events before using the file. Missing results are inconclusive; do not blindly retry.",
        "data": data,
    })


def tool_start_socks(
    client: RmmApiClient,
    session_ref: str,
    port: int = 1080,
    bind_host: str = "127.0.0.1",
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.start_socks(sid, port=port, bind_host=bind_host)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_stop_socks(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.stop_socks(sid)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_list_socks(client: RmmApiClient) -> str:
    code, data = client.list_socks()
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    relays = data.get("relays", [])
    return _json_result({"ok": True, "count": len(relays), "relays": relays})


def tool_start_tun(
    client: RmmApiClient,
    session_ref: str,
    device: str,
    route: str = "",
    tun_address: str = "198.18.0.1/15",
    interface: str = "",
    mtu: int = 1500,
    udp_timeout_seconds: int = 300,
    routes: list[str] | None = None,
) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.start_tun(
        sid,
        device,
        route=route,
        routes=routes,
        tun_address=tun_address,
        interface=interface,
        mtu=mtu,
        udp_timeout_seconds=udp_timeout_seconds,
    )
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_stop_tun(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.stop_tun(sid)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_list_tun(client: RmmApiClient) -> str:
    code, data = client.list_tun()
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    relays = data.get("relays", [])
    return _json_result({"ok": True, "count": len(relays), "relays": relays})


def tool_queue_persistent(client: RmmApiClient, session_ref: str, command: str) -> str:
    return tool_queue_command(client, session_ref, command, cmd_type="persistent")


def tool_stop_persistent(client: RmmApiClient, session_ref: str) -> str:
    return tool_queue_command(client, session_ref, "__STOP__", cmd_type="oneshot")


def _resolve_history_ref(client: RmmApiClient, session_ref: str) -> tuple[str | None, dict]:
    """Resolve archived session id from UUID prefix, full id, or hostname."""
    ref = (session_ref or "").strip()
    if not ref:
        return None, {"error": "session_ref required"}
    code, data = client.get_history_session(ref)
    if code == 200 and data.get("session"):
        sid = data["session"].get("session_id") or ref
        return sid, data["session"]
    code, data = client.list_history()
    if code != 200:
        return None, {"error": "history_lookup_failed", "detail": data}
    ref_upper = ref.upper()
    rows = data.get("sessions", [])
    exact_ids = [row for row in rows if row.get("session_id") == ref]
    exact_hosts = [row for row in rows if (row.get("hostname") or "").upper() == ref_upper]
    prefixes = [row for row in rows if (row.get("session_id") or "").startswith(ref)
                or (row.get("hostname") or "").upper().startswith(ref_upper)]
    matches = exact_ids or exact_hosts or prefixes
    if len(matches) == 1:
        return matches[0]["session_id"], matches[0]
    if len(matches) > 1:
        return None, {"error": "ambiguous_session_ref", "session_ref": ref,
                      "candidates": matches}
    return None, {"error": "history_not_found", "session_ref": ref}


def tool_list_history(client: RmmApiClient) -> str:
    code, data = client.list_history()
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    sessions = data.get("sessions", [])
    return _json_result({"ok": True, "count": len(sessions), "sessions": sessions})


def tool_get_history_session(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_history_ref(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.get_history_session(sid)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result({"ok": True, "session_id": sid, "session": data.get("session")})


def tool_get_history_events(
    client: RmmApiClient,
    session_ref: str,
    since: int = 0,
    limit: int = 500,
    forward: bool = False,
) -> str:
    sid, info = _resolve_history_ref(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.get_history_events(sid, since=since, limit=limit, forward=forward)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result({
        "ok": True,
        "session_id": data.get("session_id") or sid,
        "events": data.get("events", []),
    })


def tool_delete_history(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_history_ref(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.delete_history(sid)
    return _json_result({
        "ok": code == 200,
        "status": code,
        "session_id": sid,
        "data": data,
    })


def tool_clear_history(client: RmmApiClient) -> str:
    code, data = client.clear_history()
    payload = data if isinstance(data, dict) else {}
    return _json_result({
        "ok": code == 200,
        "status": code,
        "deleted": payload.get("deleted", 0),
        "errors": payload.get("errors", []),
        "data": data,
    })


def tool_list_session_downloads(client: RmmApiClient, session_ref: str) -> str:
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.list_session_downloads(sid)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    downloads = data.get("downloads", [])
    return _json_result({
        "ok": True,
        "session_id": data.get("session_id") or sid,
        "count": len(downloads),
        "downloads": downloads,
    })


def tool_list_artifacts(client: RmmApiClient, kind: str = "downloads") -> str:
    """List global downloads or screenshots with their originating agents."""
    normalized = (kind or "downloads").strip().lower()
    if normalized not in {"downloads", "screenshots"}:
        return _json_result({
            "ok": False,
            "error": "invalid_artifact_kind",
            "detail": "kind must be downloads or screenshots",
        })
    code, data = client.list_artifacts(normalized)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    artifacts = data.get("artifacts", [])
    return _json_result({
        "ok": True,
        "kind": normalized,
        "count": len(artifacts),
        "artifacts": artifacts,
    })


def tool_list_ai_jobs(client: RmmApiClient, session_id: str = "") -> str:
    """List persisted AI jobs without starting or retrying execution."""
    code, data = client.list_ai_jobs(session_id)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_get_ai_job(client: RmmApiClient, job_id: str) -> str:
    """Read a job's progress and final result within the caller's scope."""
    code, data = client.get_ai_job(job_id)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_get_agent_script(client: RmmApiClient) -> str:
    code, data = client.get_agent_script()
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    content = data.get("content") or ""
    return _json_result({
        "ok": True,
        "filename": data.get("filename", "client_rmm.ps1"),
        "byte_length": len(content.encode("utf-8")),
        "line_count": content.count("\n") + (1 if content else 0),
        "content": content,
    })


def tool_deploy_agent_script(
    client: RmmApiClient,
    session_id: str = "",
    sleep_seconds: int = 60,
    jitter_percent: int = 30,
    http_proxy: str = "",
    workspace_id: str = "",
) -> str:
    """Render a ready-to-run client_rmm.ps1 configured with the server's active beacon URL and secret."""
    code, data = client.deploy_agent_script(
        session_id=session_id,
        sleep_seconds=sleep_seconds,
        jitter_percent=jitter_percent,
        http_proxy=http_proxy,
        **({"workspace_id": workspace_id} if workspace_id else {}),
    )
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_build_go_agent(
    client: RmmApiClient,
    goos: str = "windows",
    goarch: str = "amd64",
    session_id: str = "",
    sleep_seconds: int = 60,
    jitter_percent: int = 30,
    http_proxy: str = "",
    workspace_id: str = "",
    stageless: bool = False,
    debug: bool = False,
) -> str:
    """Build the repository Go agent with the server's active connection settings embedded."""
    config = {
        "session_id": session_id,
        "sleep_seconds": sleep_seconds,
        "jitter_percent": jitter_percent,
        "http_proxy": http_proxy,
    }
    if workspace_id:
        config["workspace_id"] = workspace_id
    if not isinstance(debug, bool):
        return _json_result({"ok": False, "error": "debug must be a boolean"})
    code, data = client.build_go_agent(
        goos=goos,
        goarch=goarch,
        config=config,
        use_server_config=True,
        stageless=stageless,
        debug=debug,
    )
    return _json_result({"ok": code == 200, "status": code, "data": data})


def _valid_staging_id(staging_id: str) -> bool:
    """Same shape as the server's STAGING_ID_RE: 8-128 of [A-Za-z0-9_-]."""
    return 8 <= len(staging_id) <= 128 and all(
        ch.isascii() and (ch.isalnum() or ch in "-_") for ch in staging_id
    )


def tool_stage_status(client: RmmApiClient, staging_id: str) -> str:
    """Read the stage1 launcher status recorded on the server for a staged build."""
    staging_id = str(staging_id or "").strip()
    if not _valid_staging_id(staging_id):
        return _json_result({"ok": False, "error": "invalid_staging_id",
                             "detail": "staging_id must be 8-128 letters, digits, - or _"})
    code, data = client.stage_status(staging_id)
    if code == 404:
        return _json_result({"ok": False, "status": 404,
                             "error": "staging_entry_not_found",
                             "detail": "Unknown, purged, or expired staging entry."})
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_queue_keylog(client: RmmApiClient, session_ref: str, action: str) -> str:
    act = (action or "").strip().lower()
    if act not in ("start", "stop", "dump"):
        return _json_result({
            "ok": False,
            "error": "invalid_action",
            "detail": "action must be start, stop, or dump",
        })
    return tool_queue_command(client, session_ref, f"__KEYLOG__ {act}", cmd_type="oneshot")


def tool_install_persistence(client: RmmApiClient, session_ref: str) -> str:
    return tool_queue_command(client, session_ref, "__INSTALL_PERSIST__", cmd_type="oneshot")


def tool_remove_persistence(client: RmmApiClient, session_ref: str) -> str:
    return tool_queue_command(client, session_ref, "__REMOVE_PERSIST__", cmd_type="oneshot")


def tool_spawn_session(
    client: RmmApiClient,
    session_ref: str,
    verify: bool = True,
    spawn_timeout: float = 120,
    register_timeout: float = 60,
    verify_timeout: float = 120,
) -> str:
    """Spawn a detached second Go agent from a live Go session and verify both."""
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.spawn_session(
        sid,
        verify=verify,
        spawn_timeout=spawn_timeout,
        register_timeout=register_timeout,
        verify_timeout=verify_timeout,
    )
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": "__SPAWN__"})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    return _json_result({"ok": bool(data.get("ok")), "session_id": sid, "registered": data.get("registered"), "verified": data.get("verified"), "data": data})


def tool_make_token(
    client: RmmApiClient,
    session_ref: str,
    user: str = "",
    domain: str = "",
    password: str = "",
    timeout: float = 120,
    cred_id: str = "",
) -> str:
    """Create a runas /netonly style network identity on a Go agent session."""
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    if not (user or "").strip() and not cred_id:
        return _json_result({"ok": False, "error": "invalid_credentials", "detail": "user is required"})
    options = {"cred_id": cred_id} if cred_id else {}
    code, data = client.make_token(sid, domain, user, password, timeout=timeout, **options)
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": "__MAKE_TOKEN__"})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    # Report only the identity label and agent output; never the password.
    return _json_result({
        "ok": bool(data.get("ok")),
        "session_id": sid,
        "identity": data.get("identity"),
        "output": data.get("output"),
    })


def tool_rev2self(client: RmmApiClient, session_ref: str, timeout: float = 120) -> str:
    """Revert a Go agent session to its own network identity."""
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.rev2self(sid, timeout=timeout)
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": "__REV2SELF__"})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    return _json_result({"ok": bool(data.get("ok")), "session_id": sid, "output": data.get("output")})


def tool_lateral_move(
    client: RmmApiClient,
    session_ref: str,
    method: str,
    host: str,
    verify: bool = True,
    exec_timeout: float = 300,
    register_timeout: float = 60,
    verify_timeout: float = 120,
) -> str:
    """Start a Go agent on another host through psexec, wmiexec, or winrm."""
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    method = (method or "").strip().lower()
    if method not in ("psexec", "wmiexec", "winrm"):
        return _json_result({
            "ok": False,
            "error": "invalid_method",
            "detail": "method must be psexec, wmiexec, or winrm",
        })
    host = (host or "").strip().strip("\\")
    if not host:
        return _json_result({"ok": False, "error": "invalid_host"})
    code, data = client.lateral_move(
        sid,
        method,
        host,
        verify=verify,
        exec_timeout=exec_timeout,
        register_timeout=register_timeout,
        verify_timeout=verify_timeout,
    )
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": "__LATERAL__"})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    return _json_result({
        "ok": bool(data.get("ok")),
        "session_id": sid,
        "target_session_id": data.get("target_session_id"),
        "verified": data.get("verified"),
        "output": data.get("output"),
        "data": data,
    })


def tool_session_uid(client: RmmApiClient, session_ref: str, timeout: float = 120) -> str:
    """Show the identity a Go agent runs under, plus any make_token identity."""
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.session_uid(sid, timeout=timeout)
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": "__UID__"})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    return _json_result({"ok": True, "session_id": sid, "output": data.get("output")})


# ── Credential store tools ────────────────────────────────────────────────────

def tool_creds_add(client: RmmApiClient, domain: str = "", user: str = "", password: str = "") -> str:
    """Add a credential entry to the workspace store; returns a cred_id."""
    if not (user or "").strip():
        return _json_result({"ok": False, "error": "user is required"})
    code, data = client.creds_add(domain, user, password)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result(data)


def tool_creds_del(client: RmmApiClient, cred_id: str) -> str:
    """Remove a credential entry from the workspace store by cred_id."""
    if not (cred_id or "").strip():
        return _json_result({"ok": False, "error": "cred_id is required"})
    code, data = client.creds_del(cred_id)
    if code not in (200, 404):
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result(data)


def tool_creds_list(client: RmmApiClient) -> str:
    """List stored workspace credentials (labels only, no passwords)."""
    code, data = client.creds_list()
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result(data)


# ── Spawnas / runas tools ─────────────────────────────────────────────────────

def tool_spawnas(
    client: RmmApiClient,
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
    """Spawn under a different identity, inheriting the source build's tracing."""
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    if not cred_id and not (user or "").strip():
        return _json_result({"ok": False, "error": "cred_id or user is required"})
    code, data = client.spawnas(
        sid, cred_id=cred_id, domain=domain, user=user, password=password,
        verify=verify, spawn_timeout=spawn_timeout,
        register_timeout=register_timeout, verify_timeout=verify_timeout,
    )
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": "__SPAWNAS__"})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    return _json_result({
        "ok": bool(data.get("ok") if isinstance(data, dict) else data),
        "session_id": sid,
        "spawned_session_id": data.get("spawned_session_id") if isinstance(data, dict) else None,
        "identity": data.get("identity") if isinstance(data, dict) else None,
        "verified": data.get("verified") if isinstance(data, dict) else None,
        "data": data,
    })


def tool_runas(
    client: RmmApiClient,
    session_ref: str,
    command: str,
    cred_id: str = "",
    user: str = "",
    domain: str = "",
    password: str = "",
    timeout: float = 120,
) -> str:
    """Run a shell command on a Go agent under a different process identity."""
    sid, info = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    if not cred_id and not (user or "").strip():
        return _json_result({"ok": False, "error": "cred_id or user is required"})
    if not (command or "").strip():
        return _json_result({"ok": False, "error": "command is required"})
    code, data = client.runas(sid, command, cred_id=cred_id, domain=domain,
                              user=user, password=password, timeout=timeout)
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "session_id": sid, "command": "__RUNAS__"})
    if code != 200:
        return _json_result({"ok": False, "status": code, "session_id": sid, "data": data})
    return _json_result({
        "ok": bool(data.get("ok")),
        "session_id": sid,
        "identity": data.get("identity"),
        "output": data.get("output"),
    })


TOOL_HANDLERS = {
    "bloodhound": lambda c, a: tool_bloodhound(c, a.get("action", "preflight"), a.get("run_id", ""), a.get("options")),
    "list_ai_jobs": lambda c, a: tool_list_ai_jobs(c, a.get("session_id", "")),
    "get_ai_job": lambda c, a: tool_get_ai_job(c, a["job_id"]),
    "settings": lambda c, a: tool_settings(c, a.get("action", "show"), a.get("settings")),
    "embedding_profiles": lambda c, a: tool_embedding_profiles(c, a.get("action", "list"), a.get("name", ""), a.get("config")),
    "run_embeddings": lambda c, a: tool_run_embeddings(c, a["session_ref"], a["profile"], a["documents"]),
    "embedding_results": lambda c, a: tool_embedding_results(c, a.get("action", "list"), a.get("name", ""), a.get("session_ref", ""), a.get("event_id", 0)),
    "health": lambda c, a: tool_health(c),
    "list_sessions": lambda c, a: tool_list_sessions(c),
    "get_session": lambda c, a: tool_get_session(c, a["session_ref"]),
    "exec_command": lambda c, a: tool_exec_command(
        c, a["session_ref"], a["command"], float(a.get("timeout", 120))
    ),
    "queue_command": lambda c, a: tool_queue_command(
        c, a["session_ref"], a["command"], a.get("cmd_type", "oneshot")
    ),
    "patch_config": lambda c, a: tool_patch_config(
        c,
        a["session_ref"],
        a.get("sleep_seconds"),
        a.get("jitter_percent"),
    ),
    "get_events": lambda c, a: tool_get_events(
        c, a["session_ref"], int(a.get("since", 0)), int(a.get("limit", 50)), a.get("forward", False)
    ),
    "kill_session": lambda c, a: tool_kill_session(c, a["session_ref"]),
    "queue_download": lambda c, a: tool_queue_download(c, a["session_ref"], a["remote_path"]),
    "queue_exfil": lambda c, a: tool_queue_exfil(
        c, a["session_ref"], a["remote_path"], a.get("profile"), a.get("dest")
    ),
    "get_rclone_config": lambda c, a: tool_get_rclone_config(c),
    "queue_screenshot": lambda c, a: tool_queue_screenshot(c, a["session_ref"]),
    "list_plugins": lambda c, a: tool_list_plugins(c),
    "list_plugin_sources": lambda c, a: tool_list_plugin_sources(c),
    "save_plugin_source": lambda c, a: tool_save_plugin_source(c, a["source_id"], a["repository"], a.get("branch", "main"), a.get("label", ""), a.get("credential_env", "RMM_PLUGIN_GITHUB_TOKEN")),
    "remove_plugin_source": lambda c, a: tool_remove_plugin_source(c, a["source_id"]),
    "list_plugin_release": lambda c, a: tool_list_plugin_release(c, a.get("tag", "latest"), a.get("source", "default")),
    "install_plugin_release": lambda c, a: tool_install_plugin_release(c, a["name"], a["tag"], a.get("sha256", ""), a.get("source", "default"), a.get("source_revision", "")),
    "list_assets": lambda c, a: tool_list_assets(c, a.get("kind", "")),
    "add_asset": lambda c, a: tool_add_asset(
        c, a["kind"], a["url"], a.get("name", ""), bool(a.get("enabled", True))
    ),
    "set_asset_enabled": lambda c, a: tool_set_asset_enabled(
        c, a["kind"], a["name"], bool(a.get("enabled"))
    ),
    "remove_asset": lambda c, a: tool_remove_asset(c, a["kind"], a["name"]),
    "profile_status": lambda c, a: tool_profile_status(c),
    "start_profile": lambda c, a: tool_start_profile(c, a.get("name", "cloudflared"), a.get("config")),
    "stop_profile": lambda c, a: tool_stop_profile(c),
    "queue_pe_load": lambda c, a: tool_queue_pe_load(
        c,
        a["session_ref"],
        a["plugin"],
        a.get("export", "Run"),
        a.get("input", ""),
        a.get("command", ""),
    ),
    "queue_upload": lambda c, a: tool_queue_upload(
        c, a["session_ref"], a["local_path"], a["remote_path"]
    ),
    "start_socks": lambda c, a: tool_start_socks(
        c,
        a["session_ref"],
        int(a.get("port", 1080)),
        str(a.get("bind_host", "127.0.0.1")),
    ),
    "stop_socks": lambda c, a: tool_stop_socks(c, a["session_ref"]),
    "list_socks": lambda c, a: tool_list_socks(c),
    "start_tun": lambda c, a: tool_start_tun(
        c,
        a["session_ref"],
        a["device"],
        route=a.get("route", ""),
        routes=a.get("routes"),
        tun_address=a.get("tun_address", "198.18.0.1/15"),
        interface=a.get("interface", ""),
        mtu=int(a.get("mtu", 1500)),
        udp_timeout_seconds=int(a.get("udp_timeout_seconds", 300)),
    ),
    "stop_tun": lambda c, a: tool_stop_tun(c, a["session_ref"]),
    "list_tun": lambda c, a: tool_list_tun(c),
    "queue_persistent": lambda c, a: tool_queue_persistent(
        c, a["session_ref"], a["command"]
    ),
    "stop_persistent": lambda c, a: tool_stop_persistent(c, a["session_ref"]),
    "list_history": lambda c, a: tool_list_history(c),
    "get_history_session": lambda c, a: tool_get_history_session(c, a["session_ref"]),
    "get_history_events": lambda c, a: tool_get_history_events(
        c, a["session_ref"], int(a.get("since", 0)), int(a.get("limit", 500)), a.get("forward", False)
    ),
    "delete_history": lambda c, a: tool_delete_history(c, a["session_ref"]),
    "clear_history": lambda c, a: tool_clear_history(c),
    "list_session_downloads": lambda c, a: tool_list_session_downloads(c, a["session_ref"]),
    "list_artifacts": lambda c, a: tool_list_artifacts(c, a.get("kind", "downloads")),
    "get_agent_script": lambda c, a: tool_get_agent_script(c),
    "deploy_agent_script": lambda c, a: tool_deploy_agent_script(
        c,
        a.get("session_id", ""),
        int(a.get("sleep_seconds", 60)),
        int(a.get("jitter_percent", 30)),
        a.get("http_proxy", ""),
        a.get("workspace_id", ""),
    ),
    "build_go_agent": lambda c, a: tool_build_go_agent(
        c,
        a.get("goos", "windows"),
        a.get("goarch", "amd64"),
        a.get("session_id", ""),
        int(a.get("sleep_seconds", 60)),
        int(a.get("jitter_percent", 30)),
        a.get("http_proxy", ""),
        a.get("workspace_id", ""),
        bool(a.get("stageless", False)),
        a.get("debug", False),
    ),
    "stage_status": lambda c, a: tool_stage_status(c, a["staging_id"]),
    "queue_keylog": lambda c, a: tool_queue_keylog(c, a["session_ref"], a["action"]),
    "install_persistence": lambda c, a: tool_install_persistence(c, a["session_ref"]),
    "remove_persistence": lambda c, a: tool_remove_persistence(c, a["session_ref"]),
    "spawn_session": lambda c, a: tool_spawn_session(
        c,
        a["session_ref"],
        bool(a.get("verify", True)),
        float(a.get("spawn_timeout", 120)),
        float(a.get("register_timeout", 60)),
        float(a.get("verify_timeout", 120)),
    ),
    "make_token": lambda c, a: tool_make_token(
        c,
        a["session_ref"],
        a.get("user", ""),
        a.get("domain", ""),
        a.get("password", ""),
        float(a.get("timeout", 120)),
        cred_id=a.get("cred_id", ""),
    ),
    "rev2self": lambda c, a: tool_rev2self(
        c,
        a["session_ref"],
        float(a.get("timeout", 120)),
    ),
    "lateral_move": lambda c, a: tool_lateral_move(
        c,
        a["session_ref"],
        a["method"],
        a["host"],
        bool(a.get("verify", True)),
        float(a.get("exec_timeout", 300)),
        float(a.get("register_timeout", 60)),
        float(a.get("verify_timeout", 120)),
    ),
    "session_uid": lambda c, a: tool_session_uid(
        c,
        a["session_ref"],
        float(a.get("timeout", 120)),
    ),
    "creds_add": lambda c, a: tool_creds_add(
        c,
        domain=a.get("domain", ""),
        user=a.get("user", ""),
        password=a.get("password", ""),
    ),
    "creds_del": lambda c, a: tool_creds_del(c, a["cred_id"]),
    "creds_list": lambda c, a: tool_creds_list(c),
    "spawnas": lambda c, a: tool_spawnas(
        c,
        a["session_ref"],
        cred_id=a.get("cred_id", ""),
        user=a.get("user", ""),
        domain=a.get("domain", ""),
        password=a.get("password", ""),
        verify=bool(a.get("verify", True)),
        spawn_timeout=float(a.get("spawn_timeout", 120)),
        register_timeout=float(a.get("register_timeout", 60)),
        verify_timeout=float(a.get("verify_timeout", 120)),
    ),
    "runas": lambda c, a: tool_runas(
        c,
        a["session_ref"],
        a["command"],
        cred_id=a.get("cred_id", ""),
        user=a.get("user", ""),
        domain=a.get("domain", ""),
        password=a.get("password", ""),
        timeout=float(a.get("timeout", 120)),
    ),
}


def execute_tool(client: RmmApiClient, name: str, arguments: dict | str) -> str:
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments.strip() else {}
    if name in {"spawn_session", "spawnas", "lateral_move"} and "debug" in arguments:
        return _json_result({"ok": False, "error": "debug_is_build_time_only",
                             "detail": "Select debug when building the agent, not per deployment."})
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return _json_result({"ok": False, "error": f"unknown_tool: {name}"})
    try:
        return handler(client, arguments)
    except Exception as e:
        return _json_result({"ok": False, "error": str(e)})


OPENAI_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bloodhound",
            "description": "Operate BloodHound CE at its storage owner. library_search searches the full reference library with options {text, platform, category, offset, limit} (limit 1-25); library_get fetches one original query with options {id: GUID}. Reference queries are never directly executable; use mapped local template IDs with query. queries lists executable templates and deferred blockers. health reads readiness; ensure starts the deterministic MCP check/startup pass without replaying uncertain launches; ui ensures readiness then returns a proxy launch link. If ui/health/ensure report state 'uncertain' (a prior launch's reply was lost), reset clears that stuck dispatch so a startup can be retried; it refuses while a startup pass is running or once BloodHound is already ready. A launch that is confirmed to have died immediately reports bloodhound_launch_failed with the bounded launch_exit_code instead, and can be retried with ensure directly. auth bootstraps the CE API credentials automatically from the dedicated workspace's initial admin password and mints a dedicated token into owner-local secret settings; it reports auth 'already_configured' when stored credentials exist and a bounded failure code otherwise. reset_ce DESTRUCTIVELY clears CE's own database (accounts, API tokens, upload jobs) after an explicit operator decision, with options {clear_graph: boolean} to also wipe the collected neo4j graph; stored credentials are invalidated and the next fresh launch re-bootstraps them automatically. Other actions: preflight, create, collect, import, status, verify, query, report. If workspace_request_id is returned, poll result with that ID as run_id instead of resubmitting. Poll health for startup; status for collection/import. Credentials belong in service settings.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["preflight", "create", "collect", "import", "status", "verify", "query", "queries", "report", "result", "ui", "health", "ensure", "reset", "auth", "reset_ce", "library_search", "library_get"]},
                "run_id": {"type": "string"},
                "options": {"type": "object"},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_ai_jobs",
            "description": "List persisted AI jobs, optionally filtered by full session ID. Does not start or retry work.",
            "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ai_job",
            "description": "Read an AI job's status, progress, and final result by ID. An interrupted job may have executed actions; do not blindly resubmit it.",
            "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "health",
            "description": "Check RMM server API health.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "List all active RMM agent sessions (id, hostname, user, beacon status).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session",
            "description": "Get session details by session id prefix, full id, or hostname.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {
                        "type": "string",
                        "description": "Session UUID prefix, full id, or hostname (e.g. CFRANCHETZIA110)",
                    }
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "Run a command on the agent and wait for output (blocking, up to timeout seconds).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "description": "Max wait seconds (default 120)"},
                },
                "required": ["session_ref", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_command",
            "description": "Queue a command for the next agent beacon (non-blocking).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "command": {"type": "string"},
                    "cmd_type": {
                        "type": "string",
                        "enum": ["oneshot", "persistent"],
                        "description": "oneshot (default) or persistent",
                    },
                },
                "required": ["session_ref", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_config",
            "description": "Update agent beacon sleep interval and/or jitter percent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "sleep_seconds": {"type": "integer"},
                    "jitter_percent": {"type": "integer"},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": "Fetch result/operator events. Embedding output omits vectors while retaining classification scores, copy paths, errors, and dimensions. Full vectors remain in workspace evidence/export.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "forward": {"type": "boolean", "description": "Read earliest events after since; advance since to the last returned ID for complete pagination."},
                    "since": {"type": "integer", "description": "Event id cursor"},
                    "limit": {"type": "integer"},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_session",
            "description": "Terminate an agent session (client will exit on next beacon).",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_download",
            "description": "Queue download of a remote file from the agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "remote_path": {"type": "string"},
                },
                "required": ["session_ref", "remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_exfil",
            "description": "Queue upload of a remote agent file or folder via rclone (agent-side; link in events for files).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "remote_path": {"type": "string"},
                    "profile": {"type": "string", "description": "Named rclone profile on server"},
                    "dest": {"type": "string", "description": "Optional cloud destination path"},
                    "mode": {"type": "string", "enum": ["auto", "plugin", "binary"], "description": "Execution mode; auto prefers the in-process Go plugin and falls back to the binary"},
                },
                "required": ["session_ref", "remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rclone_config",
            "description": "Show rclone profiles and binary status on the RMM server.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_screenshot",
            "description": "Queue a screenshot capture on the agent.",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_plugins",
            "description": "List PE plugins available on the RMM server for the Go agent.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_plugin_release",
            "description": "Inspect compiled GitHub plugins and resolve latest to an exact release tag before installation.",
            "parameters": {"type": "object", "properties": {"tag": {"type": "string", "description": "latest or an exact published plugin tag"}, "source": {"type": "string", "description": "Saved repository/branch source ID; default if omitted"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_plugin_release",
            "description": "Install one checksum-verified plugin into the current server/workspace asset store without executing it. Use an exact tag from list_plugin_release.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "tag": {"type": "string", "description": "Exact published plugin release tag; latest is not accepted"},
                    "sha256": {"type": "string", "description": "Optional expected digest from the selected release"},
                    "source": {"type": "string", "description": "Saved repository/branch source ID; default if omitted"},
                    "source_revision": {"type": "string", "description": "Optional source configuration revision returned by release browsing"},
                },
                "required": ["name", "tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_plugin_sources",
            "description": "List saved plugin repositories, branches, and credential references in the active server/workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_plugin_source",
            "description": "Add or update a persisted plugin repository/branch source. credential_env is an environment variable name, never a secret token.",
            "parameters": {"type": "object", "properties": {
                "source_id": {"type": "string"}, "repository": {"type": "string"},
                "branch": {"type": "string"}, "label": {"type": "string"},
                "credential_env": {"type": "string", "description": "RMM_PLUGIN_GITHUB_TOKEN or RMM_PLUGIN_TOKEN_<NAME>"},
            }, "required": ["source_id", "repository"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_plugin_source",
            "description": "Remove a saved plugin source without deleting installed plugins.",
            "parameters": {"type": "object", "properties": {"source_id": {"type": "string"}}, "required": ["source_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_assets",
            "description": "List URL-installed plugins, AI skills, and rclone profile files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["plugins", "skills", "rclone"]}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_asset",
            "description": "Download and install a plugin, AI skill, or rclone profile file from an HTTP(S) URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["plugins", "skills", "rclone"]},
                    "url": {"type": "string"},
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["kind", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_asset_enabled",
            "description": "Enable or disable one managed plugin, AI skill, or rclone asset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["plugins", "skills", "rclone"]},
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["kind", "name", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_asset",
            "description": "Remove one managed operator asset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["plugins", "skills", "rclone"]},
                    "name": {"type": "string"},
                },
                "required": ["kind", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "profile_status",
            "description": "Show the active RMM-Profile and the beacon URL used by agents.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_profile",
            "description": "Start an installed RMM-Profile connector such as cloudflared.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "default": "cloudflared"},
                               "config": {"type": "object", "description": "Optional one-run configuration; omit to use configured service defaults."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_profile",
            "description": "Stop the active RMM-Profile connector.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_pe_load",
            "description": (
                "Queue a PE plugin load on the Go agent. The plugin runs in an "
                "isolated child; DLLs requiring the Windows loader use a temporary file. Use "
                "export=Run for raw input. Set command only for plugins that explicitly support "
                "the {command,input} dispatch envelope; command=Run is not equivalent to export=Run. "
                "Use run_embeddings for document classification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "plugin": {"type": "string"},
                    "export": {"type": "string", "description": "Plugin export name (default Run)"},
                    "command": {
                        "type": "string",
                        "description": (
                            "Optional plugin-owned command. When set, the agent invokes "
                            "Run and the plugin dispatches this command internally."
                        ),
                    },
                    "input": {"type": "string", "description": "Optional input passed to the export"},
                },
                "required": ["session_ref", "plugin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_upload",
            "description": "Queue a local file or artifact_ref upload. Success means queued, not delivered. Record the get_events cursor before queuing, then wait for a new successful upload result matching this session and destination before copying, extracting, or executing. Missing results are inconclusive; do not blindly retry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "local_path": {"type": "string"},
                    "remote_path": {"type": "string"},
                },
                "required": ["session_ref", "local_path", "remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_socks",
            "description": "List all active SOCKS5 relays on the RMM server and which agent each uses.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_socks",
            "description": "Start SOCKS5 relay on the RMM server (traffic exits via agent).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "port": {"type": "integer", "description": "Default 1080"},
                    "bind_host": {"type": "string", "description": "Default 127.0.0.1"},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_socks",
            "description": "Stop SOCKS5 relay for the session.",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tun",
            "description": "List active RMM-owned TUN relays configured on the server.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_tun",
            "description": "Create a server-side TUN relay whose traffic exits through a Go agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "device": {"type": "string", "description": "Server TUN device, for example tun://rmm0 or utun4"},
                    "route": {"type": "string", "description": "Optional IPv4 CIDR routed through the agent"},
                    "routes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional IPv4 CIDRs routed through the agent; use this for multiple networks",
                    },
                    "tun_address": {"type": "string", "description": "IPv4 address/prefix assigned to the TUN"},
                    "interface": {"type": "string", "description": "Physical egress interface for the helper"},
                    "mtu": {"type": "integer", "description": "TUN MTU"},
                    "udp_timeout_seconds": {"type": "integer", "description": "UDP flow idle timeout"},
                },
                "required": ["session_ref", "device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_tun",
            "description": "Disable the server-side TUN relay for a session.",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_persistent",
            "description": "Set a persistent command on the agent (until stop_persistent).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "command": {"type": "string"},
                },
                "required": ["session_ref", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_persistent",
            "description": "Stop the agent persistent command (__STOP__).",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_history",
            "description": "List archived (ended) session transcripts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history_session",
            "description": "Get metadata for an archived session by id prefix, full id, or hostname.",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history_events",
            "description": "Fetch read-only event transcript for an archived session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "forward": {"type": "boolean", "description": "Read earliest events after since; advance since to the last returned ID for complete pagination."},
                    "since": {"type": "integer", "description": "Event id cursor"},
                    "limit": {"type": "integer", "description": "Max events (default 500)"},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_history",
            "description": "Permanently delete an archived session transcript from disk (ended sessions only).",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_history",
            "description": "Permanently delete all archived (ended) session transcripts from disk.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_agent_script",
            "description": "Render a ready-to-run client_rmm.ps1 server-side, configured with the active beacon URL and beacon secret. Returns a portable artifact_ref for queue_upload; the secret is never returned.",
            "parameters": {"type": "object", "properties": {
                "workspace_id": {"type": "string", "description": "Administrator-selected configured owner; workspace callers inherit their authenticated workspace"},
                "session_id": {"type": "string", "description": "Optional fixed session ID; default is generated at first run"},
                "sleep_seconds": {"type": "integer", "description": "Beacon sleep in seconds (1-3600, default 60)"},
                "jitter_percent": {"type": "integer", "description": "Beacon jitter percent (0-100, default 30)"},
                "http_proxy": {"type": "string", "description": "Optional HTTP proxy URL used by the deployed agent"},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_go_agent",
            "description": "Build the repository Go agent on the server with the active beacon URL and secret embedded. Returns the portable artifact_ref, server-local path, binary name, size, and SHA-256 for queue_upload.",
            "parameters": {"type": "object", "properties": {
                "goos": {"type": "string", "enum": ["windows"], "description": "Target OS (default windows)"},
                "goarch": {"type": "string", "enum": ["amd64", "arm64"], "description": "Target architecture (default amd64)"},
                "workspace_id": {"type": "string", "description": "Administrator-selected configured owner; workspace callers inherit their authenticated workspace"},
                "session_id": {"type": "string", "description": "Optional fixed session ID; default is generated at first run"},
                "sleep_seconds": {"type": "integer", "description": "Beacon sleep in seconds (default 60)"},
                "jitter_percent": {"type": "integer", "description": "Beacon jitter percent (default 30)"},
                "http_proxy": {"type": "string", "description": "Optional HTTP proxy URL used by the deployed agent"},
                "stageless": {"type": "boolean", "description": "false (default): return a small stage1 ZIP (~20 KB C launcher) that fetches the Go agent from the server at runtime. true: return a ZIP with the full Go agent binary embedded directly (no staging server required, larger file on target)."},
                "debug": {"type": "boolean", "default": False, "description": "Build the main agent with the rmmdebug build tag in a full-agent ZIP. Spawn, spawnas, and lateral descendants inherit tracing automatically. Normal builds exclude diagnostic implementation."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stage_status",
            "description": "Read the stage1 launcher status recorded on the server for a staged Go agent build (staging_id comes from build_go_agent): fetch state, expiry, and the last status line posted by stage1 (for example 'stage1 ok: agent installed and started' or 'stage1 failed: ...').",
            "parameters": {
                "type": "object",
                "properties": {"staging_id": {"type": "string", "description": "Staging ID returned by the staged build"}},
                "required": ["staging_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_session_downloads",
            "description": "List files downloaded from the agent for a live session (artifact names and URLs).",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_artifacts",
            "description": "List downloads or screenshots from every agent with originating agent metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["downloads", "screenshots"],
                        "description": "Artifact collection to list (default downloads).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_script",
            "description": "Fetch the full client_rmm.ps1 agent script from the server (large payload).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_keylog",
            "description": "Queue keylogger start, stop, or dump on the agent (__KEYLOG__).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "dump"],
                    },
                },
                "required": ["session_ref", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_persistence",
            "description": "Queue agent persistence install (__INSTALL_PERSIST__). Lab use only.",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_persistence",
            "description": "Queue agent persistence removal (__REMOVE_PERSIST__). Lab use only.",
            "parameters": {
                "type": "object",
                "properties": {"session_ref": {"type": "string"}},
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_session",
            "description": "Spawn a detached second Go agent from a live Go agent session. The agent-side command starts a separate process with independent standard streams and a fresh session ID, then returns promptly so the parent beacon loop keeps polling. verify=false reports acceptance only (ok=false, pending=true). Waits for the spawned session to register without sending additional verification commands; report verified=false instead of resubmitting. PowerShell sessions are rejected (spawn requires a Go agent session).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "verify": {"type": "boolean", "default": True},
                    "spawn_timeout": {"type": "number", "default": 120},
                    "register_timeout": {"type": "number", "default": 60},
                    "verify_timeout": {"type": "number", "default": 120},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_token",
            "description": "Create a runas /netonly style network identity on a live Go agent session: subsequent spawned commands keep the agent's local identity but authenticate on the network as DOMAIN\\user. Pass user as DOMAIN\\user or split domain and user. LogonUser does not validate the credentials, so success does not prove the password is correct; test with a network command. NEVER repeat or log the password in responses; report only the identity label and agent output. PowerShell sessions are rejected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "cred_id": {"type": "string", "description": "Workspace credential ID; alternative to user/password"},
                    "user": {"type": "string", "description": "Identity as DOMAIN\\user or plain user"},
                    "domain": {"type": "string", "default": ""},
                    "password": {"type": "string", "description": "Plaintext password; never echo it back"},
                    "timeout": {"type": "number", "default": 120},
                },
                "required": ["session_ref"],
                "anyOf": [{"required": ["cred_id"]}, {"required": ["user", "password"]}],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rev2self",
            "description": "Revert a Go agent session to its own network identity after make_token. Idempotent: reverting without an active token is a success. PowerShell sessions are rejected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "timeout": {"type": "number", "default": 120},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lateral_move",
            "description": "Start a Go agent on another host through a live Go agent session, using psexec, wmiexec, or winrm. The agent stages its own binary on the target's ADMIN$ share and starts it with a fresh session ID; make_token first to use alternate credentials. The call waits for the target session to register without sending additional verification commands; report verified=false instead of resubmitting. Requires an agent binary built with embedded connection settings and admin rights on the target.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "method": {"type": "string", "enum": ["psexec", "wmiexec", "winrm"]},
                    "host": {"type": "string", "description": "Target hostname or IP address"},
                    "verify": {"type": "boolean", "default": True},
                    "exec_timeout": {"type": "number", "default": 300},
                    "register_timeout": {"type": "number", "default": 60},
                    "verify_timeout": {"type": "number", "default": 120},
                },
                "required": ["session_ref", "method", "host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_uid",
            "description": "Show the identity a Go agent runs under (process user) plus the active make_token network identity, if any. Use it to verify make_token and rev2self. PowerShell sessions are rejected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "timeout": {"type": "number", "default": 120},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "creds_add",
            "description": "Add credentials to the workspace store. Returns a cred_id to pass to make_token, spawnas, or runas. Passwords are stored server-side only and never returned. NEVER log passwords in responses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "Username, optionally as DOMAIN\\user"},
                    "domain": {"type": "string", "default": ""},
                    "password": {"type": "string", "description": "Plaintext password; never echo"},
                },
                "required": ["user", "password"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "creds_del",
            "description": "Remove a credential entry from the workspace store by cred_id.",
            "parameters": {
                "type": "object",
                "properties": {"cred_id": {"type": "string"}},
                "required": ["cred_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "creds_list",
            "description": "List stored workspace credentials (identity labels and cred_ids only, no passwords).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawnas",
            "description": "Spawn a new agent copy on the same host under a different process identity using CreateProcessWithLogonW. Children inherit source build tracing without a debug flag; normal sources do not trace by default. Diagnostic events are sanitized; raw runtime crash output may contain sensitive data. Pass cred_id from creds_add, or domain/user/password directly. NEVER repeat credentials in responses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "cred_id": {"type": "string", "description": "Preferred: cred_id from creds_add"},
                    "user": {"type": "string", "default": ""},
                    "domain": {"type": "string", "default": ""},
                    "password": {"type": "string", "default": ""},
                    "verify": {"type": "boolean", "default": True},
                    "spawn_timeout": {"type": "number", "default": 120},
                    "register_timeout": {"type": "number", "default": 60},
                    "verify_timeout": {"type": "number", "default": 120},
                },
                "required": ["session_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "runas",
            "description": "Run a shell command on a Go agent under a different process identity using the cascade. Returns the command output. Pass cred_id from creds_add, or domain/user/password directly. NEVER repeat credentials in responses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
                    "command": {"type": "string"},
                    "cred_id": {"type": "string", "default": ""},
                    "user": {"type": "string", "default": ""},
                    "domain": {"type": "string", "default": ""},
                    "password": {"type": "string", "default": ""},
                    "timeout": {"type": "number", "default": 120},
                },
                "required": ["session_ref", "command"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are an RMM (Remote Monitoring and Management) operator assistant.
You control Windows agents through the Minimal-RMM server API using the provided tools.

Guidelines:
- Use list_sessions first if you do not know which host to target.
- session_ref can be hostname, id prefix (first 8 chars), or full session UUID.
- Use list_history / get_history_events for archived (killed) session transcripts.
- clear_history removes all archived transcripts; delete_history removes one.
- Prefer exec_command when the user wants command output; use queue_command when beacon sleep is long.
- Use spawn_session to start a second Go agent on a host that already runs one; never launch an agent binary through a foreground command on an existing session.
- list_session_downloads shows completed agent→server file pulls; queue_download starts a new pull.
- Be concise; summarize command output for the user.
- Destructive actions (kill_session, delete_history, clear_history) require clear user intent.
"""


# Saved embedding operations share the same API in server and workspace mode.
OPENAI_TOOLS.append({
    "type": "function",
    "function": {
        "name": "settings",
        "description": "Read redacted service settings or atomically save overrides. Use the local workspace API for workspace settings. Updates require process restart; environment variables take precedence. Null removes a saved override. Secret values are never returned.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["show", "update"], "default": "show"},
            "settings": {"type": "object", "description": "Canonical setting keys mapped to typed values; null removes an override."},
        }},
    },
})

OPENAI_TOOLS.extend(
[{'type': 'function',
  'function': {'name': 'embedding_profiles',
               'description': 'List/get/save/delete embedding profiles, or use defaults to read the repository preset. In workspace mode use the '
                              'local workspace API. Config accepts model, auth (none/env), '
                              'prompts, labels, max_tokens, copy_documents, copy_labels, '
                              'copy_threshold; never include tokens.',
               'parameters': {'type': 'object',
                              'properties': {'action': {'type': 'string',
                                                        'enum': ['list', 'get', 'save', 'delete', 'defaults']},
                                             'name': {'type': 'string'},
                                             'config': {'type': 'object'}},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'run_embeddings',
               'description': 'Classify explicit authorized files or directories (including UNC) using a saved profile. '
                              'Requires document-embeddings-autonomous.dll and a Go PE agent. Missing DLL is a deployment blocker: '
                              'do not substitute document-embeddings.dll or retry via queue_pe_load command=Run. '
                              'Inspect the profile first: copy_documents may copy matching files. Queued is not completed; inspect matching session events.',
               'parameters': {'type': 'object',
                              'properties': {'session_ref': {'type': 'string'},
                                             'profile': {'type': 'string'},
                                             'documents': {'type': 'array',
                                                           'items': {'type': 'string'}}},
                              'required': ['session_ref', 'profile', 'documents']}}},
 {'type': 'function',
  'function': {'name': 'embedding_results',
               'description': 'List/get/save/delete saved embedding results. Save requires exact '
                              'session_ref and output event_id. Results omit vectors for context efficiency but retain '
                              'classification scores, copy paths, errors, and dimensions. Full vectors remain in workspace exports.',
               'parameters': {'type': 'object',
                              'properties': {'action': {'type': 'string',
                                                        'enum': ['list', 'get', 'save', 'delete']},
                                             'name': {'type': 'string'},
                                             'session_ref': {'type': 'string'},
                                             'event_id': {'type': 'integer'}},
                              'required': []}}}]
)
