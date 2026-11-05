"""Context pressure must preserve instructions, evidence, protocol pairs and scope."""
import asyncio
import copy
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from rmm_ai_context import ContextWindow, ContextDispatcher, ContextBudgetExceeded
from rmm_ai_skill_context import prepare_skill_context
from rmm_ai_concurrency import run_tool_batch


class ContextTests(unittest.TestCase):
    def window(self, budget=12000, **kwargs):
        values = {'RMM_AI_CONTEXT_BUDGET': str(budget), 'RMM_AI_OUTPUT_RESERVE': '1024'}
        with patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: values.get(key, default)):
            return ContextWindow(**kwargs)

    def test_large_results_are_exactly_retrievable_and_calls_are_not_replayed(self):
        remote = AsyncMock(return_value='Evidence' * 2000)
        window = self.window()
        dispatch = ContextDispatcher(remote, window)
        result = json.loads(asyncio.run(dispatch('inspect', {})))
        key = result['artifact_id']
        remote.assert_awaited_once()
        page = json.loads(asyncio.run(dispatch('read_context', {'artifact_id': key, 'offset': 4096, 'limit': 4096})))
        self.assertEqual(page['content'], ('Evidence' * 2000)[4096:8192])
        remote.assert_awaited_once()

    def test_artifacts_survive_new_runs_but_not_cross_scope_access(self):
        with tempfile.TemporaryDirectory() as root:
            values = {'RMM_LOG_DIR': root}
            with patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: values.get(key, default)):
                first = ContextWindow(scope='a' * 64)
                key = first.archive('Original evidence', 'tool_result')
                same = ContextWindow(scope='a' * 64)
                other = ContextWindow(scope='b' * 64)
                self.assertEqual(same.read(key)['content'], 'Original evidence')
                self.assertIsNone(other.read(key))
                self.assertNotEqual(key, same.archive('Other evidence', 'tool_result'))
                self.assertEqual(os.stat(first.path).st_mode & 0o777, 0o600)
                self.assertIsNone(first.read('../../secrets'))

    def test_persisted_archives_export_only_a_manifest_and_page_without_full_reads(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: root if key == 'RMM_LOG_DIR' else default):
                window = ContextWindow(scope='c' * 64)
                text = 'Large evidence ' * 100000
                key = window.archive(text, 'tool_result')
                self.assertNotIn('content', window.artifacts[key])
                self.assertLess(len(json.dumps(window.manifest())), 200)
                dispatcher = ContextDispatcher(AsyncMock(), window)
                with patch.object(window, 'read', side_effect=AssertionError('Full read is forbidden')):
                    page = json.loads(asyncio.run(dispatcher('read_context', {'artifact_id': key, 'offset': 4096, 'limit': 100})))
                self.assertEqual(page['content'], text[4096:4196])
                self.assertEqual(page['next_offset'], 4196)

    def test_persistent_pages_preserve_nul_and_unicode_across_boundaries(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: root if key == 'RMM_LOG_DIR' else default):
                window = ContextWindow(scope='d' * 64)
                text = 'before\x00after' + 'é🙂' * 5000
                key = window.archive(text, 'tool_result')
                self.assertEqual(window.read(key)['content'], text)
                dispatcher = ContextDispatcher(AsyncMock(), window)
                chunks = []
                offset = 0
                with patch.object(window, 'read', side_effect=AssertionError('No full reads for new archives')):
                    while offset is not None:
                        page = json.loads(asyncio.run(dispatcher('read_context', {'artifact_id': key, 'offset': offset, 'limit': 1000})))
                        chunks.append(page['content'])
                        offset = page['next_offset']
                self.assertEqual(''.join(chunks), text)

    def test_earlier_archive_schema_remains_readable_after_upgrade(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root, 'ai-context')
            directory.mkdir()
            key = 'context-' + 'a' * 32
            with sqlite3.connect(directory / ('e' * 64 + '.sqlite3')) as db:
                db.execute('CREATE TABLE artifacts (id TEXT PRIMARY KEY, kind TEXT, content TEXT)')
                db.execute('INSERT INTO artifacts VALUES (?, ?, ?)', (key, 'tool_result', 'before\x00after'))
            with patch('rmm_ai_context.getenv', side_effect=lambda name, default=None: root if name == 'RMM_LOG_DIR' else default):
                window = ContextWindow(scope='e' * 64)
                self.assertEqual(window.read_page(key, 0, 4096)['content'], 'before\x00after')
                new_key = window.archive('new evidence', 'tool_result')
                self.assertEqual(window.read_page(new_key, 0, 4096)['content'], 'new evidence')

    def test_compaction_preserves_pairs_and_active_instructions_for_each_protocol(self):
        for provider in ('openai', 'mistral', 'anthropic'):
            window = self.window()
            anchor = {'role': 'user', 'content': 'Current operator task'}
            window.anchor = anchor
            messages = [anchor]
            for index in range(8):
                if provider == 'anthropic':
                    messages += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': str(index), 'name': 'inspect', 'input': {}}]},
                                 {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(index), 'content': 'E' * 2000}]}]
                else:
                    messages += [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': str(index), 'function': {'name': 'inspect'}}]},
                                 {'role': 'tool', 'tool_call_id': str(index), 'content': 'E' * 2000}]
            if provider == 'anthropic':
                body = {'system': 'CORE', 'messages': messages, 'tools': []}
            else:
                messages.insert(0, {'role': 'system', 'content': 'CORE'})
                body = {'messages': messages, 'tools': []}
            pinned = 'COMPLETE_ACTIVE_INSTRUCTIONS' * 30
            prepared = window.prepare(body, pinned)
            self.assertIn(pinned, json.dumps(prepared))
            self.assertIn(anchor, messages)
            self.assertGreater(window.compactions, 0)
            for i, message in enumerate(messages):
                if message.get('role') == 'tool':
                    self.assertEqual(messages[i-1]['tool_calls'][0]['id'], message['tool_call_id'])
                if provider == 'anthropic' and message.get('role') == 'user' and isinstance(message['content'], list):
                    self.assertEqual(messages[i-1]['content'][0]['id'], message['content'][0]['tool_use_id'])
            archives = len(window.artifacts)
            # Another request must not rearchive history already removed.
            again = {'messages': messages, 'tools': []}
            if provider == 'anthropic':
                again['system'] = 'CORE'
            window.prepare(again, pinned)
            self.assertEqual(len(window.artifacts), archives)

    def test_pinned_instructions_over_budget_fail_before_provider_call(self):
        import rmm_ai
        async def tool(name, args):
            self.fail('No operation should run')
        with patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: {'RMM_AI_CONTEXT_BUDGET': '8192', 'RMM_AI_OUTPUT_RESERVE': '1024'}.get(key, default)), patch('rmm_ai._json_request') as request:
            result = asyncio.run(rmm_ai._run_provider_loop(provider='openai', api_key='test', messages=[{'role':'user','content':'Task'}], model='test', system='M' * 9000, tools=[], call_tool=tool, max_rounds=1, via='test'))
            self.assertEqual(result['error'], 'context_budget_exceeded')
            self.assertEqual(result['context_budget']['estimator'], 'utf8_bytes_upper_bound')
            self.assertGreater(result['context_budget']['estimated_input_upper_bound'],
                               result['context_budget']['effective_input_limit'])
            request.assert_not_called()

    def test_real_recon_and_pivot_skills_fit_default_budget_with_staged_schemas(self):
        import rmm_ai
        from rmm_ai_skills import resolve_ai_skill_snapshot, compose_system_prompt
        from rmm_tools import OPENAI_TOOLS, SYSTEM_PROMPT
        for provider in ('openai', 'mistral', 'anthropic'):
            requests = []
            remote = AsyncMock(return_value='{"sessions": []}')
            external = [{'type': 'function', 'function': {'name': 'list_exegol_containers',
                         'description': 'List Exegol containers', 'parameters': {'type': 'object'}}}]
            external += [{'type': 'function', 'function': {'name': f'unrelated_tool_{index}',
                          'description': 'Unrelated operational capability. ' * 100,
                          'parameters': {'type': 'object', 'properties': {'option': {'type': 'string'}}}}}
                         for index in range(12)]
            documents, policy = resolve_ai_skill_snapshot(None)
            core, catalog, tools, runtime = prepare_skill_context(documents, [*OPENAI_TOOLS, *external], remote,
                external_tool_names={tool['function']['name'] for tool in external}, policy=policy)
            system = compose_system_prompt(SYSTEM_PROMPT, skill_ids=None, skill_documents=core) + catalog
            planned = ['list_sessions', 'get_session', 'exec_command', 'list_exegol_containers']
            def request(url, headers, body):
                requests.append(copy.deepcopy(body))
                if len(requests) == 1:
                    name, args = 'prepare_step', {'skill_ids': ['local-recon-before-network', 'exegol-rmm-pivot'],
                        'tools': planned, 'summary': 'Establish the selected agent context before an authorized network inventory',
                        'checkpoint': 'No operations performed. Establish identity and network context, then reuse or establish the required Exegol relay.'}
                elif len(requests) == 2:
                    name, args = 'list_sessions', {}
                else:
                    if provider == 'anthropic':
                        return {'content': [{'type': 'text', 'text': 'No sessions are available.'}], 'stop_reason': 'end_turn'}
                    return {'choices': [{'message': {'content': 'No sessions are available.'}, 'finish_reason': 'stop'}]}
                if provider == 'anthropic':
                    return {'content': [{'type': 'tool_use', 'id': str(len(requests)), 'name': name, 'input': args}]}
                return {'choices': [{'message': {'tool_calls': [{'id': str(len(requests)), 'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}
            with self.subTest(provider=provider), patch('rmm_ai._json_request', side_effect=request), patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: default):
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', model='test',
                    messages=[{'role': 'user', 'content': 'Perform an authorized network inventory'}],
                    system=system, tools=tools, call_tool=runtime, max_rounds=4, via='test'))
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(requests), 3)
            def names(body):
                return {tool.get('name') or tool['function']['name'] for tool in body['tools']}
            internal = {'load_skill', 'prepare_step', 'read_context'}
            self.assertEqual(names(requests[0]), internal)
            self.assertEqual(names(requests[1]), internal | set(planned))
            self.assertEqual(names(requests[2]), internal)
            self.assertIn('Operational tool catalog', json.dumps(requests[0]))
            remote.assert_awaited_once_with('list_sessions', {})
            self.assertLess(result['context_budget']['estimated_input_upper_bound'],
                            result['context_budget']['effective_input_limit'])

    def test_history_eviction_preserves_prior_operator_scope_and_consent(self):
        window = self.window()
        scope = {'role': 'user', 'content': 'Only inspect host-a. Never change passwords.'}
        task = {'role': 'user', 'content': 'Continue the investigation'}
        window.operator_requests = [scope, task]
        messages = [{'role': 'system', 'content': 'CORE'}, scope,
                    {'role': 'assistant', 'content': 'Old evidence ' * 2000}, task,
                    {'role': 'assistant', 'content': 'Recent evidence ' * 200}]
        window.prepare({'messages': messages, 'tools': []}, 'ACTIVE_RULES')
        self.assertIn(scope, messages)
        self.assertIn(task, messages)
        self.assertGreater(window.compactions, 0)

    def test_retrieval_does_not_bypass_step_barrier(self):
        remote = AsyncMock(return_value='result')
        _, _, _, skill_dispatch = prepare_skill_context([], [{'function': {'name': 'inspect'}}], remote)
        dispatch = ContextDispatcher(skill_dispatch, self.window())
        results = asyncio.run(run_tool_batch([{'name': 'inspect', 'arguments': {}}], dispatch))
        self.assertEqual(json.loads(results[0])['error'], 'step_preparation_required')
        remote.assert_not_awaited()

    def test_checkpoints_are_evidence_not_a_source_of_enabled_skills(self):
        import rmm_ai
        message = {'role': 'assistant', 'content': 'Completed', 'step_state': {'active_skills': ['disabled'], 'checkpoint': 'Operation pending'}}
        for builder in (lambda: rmm_ai._build_openai_convo([message], 'CORE'), lambda: rmm_ai._build_anthropic_messages([message])):
            self.assertIn('historical evidence', builder()[-1]['content'])
        _, catalog, _, runtime = prepare_skill_context([], [], AsyncMock())
        self.assertNotIn('disabled', runtime.skills)
