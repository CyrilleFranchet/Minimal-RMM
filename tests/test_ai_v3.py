"""V3 contracts: static authority, exact evidence, bounded memory and no shell fallback."""
import asyncio
import copy
import json
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from rmm_ai_capsules import CapsuleRegistry, CAPSULES, BASELINES, FULL_BASELINE
from rmm_ai_context import ContextWindow
from rmm_ai_kernel import ExecutionKernel, KernelError
from rmm_ai_skills import resolve_ai_skill_snapshot
from rmm_ai_v3 import run_v3
from rmm_tools import OPENAI_TOOLS

EXTERNAL = {'type': 'function', 'function': {'name': 'execute_command_in_container',
    'parameters': {'type': 'object', 'properties': {'container_name': {'type': 'string'}, 'command': {'type': 'string'}},
                   'required': ['container_name', 'command']}}}


def proposal(kind='OBSERVE', target='session-a', objective='Inspect current state', basis=None):
    return {'kind': kind, 'target': target, 'objective': objective, 'basis': basis or []}


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.kernel = ExecutionKernel(self.temp.name, 'a' * 64, 'Inspect', 'session-a', 'observe', {})

    def tearDown(self):
        self.kernel.close()
        self.temp.cleanup()

    def task(self, kind='OBSERVE'):
        task = self.kernel.add_task(proposal(kind), self.kernel.revision, 4)
        return task, self.kernel.lease(task['id'])

    def test_stale_revision_and_double_lease_fail(self):
        with self.assertRaisesRegex(KernelError, 'stale_plan'):
            self.kernel.add_task(proposal(), 0, 4)
        task, attempt = self.task()
        with self.assertRaises(KernelError):
            self.kernel.lease(task['id'])
        with self.assertRaisesRegex(KernelError, 'task_already_open'):
            self.kernel.add_task(proposal(), self.kernel.revision, 4)

    def test_evidence_must_be_exact_and_queued_receipt_cannot_complete(self):
        task, attempt = self.task('AGENT_COMMAND')
        call, _ = self.kernel.start_call(attempt, 'queue_command', {'command': 'example'})
        self.kernel.receipt(call['id'], 'artifact', 'accepted')
        with self.assertRaisesRegex(KernelError, 'evidence_excerpt_not_verbatim'):
            self.kernel.finish_task(task['id'], attempt, 'done', call['id'], 'It worked', lambda _: 'queued')
        with self.assertRaisesRegex(KernelError, 'receipt_does_not_prove_completion'):
            self.kernel.finish_task(task['id'], attempt, 'done', call['id'], 'queued', lambda _: 'queued')
        oid = self.kernel.finish_task(task['id'], attempt, 'progress', call['id'], 'queued', lambda _: 'queued')
        self.assertTrue(oid)
        with self.assertRaisesRegex(KernelError, 'run_has_unresolved_work'):
            self.kernel.complete()

    def test_dispatch_intent_survives_interruption_and_mutations_are_not_replayed(self):
        task, attempt = self.task('AGENT_COMMAND')
        first, fresh = self.kernel.start_call(attempt, 'exec_command', {'command': 'example'})
        self.assertTrue(fresh)
        second, fresh = self.kernel.start_call(attempt, 'exec_command', {'command': 'example'})
        self.assertFalse(fresh)
        self.assertEqual(first['id'], second['id'])
        self.kernel.interrupt(task['id'], attempt)
        with sqlite3.connect(self.kernel.path) as db:
            self.assertEqual(db.execute('SELECT state FROM invocations').fetchone()[0], 'uncertain')
        with self.assertRaises(KernelError):
            self.kernel.lease(task['id'])

    def test_reads_refresh_then_return_previous_receipt_after_repetition(self):
        task, attempt = self.task()
        for _ in range(2):
            call, fresh = self.kernel.start_call(attempt, 'get_events', {}, read_only=True)
            self.assertTrue(fresh)
            self.kernel.receipt(call['id'], 'answer', 'read')
        previous, fresh = self.kernel.start_call(attempt, 'get_events', {}, read_only=True)
        self.assertFalse(fresh)
        self.assertEqual(previous['artifact_id'], 'answer')

    def test_hundreds_of_tasks_do_not_enlarge_projection(self):
        sizes = []
        for index in range(200):
            task = self.kernel.add_task(proposal(objective=f'Inspect item {index}'), self.kernel.revision, 4)
            attempt = self.kernel.lease(task['id'])
            call, _ = self.kernel.start_call(attempt, 'get_session', {'item': index}, read_only=True)
            self.kernel.receipt(call['id'], 'artifact', 'read')
            self.kernel.finish_task(task['id'], attempt, 'done', call['id'], 'Observed', lambda _: 'Observed')
            if index in (20, 199):
                sizes.append(len(json.dumps(self.kernel.supervisor_view())))
        view = self.kernel.supervisor_view()
        self.assertEqual(len(view['closed_tasks']), 4)
        self.assertEqual(len(view['observations']), 6)
        self.assertLess(abs(sizes[1] - sizes[0]), 100)
        self.kernel.complete()


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.documents, self.policy = resolve_ai_skill_snapshot(None)
        self.kernel = ExecutionKernel(self.temp.name, 'b' * 64, 'Scan 10.0.0.10', 'session-a', 'network', {})
        patcher = patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: self.temp.name if key == 'RMM_LOG_DIR' else default)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.archive = ContextWindow(scope='b' * 64)
        self.remote = AsyncMock(return_value='{"ok":true}')
        self.registry = CapsuleRegistry(self.documents, [*OPENAI_TOOLS, EXTERNAL], self.remote,
            'session-a', 'owned-container', {'execute_command_in_container'}, 'network',
            'Scan 10.0.0.10', self.kernel, self.archive, self.policy, {'targets': ['10.0.0.10'], 'proxy_host': '127.0.0.1'})

    def tearDown(self):
        self.kernel.close()
        self.temp.cleanup()

    def baseline_done(self):
        task = self.kernel.add_task(proposal('BASELINE'), self.kernel.revision, 5)
        attempt = self.kernel.lease(task['id'])
        call, _ = self.kernel.start_call(attempt, 'agent_baseline', {})
        self.kernel.receipt(call['id'], 'artifact', 'completed')
        self.kernel.finish_task(task['id'], attempt, 'done', call['id'], 'Observed', lambda _: 'Observed')

    def test_model_cannot_grant_an_agent_shell_or_send_commands_through_baseline(self):
        with self.assertRaisesRegex(KernelError, 'agent_command_workflow_not_selected'):
            self.registry.compile(proposal('AGENT_COMMAND'))
        task, _, schemas, _ = self.registry.compile(proposal('BASELINE'))
        names = {t['function']['name'] for t in schemas}
        self.assertNotIn('exec_command', names)
        self.assertNotIn('queue_command', names)
        with self.assertRaisesRegex(KernelError, 'invalid_baseline_arguments'):
            asyncio.run(self.registry.dispatch(task, 'agent_baseline', {'section': 'network', 'command': 'manual scan'}))
        self.remote.assert_not_awaited()

    def test_scan_requires_completed_baseline_and_verification_and_disallows_scope_expansion(self):
        with self.assertRaisesRegex(KernelError, 'completed_baseline_required'):
            self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.10'))
        self.baseline_done()
        task, _, schemas, _ = self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.10'))
        self.assertNotIn('exec_command', {t['function']['name'] for t in schemas})
        with self.assertRaisesRegex(KernelError, 'verified_pivot_receipt_required'):
            asyncio.run(self.registry.dispatch(task, 'network_scan', {'transport': 'socks'}))
        with self.assertRaisesRegex(KernelError, 'scan_target_outside_operator_scope'):
            self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.0/24'))
        with self.assertRaisesRegex(KernelError, 'route_probe_outside_task_scope'):
            asyncio.run(self.registry.dispatch(task, 'verify_network_route', {'probe_ip': '10.0.1.1', 'port': 443, 'transport': 'socks'}))

    def test_disabled_required_method_blocks_capsule(self):
        self.baseline_done()
        del self.registry.gate.skills['exegol-rmm-pivot']
        with self.assertRaisesRegex(KernelError, 'skill_disabled_or_unknown'):
            self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.10'))


