"""Build short Exegol MCP requests backed by an independent container supervisor.

The embedded program uses only Python's standard library and runs inside the
Linux Exegol container. See docs/exegol-executions.md for its file protocol.
"""

import base64
import json
import re
import shlex


REMOTE_SOURCE = r'''
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

ROOT = Path('/var/tmp') / ('minimal-rmm-executions-' + str(os.getuid()))
TERMINAL = {'completed', 'failed', 'timed_out', 'cancelled', 'outcome_unknown'}

def private_directory(path, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_execution_directory')
    return path

def open_private(path, flags, mode=0o600):
    fd = os.open(path, flags | os.O_NOFOLLOW, mode)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        os.close(fd)
        raise ValueError('unsafe_execution_file')
    return fd

def atomic_json(directory, name, value):
    path = directory / (name + '.' + str(os.getpid()) + '.tmp')
    fd = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, directory / name)
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        path.unlink(missing_ok=True)

def read_json(path):
    with os.fdopen(open_private(path, os.O_RDONLY)) as stream:
        return json.load(stream)

def lock_file(path, nonblocking=False):
    fd = open_private(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        return fd
    except BaseException:
        os.close(fd)
        raise

def alive(directory):
    try:
        fd = lock_file(directory / 'worker.lock', nonblocking=True)
    except BlockingIOError:
        return True
    os.close(fd)
    return False

def stop_group(process):
    # The direct child is unreaped while signals are sent, so its PID and PGID
    # cannot be reused. No externally supplied or persisted PID is signaled.
    for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            time.sleep(grace)
    process.wait()

def collect_output(process, output_fd):
    overflow = False
    # Bound each drain so continuous output cannot starve deadline/cancel checks.
    for _ in range(16):
        try:
            chunk = os.read(process.stdout.fileno(), 65536)
        except BlockingIOError:
            break
        if not chunk:
            break
        remaining = max(0, 64 * 1024 * 1024 - os.fstat(output_fd).st_size)
        if chunk[:remaining]:
            pending = memoryview(chunk[:remaining])
            while pending:
                written = os.write(output_fd, pending)
                if written <= 0:
                    raise OSError('output_write_made_no_progress')
                pending = pending[written:]
        if len(chunk) > remaining:
            overflow = True
            break
    return overflow

def supervise(directory):
    # Status probes briefly own the same lock. Wait for a probe to release it.
    worker_lock = lock_file(directory / 'worker.lock')
    process = None
    output_fd = None
    status = read_json(directory / 'state.json')
    try:
        task = read_json(directory / 'request.json')
        if status['state'] in TERMINAL:
            return
        if (directory / 'cancel').exists():
            status.update(state='cancelled', finished_at=time.time())
            return
        output_fd = open_private(directory / 'output', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        # Exegol's normal exec path loads zsh aliases before eval as well.
        env = dict(os.environ, CMD=task['command'])
        process = subprocess.Popen(
            ['zsh', '-c', 'autoload -Uz compinit; compinit; source ~/.zshrc; eval "$CMD"'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, start_new_session=True, close_fds=True)
        os.set_blocking(process.stdout.fileno(), False)
        status.update(state='running', started_at=time.time())
        atomic_json(directory, 'state.json', status)
        deadline = time.monotonic() + task['timeout_seconds']
        while True:
            overflow = collect_output(process, output_fd)
            # Check completion without reaping: retaining the child PID until
            # signal handling is finished prevents process-group PID reuse.
            completed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if completed is not None:
                # A command's descendants cannot keep writing after the leader
                # exits. Detached services need a separate lifecycle interface.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                exit_code = process.wait()
                overflow = collect_output(process, output_fd) or overflow
                status.update(state='completed' if exit_code == 0 else 'failed', exit_code=exit_code)
                if overflow:
                    status.update(state='failed', error='output_limit_exceeded')
                break
            reason = ('cancelled' if (directory / 'cancel').exists() else
                      'timed_out' if time.monotonic() >= deadline else
                      'failed' if overflow else None)
            if reason:
                stop_group(process)
                status.update(state=reason, exit_code=process.returncode)
                if reason == 'failed':
                    status['error'] = 'output_limit_exceeded'
                break
            time.sleep(0.1)
        status['finished_at'] = time.time()
    except BaseException:
        status.update(state='failed', error='supervisor_failed', finished_at=time.time())
        if process is not None and process.returncode is None:
            try:
                stop_group(process)
            except OSError:
                status.update(state='outcome_unknown', error='termination_unconfirmed')
    finally:
        if output_fd is not None:
            if process is not None:
                collect_output(process, output_fd)
                if process.stdout is not None:
                    process.stdout.close()
            os.fsync(output_fd)
            os.close(output_fd)
        atomic_json(directory, 'state.json', status)
        os.close(worker_lock)

def snapshot(directory, request):
    status = read_json(directory / 'state.json')
    if status['state'] not in TERMINAL and not alive(directory):
        status = read_json(directory / 'state.json')
        if status['state'] == 'running' or (status['state'] == 'starting' and time.time() - status['created_at'] > 10):
            status = dict(status, state='outcome_unknown', error='supervisor_unavailable')
    offset, limit = request['offset'], request['limit']
    try:
        with os.fdopen(open_private(directory / 'output', os.O_RDONLY), 'rb') as stream:
            total = os.fstat(stream.fileno()).st_size
            stream.seek(offset)
            chunk = stream.read(limit)
    except FileNotFoundError:
        total, chunk = 0, b''
    status.update(output_b64=base64.b64encode(chunk).decode('ascii'),
                  output_bytes=total, next_offset=offset + len(chunk),
                  cancel_requested=(directory / 'cancel').exists())
    return status

def handle(request, source):
    scope, execution_id = request['scope'], request['execution_id']
    if not (__import__('re').fullmatch('[a-f0-9]{64}', scope) and
            __import__('re').fullmatch('[a-f0-9]{32}', execution_id)):
        raise ValueError('invalid_execution_reference')
    if request['action'] not in ('start', 'status', 'cancel', '_supervise'):
        raise ValueError('invalid_execution_action')
    if type(request['offset']) is not int or request['offset'] < 0 or type(request['limit']) is not int or not 1 <= request['limit'] <= 65536:
        raise ValueError('invalid_output_page')
    create = request['action'] == 'start'
    try:
        private_directory(ROOT, create)
        owner = private_directory(ROOT / scope, create)
        directory = owner / execution_id
        private_directory(directory, create)
    except FileNotFoundError:
        return {'execution_id': execution_id, 'state': 'not_found'}
    if request['action'] == '_supervise':
        supervise(directory)
        return None
    if create:
        if not isinstance(request.get('command'), str) or not request['command'] or '\x00' in request['command']:
            raise ValueError('invalid_command')
        if type(request['timeout_seconds']) is not int or not 1 <= request['timeout_seconds'] <= 86400:
            raise ValueError('invalid_execution_timeout')
        launch_lock = lock_file(directory / 'launch.lock')
        try:
            digest = hashlib.sha256(json.dumps([request['command'], request['timeout_seconds']]).encode()).hexdigest()
            try:
                old = read_json(directory / 'state.json')
            except FileNotFoundError:
                old = None
            if old is not None:
                if old.get('request_digest') != digest:
                    raise ValueError('execution_id_conflict')
            else:
                atomic_json(directory, 'request.json', {'command': request['command'], 'timeout_seconds': request['timeout_seconds']})
                # Persist intent before creating any process. A lost response or
                # crash in the launch gap never causes the command to replay.
                atomic_json(directory, 'state.json', {'execution_id': execution_id, 'state': 'starting',
                    'exit_code': None, 'created_at': time.time(), 'request_digest': digest})
                child_request = dict(request, action='_supervise', command=None)
                payload = base64.b64encode(json.dumps(child_request, ensure_ascii=False).encode()).decode()
                loader = "import base64; s=base64.b64decode(" + repr(base64.b64encode(source.encode()).decode()) + ").decode(); exec(compile(s, '<rmm-exegol-supervisor>', 'exec'))"
                try:
                    subprocess.Popen([sys.executable, '-c', loader, payload], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                except OSError:
                    atomic_json(directory, 'state.json', {'execution_id': execution_id, 'state': 'failed',
                        'exit_code': None, 'created_at': time.time(), 'request_digest': digest, 'error': 'supervisor_launch_failed'})
        finally:
            os.close(launch_lock)
    elif request['action'] == 'cancel':
        state = read_json(directory / 'state.json')
        if state['state'] not in TERMINAL:
            fd = open_private(directory / 'cancel', os.O_WRONLY | os.O_CREAT)
            os.fsync(fd)
            os.close(fd)
    return snapshot(directory, request)

if __name__ == '__main__':
    os.umask(0o077)
    try:
        request = json.loads(base64.b64decode(sys.argv[1]))
        result = handle(request, s)
        if result is not None:
            print('RMM_EXECUTION:' + request['execution_id'] + ':' + json.dumps(result))
    except Exception as error:
        # Neither command arguments nor exception messages leave protected files.
        print(json.dumps({'state': 'error', 'error': 'supervisor_request_failed', 'exception_type': type(error).__name__}))
        sys.exit(1)
'''


