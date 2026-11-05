"""Incomplete model generations must never consume preparation or dispatch operations."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import rmm_ai
from rmm_ai_skill_context import _tool, prepare_skill_context


def native(provider, calls=(), *, truncated=False, text=''):
    """Build native provider responses, including incomplete tool-call arguments."""
    if provider == 'anthropic':
        return {'stop_reason': 'max_tokens' if truncated else ('tool_use' if calls else 'end_turn'),
                'content': [{'type': 'text', 'text': text}] + [
                    {'type': 'tool_use', 'id': f'call-{i}', 'name': name, 'input': args}
                    for i, (name, args) in enumerate(calls)]}
    return {'choices': [{'finish_reason': 'length' if truncated else ('tool_calls' if calls else 'stop'),
                         'message': {'content': text, 'tool_calls': [
                             {'id': f'call-{i}', 'type': 'function', 'function': {'name': name,
                                 'arguments': '{' if truncated else json.dumps(args)}}
                             for i, (name, args) in enumerate(calls)]}}]}


def prepared(tool, skills=()):
    """Declare one current-step method/tool bundle for the real preparation gate."""
    return ('prepare_step', {'summary': 'Inspect current evidence', 'checkpoint': 'Retain prior returned evidence',
                             'tools': [tool], 'skill_ids': list(skills)})


class OutputRecoveryTests(unittest.TestCase):
    def test_configured_reserve_is_preserved_and_included_in_context_measurement(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            bodies = []
            remote = AsyncMock()
            def request(url, headers, body):
                bodies.append(copy.deepcopy(body))
                return native(provider, truncated=True)
            values = {'RMM_AI_OUTPUT_RESERVE': '8192', 'RMM_AI_CONTEXT_BUDGET': '65536'}
            with self.subTest(provider=provider), patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: values.get(key, default)), patch('rmm_ai._json_request', side_effect=request):
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[], model='test',
                    system='test', tools=[], call_tool=remote, max_rounds=2, via='test'))
            key = 'max_completion_tokens' if provider == 'openai' else 'max_tokens'
            self.assertEqual([body[key] for body in bodies], [8192, 8192, 8192])
            self.assertEqual(result['diagnostics']['provider_output']['limit'], 8192)
            self.assertEqual(result['context_budget']['output_reserve'], 8192)
            self.assertEqual(result['context_budget']['serialized_request_bytes'], len(json.dumps(bodies[-1], ensure_ascii=False).encode('utf-8')))
            remote.assert_not_awaited()

    def test_cancellation_at_recovery_checkpoint_prevents_another_request(self):
        from rmm_ai_jobs import JobInterrupted
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            remote = AsyncMock()
            def progress(event):
                if event.get('status') == 'output_truncated_retry':
                    raise JobInterrupted('cancelled')
            with self.subTest(provider=provider), patch('rmm_ai._json_request', return_value=native(provider, [('inspect', {})], truncated=True)) as request:
                with self.assertRaises(JobInterrupted):
                    asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[], model='test', system='test',
                        tools=[], call_tool=remote, max_rounds=2, via='test', progress=progress))
            self.assertEqual(request.call_count, 1)
            remote.assert_not_awaited()

    def test_retry_budget_is_global_and_prior_completed_result_survives_exhaustion(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            bodies = []
            responses = [native(provider, truncated=True), native(provider, [('inspect', {})]),
                         native(provider, truncated=True), native(provider, truncated=True)]
            remote = AsyncMock(return_value='FIRST_EVIDENCE')
            def request(url, headers, body):
                bodies.append(copy.deepcopy(body))
                return responses[len(bodies) - 1]
            with self.subTest(provider=provider), patch('rmm_ai._json_request', side_effect=request):
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[], model='test', system='test',
                    tools=[_tool('inspect', 'Read fixture', {}, [])], call_tool=remote, max_rounds=2, via='test'))
            self.assertEqual(result['error'], 'provider_output_truncated')
            self.assertEqual(result['rounds_used'], 4)
            self.assertEqual(result['output_truncation_retries'], 2)
            self.assertEqual(result['diagnostics']['provider_output']['retries'], 2)
            self.assertEqual([call['name'] for call in result['tool_calls_made']], ['inspect'])
            self.assertIn('FIRST_EVIDENCE', json.dumps(bodies[-1]))
            self.assertEqual(len(result['context_artifacts']), 1)
            remote.assert_awaited_once_with('inspect', {})

    def test_text_truncation_can_recover_without_preserving_partial_text(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            bodies = []
            remote = AsyncMock()
            def request(url, headers, body):
                bodies.append(copy.deepcopy(body))
                return native(provider, truncated=True, text='PARTIAL_MARKER') if len(bodies) == 1 else native(provider, text='Completed.')
            with self.subTest(provider=provider), patch('rmm_ai._json_request', side_effect=request):
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[], model='test',
                    system='Keep responses concise.', tools=[], call_tool=remote, max_rounds=2, via='test'))
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['rounds_used'], 2)
            self.assertEqual(result['output_truncation_retries'], 1)
            self.assertNotIn('PARTIAL_MARKER', json.dumps(bodies[1]))
            key = 'max_completion_tokens' if provider == 'openai' else 'max_tokens'
            self.assertEqual(bodies[0][key], bodies[1][key])
            remote.assert_not_awaited()

    def test_truncated_batch_is_discarded_preparation_and_prior_evidence_survive(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            bodies = []
            responses = [native(provider, [prepared('inspect_a')]), native(provider, [('inspect_a', {})]),
                         native(provider, [prepared('inspect_b', ['step-method'])]),
                         native(provider, [('inspect_a', {}), ('inspect_b', {'partial': 'PARTIAL_MARKER'})], truncated=True, text='PARTIAL_MARKER'),
                         native(provider, [('inspect_b', {})]), native(provider, text='Done.')]
            remote = AsyncMock(side_effect=['FIRST_EVIDENCE', 'SECOND_EVIDENCE'])
            _, catalog, tools, gate = prepare_skill_context([{'id': 'step-method', 'body': 'CURRENT_METHOD_BODY'}],
                [_tool(name, 'Inspect local fixture', {}, []) for name in ('inspect_a', 'inspect_b')], remote)
            def request(url, headers, body):
                bodies.append(copy.deepcopy(body))
                return responses[len(bodies) - 1]
            with self.subTest(provider=provider), patch('rmm_ai._json_request', side_effect=request):
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[{'role': 'user', 'content': 'Inspect fixtures'}],
                    model='test', system=catalog, tools=tools, call_tool=gate, max_rounds=8, via='test'))
            self.assertTrue(result['ok'], result)
            self.assertEqual([call.args[0] for call in remote.await_args_list], ['inspect_a', 'inspect_b'])
            retry = json.dumps(bodies[4])
            self.assertIn('FIRST_EVIDENCE', retry)
            self.assertIn('CURRENT_METHOD_BODY', retry)
            self.assertNotIn('PARTIAL_MARKER', retry)
            exposed = {tool.get('name') or tool['function']['name'] for tool in bodies[4]['tools']}
            self.assertIn('inspect_b', exposed)
            self.assertNotIn('inspect_a', exposed)
            self.assertEqual(result['step_state']['step'], 2)
            self.assertEqual(result['output_truncation_retries'], 1)

    def test_persistent_truncated_calls_stop_with_safe_counters_and_no_dispatch(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            response = native(provider, [('inspect', {'partial': 'PRIVATE_MARKER'})], truncated=True)
            response['usage'] = {'completion_tokens': 4096, 'output_tokens': 4096,
                                 'completion_tokens_details': {'reasoning_tokens': 4000}, 'private': 'PRIVATE_MARKER'}
            remote = AsyncMock()
            with self.subTest(provider=provider), patch('rmm_ai._json_request', return_value=response) as request:
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[], model='test',
                    system='test', tools=[_tool('inspect', 'Read fixture', {}, [])], call_tool=remote, max_rounds=2, via='test'))
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], 'provider_output_truncated')
            self.assertEqual(request.call_count, 3)
            self.assertEqual(result['rounds_used'], 3)
            self.assertEqual(result['diagnostics']['provider_output']['reasoning_tokens'], 4000)
            self.assertNotIn('PRIVATE_MARKER', json.dumps(result))
            remote.assert_not_awaited()

    def test_last_round_reports_truncation_without_exceeding_round_ceiling(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            with self.subTest(provider=provider), patch('rmm_ai.MAX_TOOL_CONTINUATIONS', 0), patch('rmm_ai._json_request', return_value=native(provider, truncated=True)) as request:
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[], model='test',
                    system='test', tools=[], call_tool=AsyncMock(), max_rounds=1, via='test'))
            self.assertEqual(result['error'], 'provider_output_truncated')
            self.assertEqual(result['rounds_used'], 1)
            self.assertEqual(result['output_truncation_retries'], 0)
            self.assertEqual(request.call_count, 1)
