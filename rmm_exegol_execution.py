"""Durable, owner-scoped execution over Exegol's synchronous MCP tool."""
from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import time
import uuid

from rmm_settings import getenv
from rmm_ai_jobs import JobInterrupted
from rmm_ai_concurrency import CURRENT_TOOL_ROUND

EXECUTE = 'execute_command_in_container'
TERMINAL = frozenset({'completed', 'failed', 'timed_out', 'cancelled'})
STATES = TERMINAL | {'prepared', 'starting', 'running', 'outcome_unknown', 'not_found'}
PAGE_BYTES = 65536
SAFE_ERRORS = frozenset({'output_limit_exceeded', 'supervisor_failed', 'termination_unconfirmed',
                         'supervisor_unavailable', 'supervisor_launch_failed'})

# This catches common accidental clock changes, not arbitrary shell programs.
CLOCK_SET = re.compile(r'(?i)(?:\bdate\s+(?:-[uR]+\s+)*(?:-[^\s]*s\b|--set(?:\s|=)|\d{8,})|'
                       r'\btimedatectl\s+set-(?:time|ntp)\b|\b(?:hwclock|ntpdate|'
                       r'rdate)\s+(?:-[^\s]*[sw]\b|--set\b)|\bntpdate\s+(?!-[^\s]*q\b)|'
                       r'\b(?:clock_settime|settimeofday)\b)')

NTP_PROBE = r'''import datetime,json,socket,struct,sys,time
host,marker=sys.argv[1:3]
try:
    infos=socket.getaddrinfo(host,123,type=socket.SOCK_DGRAM)
    deadline=time.monotonic()+45
    for index,(family,kind,proto,_,address) in enumerate(infos):
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0: break
            with socket.socket(family,kind,proto) as s:
                s.settimeout(min(10,remaining/(len(infos)-index)))
                s.connect(address)
                packet=bytearray(48); packet[0]=0x23
                t1=time.time(); stamp=t1+2208988800
                struct.pack_into('!II',packet,40,int(stamp)%2**32,int((stamp%1)*2**32))
                s.send(packet); response=s.recv(512); t4=time.time()
            if (len(response)<48 or response[0]&7!=4 or response[0]>>6==3 or
                not 1<=response[1]<=15 or response[24:32]!=packet[40:48] or
                response[32:40]==bytes(8) or response[40:48]==bytes(8)):
                continue
            def timestamp(pos):
                seconds,fraction=struct.unpack_from('!II',response,pos)
                reference=t1+2208988800
                era=round((reference-seconds)/2**32)
                return seconds+era*2**32-2208988800+fraction/2**32
            t2,t3=timestamp(32),timestamp(40)
            offset=((t2-t1)+(t3-t4))/2
            delay=(t4-t1)-(t3-t2)
            if abs(offset)>604800 or t3<t2 or not -0.05<=delay<=20:
                continue
            break
        except (OSError,ValueError,OverflowError,struct.error):
            continue
    else:
        raise OSError('no reachable time server')
    if remaining<=0:
        raise OSError('time server deadline exceeded')
    print(marker+json.dumps({'ok':True,'source':'ntp','dc':host,'offset_seconds':offset,
        'round_trip_seconds':delay,'measured_at_utc':datetime.datetime.fromtimestamp(t4,datetime.timezone.utc).isoformat(),
        'dc_time_utc':datetime.datetime.fromtimestamp(t4+offset,datetime.timezone.utc).isoformat(),
        'local_time_utc':datetime.datetime.fromtimestamp(t4,datetime.timezone.utc).isoformat()}))
except (OSError,ValueError,OverflowError,IndexError,struct.error):
    print(marker+json.dumps({'ok':False,'error':'dc_time_unavailable','guidance':'Check UDP 123 routing or supply an operator-provided offset.'}))
'''


