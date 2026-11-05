"""Model timeout recovery must not replay completed tools."""
import asyncio
import copy
import unittest
import urllib.error
from unittest.mock import AsyncMock, patch

import rmm_ai


class ModelRetryTests(unittest.TestCase):
    def test_preserves_tool_results_for_all_providers(self):
        for provider in ('openai', 'mistral', 'anthropic'):
            with self.subTest(provider=provider):
                if provider == 'anthropic':
                    tool_response = {'content': [{'type': 'tool_use', 'id': 'one', 'name': 'probe', 'input': {}}]}
                    final = {'content': [{'type': 'text', 'text': 'Completed.'}], 'stop_reason': 'end_turn'}
                else:
                    tool_response = {'choices': [{'message': {'tool_calls': [{'id': 'one', 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}}]}}]}
                    final = {'choices': [{'message': {'content': 'Completed.'}, 'finish_reason': 'stop'}]}
                bodies = []
                def request(url, headers, body):
                    bodies.append(copy.deepcopy(body))
                    if len(bodies) == 1:
                        return tool_response
                    if len(bodies) == 2:
                        raise RuntimeError('timeout') from TimeoutError()
                    return final
                tool = AsyncMock(return_value='verified result')
                with patch('rmm_ai._json_request', side_effect=request), patch('rmm_ai.asyncio.sleep', new_callable=AsyncMock):
                    result = asyncio.run(rmm_ai._run_provider_loop(
                        provider=provider, api_key='test', messages=[], model='test',
                        system='test', tools=[], call_tool=tool, max_rounds=2, via='direct'))
                self.assertTrue(result['ok'])
                tool.assert_awaited_once_with('probe', {})
                self.assertEqual(bodies[1], bodies[2])
                self.assertIn('verified result', str(bodies[2]))

    def test_exhaustion_and_non_timeout_errors(self):
        for cause, expected in ((TimeoutError(), 3), (urllib.error.URLError(TimeoutError()), 3), (ValueError('bad JSON'), 1), (urllib.error.HTTPError('url', 401, 'unauthorized', {}, None), 1)):
            error = RuntimeError('failure')
            error.__cause__ = cause
            with self.subTest(cause=type(cause).__name__), patch('rmm_ai._json_request', side_effect=error) as request, patch('rmm_ai.asyncio.sleep', new_callable=AsyncMock):
                with self.assertRaises(RuntimeError) as caught:
                    asyncio.run(rmm_ai._model_request('url', {}, {}, progress=None, round_number=20))
                self.assertIs(caught.exception, error)
                self.assertEqual(request.call_count, expected)

    def test_interruption_prevents_retry(self):
        error = RuntimeError('timeout')
        error.__cause__ = TimeoutError()
        progress = unittest.mock.Mock(side_effect=[None, RuntimeError('interrupted')])
        with patch('rmm_ai._json_request', side_effect=error) as request, patch('rmm_ai.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                asyncio.run(rmm_ai._model_request('url', {}, {}, progress=progress, round_number=20))
            self.assertEqual(request.call_count, 1)


class ExecutionContextTests(unittest.TestCase):
    def test_step_rationale_remains_visible_during_later_model_and_tool_events(self):
        events = []
        def direct(**kwargs):
            progress = kwargs['progress']
            progress({'phase': 'step', 'step': 2, 'step_summary': 'Inspect current evidence',
                      'step_checkpoint': 'One read completed', 'active_skills': ['review']})
            progress({'phase': 'model', 'round': 3})
            progress({'phase': 'tool', 'tool': 'inspect', 'status': 'running'})
            return {'ok': True, 'message': 'Done'}
        with patch('rmm_mcp_client.use_mcp_for_ai', return_value=True), patch('rmm_mcp_client.mcp_available', return_value=True), patch('rmm_ai._run_ai_chat_mcp', side_effect=direct):
            result = rmm_ai.run_ai_chat(rmm_base_url='http://local', rmm_token='test', api_key='test', messages=[],
                exegol_mcp_enabled=False, skill_documents=[], skill_policy=[], progress=events.append)
        self.assertTrue(result['ok'])
        for event in events[-2:]:
            self.assertEqual(event['step_summary'], 'Inspect current evidence')
            self.assertEqual(event['active_skills'], ['review'])
            self.assertEqual(event['step_checkpoint'], 'One read completed')

    def test_legacy_disable_cannot_remove_exegol(self):
        with patch('rmm_mcp_client.use_mcp_for_ai', return_value=True), patch('rmm_mcp_client.mcp_available', return_value=True), patch('rmm_ai._run_ai_chat_mcp', return_value={'ok': True, 'message': 'Done'}) as chat:
            result = rmm_ai.run_ai_chat(rmm_base_url='http://local', rmm_token='test', api_key='test', messages=[], exegol_mcp_enabled=False, skill_documents=[])
            self.assertTrue(result['ok'])
            self.assertTrue(chat.call_args.kwargs['exegol_mcp_enabled'])

    def test_exegol_cannot_silently_use_direct_tools(self):
        for selected, installed in ((False, True), (True, False)):
            with self.subTest(selected=selected), patch('rmm_mcp_client.use_mcp_for_ai', return_value=selected), patch('rmm_mcp_client.mcp_available', return_value=installed), patch('rmm_ai._run_ai_chat_direct') as direct:
                events = []
                result = rmm_ai.run_ai_chat(rmm_base_url='http://local', rmm_token='test', api_key='test', messages=[], exegol_mcp_enabled=True, skill_documents=[{'id': 'exegol-rmm-pivot', 'body': 'Pivot'}], progress=events.append)
                self.assertEqual(result['error'], 'exegol_requires_mcp')
                direct.assert_not_called()
                self.assertEqual(events[0]['exegol_status'], 'requested')

    def test_legacy_disable_does_not_allow_direct_fallback(self):
        events = []
        with patch('rmm_mcp_client.use_mcp_for_ai', return_value=False), patch('rmm_ai._run_ai_chat_direct', return_value={'ok': True, 'message': 'Done'}) as direct:
            result = rmm_ai.run_ai_chat(rmm_base_url='http://local', rmm_token='test', api_key='test', messages=[], exegol_mcp_enabled=False, skill_documents=[{'id': 'example', 'title': 'Example', 'body': 'Instructions'}], progress=events.append)
            self.assertEqual(result['error'], 'exegol_requires_mcp')
            direct.assert_not_called()
        self.assertEqual(events[-1]['skills'], ['playbook'])
        self.assertEqual(events[-1]['mcp_status'], 'disabled')
        from rmm_ai_jobs import _safe_progress
        self.assertEqual(_safe_progress(events[-1])['skills'], ['playbook'])