class EpisodeIntegrationTests(unittest.TestCase):
    def test_scan_uses_bounded_baseline_then_structured_exegol_on_all_providers(self):
        for provider in ('openai', 'mistral', 'anthropic'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                docs, policy = resolve_ai_skill_snapshot(None)
                requests, dispatched, events = [], [], []
                async def remote(name, args):
                    dispatched.append((name, args))
                    if name == 'get_session':
                        return '{"ok":true,"session":{"os_type":"windows"}}'
                    if name == 'exec_command':
                        return json.dumps({'ok': True, 'output': json.dumps({key: {'status': 'known', 'value': {'fixture': True}} for key in BASELINES})})
                    if name == 'list_socks':
                        return '{"ok":true,"relays":[{"session_id":"session-a","listener_running":true,"bind_host":"127.0.0.1","port":1080}]}'
                    if name == 'execute_command_in_container':
                        self.assertEqual(args['container_name'], 'owned-container')
                        self.assertIn('proxychains', args['command'])
                        self.assertIn('nmap -sT', args['command'])
                        marker = re.search(r'RMM_(?:VERIFY|SCAN)_[a-f0-9]+', args['command']).group()
                        return '<?xml version="1.0"?><!DOCTYPE nmaprun><nmaprun><host><address addr="10.0.0.10"/><ports><port><state state="closed"/></port></ports></host></nmaprun>\n' + marker + ':0\n'
                    raise AssertionError(name)
                def request(url, headers, body):
                    requests.append(copy.deepcopy(body))
                    state = json.loads(body['messages'][-1]['content'])
                    if 'memory' in state:
                        closed = state['memory']['closed_tasks']
                        if any(t['kind'] == 'NETWORK_SCAN' for t in closed):
                            name, args = 'finish_run', {'status': 'complete', 'report': 'Scan completed; exact receipts retained.'}
                        else:
                            kind = 'NETWORK_SCAN' if closed else 'BASELINE'
                            name, args = 'plan_task', {**proposal(kind, '10.0.0.10' if closed else 'session-a', 'Scan target' if closed else 'Establish local network baseline'), 'revision': state['memory']['revision']}
                    else:
                        receipts = state['recent_receipts']
                        kind = state['task']['kind']
                        if kind == 'BASELINE' and not receipts:
                            name, args = 'agent_baseline', {}
                        elif kind == 'NETWORK_SCAN' and not receipts:
                            name, args = 'verify_network_route', {'probe_ip': '10.0.0.10', 'port': 443, 'transport': 'socks'}
                        elif kind == 'NETWORK_SCAN' and receipts[0]['tool'] == 'verify_network_route':
                            name, args = 'network_scan', {'ports': '22,443', 'transport': 'socks'}
                        else:
                            name, args = 'finish_task', {'status': 'done', 'receipt_id': receipts[0]['receipt_id'], 'evidence_excerpt': receipts[0]['excerpt']}
                    if provider == 'anthropic':
                        return {'content': [{'type': 'tool_use', 'id': str(len(requests)), 'name': name, 'input': args}]}
                    return {'choices': [{'message': {'tool_calls': [{'id': str(len(requests)), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}
                values = lambda key, default=None: root if key == 'RMM_LOG_DIR' else default
                with patch('rmm_ai._json_request', side_effect=request), patch('rmm_ai_context.getenv', side_effect=values), patch('rmm_ai_v3.getenv', side_effect=values):
                    result = asyncio.run(run_v3(provider=provider, api_key='test', model='test',
                        messages=[{'role': 'user', 'content': 'Scan 10.0.0.10'}], documents=docs,
                        tools=[*OPENAI_TOOLS, EXTERNAL], call_tool=remote, scope='c' * 64,
                        selected_session_id='session-a', container='owned-container',
                        external_tool_names={'execute_command_in_container'}, workflow='network', policy=policy, execution_scope={'targets': ['10.0.0.10'], 'proxy_host': '127.0.0.1'}, progress=events.append))
                self.assertTrue(result['ok'], result)
                self.assertEqual(sum(name == 'exec_command' for name, _ in dispatched), 1)
                self.assertEqual(sum(name == 'execute_command_in_container' for name, _ in dispatched), 2)
                for body in requests:
                    self.assertLess(len(body['messages']), 3)
                    self.assertNotIn('Recent archived evidence IDs available through read_context', body.get('system', '') + str(body['messages']))
                    tools = {t.get('name') or t['function']['name'] for t in body['tools']}
                    self.assertNotIn('exec_command', tools)
                    self.assertNotIn('execute_command_in_container', tools)
                self.assertEqual(result['execution_state']['run_state'], 'done')
                for event in events:
                    if event.get('phase') == 'supervisor':
                        self.assertEqual(event['role'], 'SUPERVISOR')
                        self.assertIsNone(event['task_id'])
                        self.assertNotIn('exegol-rmm-pivot', event['active_skills'])

class AdmissionRegressionTests(CapsuleTests):
    def test_prose_never_grants_network_scope_and_exclusions_win(self):
        self.baseline_done()
        self.registry.authorized_networks = []
        self.registry.objective = 'Scan 10.0.0.10; never scan 10.0.0.11'
        with self.assertRaisesRegex(KernelError, 'outside_operator_scope'):
            self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.10'))
        import ipaddress
        self.registry.authorized_networks = [ipaddress.ip_network('10.0.0.0/24')]
        self.registry.excluded_networks = [ipaddress.ip_network('10.0.0.11')]
        with self.assertRaisesRegex(KernelError, 'outside_operator_scope'):
            self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.0/24'))
        self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.10'))

    def test_wrong_receipt_cannot_complete_a_scan_or_baseline(self):
        for kind, tool in [('BASELINE', 'get_session'), ('NETWORK_SCAN', 'verify_network_route')]:
            task = self.kernel.add_task(proposal(kind), self.kernel.revision, 4)
            attempt = self.kernel.lease(task['id'])
            call, _ = self.kernel.start_call(attempt, tool, {}, read_only=True)
            self.kernel.receipt(call['id'], 'artifact', 'completed')
            with self.assertRaisesRegex(KernelError, 'task_specific_completion'):
                self.kernel.finish_task(task['id'], attempt, 'done', call['id'], 'ok', lambda _: 'ok')
            self.kernel.finish_task(task['id'], attempt, 'blocked', call['id'], 'ok', lambda _: 'ok')

    def test_commands_require_exact_grant_and_embeddings_are_read_only(self):
        self.registry.workflow = 'agent_commands'
        with self.assertRaisesRegex(KernelError, 'exact_command_grant'):
            self.registry.compile(proposal('AGENT_COMMAND'))
        self.registry.execution_scope['commands'] = ['whoami']
        task, _, _, _ = self.registry.compile(proposal('AGENT_COMMAND'))
        with self.assertRaisesRegex(KernelError, 'command_not_operator_approved'):
            asyncio.run(self.registry.dispatch(task, 'exec_command', {'command': 'whoami; other'}))
        self.registry.workflow = 'embeddings'
        task, _, _, _ = self.registry.compile(proposal('EMBEDDINGS'))
        for action in ('delete', 'save'):
            with self.assertRaisesRegex(KernelError, 'read_only_embedding'):
                asyncio.run(self.registry.dispatch(task, 'embedding_profiles', {'action': action}))
        self.remote.assert_not_awaited()

    def test_malformed_task_kind_is_a_compiler_error(self):
        with self.assertRaisesRegex(KernelError, 'unknown_task_kind'):
            self.registry.compile(proposal(kind=[]))

    def test_route_probe_accepts_aggregated_closed_ports_only_on_expected_host(self):
        self.baseline_done()
        task, _, _, _ = self.registry.compile(proposal('NETWORK_SCAN', '10.0.0.10'))
        async def remote(name, args):
            if name == 'list_socks':
                return '{"ok":true,"relays":[{"session_id":"session-a","listener_running":true,"bind_host":"127.0.0.1","port":1080}]}'
            marker = re.search(r'RMM_VERIFY_[a-f0-9]+', args['command']).group()
            return '<' + '?xml version="1.0"?><nmaprun><host><address addr="10.0.0.10"/><ports><extraports state="closed"><extrareasons reason="conn-refused"/></extraports></ports></host></nmaprun>\n' + marker + ':0'
        self.registry.backend = remote
        result = json.loads(asyncio.run(self.registry.dispatch(task, 'verify_network_route', {'probe_ip': '10.0.0.10', 'port': 443, 'transport': 'socks'})))
        self.assertTrue(result['verified'])


class MethodAndQueryTests(CapsuleTests):
    def test_additional_enabled_skill_loads_only_its_capsule_and_disabled_is_rejected(self):
        self.registry.documents['step-method'] = {'id': 'step-method', 'body': 'Inspect the current step carefully.'}
        self.registry.gate.skills['step-method'] = self.registry.documents['step-method']
        task, instructions, _, _ = self.registry.compile({**proposal(), 'skill_ids': ['step-method']})
        self.assertIn('Inspect the current step carefully.', instructions)
        del self.registry.gate.skills['step-method']
        with self.assertRaisesRegex(KernelError, 'skill_disabled_or_unknown'):
            self.registry.compile({**proposal(), 'skill_ids': ['step-method']})

    def test_bloodhound_health_cannot_complete_a_graph_query(self):
        from rmm_ai_capsules import receipt_class
        task = self.kernel.add_task(proposal('DIRECTORY_QUERY', 'graph'), self.kernel.revision, 4)
        attempt = self.kernel.lease(task['id'])
        raw = '{"ok":true,"data":{"runtime":{"state":"ready"}}}'
        call, _ = self.kernel.start_call(attempt, 'bloodhound', {'action': 'health'})
        self.kernel.receipt(call['id'], 'artifact', receipt_class('bloodhound', raw))
        with self.assertRaisesRegex(KernelError, 'receipt_does_not_prove_completion'):
            self.kernel.finish_task(task['id'], attempt, 'done', call['id'], 'ready', lambda _: raw)
        query = '{"ok":true,"data":{"evidence":{"nodes":1},"graph_preview":{"nodes":{},"edges":[]}}}'
        self.assertEqual(receipt_class('bloodhound', query), 'graph_query')


class RecoveryRegressionTests(unittest.TestCase):
    def test_pre_dispatch_rejection_can_retry_but_backend_failure_cannot(self):
        with tempfile.TemporaryDirectory() as root:
            kernel = ExecutionKernel(root, 'f' * 64, 'Inspect', 'session-a', 'observe', {})
            try:
                task = kernel.add_task(proposal(), kernel.revision, 4)
                attempt = kernel.lease(task['id'])
                first, _ = kernel.start_call(attempt, 'network_scan', {'transport': 'socks'})
                kernel.reject(first['id'], 'local-rejection')
                second, fresh = kernel.start_call(attempt, 'network_scan', {'transport': 'socks'})
                self.assertTrue(fresh)
                kernel.receipt(second['id'], 'backend-error', 'error')
                _, fresh = kernel.start_call(attempt, 'network_scan', {'transport': 'socks'})
                self.assertFalse(fresh)
            finally:
                kernel.close()

    def test_network_run_cannot_complete_with_partial_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            kernel = ExecutionKernel(root, 'f' * 64, 'Scan', 'session-a', 'network', {}, execution_scope={'targets': ['10.0.0.0/24']})
            try:
                task = kernel.add_task(proposal('NETWORK_SCAN', '10.0.0.10/32'), kernel.revision, 4)
                attempt = kernel.lease(task['id'])
                call, _ = kernel.start_call(attempt, 'network_scan', {})
                kernel.receipt(call['id'], 'artifact', 'completed')
                kernel.finish_task(task['id'], attempt, 'done', call['id'], 'completed', lambda _: 'completed')
                with self.assertRaisesRegex(KernelError, 'network_objective_not_covered'):
                    kernel.complete()
            finally:
                kernel.close()

    def test_async_collection_waits_in_code_across_many_pending_statuses(self):
        with tempfile.TemporaryDirectory() as root:
            docs, policy = resolve_ai_skill_snapshot(None)
            kernel = ExecutionKernel(root, 'e' * 64, 'Collect', 'session-a', 'collect', {})
            try:
                registry = CapsuleRegistry(docs, OPENAI_TOOLS, AsyncMock(), 'session-a', 'owned', set(), 'collect', 'Collect', kernel, None, policy)
                def receipt(state):
                    return {'ok': True, 'data': {'run': {'domain': 'lab.example', 'state': state}}}
                registry.owner_read = AsyncMock(side_effect=[*[receipt('collecting') for _ in range(7)], receipt('collected')])
                with patch('rmm_ai_capsules.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    result = asyncio.run(registry.dispatch(proposal('DIRECTORY_COLLECT', 'lab.example'), 'bloodhound', {'action': 'status', 'run_id': 'a' * 32}))
                self.assertEqual(json.loads(result)['data']['run']['state'], 'collected')
                self.assertGreater(sleep.await_count, 4)
            finally:
                kernel.close()


class PlanOnlyTests(unittest.TestCase):
    def test_default_plan_has_no_execution_tools_and_persists_constraints(self):
        with tempfile.TemporaryDirectory() as root:
            backend = AsyncMock()
            async def model(provider, key, model, system, state, tools, window, progress, number):
                self.assertEqual([t['function']['name'] for t in tools], ['finish_run'])
                self.assertEqual(state['capabilities'], {})
                return {'name': 'finish_run', 'arguments': {'status': 'plan', 'report': 'Use a baseline and Exegol scan.'}}
            values = lambda key, default=None: root if key == 'RMM_LOG_DIR' else default
            with patch('rmm_ai_v3.episode', side_effect=model), patch('rmm_ai_v3.getenv', side_effect=values), patch('rmm_ai_context.getenv', side_effect=values):
                result = asyncio.run(run_v3(provider='openai', api_key='test', model='test',
                    messages=[{'role': 'user', 'content': 'Scan the lab; exclude the gateway.'}], documents=[], tools=[],
                    call_tool=backend, scope='d' * 64, request_id='operator-request'))
            self.assertEqual(result['outcome'], 'plan')
            backend.assert_not_awaited()
            with sqlite3.connect(root + '/ai-runs/' + 'd' * 64 + '/' + result['run_id'] + '.sqlite3') as db:
                row = db.execute('SELECT constraints,request_id FROM runs').fetchone()
                self.assertIn('exclude the gateway', row[0])
                self.assertEqual(row[1], 'operator-request')


class ContextAdmissionTests(unittest.TestCase):
    def test_oversized_method_budget_stops_before_provider_or_remote_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            docs, policy = resolve_ai_skill_snapshot(None)
            values = lambda key, default=None: {'RMM_LOG_DIR': root, 'RMM_AI_CONTEXT_BUDGET': '6000', 'RMM_AI_OUTPUT_RESERVE': '4096'}.get(key, default)
            backend = AsyncMock()
            with patch('rmm_ai_v3.getenv', side_effect=values), patch('rmm_ai_context.getenv', side_effect=values), patch('rmm_ai._json_request') as provider:
                result = asyncio.run(run_v3(provider='openai', api_key='test', model='test', documents=docs, policy=policy,
                    messages=[{'role': 'user', 'content': 'Explain the lab.'}], tools=[], call_tool=backend, scope='a' * 64))
            self.assertEqual(result['error'], 'context_budget_exceeded')
            backend.assert_not_awaited()
            provider.assert_not_called()


class FullReviewRegressionTests(CapsuleTests):
    def test_large_baseline_retains_source_without_a_truncated_completion_receipt(self):
        from rmm_ai_capsules import receipt_class
        from rmm_ai_v3 import read_evidence
        baseline = {key: {'status': 'known', 'value': 'local fact ' * 2000} for key in BASELINES}
        async def backend(name, args):
            if name == 'get_session':
                return '{"ok":true,"session":{"os_type":"windows"}}'
            return json.dumps({'ok': True, 'output': json.dumps(baseline)})
        self.registry.backend = backend
        task = self.kernel.add_task(proposal('BASELINE'), self.kernel.revision, 5)
        attempt = self.kernel.lease(task['id'])
        raw = asyncio.run(self.registry.dispatch(task, 'agent_baseline', {}))
        self.assertEqual(receipt_class('agent_baseline', raw), 'completed')
        receipt, _ = self.kernel.start_call(attempt, 'agent_baseline', {})
        self.kernel.receipt(receipt['id'], self.archive.archive(raw, 'receipt'), 'completed')
        page = read_evidence(self.kernel, self.archive, task, attempt, {'receipt_id': receipt['id'], 'part': 'source'})
        self.assertIn('local fact', page['content'])
        self.assertIsNotNone(page['next_offset'])
        with self.assertRaisesRegex(KernelError, 'invalid_evidence_part'):
            read_evidence(self.kernel, self.archive, task, attempt, {'receipt_id': receipt['id'], 'part': []})

    def test_collection_requires_selected_session_baseline(self):
        self.registry.workflow = 'collect'
        self.registry.execution_scope['directory'] = 'lab.example'
        with self.assertRaisesRegex(KernelError, 'completed_baseline_required'):
            self.registry.compile(proposal('DIRECTORY_COLLECT', 'lab.example'))
        self.baseline_done()
        self.registry.compile(proposal('DIRECTORY_COLLECT', 'lab.example'))

    def test_tun_checks_every_destination_before_a_subnet_scan(self):
        relay = {'session_id': 'session-a', 'helper_alive': True, 'device': 'rmm0', 'routes': ['10.0.0.0/24']}
        commands = []
        async def backend(name, args):
            if name == 'list_tun':
                return json.dumps({'ok': True, 'relays': [relay]})
            command = args['command']
            commands.append(command)
            self.assertIn('10.0.0.10', command)
            return '\n'.join(json.dumps([{'dev': 'eth0' if n == 10 else 'rmm0'}]) for n in range(256))
        self.registry.backend = backend
        with self.assertRaisesRegex(KernelError, 'exegol_tun_route_unverified'):
            asyncio.run(self.registry._nmap(proposal('NETWORK_SCAN', '10.0.0.0/24'), '10.0.0.0/24', '443', 'tun', 'RMM_SCAN_test'))
        self.assertEqual(len(commands), 1)
        self.assertNotIn('nmap', commands[0])

    def test_incomplete_baseline_cannot_claim_complete(self):
        async def backend(name, args):
            if name == 'get_session':
                return '{"ok":true,"session":{"os_type":"windows"}}'
            return json.dumps({'ok': True, 'output': json.dumps({'identity': {}, 'token': {}, 'os': {}, 'network': {}})})
        self.registry.backend = backend
        result = json.loads(asyncio.run(self.registry.dispatch(proposal('BASELINE'), 'agent_baseline', {})))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'baseline_output_unverified')


class LegacyAdmissionTests(unittest.TestCase):
    def test_engine_change_during_transport_setup_cannot_enter_legacy_loop(self):
        import rmm_ai
        with patch('rmm_ai.ai_engine', return_value='v3') as engine, patch('rmm_ai._run_provider_loop', new_callable=AsyncMock) as legacy, patch('rmm_ai_v3.run_v3', new_callable=AsyncMock, return_value={'ok': True}) as modern:
            def client(*args, **kwargs):
                engine.return_value = 'legacy_v2'
                return object()
            with patch('rmm_ai.make_client', side_effect=client):
                result = rmm_ai._run_ai_chat_direct(rmm_base_url='http://unused', rmm_token='test', provider='openai', api_key='test', model='test', messages=[], selected_session_id=None, max_rounds=2, workflow='auto')
            self.assertTrue(result['ok'])
            modern.assert_awaited_once()
            legacy.assert_not_awaited()

    def test_legacy_cannot_execute_plan_explain_or_silently_ignore_scope(self):
        import rmm_ai
        with patch('rmm_ai.ai_engine', return_value='legacy_v2'), patch('rmm_ai._run_provider_loop') as loop, patch('rmm_ai.make_client') as client:
            for workflow in ('auto', 'explain', 'network', 'agent_commands'):
                result = rmm_ai._run_ai_chat_direct(rmm_base_url='http://unused', rmm_token='test', provider='openai', api_key='test', model='test', messages=[], selected_session_id=None, max_rounds=2, workflow=workflow)
                self.assertEqual(result['error'], 'legacy_workflow_required')
            rejection = rmm_ai.legacy_admission('legacy', {'commands': ['whoami']})
            self.assertEqual(rejection['error'], 'legacy_scope_not_supported')
            self.assertIsNone(rmm_ai.legacy_admission('legacy', {}))
            loop.assert_not_called()
            client.assert_not_called()


class BaselineSyntaxTests(unittest.TestCase):
    def test_fixed_powershell_script_parses_without_execution(self):
        import shutil
        import subprocess
        executable = shutil.which('pwsh')
        if not executable:
            self.skipTest('PowerShell parser unavailable')
        command = "$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(),[ref]$tokens,[ref]$errors); if($errors.Count){$errors|ForEach-Object {$_.Message}; exit 1}"
        result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-Command', command], input=FULL_BASELINE[4:], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for expected in ('whoami.exe /all', 'Win32_ComputerSystem', 'ipconfig.exe /all', 'route.exe print', 'WorkingDirectory', 'SID=', "status='unknown'"):
            self.assertIn(expected, FULL_BASELINE)
