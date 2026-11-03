"""Deterministic owner-local Exegol prerequisite and BloodHound CE lifecycle."""
from __future__ import annotations

import asyncio
import errno
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import threading
import time
from urllib.parse import urlsplit

from rmm_mcp_client import McpHttpSession
from rmm_settings import getenv

EXEC_TOOL = 'execute_command_in_container'
PROBE_INTERVAL = 5
READY_TIMEOUT = 90
MCP_TIMEOUT = 120


class RuntimeErrorCode(ValueError):
    """A public failure code that contains no MCP output or credentials."""


def timestamp():
    """Return a UTC observation timestamp."""
    return datetime.now(timezone.utc).isoformat()


def configuration():
    """Validate fixed owner settings without accepting per-request destinations."""
    url = getenv('RMM_EXEGOL_MCP_URL', '').strip()
    container = getenv('RMM_EXEGOL_CONTAINER', '').strip()
    token = getenv('RMM_EXEGOL_MCP_TOKEN', '').strip() or None
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeErrorCode('exegol_mcp_url_required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container):
        raise RuntimeErrorCode('exegol_container_required')
    ce = urlsplit(getenv('RMM_BLOODHOUND_URL', 'http://127.0.0.1:1030').rstrip('/'))
    if (ce.scheme not in ('http', 'https') or ce.hostname not in ('127.0.0.1', 'localhost', '::1')
            or ce.username or ce.password or ce.path or ce.query or ce.fragment):
        raise RuntimeErrorCode('bloodhound_must_be_workspace_local')
    try:
        _ = parsed.port, ce.port
    except ValueError:
        raise RuntimeErrorCode('invalid_runtime_port') from None
    return url, token, container, ce


def probe_ce(endpoint):
    """Check CE's local version API with a bounded direct, credential-free request."""
    cls = http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection
    connection = cls(endpoint.hostname, endpoint.port, timeout=3)
    try:
        connection.request('GET', '/api/version', headers={'Accept': 'application/json'})
        response = connection.getresponse()
        body = response.read(65537)
        if response.status in (401, 403):
            return 'authentication_failed'
        if response.status != 200:
            return 'unavailable'
        data = json.loads(body).get('data') if len(body) <= 65536 else None
        return 'ready' if isinstance(data, dict) and data.get('server_version') else 'invalid_response'
    except OSError as exc:
        return 'stopped' if exc.errno == errno.ECONNREFUSED else 'unavailable'
    except (ValueError, AttributeError, http.client.HTTPException):
        return 'invalid_response'
    finally:
        connection.close()


def execution_arguments(tools, container, command):
    """Match the installed execution schema; refuse unknown required arguments."""
    matches = [row.get('function', {}) for row in tools
               if row.get('function', {}).get('name') == EXEC_TOOL]
    if len(matches) != 1:
        raise RuntimeErrorCode('exegol_execution_tool_missing')
    schema = matches[0].get('parameters', {})
    properties = schema.get('properties', {})
    selectors = [key for key in ('container_name', 'container') if key in properties]
    if len(selectors) != 1 or properties.get('command', {}).get('type') != 'string':
        raise RuntimeErrorCode('exegol_execution_schema_unsupported')
    selector = selectors[0]
    if properties[selector].get('type') != 'string':
        raise RuntimeErrorCode('exegol_execution_schema_unsupported')
    arguments = {selector: container, 'command': command}
    if set(schema.get('required', [])) - arguments.keys():
        raise RuntimeErrorCode('exegol_execution_schema_unsupported')
    return arguments


def output_strings(value):
    """Read text fields from MCP results without treating output as instructions."""
    if isinstance(value, str):
        yield value
        try:
            decoded = json.loads(value)
        except ValueError:
            return
        if not isinstance(decoded, str):
            yield from output_strings(decoded)
    elif isinstance(value, dict):
        for item in value.values():
            yield from output_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from output_strings(item)


class ExegolRuntime:
    """Single-flight startup with durable uncertainty and read-only health refresh."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.last_probe = 0.0
        self.verified_configuration = None
        self.snapshot = dict(required=True, state='blocked', exegol_status='not_checked',
                             mcp_status='not_checked', bloodhound_status='not_checked',
                             ready=False, exegol_verified_at=None, updated_at=timestamp())

    def _update(self, **values):
        """Publish only bounded, secret-free lifecycle state."""
        with self.lock:
            if self.stopped.is_set():
                values.update(state='blocked', ready=False, exegol_status='stopped',
                              mcp_status='stopped', error='runtime_stopped')
            self.snapshot.update(values, updated_at=timestamp())
            return dict(self.snapshot)

    def _intent(self):
        """Load durable dispatch state; corrupt intent is uncertain, never absent."""
        path = self.root / 'ce-startup.json'
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            return value['state'] if value.get('state') in ('dispatched', 'ready') else 'dispatched'
        except (OSError, ValueError, KeyError, AttributeError):
            return 'dispatched'

    def _save(self, state):
        """Durably record dispatch before sending any startup command."""
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = self.root / ('ce-startup-' + secrets.token_hex(8) + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump({'state': state, 'updated_at': timestamp()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.root / 'ce-startup.json')
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def ensure(self):
        """Schedule one prerequisite/startup pass; reconnects cannot duplicate it."""
        if self.stopped.is_set():
            return self._update(state='blocked', ready=False, error='runtime_stopped')
        current = self.status()
        if current['ready'] or current['state'] == 'uncertain':
            return current
        with self.lock:
            if self.stopped.is_set():
                return self._update(state='blocked', ready=False, error='runtime_stopped')
            if self.thread is not None and self.thread.is_alive():
                return dict(self.snapshot)
            self._update(state='checking', exegol_status='checking', ready=False, error=None)
            self.thread = threading.Thread(target=self._run, name='exegol-ce-lifecycle', daemon=True)
            self.thread.start()
            return dict(self.snapshot)

    start = ensure

    def status(self):
        """Refresh CE availability without invoking MCP commands or starting services."""
        with self.lock:
            if self.stopped.is_set():
                return self._update(state='blocked', ready=False, error='runtime_stopped')
            if self.verified_configuration is not None:
                try:
                    unchanged = configuration() == self.verified_configuration
                except (RuntimeErrorCode, ValueError):
                    unchanged = False
                if not unchanged:
                    return self._update(state='blocked', ready=False, exegol_status='not_checked',
                                        mcp_status='not_checked', bloodhound_status='not_checked',
                                        exegol_verified_at=None, error='runtime_configuration_changed')
            if self.thread is not None and self.thread.is_alive():
                return dict(self.snapshot)
            if self.snapshot['exegol_status'] != 'ready' or time.monotonic() - self.last_probe < PROBE_INTERVAL:
                return dict(self.snapshot)
            self.last_probe = time.monotonic()
        try:
            endpoint = configuration()[3]
            availability = probe_ce(endpoint)
            with self.lock:
                if self.verified_configuration is not None and configuration() != self.verified_configuration:
                    return self._update(state='blocked', ready=False, exegol_status='not_checked',
                                        mcp_status='not_checked', exegol_verified_at=None,
                                        error='runtime_configuration_changed')
                if self.thread is not None and self.thread.is_alive():
                    return dict(self.snapshot)
                if self.stopped.is_set():
                    return self._update(state='blocked', ready=False, error='runtime_stopped')
                if availability == 'ready':
                    self._save('ready')
                    return self._update(state='ready', ready=True, bloodhound_status='ready', error=None)
                uncertain = self._intent() == 'dispatched'
                return self._update(state='uncertain' if uncertain else 'failed', ready=False,
                                    bloodhound_status=availability, error='bloodhound_startup_uncertain' if uncertain else 'bloodhound_' + availability)
        except (RuntimeErrorCode, OSError):
            return self._update(state='failed', ready=False, error='runtime_health_failed')

    def stop(self):
        """Cancel further startup steps without stopping CE or resetting its data."""
        self.stopped.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._update(state='blocked', ready=False, error='runtime_stopped')

    def _run(self):
        """Bound the MCP lifecycle and turn failures into observable safe states."""
        try:
            asyncio.run(asyncio.wait_for(self._check_and_start(), timeout=MCP_TIMEOUT))
        except RuntimeErrorCode as exc:
            self._update(state='blocked', exegol_status='unavailable', ready=False, error=str(exc))
        except Exception:
            uncertain = self._intent() == 'dispatched'
            values = dict(state='uncertain' if uncertain else 'failed', ready=False,
                          error='bloodhound_startup_uncertain' if uncertain else 'exegol_runtime_failed')
            if self.snapshot['exegol_status'] != 'ready':
                values.update(exegol_status='unavailable', mcp_status='error')
            self._update(**values)
        finally:
            if self.stopped.is_set():
                self._update(state='blocked', ready=False, error='runtime_stopped')

    async def _check_and_start(self):
        """Verify the selected local Exegol namespace and start only its installed CE."""
        url, token, container, endpoint = configuration()
        try:
            namespace = os.readlink('/proc/self/ns/net')
        except OSError:
            raise RuntimeErrorCode('service_must_run_inside_exegol') from None
        hostname = socket.gethostname()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        nonce = secrets.token_hex(24)
        identity_path = self.root / ('ce-identity-' + secrets.token_hex(8))
        fd = os.open(identity_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(nonce)
        expected = 'RMM_EXEGOL_ID=' + nonce + ':' + hostname + ':' + namespace
        command = ("printf 'RMM_EXEGOL_ID=%s:%s:%s\\n' \"$(cat " + shlex.quote(str(identity_path))
                   + ")\" \"$(hostname)\" \"$(readlink /proc/self/ns/net)\"; "
                   "command -v bloodhound-ce >/dev/null 2>&1 && printf 'RMM_CE_LAUNCHER_READY\\n'")
        try:
            self._update(mcp_status='connecting')
            async with McpHttpSession(url, token) as session:
                self._update(mcp_status='connected')
                arguments = execution_arguments(session.openai_tools, container, command)
                result = await session.call_tool(EXEC_TOOL, arguments)
                lines = [line.strip() for text in output_strings(result) for line in text.splitlines()]
                if expected not in lines:
                    raise RuntimeErrorCode('exegol_workspace_identity_mismatch')
                if configuration() != (url, token, container, endpoint):
                    raise RuntimeErrorCode('runtime_configuration_changed')
                self.verified_configuration = (url, token, container, endpoint)
                self._update(exegol_status='ready', exegol_verified_at=timestamp())
                if self.stopped.is_set():
                    return
                availability = await asyncio.to_thread(probe_ce, endpoint)
                if configuration() != (url, token, container, endpoint):
                    raise RuntimeErrorCode('runtime_configuration_changed')
                if availability == 'ready':
                    self._save('ready')
                    self._update(state='ready', ready=True, bloodhound_status='ready', error=None)
                    return
                if availability != 'stopped':
                    self._update(state='failed', ready=False, bloodhound_status=availability, error='bloodhound_' + availability)
                    return
                if 'RMM_CE_LAUNCHER_READY' not in lines:
                    self._update(state='failed', ready=False, bloodhound_status='unavailable', error='bloodhound_launcher_missing')
                    return
                if self._intent() == 'dispatched':
                    self._update(state='uncertain', ready=False, bloodhound_status='unavailable', error='bloodhound_startup_uncertain')
                    return
                arguments = execution_arguments(session.openai_tools, container, 'bloodhound-ce')
                if self.stopped.is_set():
                    return
                if configuration() != (url, token, container, endpoint):
                    raise RuntimeErrorCode('runtime_configuration_changed')
                self._save('dispatched')
                self._update(state='starting', bloodhound_status='starting')
                try:
                    await asyncio.wait_for(session.call_tool(EXEC_TOOL, arguments), timeout=30)
                except Exception:
                    # A lost reply does not mean the service failed to start.
                    pass
                deadline = time.monotonic() + READY_TIMEOUT
                while not self.stopped.is_set() and time.monotonic() < deadline:
                    if await asyncio.to_thread(probe_ce, endpoint) == 'ready':
                        if configuration() != (url, token, container, endpoint):
                            raise RuntimeErrorCode('runtime_configuration_changed')
                        self._save('ready')
                        self._update(state='ready', ready=True, bloodhound_status='ready', error=None)
                        return
                    await asyncio.sleep(2)
                self._update(state='uncertain', ready=False, bloodhound_status='unavailable', error='bloodhound_startup_uncertain')
        finally:
            identity_path.unlink(missing_ok=True)
            with self.lock:
                if self.snapshot['mcp_status'] == 'connected' and self.snapshot['exegol_status'] == 'ready':
                    self._update(mcp_status='verified')


_RUNTIMES = {}
_LOCK = threading.Lock()


def get_runtime(root):
    """Return the process-local lifecycle owner for one storage directory."""
    key = str(Path(root).resolve())
    with _LOCK:
        if key not in _RUNTIMES:
            _RUNTIMES[key] = ExegolRuntime(key)
        return _RUNTIMES[key]
