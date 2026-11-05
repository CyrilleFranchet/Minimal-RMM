"""An empty final generation must not turn prerequisite checks into success."""
import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

import rmm_ai
from tests.test_ai_output_recovery import native
from rmm_ai_skill_context import _tool


class EmptyResponseTests(unittest.TestCase):
    def test_prerequisite_checks_then_empty_reply_can_continue(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            with self.subTest(provider=provider):
                responses = [native(provider, [('list_tun', {})]),
                             native(provider, text='  \n'),
                             native(provider, [('start_tun', {})]),
                             native(provider, text='Relay started; verified fixture result.')]
                remote = AsyncMock(side_effect=['NO_RELAYS_EVIDENCE', 'ACTIVE_RELAY_EVIDENCE'])
                tools = [_tool(name, 'Fixture only', {}, []) for name in ('list_tun', 'start_tun')]
                bodies = []

                def request(url, headers, body):
                    bodies.append(copy.deepcopy(body))
                    return responses[len(bodies) - 1]

                with patch('rmm_ai._json_request', side_effect=request):
                    result = asyncio.run(rmm_ai._run_provider_loop(
                        provider=provider, api_key='test', messages=[], model='test',
                        system='Execute the supplied playbook.', tools=tools, call_tool=remote, max_rounds=3, via='test'))
                self.assertTrue(result['ok'], result)
                self.assertEqual([c.args[0] for c in remote.await_args_list], ['list_tun', 'start_tun'])
                self.assertIn('NO_RELAYS_EVIDENCE', str(bodies[2]))

    def test_cancellation_during_empty_recovery_prevents_next_request(self):
        from rmm_ai_jobs import JobInterrupted
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            def progress(event):
                if event.get('status') == 'empty_response_retry':
                    raise JobInterrupted('cancelled')
            remote = AsyncMock()
            with self.subTest(provider=provider), patch('rmm_ai._json_request', return_value=native(provider)) as request:
                with self.assertRaises(JobInterrupted):
                    asyncio.run(rmm_ai._run_provider_loop(
                        provider=provider, api_key='test', messages=[], model='test', system='test',
                        tools=[], call_tool=remote, max_rounds=3, via='test', progress=progress))
            self.assertEqual(request.call_count, 1)
            remote.assert_not_awaited()

    def test_empty_exhaustion_and_last_round_are_explicit_failures(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            for max_rounds, requests in ((4, 3), (1, 1)):
                remote = AsyncMock()
                with self.subTest(provider=provider, max_rounds=max_rounds), patch('rmm_ai.MAX_TOOL_CONTINUATIONS', 0), patch('rmm_ai._json_request', return_value=native(provider)) as request:
                    result = asyncio.run(rmm_ai._run_provider_loop(
                        provider=provider, api_key='test', messages=[], model='test',
                        system='test', tools=[], call_tool=remote, max_rounds=max_rounds, via='test'))
                self.assertFalse(result['ok'])
                self.assertEqual(result['error'], 'empty_provider_response')
                self.assertIn('incomplete', result['message'])
                self.assertEqual(request.call_count, requests)
                remote.assert_not_awaited()

    def test_substantive_blocker_report_does_not_trigger_recovery(self):
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            with self.subTest(provider=provider), patch('rmm_ai._json_request', return_value=native(
                    provider, text='Blocked: the selected agent lacks the required capability.')) as request:
                result = asyncio.run(rmm_ai._run_provider_loop(
                    provider=provider, api_key='test', messages=[], model='test', system='test',
                    tools=[], call_tool=AsyncMock(), max_rounds=2, via='test'))
            self.assertTrue(result['ok'])
            self.assertEqual(request.call_count, 1)
