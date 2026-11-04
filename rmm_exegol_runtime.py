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
LIST_TOOL = 'list_exegol_containers'
MAX_CONTAINERS = 32
PROBE_INTERVAL = 5
# The Exegol bloodhound-ce wrapper itself budgets up to 60 seconds just
# waiting for neo4j, on top of starting postgresql and booting the CE API
# (longer on a first run with fresh database migrations). A cold start can
# therefore legitimately exceed the readiness window below; the durable
# intent plus read-only health reconciliation cover that case.
READY_TIMEOUT = 240
MCP_TIMEOUT = 300
# bloodhound-ce runs in the foreground and never exits on its own. The exec
# tool call must return as soon as the process is detached, or it blocks for
# the lifetime of the server and the dispatch is mistaken for a lost reply;
# run it fully detached from the exec session so it survives that session
# ending instead of being killed with it. The command also keeps the
# wrapper's own output in a root-only log inside the container (dev/null
# hid startup failures such as a neo4j or postgresql error) and reports a
# bounded marker back through the exec reply: RUNNING once the detached
# process survived its first seconds, or EXITED:<code> with the wrapper's
# exit status when it died immediately, so the runtime can distinguish a
# confirmed dead launch from a genuinely lost reply.
LAUNCH_COMMAND = ("umask 077; setsid nohup bloodhound-ce >>/var/log/rmm-bloodhound-ce.log"
                  " 2>&1 </dev/null & _rmm_ce_pid=$!; sleep 2; "
                  "if kill -0 $_rmm_ce_pid 2>/dev/null; then printf 'RMM_CE_LAUNCH_RUNNING\\n'; "
                  "else wait $_rmm_ce_pid; printf 'RMM_CE_LAUNCH_EXITED:%d\\n' $?; fi; disown")
LAUNCH_RUNNING_MARKER = 'RMM_CE_LAUNCH_RUNNING'
LAUNCH_EXITED_MARKER = re.compile(r'^RMM_CE_LAUNCH_EXITED:(\d{1,3})$')


class RuntimeErrorCode(ValueError):
    """A public failure code that contains no MCP output or credentials."""


def timestamp():
    """Return a UTC observation timestamp."""
    return datetime.now(timezone.utc).isoformat()


def append_log(root, message):
    """Append one secret-free lifecycle line to '<root>/runtime.log'.

    Best-effort: a logging failure must never block startup, health checks,
    or UI access. The file never receives MCP output, URLs, or credentials —
    only the bounded state fields already safe for the operator API response.
    """
    try:
        directory = Path(root)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        line = f"{timestamp()} {message}\n".encode()
        fd = os.open(directory / 'runtime.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def runtime_failure_code(error):
    """Classify nested MCP failures without exposing URLs, tokens, or exception text."""
    pending, seen, codes = [error], set(), set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, RuntimeErrorCode):
            return str(current)
        # Python 3.10 uses the exceptiongroup backport via AnyIO; do not depend
        # on the Python 3.11 BaseExceptionGroup builtin for failure reporting.
        nested = getattr(current, 'exceptions', ())
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
        status = getattr(getattr(current, 'response', None), 'status_code', None)
        if status in (401, 403):
            codes.add('exegol_mcp_authentication_failed')
        elif isinstance(status, int) and status >= 400:
            codes.add('exegol_mcp_http_error')
        if isinstance(current, ImportError):
            codes.add('exegol_mcp_dependency_missing')
        elif isinstance(current, TimeoutError) or type(current).__name__ in ('ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout'):
            codes.add('exegol_mcp_timeout')
        elif isinstance(current, ConnectionError) or type(current).__name__ == 'ConnectError':
            codes.add('exegol_mcp_connection_failed')
    for code in ('exegol_mcp_dependency_missing', 'exegol_mcp_authentication_failed',
                 'exegol_mcp_http_error', 'exegol_mcp_connection_failed', 'exegol_mcp_timeout'):
        if code in codes:
            return code
    return 'exegol_runtime_failed'


def configuration():
    """Validate fixed owner settings without accepting per-request destinations."""
    url = getenv('RMM_EXEGOL_MCP_URL', '').strip()
    container = getenv('RMM_EXEGOL_CONTAINER', '').strip()
    token = getenv('RMM_EXEGOL_MCP_TOKEN', '').strip() or None
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeErrorCode('exegol_mcp_url_required')
    if container and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container):
        raise RuntimeErrorCode('invalid_exegol_container')
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


def ce_started(availability):
    """Classify one probe result as 'the CE API daemon is up and answering'.

    Recent BloodHound CE versions answer 401 to the credential-free
    /api/version probe even once the server has started successfully, so an
    authentication demand proves startup the same way a 200 does. The CE
    login itself happens in the operator's browser through the UI proxy with
    normal CE credentials; the workflow API uses its own configured token.
    Any other answered-but-unhealthy result keeps its failure diagnosis.
    """
    return availability in ('ready', 'authentication_failed')


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


