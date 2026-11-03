"""Verify lazy skill disclosure without executing remote tools."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock
from rmm_ai_skill_context import prepare_skill_context


class SkillContextTests(unittest.TestCase):
    def test_only_core_bodies_enter_initial_context(self):
        documents = [{'id': 'operator-boundaries', 'body': 'core instructions'},
                     {'id': 'optional', 'title': 'Optional', 'description': 'When useful', 'body': 'PRIVATE_BODY_SENTINEL'}]
        events = []
        delegate = AsyncMock(return_value='tool result')
        core, catalog, tools, dispatch = prepare_skill_context(documents, [], delegate, events.append)
        self.assertEqual([s['id'] for s in core], ['operator-boundaries'])
        self.assertNotIn('PRIVATE_BODY_SENTINEL', catalog)
        self.assertIn('When useful', catalog)
        self.assertEqual(tools[0]['function']['name'], 'load_skill')
        response = json.loads(asyncio.run(dispatch('load_skill', {'skill_id': 'optional'})))
        self.assertEqual(response['instructions'], 'PRIVATE_BODY_SENTINEL')
        self.assertEqual(events[-1]['skills'], ['operator-boundaries', 'optional'])
        again = json.loads(asyncio.run(dispatch('load_skill', {'skill_id': 'optional'})))
        self.assertTrue(again['already_loaded'])
        self.assertNotIn('instructions', again)
        delegate.assert_not_awaited()
        self.assertEqual(asyncio.run(dispatch('ordinary', {})), 'tool result')
        delegate.assert_awaited_once_with('ordinary', {})

    def test_unknown_ids_and_malformed_arguments_do_not_read_files(self):
        _, _, _, dispatch = prepare_skill_context([], [], AsyncMock())
        for args in ({'skill_id': '../../secret'}, {'skill_id': []}, [], {}):
            self.assertEqual(json.loads(asyncio.run(dispatch('load_skill', args)))['error'], 'unknown_skill')

    def test_custom_always_skill_is_still_lazy_and_state_is_per_run(self):
        docs = [{'id': 'custom', 'always': True, 'body': 'instructions'}]
        for _ in range(2):
            core, _, _, dispatch = prepare_skill_context(docs, [], AsyncMock())
            self.assertEqual(core, [])
            self.assertIn('instructions', json.loads(asyncio.run(dispatch('load_skill', {'skill_id': 'custom'}))))

    def test_reserved_tool_collision_fails(self):
        with self.assertRaisesRegex(ValueError, 'Reserved'):
            prepare_skill_context([], [{'function': {'name': 'load_skill'}}], AsyncMock())

    def test_direct_provider_receives_body_only_after_load(self):
        import copy
        from unittest.mock import patch
        import rmm_ai
        for provider in ('openai', 'mistral', 'anthropic'):
            requests = []
            def request(url, headers, body):
                requests.append(copy.deepcopy(body))
                if len(requests) == 1:
                    if provider == 'anthropic':
                        return {'content': [{'type': 'tool_use', 'id': 's1', 'name': 'load_skill', 'input': {'skill_id': 'optional'}}]}
                    return {'choices': [{'message': {'tool_calls': [{'id': 's1', 'type': 'function', 'function': {'name': 'load_skill', 'arguments': '{"skill_id":"optional"}'}}]}}]}
                if provider == 'anthropic':
                    return {'content': [{'type': 'text', 'text': 'Done.'}], 'stop_reason': 'end_turn'}
                return {'choices': [{'message': {'content': 'Done.'}, 'finish_reason': 'stop'}]}
            with self.subTest(provider=provider), patch('rmm_ai._json_request', side_effect=request), patch('rmm_ai.execute_tool') as remote:
                result = rmm_ai._run_ai_chat_direct(rmm_base_url='http://local', rmm_token='test', provider=provider, api_key='test', messages=[], model='test', selected_session_id=None, max_rounds=2, skill_documents=[{'id': 'optional', 'title': 'Optional', 'body': 'BODY_ONLY_AFTER_LOAD'}])
                self.assertTrue(result['ok'])
                self.assertNotIn('BODY_ONLY_AFTER_LOAD', json.dumps(requests[0]))
                self.assertIn('BODY_ONLY_AFTER_LOAD', json.dumps(requests[1]))
                remote.assert_not_called()
