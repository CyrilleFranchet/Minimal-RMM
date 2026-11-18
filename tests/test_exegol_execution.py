"""Exercise durable AI dispatch, lost responses, ownership, and recovery."""
import asyncio
import base64
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from rmm_ai_jobs import JobInterrupted, _safe_activity
from rmm_exegol_execution import DurableExegolBackend, ExecutionStore, execution_scope


class FakeExegol:
    server_instructions = 'Fixture Exegol'

    def __init__(self):
        self.openai_tools = [{'type': 'function', 'function': {
            'name': 'execute_command_in_container', 'description': 'Execute',
            'parameters': {'type': 'object', 'properties': {'container_name': {'type': 'string'},
                           'command': {'type': 'string'}}, 'required': ['container_name', 'command']}}}]
        self.calls = []
        self.executions = {}
        self.lose_start = False
        self.statuses = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def call_tool(self, name, arguments):
        if name != 'execute_command_in_container':
            return 'unmodified'
        request = json.loads(arguments['command'])
        self.calls.append(request)
        key, action = request['execution_id'], request['action']
        if action == 'start':
            self.executions[key] = b'fixture output\n'
            if self.lose_start:
                self.lose_start = False
                raise TimeoutError('private transport detail')
            state = 'running'
        elif action == 'cancel':
            state = 'cancelled'
        else:
            state = self.statuses.pop(0) if self.statuses else 'completed'
        output = self.executions.get(key, b'')
        chunk = output[request.get('offset', 0):request.get('offset', 0) + request.get('limit', 65536)]
        result = {'execution_id': key, 'state': state, 'exit_code': 0 if state == 'completed' else None,
                  'output_b64': base64.b64encode(chunk).decode(), 'output_bytes': len(output),
                  'next_offset': request.get('offset', 0) + len(chunk)}
        return json.dumps({'exit_code': 0, 'output': 'RMM_EXECUTION:' + key + ':' + json.dumps(result)})


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = patch('rmm_exegol_execution.getenv', side_effect=lambda key, default=None:
                       self.temp.name if key == 'RMM_LOG_DIR' else default)
        config.start()
        self.addCleanup(config.stop)
        builder = patch('rmm_exegol_supervisor.build_request', side_effect=lambda scope, execution_id, action, **kw:
                        json.dumps(dict(scope=scope, execution_id=execution_id, action=action, **kw)))
        builder.start()
        self.addCleanup(builder.stop)
        self.raw = FakeExegol()
        self.events = []
        self.backend = DurableExegolBackend(self.raw, 'a' * 64, self.events.append)
        self.args = {'container_name': 'fixture', 'command': 'printf fixture'}

    async def test_execute_persists_handle_before_dispatch_and_returns_result(self):
        original = self.raw.call_tool
        async def inspect(name, arguments):
            request = json.loads(arguments['command'])
            self.assertIsNotNone(self.backend.store.get(request['execution_id']))
            self.assertEqual(self.events[0]['status'], 'prepared')
            return await original(name, arguments)
        self.raw.call_tool = inspect
        async with self.backend:
            result = json.loads(await self.backend.call_tool('execute_command_in_container', self.args))
        self.assertEqual((result['state'], result['exit_code'], result['output']),
                         ('completed', 0, 'fixture output\n'))
        self.assertEqual(self.backend.store.get(result['execution_id'])['state'], 'completed')
        self.assertEqual([r['action'] for r in self.raw.calls], ['start', 'status'])
        self.assertTrue(self.raw.closed)
        completed = self.events[-1]
        self.assertEqual(completed['execution_id'], result['execution_id'])
        self.assertEqual(completed['execution_action'], 'status')
        self.assertEqual(completed['exit_code'], 0)
        self.assertEqual(completed['output_bytes'], len(b'fixture output\n'))

    async def test_lost_start_response_recovers_without_duplicate_launch_after_reconnect(self):
        self.raw.lose_start = True
        result = json.loads(await self.backend.call_tool('execute_command_in_container', self.args))
        self.assertEqual(result['state'], 'outcome_unknown')
        self.assertNotIn('private transport', json.dumps(result))
        reconnected = DurableExegolBackend(self.raw, 'a' * 64)
        completed = json.loads(await reconnected.call_tool('execute_command_in_container', self.args))
        self.assertEqual(completed['execution_id'], result['execution_id'])
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(sum(r['action'] == 'start' for r in self.raw.calls), 1)

    async def test_recovery_and_cancel_reject_another_owner_before_remote_call(self):
        result = json.loads(await self.backend.call_tool('execute_command_in_container', self.args))
        other = DurableExegolBackend(self.raw, 'b' * 64)
        before = len(self.raw.calls)
        for name in ('get_exegol_execution', 'cancel_exegol_execution'):
            response = json.loads(await other.call_tool(name, {'execution_id': result['execution_id']}))
            self.assertEqual(response['error'], 'invalid_execution_reference')
        self.assertEqual(len(self.raw.calls), before)

    async def test_output_pages_are_byte_addressed_and_cancel_is_explicit(self):
        row, _ = self.backend.store.claim('fixture', 'printf fixture', 3600)
        self.raw.executions[row['id']] = b'abcdef'
        result = json.loads(await self.backend.call_tool('get_exegol_execution',
                            {'execution_id': row['id'], 'offset': 2, 'limit': 2}))
        self.assertEqual((result['output'], result['next_offset']), ('cd', 4))
        result = json.loads(await self.backend.call_tool('cancel_exegol_execution', {'execution_id': row['id']}))
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(self.raw.calls[-1]['action'], 'cancel')

    async def test_shell_initialization_noise_does_not_hide_result(self):
        original = self.raw.call_tool
        async def noisy(name, arguments):
            envelope = json.loads(await original(name, arguments))
            envelope['output'] = 'Exegol welcome banner without newline' + envelope['output'] + '\nshell cleanup warning'
            return json.dumps(envelope)
        self.raw.call_tool = noisy
        result = json.loads(await self.backend.call_tool('execute_command_in_container', self.args))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['output'], 'fixture output\n')

    async def test_status_timeout_is_bounded_and_recovers_without_relaunch(self):
        original = self.raw.call_tool
        stalled = False
        async def transport(name, arguments):
            nonlocal stalled
            request = json.loads(arguments['command'])
            if request['action'] == 'status' and not stalled:
                stalled = True
                await asyncio.Future()
            return await original(name, arguments)
        self.raw.call_tool = transport
        wait_for = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            self.assertEqual(timeout, 30)
            return await wait_for(awaitable, timeout=.01)
        with patch('rmm_exegol_execution.asyncio.wait_for', side_effect=short_wait):
            result = json.loads(await self.backend.call_tool('execute_command_in_container', self.args))
        self.assertEqual(result['state'], 'outcome_unknown')
        recovered = DurableExegolBackend(self.raw, 'a' * 64)
        result = json.loads(await recovered.call_tool('get_exegol_execution',
                            {'execution_id': result['execution_id']}))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(sum(r['action'] == 'start' for r in self.raw.calls), 1)

    async def test_supervisor_error_and_execution_failure_are_preserved(self):
        original = self.raw.call_tool
        for state, code, error in [('failed', 7, 'output_limit_exceeded'),
                                   ('timed_out', -9, None),
                                   ('outcome_unknown', None, 'termination_unconfirmed')]:
            async def failing(name, arguments):
                envelope = json.loads(await original(name, arguments))
                request = json.loads(arguments['command'])
                prefix = 'RMM_EXECUTION:' + request['execution_id'] + ':'
                result = json.loads(envelope['output'][len(prefix):])
                if request['action'] == 'status':
                    result.update(state=state, exit_code=code, error=error)
                envelope['output'] = prefix + json.dumps(result)
                return json.dumps(envelope)
            self.raw.call_tool = failing
            args = {**self.args, 'command': 'fixture-' + state}
            result = json.loads(await self.backend.call_tool('execute_command_in_container', args))
            self.assertEqual((result['state'], result['exit_code']), (state, code))
            self.assertEqual(result.get('error'), error)
            self.assertEqual(self.events[-1].get('error_code'), error)
            self.assertEqual(self.events[-1].get('exit_code'), code)

    async def test_long_execution_polls_without_more_model_calls(self):
        self.raw.statuses = ['running', 'running', 'completed']
        with patch('rmm_exegol_execution.asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = json.loads(await self.backend.call_tool('execute_command_in_container', self.args))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual([r['action'] for r in self.raw.calls], ['start', 'status', 'status', 'status'])

    async def test_job_stop_stops_polling_without_canceling_or_replaying_remote_work(self):
        def progress(event):
            if event['status'] == 'running':
                raise JobInterrupted('stopped')
        self.backend.progress = progress
        with self.assertRaises(JobInterrupted):
            await self.backend.call_tool('execute_command_in_container', self.args)
        self.assertEqual([r['action'] for r in self.raw.calls], ['start'])
        self.assertEqual(self.backend.store.list()[0]['state'], 'running')

    async def test_task_cancellation_leaves_recoverable_handle(self):
        original = self.raw.call_tool
        async def interrupted(name, arguments):
            await original(name, arguments)
            raise asyncio.CancelledError()
        self.raw.call_tool = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await self.backend.call_tool('execute_command_in_container', self.args)
        self.assertEqual(len(self.backend.store.list()), 1)
        self.assertEqual([r['action'] for r in self.raw.calls], ['start'])

    async def test_large_output_returns_bounded_prefix_and_retrievable_offset(self):
        original = self.raw.call_tool
        payload = b'x' * (1048576 + 123)
        async def verbose(name, arguments):
            raw = await original(name, arguments)
            request = json.loads(arguments['command'])
            if request['action'] == 'start':
                self.raw.executions[request['execution_id']] = payload
            return raw
        self.raw.call_tool = verbose
        result = json.loads(await self.backend.call_tool('execute_command_in_container', self.args))
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['output_truncated'])
        self.assertEqual(len(result['output']), 1048576)
        page = json.loads(await self.backend.call_tool('get_exegol_execution',
                          {'execution_id': result['execution_id'], 'offset': result['next_offset']}))
        self.assertEqual(page['output'], 'x' * 123)

    async def test_production_composite_session_routes_recovery_and_preserves_raw_lifecycle_class(self):
        from rmm_mcp_client import run_with_mcp_session
        local = FakeExegol()
        local.openai_tools[0]['function']['name'] = 'list_sessions'
        async def chat(session):
            self.assertIn('get_exegol_execution', session.external_tool_names)
            self.assertNotIn('list_sessions', session.external_tool_names)
            result = json.loads(await session.call_tool('execute_command_in_container', self.args))
            recovered = json.loads(await session.call_tool('get_exegol_execution',
                                   {'execution_id': result['execution_id']}))
            self.assertEqual(recovered['output'], 'fixture output\n')
            return result
        with patch('rmm_mcp_client.McpRmmSession', return_value=local), patch(
                'rmm_mcp_client.McpHttpSession', return_value=self.raw) as factory:
            result = await asyncio.to_thread(run_with_mcp_session, 'http://rmm', 'token', chat,
                                            exegol_enabled=True, exegol_mcp_url='http://exegol',
                                            exegol_mcp_token='fixture', execution_progress=self.events.append)
        self.assertEqual(result['state'], 'completed')
        factory.assert_called_once_with('http://exegol', 'fixture')
        self.assertTrue(self.raw.closed)
        self.assertTrue(local.closed)

    async def test_schema_preserved_and_lifecycle_backend_not_changed(self):
        async with self.backend:
            self.assertEqual(self.backend.openai_tools[0]['function']['parameters'],
                             self.raw.openai_tools[0]['function']['parameters'])
            names = {t['function']['name'] for t in self.backend.openai_tools}
            self.assertTrue({'get_exegol_execution', 'cancel_exegol_execution', 'list_exegol_executions'} <= names)
            self.assertEqual(await self.backend.call_tool('list_exegol_containers', {}), 'unmodified')
        self.assertEqual(len(self.raw.openai_tools), 1)

    async def test_invalid_arguments_never_launch(self):
        for args in ({}, {'container_name': 'fixture', 'command': ''},
                     {'container_name': 'fixture', 'command': 'a\x00b'}):
            result = json.loads(await self.backend.call_tool('execute_command_in_container', args))
            self.assertEqual(result['error'], 'invalid_execution_arguments')
        self.assertEqual(self.raw.calls, [])

    def test_store_and_activity_do_not_reveal_command_or_credentials(self):
        row, _ = self.backend.store.claim('fixture', 'private command', 3600)
        listing = json.dumps(self.backend.store.list())
        self.assertNotIn('private command', listing)
        self.assertEqual(os.stat(self.backend.store.path).st_mode & 0o777, 0o600)
        event = _safe_activity({'phase': 'exegol_execution', 'execution_id': row['id'],
                                'command': 'private command'})
        self.assertEqual(event['execution_id'], row['id'])
        self.assertNotIn('command', event)

    def test_scope_changes_with_authentication_and_endpoint(self):
        args = ['http://rmm/', 'token', {'X-RMM-Workspace': 'one'}, 'http://exegol/', 'secret']
        first = execution_scope(*args)
        self.assertEqual(first, execution_scope('http://rmm', *args[1:3], 'http://exegol', args[4]))
        for i, value in ((1, 'other'), (2, {'X-RMM-Workspace': 'two'}), (3, 'http://other'), (4, 'changed')):
            changed = list(args)
            changed[i] = value
            self.assertNotEqual(first, execution_scope(*changed))

    def test_same_command_claims_one_unresolved_execution_then_allows_completed_repeat(self):
        first, fresh = self.backend.store.claim('fixture', 'command', 3600)
        second, again = self.backend.store.claim('fixture', 'command', 3600)
        self.assertTrue(fresh)
        self.assertFalse(again)
        self.assertEqual(first['id'], second['id'])
        self.backend.store.update(first['id'], 'completed')
        third, fresh = self.backend.store.claim('fixture', 'command', 3600)
        self.assertTrue(fresh)
        self.assertNotEqual(third['id'], first['id'])

    def test_store_transactions_close_connections_on_success_and_rollback(self):
        store = self.backend.store
        with store.connect() as db:
            db.execute('CREATE TABLE lifecycle (value TEXT)')
            db.execute('INSERT INTO lifecycle VALUES (?)', ('committed',))
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        with self.assertRaisesRegex(RuntimeError, 'fixture rollback'):
            with store.connect() as failed:
                failed.execute('INSERT INTO lifecycle VALUES (?)', ('rolled back',))
                raise RuntimeError('fixture rollback')
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute('SELECT 1')
        with store.connect() as reopened:
            values = [row[0] for row in reopened.execute('SELECT value FROM lifecycle')]
        self.assertEqual(values, ['committed'])

    def test_symlink_store_is_rejected(self):
        target = Path(self.temp.name) / 'foreign'
        target.mkdir()
        directory = Path(self.temp.name) / 'exegol-executions'
        (directory / ('c' * 64 + '.sqlite3')).symlink_to(target / 'database')
        with self.assertRaises(OSError):
            ExecutionStore('c' * 64)
