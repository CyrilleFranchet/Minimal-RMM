"""Persist operation evidence without exposing it in job audit metadata."""
import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_PY311 = sys.version_info >= (3, 11)

from rmm_ai_context import ContextDispatcher, ContextWindow
from rmm_ai_concurrency import run_tool_batch
from rmm_ai_jobs import AIJobManager, JobInterrupted, _failure_diagnostics


class OperationActivityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        setting = patch('rmm_ai_context.getenv', side_effect=lambda key, default=None:
                        self.directory.name if key == 'RMM_LOG_DIR' else default)
        setting.start()
        self.addCleanup(setting.stop)
        self.path = Path(self.directory.name) / 'jobs.sqlite3'
        self.manager = AIJobManager(self.path)
        self.addCleanup(self.manager.close)

    def finish(self, run):
        job, _ = self.manager.submit(owner='operator', session_id='session', request_id='request', run=run)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            final = self.manager.get(job['id'], 'operator')
            if final['status'] in ('succeeded', 'failed'):
                return final
            time.sleep(.005)
        self.fail('job did not finish')

    def test_tool_evidence_is_private_scoped_and_survives_reopen(self):
        scope = 'a' * 64
        async def execute(name, args):
            running = self.manager.list('operator', 'session')[0]
            start = running['activity'][-1]
            self.assertEqual(start['status'], 'running')
            evidence = self.manager.read_evidence(running['id'], 'operator', start['arguments_artifact_id'])
            self.assertEqual(json.loads(evidence['content'])['command'], 'private-command-token')
            return 'private-command-output-🔐'
        def run(progress):
            progress({'phase': 'context_ready', 'context_scope': scope})
            dispatcher = ContextDispatcher(execute, ContextWindow(scope=scope))
            asyncio.run(run_tool_batch([{'name': 'execute_command_in_container',
                'arguments': {'command': 'private-command-token'}}], dispatcher, progress=progress))
            return {'ok': True}
        final = self.finish(run)
        events = [event for event in final['activity'] if event.get('phase') == 'tool']
        self.assertEqual([event['status'] for event in events], ['running', 'completed'])
        self.assertEqual(events[0]['call_id'], events[1]['call_id'])
        self.assertGreaterEqual(events[1]['duration_ms'], 0)
        serialized = json.dumps(final)
        self.assertNotIn('private-command', serialized)
        self.assertNotIn(scope, serialized)
        evidence_id = events[1]['artifact_id']
        self.assertEqual(self.manager.read_evidence(final['id'], 'operator', evidence_id)['content'], 'private-command-output-🔐')
        args = self.manager.read_evidence(final['id'], 'operator', events[1]['arguments_artifact_id'])
        self.assertEqual(json.loads(args['content'])['command'], 'private-command-token')
        self.assertIsNone(self.manager.read_evidence(final['id'], 'other-owner', evidence_id))
        unrelated = ContextWindow(scope=scope).archive('another-job-secret', 'tool_result')
        self.assertIsNone(self.manager.read_evidence(final['id'], 'operator', unrelated))
        self.manager.close()
        reopened = AIJobManager(self.path)
        try:
            self.assertEqual(reopened.get(final['id'], 'operator')['activity'], final['activity'])
            self.assertEqual(reopened.read_evidence(final['id'], 'operator', evidence_id)['content'], 'private-command-output-🔐')
        finally:
            reopened.close()

    @unittest.skipUnless(_PY311, "ExceptionGroup is a Python 3.11+ builtin")
    def test_exception_group_records_unknown_outcome_without_exception_messages(self):
        async def execute(name, args):
            raise ExceptionGroup('secret-group', [OSError('secret-output')])
        def run(progress):
            progress({'phase': 'context_ready', 'context_scope': 'b' * 64})
            dispatcher = ContextDispatcher(execute, ContextWindow(scope='b' * 64))
            asyncio.run(run_tool_batch([{'name': 'execute_command_in_container', 'arguments': {}}], dispatcher, progress=progress))
        final = self.finish(run)
        event = final['activity'][-1]
        self.assertEqual(event['status'], 'outcome_unknown')
        self.assertEqual([item['type'] for item in event['diagnostics']['exception_chain']], ['ExceptionGroup', 'OSError'])
        self.assertIn('arguments_artifact_id', event)
        self.assertNotIn('secret-', json.dumps(final))

    def test_large_output_reports_raw_size_not_context_preview(self):
        output = '🔐' * 5000
        async def execute(name, args):
            return output
        def run(progress):
            dispatcher = ContextDispatcher(execute, ContextWindow())
            asyncio.run(run_tool_batch([{'name': 'inspect', 'arguments': {}}], dispatcher, progress=progress))
            return {'ok': True}
        final = self.finish(run)
        self.assertEqual(final['activity'][-1]['output_bytes'], len(output.encode()))

    def test_cancel_retains_late_response_without_admitting_new_work(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        async def execute(name, args):
            calls.append(name)
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return 'known-response-after-stop'
        def run(progress):
            progress({'phase': 'context_ready', 'context_scope': 'c' * 64})
            dispatcher = ContextDispatcher(execute, ContextWindow(scope='c' * 64))
            asyncio.run(run_tool_batch([
                {'name': 'execute_command_in_container', 'arguments': {'command': 'first'}},
                {'name': 'execute_command_in_container', 'arguments': {'command': 'must-not-start'}}
            ], dispatcher, progress=progress))
            return {'ok': True}
        job, _ = self.manager.submit(owner='operator', session_id='session', request_id='cancel-response', run=run)
        self.assertTrue(started.wait(3))
        stopped = self.manager.cancel(job['id'], 'operator')
        call_id = stopped['activity'][-1]['call_id']
        for event in ({'phase': 'tool', 'status': 'running', 'call_id': 'new-call'},
                      {'phase': 'tool', 'status': 'completed', 'call_id': 'unknown-call'}):
            with self.assertRaises(JobInterrupted):
                self.manager._progress(job['id'], event)
        release.set()
        for worker in list(self.manager._threads):
            worker.join(3)
            self.assertFalse(worker.is_alive())
        final = self.manager.get(job['id'], 'operator')
        self.assertEqual(final['status'], 'interrupted')
        self.assertEqual(final['completed_at'], stopped['completed_at'])
        event = final['activity'][-1]
        self.assertEqual((event['call_id'], event['status']), (call_id, 'completed'))
        self.assertEqual(calls, ['execute_command_in_container'])
        self.assertEqual(self.manager.read_evidence(job['id'], 'operator', event['artifact_id'])['content'],
                         'known-response-after-stop')
        with self.assertRaises(JobInterrupted):
            self.manager._progress(job['id'], event)

    def test_activity_is_bounded_and_drops_arbitrary_data(self):
        def run(progress):
            for index in range(505):
                progress({'phase': 'tool', 'round': index, 'command': 'secret', 'output': 'secret',
                          'step_checkpoint': 'secret'})
            return {'ok': True}
        final = self.finish(run)
        self.assertEqual(len(final['activity']), 500)
        self.assertEqual(final['activity_omitted'], 5)
        self.assertEqual(final['activity'][0]['round'], 5)
        self.assertNotIn('secret', json.dumps(final['activity']))

    @unittest.skipUnless(_PY311, "ExceptionGroup is a Python 3.11+ builtin")
    def test_evidence_page_validation_and_group_cycles_are_bounded(self):
        self.assertIsNone(self.manager.read_evidence('missing', 'operator', '../escape'))
        error = ExceptionGroup('private', [ValueError('private') for _ in range(50)])
        error.__cause__ = error
        self.assertEqual(len(_failure_diagnostics(error)['exception_chain']), 8)

    def test_legacy_database_is_migrated(self):
        legacy = Path(self.directory.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.execute('CREATE TABLE ai_jobs (id TEXT PRIMARY KEY,owner TEXT,session_id TEXT,request_id TEXT,state TEXT,created_at TEXT,updated_at TEXT,started_at TEXT,completed_at TEXT,progress TEXT,result TEXT,error TEXT)')
            db.execute("INSERT INTO ai_jobs VALUES ('old','operator',NULL,'request','failed','','',NULL,NULL,'{}',NULL,NULL)")
        manager = AIJobManager(legacy)
        try:
            job = manager.get('old', 'operator')
            self.assertEqual(job['activity'], [])
            self.assertEqual(job['activity_omitted'], 0)
        finally:
            manager.close()

    def test_restart_preserves_unresolved_start_event(self):
        # Simulate a persisted running row left by a lost server process.
        event = {'phase': 'tool', 'tool': 'execute_command_in_container',
                 'status': 'running', 'call_id': 'pending', 'timestamp': '2026-11-06T00:00:00Z'}
        with self.manager._db:
            self.manager._db.execute(
                "INSERT INTO ai_jobs (id,owner,session_id,request_id,state,created_at,updated_at,activity,activity_total) "
                "VALUES ('lost','operator','session','lost-request','running','','',?,1)", (json.dumps([event]),))
        restarted = AIJobManager(self.path)
        try:
            job = restarted.get('lost', 'operator')
            self.assertEqual(job['status'], 'interrupted')
            self.assertEqual(job['activity'], [event])
        finally:
            restarted.close()
