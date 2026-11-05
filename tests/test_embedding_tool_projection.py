"""MCP embedding summaries preserve copy evidence without sending vectors to AI."""
import copy
import asyncio
import json
import unittest
from unittest.mock import Mock, patch

import rmm_ai
from rmm_embeddings import compact_embedding_payload
from rmm_tools import tool_embedding_results, tool_exec_command, tool_get_events
from tests.test_ai_output_recovery import native


def output(copy_files=True):
    result = {'model': 'Xenova/all-MiniLM-L6-v2', 'revision': 'a' * 40,
              'results': [{'path': r'C:\Lab\report.txt', 'embedding': [0.123456789] * 384,
                           'classification': {'label': 'business', 'score': 0.8}},
                          {'path': r'C:\Lab\missing.txt', 'embedding': None, 'error': 'not found'}]}
    if copy_files:
        result['copy_directory'] = r'C:\Temp\rmm-embeddings-1'
        result['results'][0]['copied_to'] = r'C:\Temp\rmm-embeddings-1\report.txt'
    return result


class EmbeddingToolProjectionTests(unittest.TestCase):
    def test_provider_followup_receives_classifications_and_copy_evidence_without_vectors(self):
        client = Mock()
        event = {'id': 42, 'type': 'output', 'body': json.dumps(output())}
        client.list_sessions.return_value = (200, {'sessions': [{'id': 'session-full', 'hostname': 'lab'}]})
        client.get_events.return_value = (200, {'events': [event]})
        async def dispatch(name, args):
            self.assertEqual(name, 'get_events')
            return tool_get_events(client, **args)
        for provider in rmm_ai.SUPPORTED_PROVIDERS:
            bodies = []
            def request(url, headers, body):
                bodies.append(copy.deepcopy(body))
                return native(provider, [('get_events', {'session_ref': 'session-full'})]) if len(bodies) == 1 else native(provider, text='Fixture report.')
            with self.subTest(provider=provider), patch('rmm_ai._json_request', side_effect=request):
                result = asyncio.run(rmm_ai._run_provider_loop(
                    provider=provider, api_key='test', messages=[{'role': 'user', 'content': 'Check copy results.'}],
                    model='test', system='test', tools=rmm_ai.OPENAI_TOOLS,
                    call_tool=dispatch, max_rounds=2, via='test'))
            self.assertTrue(result['ok'], result)
            followup = json.dumps(bodies[1])
            self.assertNotIn('0.123456789', followup)
            self.assertIn('embedding_dimensions', followup)
            self.assertIn('copied_to', followup)
            self.assertIn('business', followup)

    def test_all_error_output_still_has_a_summary_and_no_successful_copies(self):
        original = output()
        original['results'] = [original['results'][1]]
        summary = json.loads(compact_embedding_payload(json.dumps(original)))
        self.assertTrue(summary['vectors_omitted'])
        self.assertEqual(summary['error_count'], 1)
        self.assertEqual(summary['copied_count'], 0)
        self.assertNotIn('embedding_dimensions', summary['results'][0])

    def test_vectors_are_omitted_with_and_without_copy_and_original_is_unchanged(self):
        for copy_files in (False, True):
            original = output(copy_files)
            before = copy.deepcopy(original)
            summary = compact_embedding_payload(original)
            self.assertEqual(original, before)
            self.assertTrue(summary['vectors_omitted'])
            self.assertEqual(summary['copied_count'], int(copy_files))
            self.assertEqual(summary['error_count'], 1)
            self.assertEqual(summary['document_count'], 2)
            self.assertEqual(summary['results'][0]['embedding_dimensions'], 384)
            self.assertEqual(summary['results'][0]['classification'], original['results'][0]['classification'])
            self.assertEqual(summary['results'][1]['error'], 'not found')
            self.assertNotIn('embedding', summary['results'][0])
            if copy_files:
                self.assertEqual(summary['copy_directory'], original['copy_directory'])
                self.assertEqual(summary['results'][0]['copied_to'], original['results'][0]['copied_to'])

    def test_event_exec_and_saved_result_tools_all_project_vectors(self):
        event = {'id': 42, 'type': 'output', 'body': json.dumps(output())}
        client = Mock()
        client.list_sessions.return_value = (200, {'sessions': [{'id': 'session-full', 'hostname': 'lab'}]})
        client.get_events.return_value = (200, {'events': [event]})
        client.exec_command.return_value = (200, {'event': event})
        client.embedding_results.return_value = (200, {'record': {'name': 'saved', 'event_id': 42, 'result': output()}})
        for result in (tool_get_events(client, 'session-full'), tool_exec_command(client, 'session-full', 'fixture'),
                       tool_embedding_results(client, 'save', session_ref='session-full', event_id=42),
                       tool_embedding_results(client, 'get', name='saved')):
            self.assertNotIn('0.123456789', result)
            self.assertIn('embedding_dimensions', result)
            self.assertIn('copied_to', result)
            self.assertIn('42', result)
        self.assertIn('0.123456789', event['body'], 'raw source event remains intact')

    def test_unrelated_json_and_malformed_embedding_payloads_are_preserved(self):
        for value in ('normal output', '{broken', {'embedding': [1, 2], 'custom': True},
                      {'model': 'custom', 'revision': 'bad', 'results': []}):
            self.assertEqual(compact_embedding_payload(value), value)

    def test_malformed_unicode_and_deep_json_do_not_break_tool_results(self):
        malformed = output()
        malformed['results'][0]['path'] = '\ud800'
        encoded = json.dumps(malformed)
        self.assertEqual(compact_embedding_payload(encoded), encoded)
        self.assertEqual(compact_embedding_payload(malformed), malformed)
        nested = '{"model":"fixture","results":' + '[' * 2000 + '"embedding"' + ']' * 2000 + '}'
        self.assertEqual(compact_embedding_payload(nested), nested)

    def test_full_batch_summary_is_much_smaller_and_retains_every_path(self):
        original = output(False)
        original['results'] = [{**original['results'][0], 'path': f'C:\\Lab\\report-{i}.txt',
                                'embedding': [0.123456789] * 4096} for i in range(32)]
        summary = compact_embedding_payload(original)
        self.assertEqual(len(summary['results']), 32)
        self.assertLess(len(json.dumps(summary)), len(json.dumps(original)) // 20)
