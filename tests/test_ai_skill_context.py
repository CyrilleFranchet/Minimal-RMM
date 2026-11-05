"""Exercise general per-step instruction loading without remote execution."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from rmm_ai_skill_context import prepare_skill_context
from rmm_ai_concurrency import run_tool_batch


def schema(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


def step(ids, tools=('operate',), summary='Inspect the current evidence', checkpoint='No operations completed'):
    return {'name': 'prepare_step', 'arguments': {'skill_ids': ids, 'tools': list(tools),
            'summary': summary, 'checkpoint': checkpoint}}


def batch(dispatch, *calls):
    return [json.loads(x) if x.startswith('{') else x for x in asyncio.run(run_tool_batch(list(calls), dispatch))]


class SkillContextTests(unittest.TestCase):
    def test_optional_bodies_are_lazy_but_step_selection_loads_instructions(self):
        docs = [{'id': 'operator-boundaries', 'body': 'CORE'},
                {'id': 'file-review', 'description': 'Inspect files', 'body': 'FILE_RULES'}]
        remote = AsyncMock(return_value='result')
        core, catalog, _, dispatch = prepare_skill_context(docs, [schema('operate')], remote)
        self.assertEqual([s['body'] for s in core], ['CORE'])
        self.assertNotIn('FILE_RULES', catalog)
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {}})[0]['error'], 'step_preparation_required')
        loaded = batch(dispatch, step(['file-review']))[0]
        self.assertIn('FILE_RULES', loaded['instructions'])
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {}}), ['result'])
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {}})[0]['error'], 'step_preparation_required')
        remote.assert_awaited_once()

    def test_selection_is_per_step_and_rebuilds_complete_instructions(self):
        docs = [{'id': 'web-review', 'body': 'WEB_ONLY'}, {'id': 'file-review', 'body': 'FILE_ONLY'}]
        _, _, _, dispatch = prepare_skill_context(docs, [schema('operate')], AsyncMock(return_value='ok'))
        batch(dispatch, step(['web-review']))
        self.assertIn('WEB_ONLY', dispatch.instructions())
        batch(dispatch, {'name': 'operate', 'arguments': {}})
        batch(dispatch, step(['file-review']))
        self.assertNotIn('WEB_ONLY', dispatch.instructions())
        self.assertIn('FILE_ONLY', dispatch.instructions())
        self.assertEqual(dispatch.step, 2)

    def test_dependencies_priority_disabled_ids_and_cycles(self):
        docs = [{'id': 'workflow', 'requires': ['base'], 'priority': 30, 'body': 'WORK'},
                {'id': 'base', 'priority': 1, 'body': 'BASE'}]
        _, _, _, dispatch = prepare_skill_context(docs, [schema('operate')], AsyncMock())
        self.assertTrue(batch(dispatch, step(['workflow']))[0]['ok'])
        self.assertEqual(dispatch.active, ['base', 'workflow'])
        self.assertEqual(batch(dispatch, step(['disabled']))[0]['error'], 'skill_disabled_or_unknown')
        dispatch.skills['base']['requires'] = ['workflow']
        self.assertEqual(batch(dispatch, step(['workflow']))[0]['error'], 'skill_dependency_cycle')
        del dispatch.skills['base']
        self.assertEqual(batch(dispatch, step(['workflow']))[0]['error'], 'skill_disabled_or_unknown')

    def test_same_batch_preparation_never_admits_operations(self):
        remote = AsyncMock(return_value='ok')
        _, _, _, dispatch = prepare_skill_context([], [schema('operate')], remote)
        results = batch(dispatch, step([]), {'name': 'operate', 'arguments': {}})
        self.assertFalse(results[0]['ok'])
        self.assertFalse(results[1]['ok'])
        remote.assert_not_awaited()

    def test_guards_are_declarative_for_arbitrary_skills_and_backends(self):
        policy = [{'tool': 'operate', 'actions': ['write'], 'requires': ['file-review']},
                  {'backend': 'external', 'requires': ['remote-review']}]
        docs = [{'id': 'file-review', 'body': 'FILES'}]
        remote = AsyncMock(return_value='ok')
        _, _, tools, dispatch = prepare_skill_context(docs, [schema('operate'), schema('remote_tool')], remote,
            external_tool_names={'remote_tool'}, policy=policy)
        self.assertNotIn('remote_tool', [t['function']['name'] for t in tools])
        self.assertEqual(batch(dispatch, {'name': 'remote_tool', 'arguments': {}})[0]['error'], 'required_skill_disabled')
        batch(dispatch, step([]))
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {'action': 'write'}})[0]['error'], 'required_skill_not_active')
        batch(dispatch, step(['file-review']))
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {'action': 'write'}}), ['ok'])
        remote.assert_awaited_once()
        batch(dispatch, step([]))
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {}})[0]['error'], 'explicit_action_required')
        dispatch.action_defaults['operate'] = 'write'
        batch(dispatch, step([]))
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {}})[0]['error'], 'required_skill_not_active')
        batch(dispatch, step([]))
        self.assertEqual(batch(dispatch, {'name': 'operate', 'arguments': {'action': ' WRITE '}})[0]['error'], 'required_skill_not_active')
        remote.assert_awaited_once()

    def test_explicit_invocation_and_disabled_dependency(self):
        _, _, _, dispatch = prepare_skill_context([{'id': 'review', 'body': 'RULES'}], [schema('operate')], AsyncMock())
        dispatch.require_from_messages([{'role': 'user', 'content': '/review the current step'}])
        batch(dispatch, step([]))
        self.assertEqual(dispatch.active, ['review'])
        dispatch.require_from_messages([{'role': 'user', 'content': '/disabled'}])
        self.assertEqual(batch(dispatch, step([]))[0]['error'], 'skill_disabled_or_unknown')

    def test_required_tools_and_public_step_progress(self):
        events = []
        docs = [{'id': 'review', 'body': 'RULES', 'required_tools': ['inspect']}]
        _, _, _, runtime = prepare_skill_context(docs, [schema('operate')], AsyncMock(), events.append)
        self.assertEqual(batch(runtime, step(['review']))[0]['error'], 'required_tool_unavailable')
        runtime.skills['review']['required_tools'] = ['operate']
        self.assertTrue(batch(runtime, step(['review']))[0]['ok'])
        from rmm_ai_jobs import _safe_progress
        progress = _safe_progress(events[-1])
        self.assertEqual(progress['active_skills'], ['review'])
        self.assertEqual(progress['step_summary'], 'Inspect the current evidence')

    def test_explicit_invocation_is_loaded_even_for_tool_free_responses(self):
        import rmm_ai
        for provider in ('openai', 'mistral', 'anthropic'):
            _, catalog, tools, dispatch = prepare_skill_context(
                [{'id': 'review', 'body': 'EXPLICIT_INSTRUCTIONS'}], [schema('operate')], AsyncMock())
            response = ({'content': [{'type': 'text', 'text': 'Reviewed'}], 'stop_reason': 'end_turn'}
                        if provider == 'anthropic' else {'choices': [{'message': {'content': 'Reviewed'}, 'finish_reason': 'stop'}]})
            with self.subTest(provider=provider), patch('rmm_ai._json_request', return_value=response) as request:
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[{'role': 'user', 'content': '/review Explain this'}], model='test', system=catalog, tools=tools, call_tool=dispatch, max_rounds=1, via='test'))
                self.assertTrue(result['ok'])
                self.assertIn('EXPLICIT_INSTRUCTIONS', json.dumps(request.call_args.args[2]))
                self.assertEqual(result['step_state']['active_skills'], ['review'])

    def test_disabled_explicit_invocation_never_contacts_provider(self):
        import rmm_ai
        _, catalog, tools, dispatch = prepare_skill_context([], [], AsyncMock())
        with patch('rmm_ai._json_request') as request:
            result = asyncio.run(rmm_ai._run_provider_loop(provider='openai', api_key='test', messages=[{'role': 'user', 'content': '/disabled Explain this'}], model='test', system=catalog, tools=tools, call_tool=dispatch, max_rounds=1, via='test'))
            self.assertEqual(result['error'], 'skill_disabled_or_unknown')
            request.assert_not_called()

    def test_invalid_action_enum_is_rejected_before_backend_normalization(self):
        tool = schema('operate')
        tool['function']['parameters']['properties'] = {'action': {'type': 'string', 'enum': ['start', 'stop']}}
        remote = AsyncMock(return_value='ok')
        _, _, _, dispatch = prepare_skill_context([], [tool], remote)
        batch(dispatch, step([]))
        result = batch(dispatch, {'name': 'operate', 'arguments': {'action': ' START '}})[0]
        self.assertEqual(result['error'], 'invalid_tool_arguments')
        remote.assert_not_awaited()

    def test_frontmatter_rules_survive_disablement_without_exposing_body(self):
        import tempfile
        from pathlib import Path
        from rmm_ai_skills import resolve_ai_skills, resolve_ai_skill_policy
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'review.md')
            path.write_text('---\nid: review\nrequires: ["base"]\npriority: 7\ntool_rules: [{"tool":"operate","requires":["review"]}]\n---\nPRIVATE_RULES')
            self.assertEqual(resolve_ai_skills([], [directory], {'review': False}), [])
            policy = resolve_ai_skill_policy([directory])
            self.assertNotIn('PRIVATE_RULES', json.dumps(policy))
            self.assertEqual(policy[0]['requires'], ['review'])
            _, _, _, runtime = prepare_skill_context([], [schema('operate')], AsyncMock(), policy=policy)
            self.assertEqual(batch(runtime, {'name': 'operate', 'arguments': {}})[0]['error'], 'required_skill_disabled')
            path.write_text('---\nid: review\nrequires: not-json\n---\nPRIVATE_RULES')
            with self.assertRaisesRegex(ValueError, 'Invalid skill metadata'):
                resolve_ai_skill_policy([directory])

    def test_unknown_and_malformed_loads_never_read_paths(self):
        _, _, _, dispatch = prepare_skill_context([], [], AsyncMock())
        for args in ({'skill_id': '../../secret'}, {'skill_id': []}, [], {}):
            self.assertEqual(json.loads(asyncio.run(dispatch('load_skill', args)))['error'], 'unknown_skill')
        for name in ('load_skill', 'prepare_step', 'read_context'):
            with self.assertRaisesRegex(ValueError, 'Reserved'):
                prepare_skill_context([], [schema(name)], AsyncMock())

    def test_provider_protocols_load_before_execution_and_reprepare_next_step(self):
        import rmm_ai
        for provider in ('openai', 'mistral', 'anthropic'):
            requests = []
            def request(url, headers, body):
                requests.append(copy.deepcopy(body))
                calls = [step(['review']), {'name': 'operate', 'arguments': {}},
                         {'name': 'operate', 'arguments': {}}, step(['other']),
                         {'name': 'operate', 'arguments': {}}]
                index = len(requests) - 1
                if index == len(calls):
                    if provider == 'anthropic':
                        return {'content': [{'type': 'text', 'text': 'Done.'}], 'stop_reason': 'end_turn'}
                    return {'choices': [{'message': {'content': 'Done.'}, 'finish_reason': 'stop'}]}
                call = calls[index]
                if provider == 'anthropic':
                    return {'content': [{'type': 'tool_use', 'id': str(index), 'name': call['name'], 'input': call['arguments']}]}
                return {'choices': [{'message': {'tool_calls': [{'id': str(index), 'type': 'function', 'function': {'name': call['name'], 'arguments': json.dumps(call['arguments'])}}]}}]}
            remote = AsyncMock(return_value='Evidence')
            core, catalog, tools, dispatch = prepare_skill_context(
                [{'id': 'review', 'body': 'REVIEW_INSTRUCTIONS'}, {'id': 'other', 'body': 'OTHER_INSTRUCTIONS'}], [schema('operate')], remote)
            with self.subTest(provider=provider), patch('rmm_ai._json_request', side_effect=request):
                result = asyncio.run(rmm_ai._run_provider_loop(provider=provider, api_key='test', messages=[{'role': 'user', 'content': 'Investigate'}], model='test', system=catalog, tools=tools, call_tool=dispatch, max_rounds=6, via='test'))
                self.assertTrue(result['ok'])
                self.assertNotIn('REVIEW_INSTRUCTIONS', json.dumps(requests[0]))
                self.assertIn('REVIEW_INSTRUCTIONS', json.dumps(requests[1]))
                self.assertIn('step_preparation_required', json.dumps(requests[3]))
                self.assertEqual(remote.await_count, 2)
                self.assertEqual(result['step_state']['active_skills'], ['other'])
