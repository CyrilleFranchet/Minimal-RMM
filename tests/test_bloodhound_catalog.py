"""Query catalog contracts; semantic CE compatibility still needs a live smoke test."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import rmm_bloodhound as bh


class CatalogTests(unittest.TestCase):
    def test_official_sources_map_to_pinned_reference_entries(self):
        import rmm_bloodhound_library as library

        document, mappings = library._documents()
        urls = {entry['source_url'] for entry in document['entries']}
        for source, template_ids in mappings.items():
            if '/SpecterOps/BloodHoundQueryLibrary/' in source:
                with self.subTest(templates=template_ids):
                    self.assertIn(source, urls)

    def test_every_active_query_is_bounded_graph_output_with_provenance(self):
        rows = bh.query_catalog()
        self.assertEqual(len(rows), 16)
        self.assertEqual(len({row['id'] for row in rows}), len(rows))
        for row in rows:
            with self.subTest(query=row['id']):
                parameters = {name: 'lab.example' if kind == 'domain' else 'S-1-5-21-1-2-3-1100'
                              for name, kind in row['parameters'].items()}
                _, rendered = bh.render_query(row['id'], parameters)
                self.assertTrue(rendered.startswith('MATCH '))
                self.assertRegex(rendered, r'\bRETURN\b')
                self.assertRegex(rendered, r'\bLIMIT ' + str(row['result_limit']) + r'$')
                self.assertLessEqual(row['result_limit'], 50)
                self.assertNotRegex(rendered, r'\b(?:CREATE|DELETE|DETACH|SET|REMOVE|MERGE|CALL|LOAD)\b')
                self.assertNotRegex(rendered, r'\*\d*\.\.(?!\d)')
                self.assertNotIn('{', rendered)
                self.assertTrue(row['sources'])
                self.assertTrue(all(source.startswith('https://') for source in row['sources']))
                self.assertTrue(set(row['required_collection']) <= {'Group', 'ACL', 'Trusts', 'ObjectProps', 'Container'})
                self.assertTrue(row['interpretation'])
                self.assertTrue(row['limitations'])
                for field, kind in row['parameters'].items():
                    invalid = dict(parameters, **{field: 'example" RETURN n'})
                    with self.assertRaises(bh.WorkflowError):
                        bh.render_query(row['id'], invalid)

    def test_deferred_ideas_cannot_be_executed(self):
        deferred = bh.deferred_query_catalog()
        self.assertTrue(deferred)
        for row in deferred:
            self.assertNotIn('query', row)
            self.assertTrue(row['blockers'])
            with self.assertRaises(bh.WorkflowError):
                bh.render_query(row['id'], {})

    def test_coverage_rejection_precedes_network_and_evidence_retains_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.endpoint = 'http://127.0.0.1:1030'
            client.graph.return_value = {'nodes': {}, 'edges': []}
            store = bh.BloodHoundWorkflow(directory, lambda: client)
            self.addCleanup(store.db.close)
            run = store.create(dict(domain='lab.example', dc='dc.lab.example', nameserver='192.0.2.1',
                                    username='reader', source_sid='S-1-5-21-1-2-3-1100', target_sid='S-1-5-21-1-2-3-512'))
            run.update(state='verified', ingestion_id=1, bloodhound_instance=client.endpoint, coverage=['ObjectProps'])
            store.save(run)
            with self.assertRaisesRegex(bh.WorkflowError, 'query_collection_coverage_missing:Group'):
                store.query(run['id'], 'memberships', {'sid': run['source_sid']})
            client.graph.assert_not_called()
            self.assertEqual(store.get(run['id'])['queries'], [])
            summary, _ = store.query(run['id'], 'identity', {'sid': run['source_sid']})
            evidence = json.loads(Path(summary['artifact']).read_text())
            self.assertEqual(summary['result_status'], 'no_matches_in_query_scope')
            self.assertEqual(evidence['sources'], summary['sources'])
            self.assertEqual(summary['catalog_sha256'], hashlib.sha256(bh.CATALOG.read_bytes()).hexdigest())
            self.assertEqual(summary['required_collection'], ['ObjectProps'])
            self.assertIn('not_per_property', summary['coverage_basis'])
            self.assertIn('older', summary['provenance_warning'])