def build_request(scope, execution_id, action, *, command=None,
                  timeout_seconds=3600, offset=0, limit=65536):
    """Encode one validated, bounded supervisor request for the Exegol exec tool."""
    if not isinstance(scope, str) or not re.fullmatch(r'[a-f0-9]{64}', scope):
        raise ValueError('invalid execution scope')
    if not isinstance(execution_id, str) or not re.fullmatch(r'[a-f0-9]{32}', execution_id):
        raise ValueError('invalid execution ID')
    if action not in ('start', 'status', 'cancel'):
        raise ValueError('invalid execution action')
    if type(offset) is not int or not 0 <= offset <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 65536:
        raise ValueError('invalid output page')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
        raise ValueError('invalid execution timeout')
    if action == 'start' and (not isinstance(command, str) or not command or '\x00' in command or len(command.encode()) > 65536):
        raise ValueError('invalid command')
    request = {'scope': scope, 'execution_id': execution_id, 'action': action,
               'command': command if action == 'start' else None,
               'timeout_seconds': timeout_seconds, 'offset': offset, 'limit': limit}
    source = base64.b64encode(REMOTE_SOURCE.encode()).decode('ascii')
    loader = "import base64; s=base64.b64decode(" + repr(source) + ").decode(); exec(compile(s, '<rmm-exegol-supervisor>', 'exec'))"
    payload = base64.b64encode(json.dumps(request, ensure_ascii=False).encode()).decode('ascii')
    shell_command = 'python3 -c ' + shlex.quote(loader) + ' ' + shlex.quote(payload)
    # Exegol forwards the entire command through one CMD environment string.
    # Linux limits each argument/environment string to 128 KiB independently
    # of ARG_MAX. Leave room for shell wrapping and the environment key.
    if len(shell_command.encode('utf-8')) > 120000:
        raise ValueError('encoded execution request is too large')
    return shell_command
