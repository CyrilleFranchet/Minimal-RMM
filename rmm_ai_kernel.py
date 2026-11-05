"""Durable, revisioned execution state for bounded v3 AI episodes."""
import hashlib
import ipaddress
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    """Return a UTC transition timestamp."""
    return datetime.now(timezone.utc).isoformat()


class KernelError(ValueError):
    """A bounded public state-contract rejection."""


class ExecutionKernel:
    """Own one authenticated run; provider transcripts never become canonical state."""

    def __init__(self, root, scope, objective, session_id, workflow, skill_versions, constraints=None, execution_scope=None, request_id=None, container=None):
        if not isinstance(scope, str) or len(scope) != 64 or any(c not in '0123456789abcdef' for c in scope):
            raise KernelError('invalid_run_scope')
        self.run_id = uuid.uuid4().hex
        directory = Path(root) / 'ai-runs' / scope
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = directory / (self.run_id + '.sqlite3')
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE runs(id TEXT PRIMARY KEY, objective TEXT, session_id TEXT, workflow TEXT, skills TEXT, revision INTEGER, state TEXT, constraints TEXT, execution_scope TEXT, request_id TEXT, container TEXT);
            CREATE TABLE tasks(id TEXT PRIMARY KEY, kind TEXT, target TEXT, objective TEXT, basis TEXT, state TEXT, budget INTEGER, skills TEXT);
            CREATE TABLE attempts(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), state TEXT, diagnostic TEXT);
            CREATE TABLE invocations(id TEXT PRIMARY KEY, attempt_id TEXT REFERENCES attempts(id), tool TEXT, fingerprint TEXT, state TEXT, artifact_id TEXT, receipt_class TEXT, arguments_artifact_id TEXT);
            CREATE TABLE observations(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), invocation_id TEXT REFERENCES invocations(id), target TEXT, excerpt TEXT, artifact_id TEXT, receipt_class TEXT);
            CREATE TABLE transitions(revision INTEGER PRIMARY KEY, entity TEXT, entity_id TEXT, old_state TEXT, new_state TEXT, reason TEXT, at TEXT);
        ''')
        with self.db:
            self.db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)',
                            (self.run_id, objective, session_id, workflow, json.dumps(skill_versions), 'running', json.dumps(constraints or []), json.dumps(execution_scope or {}), request_id, container))
        with self.db:
            self._transition('run', self.run_id, 'new', 'running', 'accepted')

    @property
    def revision(self):
        """Read the authoritative revision."""
        return self.db.execute('SELECT revision FROM runs').fetchone()[0]

    def _transition(self, entity, entity_id, old, new, reason):
        """Record a state transition in the caller's transaction."""
        revision = self.revision + 1
        self.db.execute('UPDATE runs SET revision=?', (revision,))
        self.db.execute('INSERT INTO transitions VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (revision, entity, entity_id, old, new, reason, now()))

    def add_task(self, proposal, expected_revision, budget):
        """Admit one compiled task against a current revision and canonical basis IDs."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if type(expected_revision) is not int or self.revision != expected_revision:
                raise KernelError('stale_plan')
            if self.db.execute("SELECT 1 FROM tasks WHERE state IN ('pending','running')").fetchone():
                raise KernelError('task_already_open')
            for oid in proposal['basis']:
                if not self.db.execute('SELECT 1 FROM observations WHERE id=?', (oid,)).fetchone():
                    raise KernelError('unknown_basis_observation')
            identity = (proposal['kind'], proposal['target'], proposal['objective'])
            if self.db.execute("SELECT 1 FROM tasks WHERE kind=? AND target=? AND objective=? AND state IN ('done','uncertain')", identity).fetchone():
                raise KernelError('task_already_covered_or_uncertain')
            task_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (task_id, *identity, json.dumps(proposal['basis']), 'pending', budget, json.dumps(proposal.get('skill_ids', []))))
            self._transition('task', task_id, 'new', 'pending', 'compiled')
        return self.task(task_id)

    def task(self, task_id):
        """Read one task owned by this run."""
        if not isinstance(task_id, str):
            raise KernelError('invalid_task_id')
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KernelError('unknown_task')
        result = dict(row)
        result['basis'] = json.loads(result['basis'])
        result['skill_ids'] = json.loads(result.pop('skills'))
        return result

    def lease(self, task_id):
        """Lease a task exactly once; running or uncertain work is never auto-replayed."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.task(task_id)['state']
            if state not in {'pending', 'progress'} or self.db.execute('SELECT count(*) FROM attempts WHERE task_id=?', (task_id,)).fetchone()[0] >= 2:
                raise KernelError('task_not_retryable')
            attempt_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO attempts VALUES (?, ?, ?, NULL)', (attempt_id, task_id, 'running'))
            self.db.execute("UPDATE tasks SET state='running' WHERE id=?", (task_id,))
            self._transition('task', task_id, state, 'running', 'leased')
        return attempt_id

    def start_call(self, attempt_id, name, arguments, read_only=False, arguments_artifact_id=None):
        """Persist dispatch intent before side effects; duplicates return their previous record."""
        fingerprint = hashlib.sha256(json.dumps([name, {k: v for k, v in arguments.items() if k not in {'message', 'summary'}}], sort_keys=True).encode()).hexdigest()
        previous = self.db.execute("SELECT * FROM invocations WHERE fingerprint=? AND (receipt_class IS NULL OR receipt_class!='rejected') ORDER BY rowid DESC LIMIT 1", (fingerprint,)).fetchone()
        repeats = self.db.execute('SELECT count(*) FROM invocations WHERE attempt_id=? AND fingerprint=?', (attempt_id, fingerprint)).fetchone()[0]
        if previous and (not read_only or repeats >= 2 or previous['state'] in {'started', 'uncertain'}):
            return dict(previous), False
        if not self.db.execute("SELECT 1 FROM attempts WHERE id=? AND state='running'", (attempt_id,)).fetchone():
            raise KernelError('attempt_not_running')
        call_id = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO invocations VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)',
                            (call_id, attempt_id, name, fingerprint, 'started', arguments_artifact_id))
            self._transition('invocation', call_id, 'new', 'started', 'dispatch_intent')
        return {'id': call_id, 'state': 'started', 'artifact_id': None}, True

    def receipt(self, call_id, artifact_id, receipt_class):
        """Commit exact receipt provenance before another provider or cancellation checkpoint."""
        with self.db:
            changed = self.db.execute("UPDATE invocations SET state='returned',artifact_id=?,receipt_class=? WHERE id=? AND state='started'",
                                      (artifact_id, receipt_class, call_id)).rowcount
            if changed != 1:
                raise KernelError('invocation_not_started')
            self._transition('invocation', call_id, 'started', 'returned', receipt_class)

    def reject(self, call_id, artifact_id):
        """Record a trusted compiler rejection before operational dispatch; it can be retried."""
        self.receipt(call_id, artifact_id, 'rejected')

    def finish_task(self, task_id, attempt_id, outcome, call_id, excerpt, read_receipt):
        """Validate an exact evidence quote and receipt class before recording completion."""
        if not isinstance(outcome, str) or outcome not in {'done', 'progress', 'blocked'}:
            raise KernelError('invalid_task_outcome')
        if not isinstance(call_id, str):
            raise KernelError('invalid_receipt_id')
        row = self.db.execute('SELECT i.* FROM invocations i JOIN attempts a ON a.id=i.attempt_id WHERE i.id=? AND a.task_id=? AND i.state=?',
                              (call_id, task_id, 'returned')).fetchone()
        if not row or not isinstance(excerpt, str) or not 1 <= len(excerpt) <= 4000:
            raise KernelError('canonical_evidence_required')
        raw = read_receipt(row['artifact_id'])
        if excerpt not in raw:
            raise KernelError('evidence_excerpt_not_verbatim')
        task = self.task(task_id)
        completion_classes = {'read', 'completed'} if task['kind'] in {'OBSERVE', 'BASELINE', 'DIRECTORY_QUERY', 'FILES', 'EMBEDDINGS'} else {'completed'}
        if outcome == 'done' and ((task['kind'] == 'NETWORK_SCAN' and row['tool'] != 'network_scan')
                                  or (task['kind'] == 'BASELINE' and row['tool'] != 'agent_baseline')):
            raise KernelError('task_specific_completion_receipt_required')
        if task['kind'] == 'DIRECTORY_QUERY':
            completion_classes = {'graph_query'}
        if outcome == 'done' and task['kind'] == 'EMBEDDINGS' and row['tool'] != 'embedding_results':
            raise KernelError('task_specific_completion_receipt_required')
        if outcome == 'done' and task['kind'] == 'FILES' and row['tool'] != 'list_session_downloads':
            raise KernelError('task_specific_completion_receipt_required')
        if outcome == 'done' and row['receipt_class'] not in completion_classes:
            raise KernelError('receipt_does_not_prove_completion')
        with self.db:
            if not self.db.execute("SELECT 1 FROM attempts WHERE id=? AND task_id=? AND state='running'", (attempt_id, task_id)).fetchone():
                raise KernelError('stale_attempt')
            observation_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)',
                            (observation_id, task_id, call_id, task['target'], excerpt, row['artifact_id'], row['receipt_class']))
            self.db.execute('UPDATE tasks SET state=? WHERE id=?', (outcome, task_id))
            self.db.execute('UPDATE attempts SET state=? WHERE id=?', (outcome, attempt_id))
            self._transition('task', task_id, 'running', outcome, 'evidence_compiled')
        return observation_id

    def interrupt(self, task_id=None, attempt_id=None, reason='interrupted'):
        """Keep unacknowledged dispatch uncertain and prohibit automatic retry."""
        with self.db:
            if task_id:
                self.db.execute("UPDATE tasks SET state='uncertain' WHERE id=? AND state='running'", (task_id,))
            if attempt_id:
                self.db.execute("UPDATE attempts SET state='uncertain',diagnostic=? WHERE id=? AND state='running'", (reason, attempt_id))
                self.db.execute("UPDATE invocations SET state='uncertain' WHERE attempt_id=? AND state='started'", (attempt_id,))
            self.db.execute("UPDATE runs SET state='interrupted'")
            self._transition('run', self.run_id, 'running', 'interrupted', reason)

    def supervisor_view(self):
        """Return fixed-count, bounded execution memory rather than the transcript."""
        def rows(sql):
            return [dict(row) for row in self.db.execute(sql)]
        return {'run_id': self.run_id, 'revision': self.revision, 'run_state': self.db.execute('SELECT state FROM runs').fetchone()[0],
                'open_tasks': rows("SELECT id,kind,target,objective,state FROM tasks WHERE state IN ('pending','running','progress','uncertain') ORDER BY rowid DESC LIMIT 8"),
                'closed_tasks': rows("SELECT id,kind,target,objective,state FROM tasks WHERE state IN ('done','blocked') ORDER BY rowid DESC LIMIT 4"),
                'observations': rows('SELECT id,task_id,invocation_id AS receipt_id,target,substr(excerpt,1,800) AS excerpt,artifact_id,receipt_class FROM observations ORDER BY rowid DESC LIMIT 6'),
                'diagnostics': rows('SELECT task_id,state,diagnostic FROM attempts WHERE diagnostic IS NOT NULL ORDER BY rowid DESC LIMIT 4'),
                'counts': rows('SELECT state,count(*) AS count FROM tasks GROUP BY state')}

    def executor_view(self, task):
        """Return exactly one task with explicit basis and at most two prior observations."""
        basis = []
        for oid in task['basis'][:6]:
            row = self.db.execute('SELECT id,invocation_id AS receipt_id,target,substr(excerpt,1,800) AS excerpt,artifact_id,receipt_class FROM observations WHERE id=?', (oid,)).fetchone()
            if row:
                basis.append(dict(row))
        prior = [dict(row) for row in self.db.execute('SELECT id,invocation_id AS receipt_id,substr(excerpt,1,800) AS excerpt,artifact_id FROM observations WHERE task_id=? ORDER BY rowid DESC LIMIT 2', (task['id'],))]
        return {'task': task, 'basis': basis, 'prior_observations': prior}

    def answer(self):
        """Mark a nonoperational explanation distinctly from verified execution."""
        if self.db.execute('SELECT 1 FROM tasks').fetchone():
            raise KernelError('explanation_has_execution_tasks')
        with self.db:
            self.db.execute("UPDATE runs SET state='answered'")
            self._transition('run', self.run_id, 'running', 'answered', 'nonoperational_answer')

    def block(self):
        """Record explicit noncompletion without rewriting uncertain task evidence."""
        with self.db:
            self.db.execute("UPDATE runs SET state='blocked'")
            self._transition('run', self.run_id, 'running', 'blocked', 'supervisor_blocker')

    def complete(self):
        """Close only a run with canonical completed tasks and no unresolved work."""
        states = [row[0] for row in self.db.execute('SELECT state FROM tasks')]
        if not states or any(state != 'done' for state in states):
            raise KernelError('run_has_unresolved_work')
        run = self.db.execute('SELECT workflow,execution_scope FROM runs').fetchone()
        if run['workflow'] in {'auto', 'explain'}:
            raise KernelError('nonoperational_workflow')
        required_kind = {'directory': 'DIRECTORY_QUERY', 'collect': 'DIRECTORY_COLLECT',
                         'files': 'FILES', 'embeddings': 'EMBEDDINGS', 'agent_commands': 'AGENT_COMMAND'}.get(run['workflow'])
        if required_kind and not self.db.execute("SELECT 1 FROM tasks WHERE kind=? AND state='done'", (required_kind,)).fetchone():
            raise KernelError('workflow_objective_not_covered')
        if run['workflow'] == 'network':
            scans = [ipaddress.ip_network(row[0]) for row in self.db.execute("SELECT target FROM tasks WHERE kind='NETWORK_SCAN' AND state='done'")]
            scope = json.loads(run['execution_scope'])
            expected = [ipaddress.ip_network(x) for x in scope.get('targets', [])]
            for excluded in scope.get('exclude', []):
                deny = ipaddress.ip_network(excluded)
                remaining = []
                for target in expected:
                    if not target.overlaps(deny):
                        remaining.append(target)
                    elif deny.subnet_of(target):
                        remaining.extend(target.address_exclude(deny))
                expected = remaining
            coverage = list(ipaddress.collapse_addresses(scans))
            if not expected or any(not any(target.subnet_of(covered) for covered in coverage) for target in expected):
                raise KernelError('network_objective_not_covered')
        with self.db:
            self.db.execute("UPDATE runs SET state='done'")
            self._transition('run', self.run_id, 'running', 'done', 'canonical_tasks_complete')

    def close(self):
        """Release the database after preserving durable state."""
        self.db.close()