def execution_scope(base_url, token, headers, exegol_url, exegol_token):
    """Bind recovery to authenticated operator and Exegol endpoint credentials."""
    identity = [base_url.rstrip('/'), token, headers or {}, exegol_url.rstrip('/'), exegol_token]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class ExecutionStore:
    """Keep execution handles and duplicate-launch claims in private local SQLite."""

    def __init__(self, scope):
        if not isinstance(scope, str) or not re.fullmatch('[a-f0-9]{64}', scope):
            raise ValueError('invalid execution scope')
        self.scope = scope
        directory = Path(getenv('RMM_LOG_DIR', 'RMM_logs')) / 'exegol-executions'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe execution store directory')
        self.path = directory / (scope + '.sqlite3')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('unsafe execution store file')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS executions ('
                       'id TEXT PRIMARY KEY, container TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                       'state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')

    @contextmanager
    def connect(self):
        """Open a short local transaction with bounded lock contention."""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, container, command, timeout):
        """Persist a new handle or reuse an unresolved matching command atomically."""
        fingerprint = hashlib.sha256(json.dumps([container, command]).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM executions WHERE fingerprint=? ORDER BY created DESC',
                              (fingerprint,)).fetchall()
            prior = next((row for row in rows if row['state'] not in TERMINAL), None)
            if prior:
                return dict(prior), False
            now = time.time()
            key = uuid.uuid4().hex
            db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?)',
                       (key, container, fingerprint, 'prepared', now, now))
            return dict(db.execute('SELECT * FROM executions WHERE id=?', (key,)).fetchone()), True

    def get(self, key):
        """Resolve only handles registered in this authenticated owner's store."""
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key):
            return None
        with self.connect() as db:
            row = db.execute('SELECT * FROM executions WHERE id=?', (key,)).fetchone()
            return dict(row) if row else None

    def update(self, key, state):
        """Retain the last observed execution state without discarding uncertainty."""
        if state not in STATES:
            raise ValueError('invalid execution state')
        with self.connect() as db:
            db.execute('UPDATE executions SET state=?,updated=? WHERE id=?', (state, time.time(), key))

    def list(self):
        """Return the owner's latest handles without command text or credentials."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT id AS execution_id,state,created,updated FROM executions ORDER BY created DESC LIMIT 100')]


def recovery_tools():
    """Describe adapter-owned recovery tools without changing Exegol's server."""
    def tool(name, description, properties, required):
        return {'type': 'function', 'function': {'name': name, 'description': description,
                'parameters': {'type': 'object', 'properties': properties, 'required': required,
                               'additionalProperties': False}}}
    key = {'execution_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}}
    return [
        tool('inspect_exegol_dc_time', 'Measure a DC clock using NTP over UDP 123 from the Exegol container. '
             'If unavailable, ask the operator for an offset; do not alter system time.',
             {'container_name': {'type': 'string'}, 'dc': {'type': 'string'}}, ['container_name', 'dc']),
        tool('list_exegol_executions', 'List your recent durable Exegol execution handles. '
             'States are last observations; use get_exegol_execution to refresh.', {}, []),
        tool('get_exegol_execution', 'Inspect an existing execution and read one output page. '
             'Recover this handle after a timeout; never relaunch the command to obtain output.',
             {**key, 'offset': {'type': 'integer', 'minimum': 0},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': PAGE_BYTES}}, ['execution_id']),
        tool('cancel_exegol_execution', 'Request termination of your existing Exegol execution. '
             'Cancellation is confirmed only by a terminal response; poll afterward if still running.',
             key, ['execution_id']),
    ]


class DurableExegolBackend:
    """Wrap AI Exegol commands with a supervisor and automatic short-request polling."""

    def __init__(self, backend, scope, progress=None):
        self.backend = backend
        self.store = ExecutionStore(scope)
        self.progress = progress
        self._openai_tools = []

    @property
    def openai_tools(self):
        return self._openai_tools

    @property
    def server_instructions(self):
        return (self.backend.server_instructions or '') + (
            '\nExegol execute_command_in_container uses durable supervised execution. '
            'A pending or unknown result is not command failure. Recover its execution_id '
            'with get_exegol_execution; do not repeat the command. '
            'Stopping the AI job stops waiting; cancel_exegol_execution explicitly stops a command. '
            'Use list_exegol_executions to recover handles after reconnecting. '
            'Never change host or container system time. For suspected Kerberos skew, use '
            'inspect_exegol_dc_time, then set clock_offset_seconds only on the affected command. '
            'If measurement fails, request an operator-provided offset. Do not retry automatically.')

    async def __aenter__(self):
        await self.backend.__aenter__()
        try:
            self._openai_tools = copy.deepcopy(self.backend.openai_tools)
            if any(tool['function']['name'] == EXECUTE for tool in self._openai_tools):
                for tool in self._openai_tools:
                    if tool['function']['name'] == EXECUTE:
                        tool['function']['description'] += (
                            ' Runs with durable tracking and automatic polling. Results include execution_id. '
                            'After an unknown response, recover that ID instead of relaunching.')
                        tool['function']['parameters']['properties']['clock_offset_seconds'] = {
                            'type': 'integer', 'minimum': -604800, 'maximum': 604800,
                            'description': 'Optional measured or operator-provided DC minus local offset in seconds; '
                                           'applies faketime only to this command.'}
                names = {tool['function']['name'] for tool in self._openai_tools}
                extra = recovery_tools()
                if names.intersection(tool['function']['name'] for tool in extra):
                    raise ValueError('duplicate Exegol recovery tool')
                self._openai_tools.extend(extra)
        except BaseException:
            import sys
            await self.backend.__aexit__(*sys.exc_info())
            raise
        return self

    async def __aexit__(self, *args):
        await self.backend.__aexit__(*args)

    def checkpoint(self, key, state, *, action='prepare', result=None):
        """Persist status before reporting a safe cooperative job checkpoint."""
        self.store.update(key, state)
        if self.progress:
            event = {'phase': 'exegol_execution', 'execution_id': key, 'status': state,
                     'execution_action': action, 'backend': 'exegol'}
            round_number = CURRENT_TOOL_ROUND.get()
            if type(round_number) is int and 1 <= round_number <= 1000000:
                event['round'] = round_number
            observed = result or {}
            code, size = observed.get('exit_code'), observed.get('output_bytes')
            if type(code) is int and -(2**31) <= code < 2**31:
                event['exit_code'] = code
            if type(size) is int and 0 <= size <= 2**53 - 1:
                event['output_bytes'] = size
            if observed.get('error') in SAFE_ERRORS:
                event['error_code'] = observed['error']
            self.progress(event)

    async def request(self, row, action, **kwargs):
        """Use the existing Exegol tool only for bounded supervisor operations."""
        from rmm_exegol_supervisor import build_request
        command = build_request(self.store.scope, row['id'], action, **kwargs)
        raw = await asyncio.wait_for(self.backend.call_tool(
            EXECUTE, {'container_name': row['container'], 'command': command}), timeout=30)
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
            raise RuntimeError('Exegol supervisor request failed')
        marker = 'RMM_EXECUTION:' + row['id'] + ':'
        output = envelope.get('output')
        if not isinstance(output, str) or output.count(marker) != 1:
            raise RuntimeError('missing Exegol supervisor response frame')
        result, _ = json.JSONDecoder().raw_decode(output.split(marker, 1)[1])
        if (not isinstance(result, dict) or result.get('execution_id') != row['id']
                or result.get('state') not in STATES):
            raise RuntimeError('invalid Exegol supervisor response')
        self.checkpoint(row['id'], result['state'], action=action, result=result)
        return result

    @staticmethod
    def outcome(result):
        """Expose observed state and allowlisted supervisor failure codes."""
        response = {'execution_id': result['execution_id'], 'state': result['state'],
                    'exit_code': result.get('exit_code')}
        if result.get('error') in SAFE_ERRORS:
            response['error'] = result['error']
        return response

    def pending(self, row):
        """Return a recoverable failure without claiming the remote process stopped."""
        self.store.update(row['id'], 'outcome_unknown')
        return json.dumps({'ok': False, 'state': 'outcome_unknown', 'execution_id': row['id'],
                           'guidance': 'Use get_exegol_execution with this ID. Do not relaunch the command.'})

    async def call_tool(self, name, arguments):
        """Dispatch ordinary tools unchanged and supervise only container commands."""
        if name not in {EXECUTE, 'inspect_exegol_dc_time', 'list_exegol_executions', 'get_exegol_execution', 'cancel_exegol_execution'}:
            return await self.backend.call_tool(name, arguments)
        if not isinstance(arguments, dict):
            return json.dumps({'ok': False, 'error': 'invalid_execution_arguments'})
        if name == 'inspect_exegol_dc_time':
            container, dc = arguments.get('container_name'), arguments.get('dc')
            if (not isinstance(container, str) or not container or len(container) > 255
                    or not isinstance(dc, str) or not 1 <= len(dc) <= 253
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', dc)):
                return json.dumps({'ok': False, 'error': 'invalid_time_probe_arguments'})
            marker = 'RMM_DC_TIME_' + hashlib.sha256(
                json.dumps([self.store.scope, container, dc]).encode()).hexdigest()[:32] + ':'
            command = ('timeout 55s python3 -c ' + shlex.quote(NTP_PROBE) + ' '
                       + shlex.quote(dc) + ' ' + shlex.quote(marker))
            response = json.loads(await self.call_tool(EXECUTE, {'container_name': container, 'command': command}))
            if response.get('state') != 'completed' or response.get('exit_code') != 0:
                guidance = ('Use get_exegol_execution with this execution ID to recover its outcome; '
                            'do not launch another probe while the outcome is unknown.'
                            if response.get('state') in {'outcome_unknown', 'not_found'} else
                            'Check UDP 123 routing or provide an operator-measured offset.')
                return json.dumps({'ok': False, 'error': 'dc_time_probe_failed',
                                   'execution_id': response.get('execution_id'),
                                   'state': response.get('state'),
                                   'guidance': guidance})
            try:
                output = response['output']
                if output.count(marker) != 1:
                    raise ValueError('missing time probe frame')
                result, _ = json.JSONDecoder().raw_decode(output.split(marker, 1)[1])
                if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                    raise ValueError('invalid result')
            except (ValueError, KeyError):
                return json.dumps({'ok': False, 'error': 'invalid_dc_time_response'})
            result['execution_id'] = response['execution_id']
            return json.dumps(result)
        if name == 'list_exegol_executions':
            return json.dumps({'executions': self.store.list()})
        if name != EXECUTE:
            row = self.store.get(arguments.get('execution_id'))
            offset, limit = arguments.get('offset', 0), arguments.get('limit', PAGE_BYTES)
            if (not row or type(offset) is not int or not 0 <= offset <= 2**53 - 1
                    or type(limit) is not int or not 1 <= limit <= PAGE_BYTES):
                return json.dumps({'ok': False, 'error': 'invalid_execution_reference'})
            try:
                result = await self.request(row, 'cancel' if name == 'cancel_exegol_execution' else 'status',
                                            offset=offset, limit=limit)
            except JobInterrupted:
                raise
            except Exception:
                return self.pending(row)
            return json.dumps(self.page_result(result))

        container, command = arguments.get('container_name'), arguments.get('command')
        clock_offset = arguments.get('clock_offset_seconds')
        if (not isinstance(container, str) or not container or len(container) > 255
                or not isinstance(command, str) or not command.strip() or '\x00' in command
                or len(command.encode()) > 65536
                or (clock_offset is not None and (type(clock_offset) is not int or abs(clock_offset) > 604800))):
            return json.dumps({'ok': False, 'error': 'invalid_execution_arguments'})
        if CLOCK_SET.search(command):
            return json.dumps({'ok': False, 'error': 'system_clock_change_disallowed',
                               'guidance': 'Use inspect_exegol_dc_time and clock_offset_seconds instead.'})
        if clock_offset is not None:
            command = ('faketime --exclude-monotonic -f ' + shlex.quote(f'{clock_offset:+d}')
                       + ' zsh -c ' + shlex.quote('autoload -Uz compinit; compinit; source ~/.zshrc; eval "$1"')
                       + ' rmm-faketime ' + shlex.quote(command))
        timeout = int(getenv('RMM_EXEGOL_EXECUTION_TIMEOUT', '3600'))
        from rmm_exegol_supervisor import build_request
        try:
            # Reject payload expansion before claiming or dispatching any work.
            build_request(self.store.scope, '0' * 32, 'start', command=command, timeout_seconds=timeout)
        except ValueError:
            return json.dumps({'ok': False, 'error': 'invalid_execution_arguments'})
        row, fresh = self.store.claim(container, command, timeout)
        self.checkpoint(row['id'], row['state'])
        try:
            if fresh:
                await self.request(row, 'start', command=command, timeout_seconds=timeout)
            # Read-only reconciliation is safe even when launch acknowledgment was lost.
            chunks, offset = [], 0
            while True:
                remaining = max(0, 1048576 - offset)
                result = await self.request(row, 'status', offset=offset, limit=min(PAGE_BYTES, remaining) or 1)
                chunk = base64.b64decode(result.get('output_b64', ''), validate=True)
                if remaining:
                    chunks.append(chunk)
                next_offset = result.get('next_offset', offset + len(chunk))
                if type(next_offset) is not int or next_offset < offset or next_offset - offset != len(chunk):
                    raise RuntimeError('invalid Exegol output offset')
                if remaining:
                    offset = next_offset
                # Bound provider result memory; remaining output stays available remotely.
                if result['state'] in TERMINAL and (not remaining or offset >= result.get('output_bytes', offset)):
                    response = dict(self.outcome(result), output=b''.join(chunks).decode('utf-8', errors='replace'))
                    if offset < result.get('output_bytes', offset):
                        response.update(next_offset=offset, output_truncated=True,
                                        guidance='Use get_exegol_execution for the remaining output.')
                    return json.dumps(response)
                if result['state'] in {'outcome_unknown', 'not_found'}:
                    return json.dumps({**self.outcome(result),
                                       'output': b''.join(chunks).decode('utf-8', errors='replace'),
                                       'next_offset': offset,
                                       'guidance': 'Use get_exegol_execution to retrieve further status/output. Do not relaunch.'})
                if not remaining or len(chunk) < PAGE_BYTES:
                    await asyncio.sleep(2)
        except JobInterrupted:
            raise
        except Exception:
            return self.pending(row)

    @staticmethod
    def page_result(result):
        """Decode one byte-offset output page while keeping recovery metadata."""
        result = dict(result)
        result['output'] = base64.b64decode(result.get('output_b64', ''), validate=True).decode('utf-8', errors='replace')
        return result
