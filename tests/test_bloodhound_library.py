"""Reference discovery never expands the executable BloodHound allowlist."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rmm_bloodhound_library as library


class LibraryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.rows = []
        for index in range(30):
            self.rows.append({
                'id': f'12345678-1234-1234-1234-{index:012d}',
                'name': f'Query {index:02d}', 'platforms': ['Active Directory'],
                'category': 'Privileges', 'description': 'Find candidate relationships',
                'query': 'MATCH p=(a)-[:HasSession]->(b) RETURN p',
                'source_url': f'https://example.invalid/queries/{index}',
                'source_path': f'queries/{index}.yml', 'sha256': 'a' * 64,
                'raw_yaml': 'SECRET_RAW_YAML_SENTINEL',
                'upstream': {'guid': f'12345678-1234-1234-1234-{index:012d}', 'revision': 2},
            })
        self.rows[1]['platforms'] = ['Azure']
        self.rows[1]['category'] = 'Tier Zero'
        self.snapshot = {'schema_version': 1, 'repository': 'https://example.invalid/library',
                         'revision': 'pinned-revision', 'entry_count': len(self.rows), 'entries': self.rows}
        self.write()
        (self.root / 'catalog.json').write_text(json.dumps({'queries': [
            {'id': 'local_session_review', 'sources': [self.rows[0]['source_url']]},
        ]}))
        for name, value in (('LIBRARY', self.root / 'library.json'), ('CATALOG', self.root / 'catalog.json')):
            patcher = patch.object(library, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self):
        (self.root / 'library.json').write_text(json.dumps(self.snapshot))

    def test_bounded_search_contains_no_cypher_and_filters_are_exact(self):
        result = library.search_library({})
        self.assertEqual((result['total'], len(result['entries']), result['next_offset']), (30, 10, 10))
        self.assertNotIn('MATCH p=', json.dumps(result))
        self.assertNotIn('SECRET_RAW_YAML_SENTINEL', json.dumps(result))
        self.assertEqual(library.search_library({'offset': 25, 'limit': 25})['next_offset'], None)
        self.assertEqual(library.search_library({'platform': 'active directory'})['total'], 29)
        self.assertEqual(library.search_library({'platform': 'Active'})['total'], 0)
        self.assertEqual(library.search_library({'category': 'tier zero'})['total'], 1)
        self.assertEqual(library.search_library({'text': 'Query 02 HasSession'})['total'], 1)
        self.assertEqual(library.search_library({'text': 'missing'})['entries'], [])

    def test_selected_detail_is_complete_but_never_executable(self):
        result = library.get_library_entry({'id': self.rows[0]['id']})
        entry = result['entry']
        self.assertEqual(entry['query'], self.rows[0]['query'])
        self.assertEqual(entry['local_template_ids'], ['local_session_review'])
        self.assertEqual(entry['compatibility']['status'], 'adapted_template_available')
        self.assertFalse(entry['compatibility']['upstream_executable'])
        self.assertNotIn('raw_yaml', entry)
        self.assertIn('not_verified', entry['compatibility']['coverage_hints_basis'])
        unmapped = library.get_library_entry({'id': self.rows[1]['id']})['entry']
        self.assertEqual(unmapped['compatibility']['status'], 'reference_only')
        self.assertTrue(any('Azure' in hint for hint in unmapped['compatibility']['coverage_hints']))

    def test_search_arguments_reject_unbounded_or_unknown_inputs(self):
        for options in (None, [], {'query': 'MATCH n RETURN n'}, {'limit': 0}, {'limit': 26},
                        {'limit': True}, {'offset': -1}, {'offset': 1.5}, {'text': []}, {'text': 'x' * 201}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'invalid_library_search_options'):
                library.search_library(options)

    def test_detail_is_exact_id_lookup_not_a_path_or_query(self):
        for options in ({}, {'id': '../../secret'}, {'id': []}, {'id': self.rows[0]['id'], 'query': 'RETURN 1'}):
            with self.assertRaisesRegex(ValueError, 'invalid_library_entry_id'):
                library.get_library_entry(options)
        with self.assertRaisesRegex(ValueError, 'library_entry_not_found'):
            library.get_library_entry({'id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'})
        self.rows[0]['query'] = 'x' * (library.MAX_QUERY + 1)
        self.write()
        with self.assertRaisesRegex(ValueError, 'library_entry_too_large'):
            library.get_library_entry({'id': self.rows[0]['id']})

    def test_summary_no_bodies_and_invalid_snapshot_fails_closed(self):
        summary = library.library_summary()
        self.assertEqual(summary['entry_count'], 30)
        self.assertEqual(summary['search_action'], 'library_search')
        self.assertNotIn('entries', summary)
        self.snapshot['entry_count'] = 31
        self.write()
        with self.assertRaisesRegex(ValueError, 'bloodhound_library_unavailable'):
            library.search_library({})

    def test_malformed_metadata_returns_configuration_error(self):
        for field, value in (('source_url', []), ('upstream', []), ('platforms', [{}])):
            original = self.rows[0][field]
            self.rows[0][field] = value
            self.write()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'bloodhound_library_unavailable'):
                library.search_library({})
            self.rows[0][field] = original
        self.write()
        for value in (None, [None], [{'id': 'bad', 'sources': [{}]}]):
            (self.root / 'catalog.json').write_text(json.dumps({'queries': value}))
            with self.assertRaisesRegex(ValueError, 'bloodhound_library_unavailable'):
                library.search_library({})

    def test_guid_lookup_accepts_uppercase(self):
        self.rows[0]['id'] = 'abcdefab-1234-1234-1234-123456789abc'
        self.write()
        entry = library.get_library_entry({'id': self.rows[0]['id'].upper()})['entry']
        self.assertEqual(entry['id'], self.rows[0]['id'])