def discovery_candidates(value):
    """Parse official ContainerInfo JSON, including FastMCP text-block sequences."""
    if not isinstance(value, str) or len(value.encode()) > 1048576:
        raise RuntimeErrorCode('exegol_discovery_response_invalid')
    decoder = json.JSONDecoder()
    documents = []
    try:
        remaining = value.strip()
        while remaining:
            document, end = decoder.raw_decode(remaining)
            documents.append(document)
            remaining = remaining[end:].lstrip()
    except ValueError:
        raise RuntimeErrorCode('exegol_discovery_response_invalid') from None
    if len(documents) == 1 and isinstance(documents[0], list):
        rows = documents[0]
    elif len(documents) == 1 and isinstance(documents[0], dict) and set(documents[0]) == {'result'}:
        rows = documents[0]['result']
    else:
        rows = documents
    if not isinstance(rows, list) or len(rows) > MAX_CONTAINERS:
        raise RuntimeErrorCode('exegol_discovery_response_invalid')
    names, running = set(), []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', row['name'])
                or not isinstance(row.get('status'), str) or row['name'] in names):
            raise RuntimeErrorCode('exegol_discovery_response_invalid')
        names.add(row['name'])
        if row['status'] == 'running':
            running.append(row['name'])
    return running


async def discover_container(session, command, expected, root=None):
    """Select exactly one running container proving the owner identity; never start candidates."""
    matches = [row.get('function', {}) for row in session.openai_tools
               if row.get('function', {}).get('name') == LIST_TOOL]
    if len(matches) != 1 or matches[0].get('parameters', {}).get('required', []):
        raise RuntimeErrorCode('exegol_discovery_tool_unsupported')
    candidates = discovery_candidates(await asyncio.wait_for(session.call_tool(LIST_TOOL, {}), timeout=10))
    verified = []
    for name in candidates:
        arguments = execution_arguments(session.openai_tools, name, command)
        result = await asyncio.wait_for(session.call_tool(EXEC_TOOL, arguments), timeout=10)
        lines = [line.strip() for text in output_strings(result) for line in text.splitlines()]
        if expected in lines:
            verified.append((name, lines))
    if root:
        # Container names only (already validated, logged elsewhere once identity is
        # confirmed); this line is what makes a live ambiguous/not-found failure
        # diagnosable without rerunning with extra instrumentation.
        append_log(root, f"discovery: {len(candidates)} running candidate(s) ({', '.join(candidates) or 'none'}), "
                          f"{len(verified)} matched identity")
    if len(verified) != 1:
        raise RuntimeErrorCode('exegol_workspace_identity_ambiguous' if verified else 'exegol_workspace_not_found')
    return verified[0]


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
            previous = dict(self.snapshot)
            self.snapshot.update(values, updated_at=timestamp())
            if any(self.snapshot.get(k) != previous.get(k) for k in ('state', 'container', 'error')):
                self._log_transition(previous)
            return dict(self.snapshot)

    def _log_transition(self, previous):
        """Record one line per meaningful lifecycle change (state, container, or error)."""
        parts = [f"state={previous.get('state')}->{self.snapshot.get('state')}"]
        if self.snapshot.get('container') and self.snapshot.get('container') != previous.get('container'):
            parts.append(f"container={self.snapshot['container']}")
        if self.snapshot.get('error'):
            parts.append(f"error={self.snapshot['error']}")
        if self.snapshot.get('launch_exit_code') is not None:
            parts.append(f"launch_exit_code={self.snapshot['launch_exit_code']}")
        append_log(self.root, ' '.join(parts))

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
            self._update(state='checking', exegol_status='checking', ready=False, error=None, container=None, launch_exit_code=None)
            self.thread = threading.Thread(target=self._run, name='exegol-ce-lifecycle', daemon=True)
            self.thread.start()
            return dict(self.snapshot)

    start = ensure

    def reset(self):
        """Clear a durably stuck 'uncertain' dispatch so an operator can retry.

        'uncertain' means a previous launch command's reply was lost, so the
        service might still be starting elsewhere; ensure() refuses to dispatch
        again on its own to avoid a double launch (see docstring above). This
        only clears that bookkeeping after an explicit operator decision — it
        never touches bloodhound-ce itself or its data, and it refuses while a
        startup pass is still running or once the service is already ready.
        """
        with self.lock:
            if self.stopped.is_set():
                return self._update(state='blocked', ready=False, error='runtime_stopped')
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeErrorCode('bloodhound_startup_in_progress')
            if self.snapshot.get('ready'):
                raise RuntimeErrorCode('bloodhound_already_ready')
            if self.snapshot.get('state') != 'uncertain':
                raise RuntimeErrorCode('bloodhound_reset_requires_uncertain_state')
            previous_state = self.snapshot.get('state')
            (self.root / 'ce-startup.json').unlink(missing_ok=True)
            self.verified_configuration = None
            self.last_probe = 0.0
            self.snapshot = dict(required=True, state='blocked', exegol_status='not_checked',
                                 mcp_status='not_checked', bloodhound_status='not_checked',
                                 ready=False, exegol_verified_at=None, updated_at=timestamp())
            append_log(self.root, f"operator reset: cleared uncertain dispatch intent (state={previous_state}->blocked)")
            return dict(self.snapshot)

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
                                        exegol_verified_at=None, container=None, error='runtime_configuration_changed')
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
                                        mcp_status='not_checked', exegol_verified_at=None, container=None,
                                        error='runtime_configuration_changed')
                if self.thread is not None and self.thread.is_alive():
                    return dict(self.snapshot)
                if self.stopped.is_set():
                    return self._update(state='blocked', ready=False, error='runtime_stopped')
                if ce_started(availability):
                    self._save('ready')
                    return self._update(state='ready', ready=True, bloodhound_status=availability,
                                        error=None, launch_exit_code=None)
                uncertain = self._intent() == 'dispatched'
                if uncertain:
                    error = 'bloodhound_startup_uncertain'
                elif availability == 'stopped' and self.snapshot.get('error') == 'bloodhound_launch_failed':
                    # A previous pass proved the launch died with this exit
                    # code; keep that diagnosis instead of relabeling the
                    # service as merely stopped. The next ensure() pass
                    # replaces it, and a CE that actually comes up reports
                    # ready.
                    error = 'bloodhound_launch_failed'
                else:
                    error = 'bloodhound_' + availability
                return self._update(state='uncertain' if uncertain else 'failed', ready=False,
                                    bloodhound_status=availability, error=error)
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
        except Exception as exc:
            uncertain = self._intent() == 'dispatched'
            values = dict(state='uncertain' if uncertain else 'failed', ready=False,
                          error='bloodhound_startup_uncertain' if uncertain else runtime_failure_code(exc))
            if self.snapshot['exegol_status'] != 'ready':
                values.update(exegol_status='unavailable', mcp_status='error')
            self._update(**values)
        finally:
            if self.stopped.is_set():
                self._update(state='blocked', ready=False, error='runtime_stopped')

    async def _check_and_start(self):
        """Verify the selected local Exegol namespace and start only its installed CE."""
        config = configuration()
        url, token, container, endpoint = config
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
                append_log(self.root, 'mcp: connected to Exegol MCP')
                if container:
                    arguments = execution_arguments(session.openai_tools, container, command)
                    result = await session.call_tool(EXEC_TOOL, arguments)
                    lines = [line.strip() for text in output_strings(result) for line in text.splitlines()]
                else:
                    container, lines = await discover_container(session, command, expected, self.root)
                if expected not in lines:
                    raise RuntimeErrorCode('exegol_workspace_identity_mismatch')
                if configuration() != config:
                    raise RuntimeErrorCode('runtime_configuration_changed')
                self.verified_configuration = config
                self._update(exegol_status='ready', exegol_verified_at=timestamp(), container=container)
                if self.stopped.is_set():
                    return
                availability = await asyncio.to_thread(probe_ce, endpoint)
                if configuration() != config:
                    raise RuntimeErrorCode('runtime_configuration_changed')
                if ce_started(availability):
                    self._save('ready')
                    self._update(state='ready', ready=True, bloodhound_status=availability, error=None, launch_exit_code=None)
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
                arguments = execution_arguments(session.openai_tools, container, LAUNCH_COMMAND)
                if self.stopped.is_set():
                    return
                if configuration() != config:
                    raise RuntimeErrorCode('runtime_configuration_changed')
                self._save('dispatched')
                self._update(state='starting', bloodhound_status='starting', launch_exit_code=None)
                reply = None
                try:
                    reply = await asyncio.wait_for(session.call_tool(EXEC_TOOL, arguments), timeout=30)
                except Exception:
                    # A lost reply does not mean the service failed to start.
                    reply = None
                if reply is not None:
                    lines = [line.strip() for text in output_strings(reply) for line in text.splitlines()]
                    exited = [int(match.group(1)) for match in
                              (LAUNCH_EXITED_MARKER.match(line) for line in lines) if match]
                    if exited and exited[0] != 0:
                        # The reply proves the launch ran and the detached
                        # process already died on its own, so a retry cannot
                        # double-launch. Clear the durable intent so the next
                        # ensure() makes a clean attempt without a manual
                        # reset, and report the definite failure instead of
                        # a permanent fake 'uncertain'.
                        (self.root / 'ce-startup.json').unlink(missing_ok=True)
                        self._update(state='failed', ready=False, bloodhound_status='unavailable',
                                     error='bloodhound_launch_failed', launch_exit_code=exited[0])
                        return
                    # A running marker, a zero exit, an unrecognized reply
                    # shape, or a lost reply all fall through: the service
                    # may still be starting, so poll without replaying it.
                deadline = time.monotonic() + READY_TIMEOUT
                while not self.stopped.is_set() and time.monotonic() < deadline:
                    availability = await asyncio.to_thread(probe_ce, endpoint)
                    if ce_started(availability):
                        if configuration() != config:
                            raise RuntimeErrorCode('runtime_configuration_changed')
                        self._save('ready')
                        self._update(state='ready', ready=True, bloodhound_status=availability, error=None, launch_exit_code=None)
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
