"""The production chat path uses only the explicitly supplied playbook."""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import rmm_ai
from rmm_ai_skills import playbook_documents
from tests.test_ai_output_recovery import native


class PlaybookTests(unittest.TestCase):
    def test_shipped_template_and_real_rmm_schemas_fit_default_context(self):
        body = (Path(__file__).resolve().parents[1] / 'web' / 'playbook-template.md').read_text()
        system = rmm_ai.playbook_system(rmm_ai.SYSTEM_PROMPT, [{'body': body}])
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            with self.subTest(provider=provider), patch('rmm_ai_context.getenv', side_effect=lambda key, default=None: default), patch(
                    'rmm_ai._json_request', return_value=native(provider, text='Fixture report.')) as request:
                result = asyncio.run(rmm_ai._run_provider_loop(
                    provider=provider, api_key='test', messages=[{'role': 'user', 'content': 'Execute this playbook.'}],
                    model='test', system=system, tools=rmm_ai.OPENAI_TOOLS,
                    call_tool=AsyncMock(), max_rounds=2, via='test'))
            self.assertTrue(result['ok'], result)
            self.assertEqual(request.call_count, 1)
            self.assertLess(result['context_budget']['estimated_input_upper_bound'],
                            result['context_budget']['effective_input_limit'])

    def test_reject_multiple_and_malformed_playbooks(self):
        for value in ({}, [None], [{'body': ''}], [{'body': 'a'}, {'body': 'b'}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                playbook_documents(value)

    def test_no_default_skill_loading(self):
        with patch('rmm_ai_skills.resolve_ai_skill_snapshot', side_effect=AssertionError('directory read')), patch(
                'rmm_mcp_client.use_mcp_for_ai', return_value=True), patch(
                'rmm_mcp_client.mcp_available', return_value=True), patch(
                'rmm_ai._run_ai_chat_mcp', return_value={'ok': True, 'message': 'Done'}) as direct:
            rmm_ai.run_ai_chat(rmm_base_url='http://local', rmm_token='test', api_key='test',
                              messages=[], skill_ids=['lateral-movement'], exegol_mcp_enabled=False)
        self.assertEqual(direct.call_args.kwargs['skill_documents'], [])
        self.assertEqual(direct.call_args.kwargs['skill_policy'], [])

    def test_direct_and_mcp_pin_one_playbook_without_preparation_tools(self):
        document = [{'body': 'UNIQUE_PLAYBOOK: inspect, act, verify.'}]

        async def inspect(**kwargs):
            self.assertEqual(kwargs['system'].count('UNIQUE_PLAYBOOK'), 1)
            names = {t['function']['name'] for t in kwargs['tools']}
            self.assertFalse(names.intersection({'load_skill', 'prepare_step'}))
            self.assertIn('list_sessions', names)
            await kwargs['call_tool']('list_sessions', {})
            return {'ok': True, 'message': 'Verified'}

        common = dict(rmm_base_url='http://local', rmm_token='test', provider='openai',
                      api_key='test', messages=[], model='test', selected_session_id=None,
                      max_rounds=2, skill_documents=document)
        with patch('rmm_ai._run_provider_loop', side_effect=inspect), patch('rmm_ai.execute_tool', return_value='evidence') as execute:
            rmm_ai._run_ai_chat_direct(**common)
            execute.assert_called_once()

        remote = AsyncMock(return_value='evidence')
        def connected(base, token, chat, **kwargs):
            return asyncio.run(chat(SimpleNamespace(openai_tools=rmm_ai.OPENAI_TOOLS,
                                                    server_instructions='', call_tool=remote)))
        with patch('rmm_ai._run_provider_loop', side_effect=inspect), patch('rmm_mcp_client.run_with_mcp_session', side_effect=connected):
            rmm_ai._run_ai_chat_mcp(**common)
        remote.assert_awaited_once_with('list_sessions', {})
