"""Workspace-scoped relay for central AI BloodHound calls; no artifact transport."""
import json
import os
from pathlib import Path
import threading
import time
import uuid

from rmm_bloodhound import bloodhound_api, validate, WorkflowError
from rmm_settings import getenv

ACTIONS = {'preflight', 'create', 'collect', 'import', 'status', 'verify', 'query', 'queries', 'report', 'ui', 'health', 'ensure', 'library_search', 'library_get'}


class BloodHoundBroker:
    """Bounded in-memory requests, visible only to their authenticated workspace."""
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = {}

    def submit(self, owner, body):
        """Queue an operation or retrieve an existing response without replay."""
        if not isinstance(body, dict) or set(body) - {'action', 'run_id', 'options'}:
            return 400, {'error': 'invalid_workspace_request'}
        action = body.get('action', 'preflight')
        if not isinstance(action, str):
            return 400, {'error': 'invalid_workspace_request'}
        if action == 'result':
            return self.result(owner, body.get('run_id', ''))
        if action not in ACTIONS or len(json.dumps(body).encode()) > 16384:
            return 400, {'error': 'invalid_workspace_request'}
        with self.lock:
            self._expire()
            if len(self.requests) >= 128 or sum(r['owner'] == owner and r['result'] is None for r in self.requests.values()) >= 8:
                return 503, {'error': 'workspace_request_capacity'}
            request_id = uuid.uuid4().hex
            self.requests[request_id] = {'owner': owner, 'body': body, 'result': None, 'created': time.monotonic()}
        return 202, {'workspace_request_id': request_id, 'state': 'pending',
                     'next_action': 'Call bloodhound action=result with this request ID as run_id. Do not resubmit the original action.'}

    def resolve_ai_context(self, owner, timeout=20):
        """Resolve owner-controlled MCP settings privately without retaining secrets."""
        timeout = max(0, min(float(timeout), 60))
        wake = threading.Event()
        request_id = uuid.uuid4().hex
        with self.lock:
            self._expire()
            if len(self.requests) >= 128 or sum(r['owner'] == owner and r['result'] is None for r in self.requests.values()) >= 8:
                return 503, {'error': 'workspace_request_capacity'}
            self.requests[request_id] = {
                'owner': owner, 'body': {'action': '_ai_context'}, 'result': None,
                'created': time.monotonic(), 'private': True, 'wake': wake,
            }
        try:
            wake.wait(timeout)
            with self.lock:
                row = self.requests.get(request_id)
                if row is not None and row['result'] is not None:
                    return row['result']
            return 503, {'error': 'workspace_ai_context_unavailable'}
        finally:
            with self.lock:
                self.requests.pop(request_id, None)

    def _expire(self):
        """Drop old results and stop redispatching requests after five minutes."""
        current = time.monotonic()
        for key, row in list(self.requests.items()):
            age = current - row['created']
            result_age = current - row.get('completed_at', row['created'])
            if age > 3600 or (row['body'].get('action') in ('health', 'library_search', 'library_get') and row['result'] is not None and result_age > 30):
                del self.requests[key]
            elif age > 300 and row['result'] is None:
                row['result'] = (409, {'error': 'workspace_request_uncertain', 'workspace_request_id': key})
                row['completed_at'] = current

    def pending(self, owner):
        """Return at most one pending operation for the requesting workspace."""
        with self.lock:
            self._expire()
            return [{'id': key, 'body': row['body']} for key, row in self.requests.items()
                    if row['owner'] == owner and row['result'] is None][:1]

    def result(self, owner, request_id):
        """Return only the owner's result; missing IDs never imply no side effects."""
        if not isinstance(request_id, str):
            return 400, {'error': 'invalid_workspace_request_id'}
        with self.lock:
            self._expire()
            row = self.requests.get(request_id)
            if row is None or row['owner'] != owner or row.get('private'):
                return 404, {'error': 'workspace_request_not_found_inspect_before_retry'}
            return row['result'] or (202, {'workspace_request_id': request_id, 'state': 'pending'})

    def complete(self, owner, request_id, body):
        """Accept a bounded result only from the workspace that owns the request."""
        if not isinstance(body, dict) or set(body) != {'status', 'payload'} or type(body['status']) is not int or not 200 <= body['status'] <= 599 or not isinstance(body['payload'], dict) or len(json.dumps(body).encode()) > 1048576:
            return 400, {'error': 'invalid_workspace_result'}
        with self.lock:
            row = self.requests.get(request_id)
            if row is None or row['owner'] != owner:
                return 404, {'error': 'not_found'}
            if row['result'] is None or (row['result'][0] == 409 and row['result'][1].get('error') == 'workspace_request_uncertain'):
                row['result'] = (body['status'], body['payload'])
                row['completed_at'] = time.monotonic()
                if row.get('private'):
                    row['wake'].set()
        return 200, {'ok': True}


def execute_workspace_request(root, task):
    """Persist intent and cache replies locally so redispatch never repeats work."""
    if not isinstance(task, dict) or set(task) != {'id', 'body'}:
        raise WorkflowError('invalid_workspace_request')
    request_id = validate(task['id'], 'run_id')
    if task['body'] == {'action': '_ai_context'}:
        # This internal read is never journaled: its reply contains service secrets.
        from rmm_exegol_runtime import get_runtime
        runtime = get_runtime(root).status()
        if runtime.get('exegol_status') != 'ready':
            return 503, {'error': 'workspace_exegol_not_ready', 'runtime': runtime}
        return 200, {
            'exegol_mcp_enabled': True,
            'exegol_mcp_url': getenv('RMM_EXEGOL_MCP_URL', ''),
            'exegol_mcp_token': getenv('RMM_EXEGOL_MCP_TOKEN', ''),
        }
    if isinstance(task['body'], dict) and task['body'].get('action') in ('health', 'library_search', 'library_get'):
        # Read-only discovery may repeat and must not fill the durable journal.
        return bloodhound_api(root, task['body'])
    directory = Path(root) / 'relay-results'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (request_id + '.json')
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            cached = json.loads(path.read_text())
            if cached.get('state') == 'complete':
                return cached['status'], cached['payload']
        except (ValueError, OSError):
            pass
        return 409, {'error': 'workspace_operation_uncertain_inspect_local_runs', 'workspace_request_id': request_id}
    with os.fdopen(descriptor, 'w') as handle:
        json.dump({'state': 'started'}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    status, payload = bloodhound_api(root, task['body'])
    result = json.dumps({'state': 'complete', 'status': status, 'payload': payload})
    temporary = directory / (request_id + '.tmp')
    with open(temporary, 'x', encoding='utf-8') as handle:
        os.chmod(temporary, 0o600)
        handle.write(result)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return status, payload
