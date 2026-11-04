"""Deterministic Exegol identity, readiness, and startup replay contracts."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from rmm_exegol_runtime import ExegolRuntime, RuntimeErrorCode, configuration, execution_arguments, probe_ce, discovery_candidates, discover_container, runtime_failure_code, LAUNCH_COMMAND, MCP_TIMEOUT, READY_TIMEOUT

TOOLS = [{'function': {'name': 'execute_command_in_container', 'parameters': {
    'type': 'object', 'properties': {'container_name': {'type': 'string'}, 'command': {'type': 'string'}},
    'required': ['container_name', 'command']}}}]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = ExegolRuntime(self.root)
        self.addCleanup(self.runtime.stop)
        self.calls = []
        self.wrong_identity = False
        self.missing_launcher = False
        self.launch_raises = False
        self.launch_exits = None
        self.launch_reply = None
        self.inventory = [{'name': 'lab', 'status': 'running'}]
        self.matching = {'lab'}
        owner = self

        class Session:
            openai_tools = TOOLS + [{'function': {'name': 'list_exegol_containers', 'parameters': {'type': 'object', 'properties': {}}}}]

            def __init__(self, url, token):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def call_tool(self, name, arguments):
                owner.calls.append(arguments)
                if name == 'list_exegol_containers':
                    return json.dumps(owner.inventory)
                if arguments['command'] == LAUNCH_COMMAND:
                    owner.assertEqual(owner.runtime._intent(), 'dispatched')
                    if owner.launch_raises:
                        raise TimeoutError('secret MCP diagnostic must not escape')
                    if owner.launch_reply is not None:
                        return owner.launch_reply
                    if owner.launch_exits is not None:
                        return json.dumps({'output': f'RMM_CE_LAUNCH_EXITED:{owner.launch_exits}\n'})
                    return json.dumps({'output': 'RMM_CE_LAUNCH_RUNNING\n'})
                nonce = next(owner.root.glob('ce-identity-*')).read_text()
                host = 'wrong' if owner.wrong_identity or arguments['container_name'] not in owner.matching else 'exegol-lab'
                output = f'RMM_EXEGOL_ID={nonce}:{host}:net:[123]\n'
                if not owner.missing_launcher:
                    output += 'RMM_CE_LAUNCHER_READY\n'
                return json.dumps({'stdout': output})

        values = {'RMM_EXEGOL_MCP_URL': 'http://127.0.0.1:8000/mcp',
                  'RMM_EXEGOL_CONTAINER': 'lab', 'RMM_EXEGOL_MCP_TOKEN': 'secret'}
        self.values = values
        real_readlink = os.readlink
        for context in (patch('rmm_exegol_runtime.getenv', side_effect=lambda key, default='': values.get(key, default)),
                        patch('rmm_exegol_runtime.McpHttpSession', Session),
                        patch('rmm_exegol_runtime.os.readlink', side_effect=lambda path, *args, **kwargs: 'net:[123]' if str(path) == '/proc/self/ns/net' else real_readlink(path, *args, **kwargs)),
                        patch('rmm_exegol_runtime.socket.gethostname', return_value='exegol-lab'),
                        patch('rmm_exegol_runtime.READY_TIMEOUT', 0)):
            context.start()
            self.addCleanup(context.stop)

    def test_healthy_ce_reused_without_launch(self):
        with patch('rmm_exegol_runtime.probe_ce', return_value='ready'):
            self.runtime._run()
            self.assertTrue(self.runtime.status()['ready'])
            self.assertTrue(self.runtime.ensure()['ready'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.runtime._intent(), 'ready')
        self.assertEqual(self.runtime.snapshot['mcp_status'], 'verified')
        self.assertIsNotNone(self.runtime.snapshot['exegol_verified_at'])
        verified_at = self.runtime.snapshot['exegol_verified_at']
        self.runtime.last_probe = 0
        with patch('rmm_exegol_runtime.probe_ce', return_value='ready'):
            self.runtime.status()
        self.assertEqual(self.runtime.snapshot['exegol_verified_at'], verified_at)
        self.assertEqual(list(self.root.glob('ce-identity-*')), [])

    def test_nested_connection_failures_have_secret_free_diagnostics(self):
        for error, code in (
            (ConnectionRefusedError('secret URL'), 'exegol_mcp_connection_failed'),
            (TimeoutError('secret token'), 'exegol_mcp_timeout'),
            (ModuleNotFoundError('private path'), 'exegol_mcp_dependency_missing'),
            (RuntimeErrorCode('exegol_execution_schema_unsupported'), 'exegol_execution_schema_unsupported'),
            (ValueError('private diagnostic'), 'exegol_runtime_failed'),
        ):
            wrapper = RuntimeError('private wrapper')
            wrapper.exceptions = (error,)
            with self.subTest(code=code), patch.object(self.runtime, '_check_and_start',
                    AsyncMock(side_effect=wrapper)):
                self.runtime._run()
                self.assertEqual(self.runtime.snapshot['error'], code)
                self.assertNotIn('secret', json.dumps(self.runtime.snapshot))
                self.assertNotIn('private', json.dumps(self.runtime.snapshot))

    def test_diagnostics_do_not_require_python_311_exception_group_builtin(self):
        wrapper = RuntimeError('private wrapper')
        wrapper.exceptions = (ConnectionRefusedError('private endpoint'),)
        # An unavailable builtin must not break ordinary failure reporting.
        with patch('rmm_exegol_runtime.BaseExceptionGroup', None, create=True):
            self.assertEqual(runtime_failure_code(wrapper), 'exegol_mcp_connection_failed')

    def test_http_authentication_diagnostics(self):
        error = RuntimeError('secret URL')
        error.response = Mock(status_code=401)
        with patch.object(self.runtime, '_check_and_start', AsyncMock(side_effect=error)):
            self.runtime._run()
        self.assertEqual(self.runtime.snapshot['error'], 'exegol_mcp_authentication_failed')

    def test_automatic_discovery_selects_matching_running_owner(self):
        self.values.pop('RMM_EXEGOL_CONTAINER')
        self.inventory = [{'name': 'other', 'status': 'running'}, {'name': 'stopped', 'status': 'exited'}, {'name': 'lab', 'status': 'running'}]
        with patch('rmm_exegol_runtime.probe_ce', side_effect=['stopped', 'ready']), patch('rmm_exegol_runtime.READY_TIMEOUT', 1):
            self.runtime._run()
        self.assertEqual(self.runtime.snapshot['state'], 'ready')
        self.assertEqual(self.runtime.snapshot['container'], 'lab')
        self.assertEqual(self.runtime.verified_configuration[2], '')
        self.assertEqual(self.calls[-1], {'container_name': 'lab', 'command': LAUNCH_COMMAND})
        self.assertFalse(any(row.get('container_name') == 'stopped' for row in self.calls))

    def test_automatic_discovery_no_match_or_ambiguity_never_launches(self):
        self.values.pop('RMM_EXEGOL_CONTAINER')
        self.inventory = [{'name': 'other', 'status': 'running'}, {'name': 'lab', 'status': 'running'}]
        for matches, error in ((set(), 'exegol_workspace_not_found'), ({'other', 'lab'}, 'exegol_workspace_identity_ambiguous')):
            self.matching = matches
            with patch('rmm_exegol_runtime.probe_ce') as probe:
                self.runtime._run()
                self.assertEqual(self.runtime.snapshot['error'], error)
                probe.assert_not_called()
            self.assertIsNone(self.runtime._intent())
        self.assertFalse(any(row.get('command') == LAUNCH_COMMAND for row in self.calls))

    def test_discovery_json_shapes_and_fail_closed_inputs(self):
        rows = [{'name': 'lab', 'status': 'running'}, {'name': 'stopped', 'status': 'exited'}]
        for value in (json.dumps(rows), json.dumps({'result': rows}), '\n'.join(json.dumps(row) for row in rows)):
            self.assertEqual(discovery_candidates(value), ['lab'])
        for value in ('not JSON', '{"name":"lab"}', json.dumps(rows + rows),
                      json.dumps([{'name': 'bad;name', 'status': 'running'}]),
                      json.dumps([{'name': str(n), 'status': 'running'} for n in range(33)])):
            with self.assertRaisesRegex(RuntimeErrorCode, 'discovery_response_invalid'):
                discovery_candidates(value)

    def test_discovery_incomplete_probe_and_missing_schema_do_not_select(self):
        session = Mock()
        session.openai_tools = TOOLS
        session.call_tool = AsyncMock()
        with self.assertRaisesRegex(RuntimeErrorCode, 'discovery_tool_unsupported'):
            asyncio.run(discover_container(session, 'identity', 'expected'))
        session.call_tool.assert_not_called()
        session.openai_tools = TOOLS + [{'function': {'name': 'list_exegol_containers', 'parameters': {}}}]
        session.call_tool.side_effect = [json.dumps([{'name': 'a', 'status': 'running'}, {'name': 'b', 'status': 'running'}]), 'expected', TimeoutError()]
        with self.assertRaises(TimeoutError):
            asyncio.run(discover_container(session, 'identity', 'expected'))

    def test_authenticated_ce_api_counts_as_started_without_launch(self):
        with patch('rmm_exegol_runtime.probe_ce', return_value='authentication_failed'):
            self.runtime._run()
            self.assertTrue(self.runtime.status()['ready'])
            self.assertTrue(self.runtime.ensure()['ready'])
        self.assertEqual(self.runtime.snapshot['state'], 'ready')
        self.assertEqual(self.runtime.snapshot['bloodhound_status'], 'authentication_failed')
        self.assertEqual(self.runtime._intent(), 'ready')
        self.assertFalse(any(row.get('command') == LAUNCH_COMMAND for row in self.calls))
        self.assertEqual(self.runtime.snapshot['mcp_status'], 'verified')
        # A later probe answering 401 again keeps the ready state stable.
        self.runtime.last_probe = 0
        with patch('rmm_exegol_runtime.probe_ce', return_value='authentication_failed'):
            self.runtime.status()
        self.assertTrue(self.runtime.snapshot['ready'])
        self.assertEqual(self.runtime.snapshot['bloodhound_status'], 'authentication_failed')

    def test_launch_poll_treats_authenticated_ce_as_started(self):
        with patch('rmm_exegol_runtime.probe_ce', side_effect=['stopped', 'authentication_failed']), \
                patch('rmm_exegol_runtime.READY_TIMEOUT', 1):
            self.runtime._run()
        self.assertEqual(self.calls[-1]['command'], LAUNCH_COMMAND)
        self.assertEqual(self.runtime.snapshot['state'], 'ready')
        self.assertEqual(self.runtime.snapshot['bloodhound_status'], 'authentication_failed')
        self.assertEqual(self.runtime._intent(), 'ready')

    def test_launch_dispatch_durable_before_mcp_and_reconciled(self):
        with patch('rmm_exegol_runtime.probe_ce', side_effect=['stopped', 'ready']), \
                patch('rmm_exegol_runtime.READY_TIMEOUT', 1):
            self.runtime._run()
        self.assertEqual(self.calls[-1]['command'], LAUNCH_COMMAND)
        self.assertEqual(self.runtime.snapshot['state'], 'ready')
        self.assertEqual(self.runtime._intent(), 'ready')

    def test_startup_pass_never_outlives_its_lifecycle_bound(self):
        # The whole lifecycle (identity checks, launch reply, readiness poll)
        # runs inside one MCP_TIMEOUT bound; it must cover the poll window.
        self.assertGreaterEqual(MCP_TIMEOUT, READY_TIMEOUT + 45)

    def test_confirmed_dead_launch_fails_definitely_and_retries_without_reset(self):
        self.launch_exits = 7
        with patch('rmm_exegol_runtime.probe_ce', return_value='stopped'), \
                patch('rmm_exegol_runtime.READY_TIMEOUT', 1):
            self.runtime._run()
        self.assertEqual(self.runtime.snapshot['state'], 'failed')
        self.assertEqual(self.runtime.snapshot['error'], 'bloodhound_launch_failed')
        self.assertEqual(self.runtime.snapshot['launch_exit_code'], 7)
        self.assertEqual(self.runtime.snapshot['bloodhound_status'], 'unavailable')
        # The reply proves the dispatch happened and the process died, so the
        # durable intent is cleared and a plain ensure() can retry.
        self.assertIsNone(self.runtime._intent())
        self.launch_exits = None
        with patch('rmm_exegol_runtime.probe_ce', side_effect=['stopped', 'ready']), \
                patch('rmm_exegol_runtime.READY_TIMEOUT', 1):
            self.runtime._run()
        self.assertEqual(self.runtime.snapshot['state'], 'ready')
        self.assertIsNone(self.runtime.snapshot.get('launch_exit_code'))
        self.assertEqual(sum(row['command'] == LAUNCH_COMMAND for row in self.calls), 2)

    def test_zero_or_markerless_launch_reply_keeps_uncertainty(self):
        # EXITED:0 cannot distinguish a live boot from a forked detacher, and
        # an unrecognized reply shape proves nothing: both stay uncertain and
        # keep the durable intent so the launch is never replayed.
        for reply in ('RMM_CE_LAUNCH_EXITED:0', json.dumps({'output': 'unexpected shape'})):
            with self.subTest(reply=reply):
                (self.runtime.root / 'ce-startup.json').unlink(missing_ok=True)
                self.runtime.verified_configuration = None
                self.runtime._update(state='blocked', ready=False, exegol_status='not_checked')
                self.launch_reply = reply
                with patch('rmm_exegol_runtime.probe_ce', return_value='stopped'), \
                        patch('rmm_exegol_runtime.READY_TIMEOUT', 0):
                    self.runtime._run()
                self.assertEqual(self.runtime.snapshot['state'], 'uncertain')
                self.assertEqual(self.runtime.snapshot['error'], 'bloodhound_startup_uncertain')
                self.assertEqual(self.runtime._intent(), 'dispatched')
                self.assertEqual(sum(row['command'] == LAUNCH_COMMAND for row in self.calls), 1)
                self.calls.clear()

    def test_definite_launch_failure_survives_read_only_health_refresh(self):
        # The proven launch-failure diagnosis must not be relabeled as a
        # plain 'stopped' service by the periodic health probe; the operator
        # would lose the pointer to the wrapper log and exit code.
        self.launch_exits = 1
        with patch('rmm_exegol_runtime.probe_ce', return_value='stopped'), \
                patch('rmm_exegol_runtime.READY_TIMEOUT', 1):
            self.runtime._run()
        self.assertEqual(self.runtime.snapshot['error'], 'bloodhound_launch_failed')
        self.runtime.last_probe = 0
        with patch('rmm_exegol_runtime.probe_ce', return_value='stopped') as probe:
            state = self.runtime.status()
        probe.assert_called_once()
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['error'], 'bloodhound_launch_failed')
        self.assertEqual(state['launch_exit_code'], 1)
        self.assertEqual(state['bloodhound_status'], 'stopped')
        # A CE that actually comes up still reports ready with the code cleared.
        self.runtime.last_probe = 0
        with patch('rmm_exegol_runtime.probe_ce', return_value='ready'):
            state = self.runtime.status()
        self.assertTrue(state['ready'])
        self.assertIsNone(state.get('launch_exit_code'))

    def test_owner_configuration_change_invalidates_verified_readiness(self):
        with patch('rmm_exegol_runtime.probe_ce', return_value='ready'):
            self.runtime._run()
            self.assertTrue(self.runtime.status()['ready'])
        old = configuration()
        self.runtime.last_probe = float('inf')
        for changed in ((old[0] + '/new', *old[1:]),
                        (old[0], 'changed-token', *old[2:]),
                        (*old[:2], 'other-container', old[3])):
            self.runtime.verified_configuration = old
            self.runtime._update(state='ready', ready=True, exegol_status='ready')
            with patch('rmm_exegol_runtime.configuration', return_value=changed), \
                    patch('rmm_exegol_runtime.probe_ce') as probe:
                state = self.runtime.status()
                self.assertFalse(state['ready'])
                self.assertEqual(state['exegol_status'], 'not_checked')
                self.assertEqual(state['error'], 'runtime_configuration_changed')
                probe.assert_not_called()
                self.runtime.thread = Mock()
                self.runtime.thread.is_alive.return_value = True
                self.runtime.thread.join.return_value = None
                self.assertFalse(self.runtime.status()['ready'])
                self.runtime.thread = None

    def test_lost_reply_uncertain_never_replayed_after_restart(self):
        self.launch_raises = True
        with patch('rmm_exegol_runtime.probe_ce', return_value='stopped'):
            self.runtime._run()
            self.assertEqual(self.runtime.snapshot['state'], 'uncertain')
            self.assertNotIn('secret', str(self.runtime.snapshot))
            recovered = ExegolRuntime(self.root)
            recovered._run()
            self.assertEqual(recovered.snapshot['state'], 'uncertain')
        self.assertEqual(sum(row['command'] == LAUNCH_COMMAND for row in self.calls), 1)

    def test_reset_unblocks_a_retry_after_lost_reply(self):
        self.launch_raises = True
        with patch('rmm_exegol_runtime.probe_ce', return_value='stopped'):
            self.runtime._run()
            self.assertEqual(self.runtime.snapshot['state'], 'uncertain')
            recovered = ExegolRuntime(self.root)
            recovered._run()
            self.assertEqual(recovered.snapshot['state'], 'uncertain')
        snapshot = recovered.reset()
        self.assertEqual(snapshot['state'], 'blocked')
        self.assertFalse(snapshot['ready'])
        self.assertIsNone(recovered._intent())
        self.launch_raises = False
        with patch('rmm_exegol_runtime.probe_ce', side_effect=['stopped', 'ready']), \
                patch('rmm_exegol_runtime.READY_TIMEOUT', 1):
            recovered._run()
        self.assertEqual(recovered.snapshot['state'], 'ready')
        self.assertEqual(sum(row['command'] == LAUNCH_COMMAND for row in self.calls), 2)

    def test_reset_refuses_while_ready_or_in_progress(self):
        with patch('rmm_exegol_runtime.probe_ce', return_value='ready'):
            self.runtime._run()
        with self.assertRaisesRegex(RuntimeErrorCode, 'bloodhound_already_ready'):
            self.runtime.reset()
        self.runtime._update(state='checking', ready=False)
        self.runtime.thread = Mock()
        self.runtime.thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeErrorCode, 'bloodhound_startup_in_progress'):
            self.runtime.reset()
        self.runtime.thread = None
        self.runtime._update(state='failed', ready=False)
        with self.assertRaisesRegex(RuntimeErrorCode, 'bloodhound_reset_requires_uncertain_state'):
            self.runtime.reset()

    def test_read_only_health_reconciles_uncertainty(self):
        self.runtime._save('dispatched')
        self.runtime._update(exegol_status='ready', state='uncertain')
        with patch('rmm_exegol_runtime.probe_ce', return_value='ready'):
            self.assertTrue(self.runtime.status()['ready'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime._intent(), 'ready')

    def test_wrong_container_cannot_start_ce(self):
        self.wrong_identity = True
        with patch('rmm_exegol_runtime.probe_ce', return_value='stopped'):
            self.runtime._run()
        self.assertEqual(self.runtime.snapshot['error'], 'exegol_workspace_identity_mismatch')
        self.assertEqual(self.runtime.snapshot['exegol_status'], 'unavailable')
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.runtime._intent())

    def test_unavailable_endpoint_answers_are_never_restarted(self):
        # 'authentication_failed' is deliberately absent: a CE answering 401
        # to the credential-free probe has started (see the tests above);
        # only an unhealthy or invalid answer must not relaunch the service.
        for result in ('invalid_response', 'unavailable'):
            with self.subTest(result=result), patch('rmm_exegol_runtime.probe_ce', return_value=result):
                self.runtime._run()
                self.assertEqual(self.runtime.snapshot['exegol_status'], 'ready')
                self.assertEqual(self.runtime.snapshot['error'], 'bloodhound_' + result)
                self.assertIsNone(self.runtime._intent())
        self.assertFalse(any(row['command'] == LAUNCH_COMMAND for row in self.calls))

    def test_probe_only_connection_refusal_counts_as_stopped(self):
        endpoint = configuration()[3]
        connection = Mock()
        response = connection.getresponse.return_value
        with patch('rmm_exegol_runtime.http.client.HTTPConnection', return_value=connection):
            response.status = 403
            response.read.return_value = b'forbidden'
            self.assertEqual(probe_ce(endpoint), 'authentication_failed')
            response.status = 200
            response.read.return_value = b'{}'
            self.assertEqual(probe_ce(endpoint), 'invalid_response')
            response.read.return_value = b'{"data":{"server_version":"v8"}}'
            self.assertEqual(probe_ce(endpoint), 'ready')
            connection.request.side_effect = TimeoutError()
            self.assertEqual(probe_ce(endpoint), 'unavailable')
            connection.request.side_effect = ConnectionRefusedError(61, 'refused')
            with patch('rmm_exegol_runtime.errno.ECONNREFUSED', 61):
                self.assertEqual(probe_ce(endpoint), 'stopped')

    def test_stopped_runtime_cannot_return_ready_or_probe(self):
        self.runtime._update(exegol_status='ready', ready=True, state='ready')
        self.runtime.stop()
        with patch('rmm_exegol_runtime.probe_ce') as probe:
            state = self.runtime.status()
            self.assertEqual(state['error'], 'runtime_stopped')
            self.assertEqual(state['exegol_status'], 'stopped')
            self.assertEqual(state['mcp_status'], 'stopped')
            self.assertFalse(self.runtime.ensure()['ready'])
            late = self.runtime._update(exegol_status='ready', mcp_status='connected', ready=True)
            self.assertEqual(late['exegol_status'], 'stopped')
            self.assertFalse(late['ready'])
            probe.assert_not_called()

    def test_missing_launcher_does_not_disable_verified_exegol(self):
        self.missing_launcher = True
        with patch('rmm_exegol_runtime.probe_ce', return_value='stopped'):
            self.runtime._run()
        self.assertEqual(self.runtime.snapshot['exegol_status'], 'ready')
        self.assertEqual(self.runtime.snapshot['error'], 'bloodhound_launcher_missing')
        self.assertEqual(len(self.calls), 1)

    def test_single_flight_and_stop(self):
        entered, release = threading.Event(), threading.Event()
        def work():
            entered.set()
            release.wait(2)
        with patch.object(self.runtime, '_run', side_effect=work) as worker:
            self.runtime.ensure()
            self.assertTrue(entered.wait(1))
            for _ in range(5):
                self.runtime.ensure()
            release.set()
            self.runtime.stop()
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(self.runtime.ensure()['error'], 'runtime_stopped')

    def test_schema_and_configuration_fail_closed(self):
        with self.assertRaises(RuntimeErrorCode):
            execution_arguments([], 'lab', 'bloodhound-ce')
        schema = json.loads(json.dumps(TOOLS))
        schema[0]['function']['parameters']['required'].append('unknown')
        with self.assertRaises(RuntimeErrorCode):
            execution_arguments(schema, 'lab', 'bloodhound-ce')
        with patch('rmm_exegol_runtime.getenv', return_value=''):
            with self.assertRaises(RuntimeErrorCode):
                configuration()
        with patch('rmm_exegol_runtime.getenv', side_effect=lambda key, default='': {
                'RMM_EXEGOL_MCP_URL': 'http://localhost/mcp', 'RMM_EXEGOL_CONTAINER': 'lab',
                'RMM_BLOODHOUND_URL': 'https://other-workspace.example'}.get(key, default)):
            with self.assertRaisesRegex(RuntimeErrorCode, 'workspace_local'):
                configuration()


if __name__ == '__main__':
    unittest.main()
