"""BloodHound workflow tests use local fixtures, never a domain controller."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

import rmm_bloodhound as bh

SOURCE = 'S-1-5-21-1-2-3-1100'
TARGET = 'S-1-5-21-1-2-3-512'
OPTIONS = dict(domain='lab.example', dc='dc.lab.example', nameserver='192.0.2.10', username='reader', source_sid=SOURCE, target_sid=TARGET)


def collection_file(directory, version=6):
    path = Path(directory) / 'collection.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        for kind, rows in (
            ('domains', [{'ObjectIdentifier': 'S-1-5-21-1-2-3', 'Properties': {'name': 'LAB.EXAMPLE'}}]),
            ('users', [{'ObjectIdentifier': SOURCE, 'Properties': {}}]),
            ('groups', [{'ObjectIdentifier': TARGET, 'Properties': {}}]),
        ):
            archive.writestr(kind + '.json', json.dumps({'meta': {'type': kind, 'count': len(rows), 'version': version}, 'data': rows}))
    return path


class FakeClient:
    endpoint = 'http://127.0.0.1:1030'
    def __init__(self):
        self.calls = []
        self.job_status = 2
        self.failed_files = 0
        self.fail_end = False
        self.missing = False

    def request(self, method, path, body=b'', content_type='application/json', expected=200):
        self.calls.append((method, path, body))
        if path == '/api/version':
            return {'data': {'server_version': 'fixture', 'API': {'current_version': 'v2'}}}
        if path.endswith('/start'):
            return {'data': {'id': 42}}
        if path.endswith('/end') and self.fail_end:
            raise bh.WorkflowError('bloodhound_transport_uncertain')
        if method == 'GET' and '/file-upload?' in path:
            return {'data': [{'id': 42, 'status': self.job_status, 'failed_files': self.failed_files, 'total_files': 3}]}
        return {}

    def graph(self, query):
        if self.missing:
            return {'nodes': {}, 'edges': []}
        if 'shortestPath' in query:
            return {'nodes': {'a': {'objectId': SOURCE}, 'b': {'objectId': TARGET}},
                    'edges': [{'source': 'a', 'target': 'b', 'kind': 'MemberOf'}]}
        sid = SOURCE if SOURCE in query else TARGET
        return {'nodes': {'1': {'objectId': sid}}, 'edges': []}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.store = bh.BloodHoundWorkflow(self.temp.name, lambda: self.client)
        self.run = self.store.create(OPTIONS)

    def tearDown(self):
        for worker in list(self.store.threads):
            worker.join(3)
        self.store.db.close()
        self.temp.cleanup()

    def collected(self):
        path = collection_file(self.run['artifact_directory'])
        self.run.update(state='collected', collection='complete', collection_started_at=bh.now(),
                        artifacts=[bh.validate_artifact(path, self.run)])
        self.store.save(self.run)

    def imported(self):
        self.collected()
        self.store.import_artifacts(self.run)
        self.store.status(self.run['id'])

    def test_collect_import_verify_query_report(self):
        def collector(command, **kwargs):
            self.assertIn('-no-pass', command)
            self.assertIn('--auth-method', command)
            self.assertEqual(command.count('-c'), 1)
            self.assertEqual(command[command.index('-c') + 1], 'DCOnly')
            self.assertEqual(command[command.index('-u') + 1], 'reader')
            self.assertNotIn('shell', kwargs)
            collection_file(kwargs['cwd'])
            return Mock(returncode=0)
        with patch('rmm_bloodhound.collector_info', return_value={'executable': '/tools/bloodhound-ce-python'}), patch('rmm_bloodhound.validate_cache'), patch('rmm_bloodhound.subprocess.run', side_effect=collector):
            self.store.collect(self.run)
        self.assertEqual(self.store.get(self.run['id'])['collection'], 'complete')
        self.store.import_artifacts(self.run)
        self.assertEqual(self.store.get(self.run['id'])['ingestion'], 'pending')
        self.assertEqual(self.store.status(self.run['id'])['state'], 'ingested')
        self.assertEqual(self.store.verify(self.run['id'])['verification'], 'passed')
        summary, graph = self.store.query(self.run['id'], 'candidate_path', {'source_sid': SOURCE, 'target_sid': TARGET})
        self.assertEqual(summary['ordered_path'], [{'source': SOURCE, 'target': TARGET, 'relationship': 'MemberOf'}])
        self.assertEqual(hashlib.sha256(Path(summary['artifact']).read_bytes()).hexdigest(), summary['sha256'])
        report = self.store.report(self.run['id'])
        self.assertEqual(report['detection'], 'not_verified')
        self.assertIn('older', report['run']['provenance_warning'])
        self.assertEqual([path for method, path, _ in self.client.calls if method == 'POST'], ['/api/v2/file-upload/start', '/api/v2/file-upload/42', '/api/v2/file-upload/42/end'])

    def test_repeat_submissions_do_not_repeat_collection(self):
        entered, release = threading.Event(), threading.Event()
        def collect(run):
            entered.set()
            release.wait(2)
            run.update(collection_started_at=bh.now(), state='collected', collection='complete')
            self.store.save(run)
        with patch.object(self.store, 'collect', side_effect=collect) as work:
            first = self.store.launch(self.run['id'], 'collect')
            self.assertTrue(entered.wait(1))
            second = self.store.launch(self.run['id'], 'collect')
            self.assertEqual(first['id'], second['id'])
            release.set()
            for thread in list(self.store.threads):
                thread.join(2)
            self.store.launch(self.run['id'], 'collect')
            self.assertEqual(work.call_count, 1)

    def test_launch_returns_a_snapshot_separate_from_the_worker(self):
        entered, release = threading.Event(), threading.Event()
        def collect(run):
            entered.set()
            release.wait(2)
            run.update(state='collected', collection='complete')
            run['queries'].append({'id': 'worker-update'})
            self.store.save(run)
        with patch.object(self.store, 'collect', side_effect=collect):
            response = self.store.launch(self.run['id'], 'collect')
            self.assertTrue(entered.wait(1))
            release.set()
            for worker in list(self.store.threads):
                worker.join(2)
        self.assertEqual(response['state'], 'collecting')
        self.assertEqual(response['queries'], [])
        self.assertEqual(self.store.get(self.run['id'])['state'], 'collected')

    def test_phase_order_and_changed_artifact(self):
        with self.assertRaisesRegex(bh.WorkflowError, 'invalid_phase_transition'):
            self.store.launch(self.run['id'], 'import')
        self.collected()
        path = Path(self.run['artifact_directory']) / 'collection.zip'
        path.write_bytes(b'bad')
        with self.assertRaises(bh.WorkflowError):
            self.store.import_artifacts(self.run)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.client.calls))
        self.assertFalse(any(path == '/api/v2/file-upload/42' for _, path, _ in self.client.calls))

    def test_artifact_changed_after_validation_is_not_uploaded(self):
        self.collected()
        original = bh.validate_artifact
        calls = 0
        def validate_then_change(path, expected):
            nonlocal calls
            checked = original(path, expected)
            calls += 1
            if calls == 2:
                path.write_bytes(b'changed-after-validation')
            return checked
        with patch.object(bh, 'validate_artifact', side_effect=validate_then_change):
            with self.assertRaisesRegex(bh.WorkflowError, 'collection_artifact_changed'):
                self.store.import_artifacts(self.run)
        self.assertFalse(any(path == '/api/v2/file-upload/42' for _, path, _ in self.client.calls))

    def test_collection_method_cannot_be_overridden(self):
        with self.assertRaisesRegex(bh.WorkflowError, 'create_requires'):
            self.store.create({**OPTIONS, 'collection_method': 'All'})
        status, payload = bh.bloodhound_api(self.temp.name, {
            'action': 'collect', 'run_id': self.run['id'], 'options': {'collection_method': 'All'},
        })
        self.assertEqual(status, 400)
        self.assertEqual(payload['error'], 'unexpected_options')

    def test_partial_ingestion_never_verifies(self):
        for status, failed in ((8, 0), (2, 1), (99, 0)):
            with self.subTest(status=status):
                self.collected() if not self.run['artifacts'] else None
                self.store.import_artifacts(self.run)
                self.client.job_status, self.client.failed_files = status, failed
                self.assertEqual(self.store.status(self.run['id'])['state'], 'ingestion_incomplete')
                with self.assertRaisesRegex(bh.WorkflowError, 'verify_ingestion'):
                    self.store.verify(self.run['id'])

    def test_lost_finalize_response_reconciles_without_upload(self):
        self.collected()
        self.client.fail_end = True
        self.store.launch(self.run['id'], 'import')
        for thread in list(self.store.threads):
            thread.join(2)
        self.assertEqual(self.store.get(self.run['id'])['state'], 'uncertain')
        posts = len([c for c in self.client.calls if c[0] == 'POST'])
        reconciled = self.store.status(self.run['id'])
        self.assertEqual(reconciled['state'], 'ingested')
        self.assertNotIn('error', reconciled)
        self.assertNotIn('end_pending', reconciled)
        self.store.launch(self.run['id'], 'import')
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'POST']), posts)

    def test_malformed_upload_start_has_a_bounded_error_and_is_not_replayed(self):
        self.collected()
        original = self.client.request
        def request(method, path, *args, **kwargs):
            if path.endswith('/start'):
                self.client.calls.append((method, path, b''))
                return {'data': []}
            return original(method, path, *args, **kwargs)
        with patch.object(self.client, 'request', side_effect=request):
            self.store.launch(self.run['id'], 'import')
            for worker in list(self.store.threads):
                worker.join(2)
        run = self.store.get(self.run['id'])
        self.assertEqual(run['state'], 'uncertain')
        self.assertEqual(run['error'], 'invalid_upload_job_id')
        self.store.launch(self.run['id'], 'import')
        self.assertEqual([path for method, path, _ in self.client.calls if method == 'POST'],
                         ['/api/v2/file-upload/start'])

    def test_partial_file_failure_blocks_complete_job(self):
        self.collected()
        self.store.import_artifacts(self.run)
        with patch.object(self.client, 'request', return_value={'data': [{
                'id': 42, 'status': 2, 'total_files': 3, 'failed_files': 0,
                'partial_failed_files': 1}]}):
            state = self.store.status(self.run['id'])
        self.assertEqual(state['state'], 'ingestion_incomplete')
        self.assertEqual(state['ingestion_partial_failed_files'], 1)

    def test_malformed_ingestion_cannot_be_accepted_as_complete(self):
        self.collected()
        self.store.import_artifacts(self.run)
        valid = {'id': 42, 'status': 2, 'total_files': 3, 'failed_files': 0}
        for field, value in (('status', []), ('status', True), ('failed_files', False),
                             ('failed_files', -1), ('partial_failed_files', None),
                             ('total_files', '3')):
            with self.subTest(field=field, value=value), \
                    patch.object(self.client, 'request', return_value={'data': [{**valid, field: value}]}):
                with self.assertRaisesRegex(bh.WorkflowError, 'invalid_ingestion_job_response'):
                    self.store.status(self.run['id'])
                self.assertEqual(self.store.get(self.run['id'])['state'], 'ingesting')
        with patch.object(self.client, 'request', return_value={'data': [None]}):
            with self.assertRaisesRegex(bh.WorkflowError, 'ingestion_job_not_found'):
                self.store.status(self.run['id'])

    def test_missing_objects_and_instance_switch_fail_closed(self):
        self.imported()
        self.client.missing = True
        self.assertEqual(self.store.verify(self.run['id'])['verification'], 'missing_expected_objects')
        with self.assertRaisesRegex(bh.WorkflowError, 'verify_ingestion'):
            self.store.query(self.run['id'], 'identity', {'sid': SOURCE})
        self.client.endpoint = 'https://other.example'
        with self.assertRaisesRegex(bh.WorkflowError, 'instance_changed'):
            self.store.verify(self.run['id'])

    def test_restart_does_not_replay_work(self):
        self.run.update(state='collecting', collection='running')
        self.store.save(self.run)
        second = bh.BloodHoundWorkflow(self.temp.name, lambda: self.client)
        try:
            self.assertEqual(second.get(self.run['id'])['state'], 'uncertain')
            self.assertEqual(second.get(self.run['id'])['collection'], 'uncertain')
            self.assertEqual(self.client.calls, [])
        finally:
            second.db.close()

    def test_old_graph_cannot_replace_missing_current_collection_objects(self):
        self.imported()
        run = self.store.get(self.run['id'])
        run['artifacts'][0]['expected_present'] = ['domain']
        self.store.save(run)
        with self.assertRaisesRegex(bh.WorkflowError, 'missing_from_collection'):
            self.store.verify(run['id'])

    def test_admission_checkpoint_failure_releases_capacity(self):
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.launch(self.run['id'], 'collect')
        self.assertTrue(self.store.slots.acquire(blocking=False))
        self.assertTrue(self.store.slots.acquire(blocking=False))
        self.store.slots.release()
        self.store.slots.release()


class ValidationTests(unittest.TestCase):
    def test_api_rejects_falsy_non_object_options_and_non_string_actions(self):
        for options in ([], '', 0, False):
            code, payload = bh.bloodhound_api('/unused', {'action': 'health', 'options': options})
            self.assertEqual(code, 400)
            self.assertEqual(payload['error'], 'invalid_options')
        for action in ([], {}, 0, None):
            code, payload = bh.bloodhound_api('/unused', {'action': action})
            self.assertEqual(code, 400)
            self.assertEqual(payload['error'], 'invalid_action')

    def test_fixed_templates_reject_injection_and_unknown_parameters(self):
        for values in ({'sid': 'S-1-5-21-1" RETURN 1'}, {'sid': SOURCE, 'query': 'DELETE n'}, {'sid': []}):
            with self.assertRaises(bh.WorkflowError):
                bh.render_query('identity', values)
        with self.assertRaises(bh.WorkflowError):
            bh.render_query('arbitrary', {})
        self.assertIn(SOURCE, bh.render_query('identity', {'sid': SOURCE})[1])

    def test_legacy_and_invalid_collection_formats_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(bh.WorkflowError, 'unsupported_collection_format'):
                bh.validate_artifact(collection_file(tmp, version=5))
            path = Path(tmp) / 'secret.json'
            path.write_text('{"password":"secret"}')
            with self.assertRaises(bh.WorkflowError):
                bh.validate_artifact(path)
            link = Path(tmp) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(bh.WorkflowError):
                bh.validate_artifact(link)

    def test_malformed_collection_properties_have_a_safe_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'domains.json'
            for properties in (None, [], 'invalid'):
                path.write_text(json.dumps({'meta': {'type': 'domains', 'count': 1, 'version': 6},
                                            'data': [{'ObjectIdentifier': SOURCE, 'Properties': properties}]}))
                with self.assertRaisesRegex(bh.WorkflowError, '^unsupported_collection_format$'):
                    bh.validate_artifact(path, OPTIONS)

    def test_ce_json_envelopes_and_partial_http_responses_have_safe_errors(self):
        with patch('rmm_bloodhound.getenv', side_effect=lambda key, default='': {
                'RMM_BLOODHOUND_TOKEN_ID': 'id', 'RMM_BLOODHOUND_TOKEN_KEY': 'key'}.get(key, default)):
            client = bh.BloodHoundClient()
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        client.opener = Mock()
        client.opener.open.return_value = response
        for raw in (b'[]', b'null', b'"invalid"'):
            response.read.return_value = raw
            with self.assertRaisesRegex(bh.WorkflowError, '^invalid_bloodhound_response$'):
                client.request('GET', '/api/version')
        response.read.side_effect = bh.http.client.IncompleteRead(b'private-value')
        with self.assertRaisesRegex(bh.WorkflowError, '^bloodhound_transport_uncertain$'):
            client.request('GET', '/api/version')

    def test_malformed_graph_records_are_rejected_before_evidence_is_saved(self):
        with patch('rmm_bloodhound.getenv', side_effect=lambda key, default='': {
                'RMM_BLOODHOUND_TOKEN_ID': 'id', 'RMM_BLOODHOUND_TOKEN_KEY': 'key'}.get(key, default)):
            client = bh.BloodHoundClient()
        for graph in ({'nodes': {'1': None}, 'edges': []},
                      {'nodes': {'1': {'properties': None}}, 'edges': []},
                      {'nodes': {}, 'edges': [None]}):
            with patch.object(client, 'request', return_value={'data': graph}):
                with self.assertRaisesRegex(bh.WorkflowError, '^unsupported_graph_response$'):
                    client.graph('fixture')

    def test_path_uses_edge_direction_not_array_order(self):
        graph = {'nodes': {'a': {'objectId': SOURCE}, 'b': {'objectId': 'middle'}, 'c': {'objectId': TARGET}},
                 'edges': [{'source': 'b', 'target': 'c', 'kind': 'GenericAll'}, {'source': 'a', 'target': 'b', 'kind': 'MemberOf'}]}
        path = bh.ordered_path(graph, SOURCE, TARGET)
        self.assertEqual([step['relationship'] for step in path], ['MemberOf', 'GenericAll'])
        self.assertIsNone(bh.ordered_path(graph, TARGET, SOURCE))

    def test_exact_byte_hmac_and_safe_transport_failure(self):
        settings = {'RMM_BLOODHOUND_URL': 'https://bh.example', 'RMM_BLOODHOUND_TOKEN_ID': 'id', 'RMM_BLOODHOUND_TOKEN_KEY': 'private-key'}
        with patch('rmm_bloodhound.getenv', side_effect=lambda key, default='': settings.get(key, default)), patch('rmm_bloodhound.now', return_value='2026-11-03T12:34:56+00:00'):
            client = bh.BloodHoundClient()
            response = Mock(status=200)
            response.read.return_value = b'{}'
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            client.opener = Mock()
            client.opener.open.return_value = response
            body = b'{"query":"test"}'
            client.request('POST', '/api/v2/graphs/cypher', body)
            request = client.opener.open.call_args.args[0]
            op = hmac.new(b'private-key', b'POST/api/v2/graphs/cypher', hashlib.sha256).digest()
            dated = hmac.new(op, b'2026-11-03T12', hashlib.sha256).digest()
            self.assertEqual(request.headers['Signature'], base64.b64encode(hmac.new(dated, body, hashlib.sha256).digest()).decode())
            self.assertEqual(request.data, body)
            client.opener.open.side_effect = TimeoutError('contains private-key')
            with self.assertRaisesRegex(bh.WorkflowError, '^bloodhound_transport_uncertain$'):
                client.request('POST', '/api/v2/file-upload/start')

    def test_endpoint_and_redirect_restrictions(self):
        for endpoint in ('http://remote.example', 'https://user:password@bh.example', 'https://bh.example/path', 'https://bh.example?token=x'):
            with patch('rmm_bloodhound.getenv', return_value=endpoint), self.assertRaises(bh.WorkflowError):
                bh.BloodHoundClient()
        self.assertIsNone(bh.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))


class CacheTests(unittest.TestCase):
    def test_requires_matching_client_domain_tgt_and_expiry(self):
        import sys
        from types import ModuleType
        with tempfile.NamedTemporaryFile() as cachefile:
            ccache_module = ModuleType('impacket.krb5.ccache')
            cache = Mock()
            cache.principal.prettyPrint.return_value = b'reader@LAB.EXAMPLE'
            server, client = Mock(), Mock()
            server.prettyPrint.return_value = b'krbtgt/LAB.EXAMPLE@LAB.EXAMPLE'
            client.prettyPrint.return_value = b'reader@LAB.EXAMPLE'
            credential = {'server': server, 'client': client, 'time': {'starttime': 0, 'authtime': 1, 'endtime': 2000}}
            cache.credentials = [credential]
            ccache_module.CCache = Mock()
            ccache_module.CCache.loadFile.return_value = cache
            with patch.dict(sys.modules, {'impacket.krb5.ccache': ccache_module}), patch.dict('os.environ', {'KRB5CCNAME': cachefile.name}), patch('rmm_bloodhound.time.time', return_value=1000):
                bh.validate_cache('reader', 'lab.example')
                credential['time']['endtime'] = 1000
                with self.assertRaisesRegex(bh.WorkflowError, 'valid_domain_tgt'):
                    bh.validate_cache('reader', 'lab.example')
                credential['time']['endtime'] = 2000
                client.prettyPrint.return_value = b'other@LAB.EXAMPLE'
                with self.assertRaisesRegex(bh.WorkflowError, 'valid_domain_tgt'):
                    bh.validate_cache('reader', 'lab.example')
                with self.assertRaisesRegex(bh.WorkflowError, 'principal_mismatch'):
                    bh.validate_cache('different', 'lab.example')


class HttpWorkflowTests(unittest.TestCase):
    def test_real_signed_http_import_to_graph_evidence(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = []
        failures = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, status, data=None):
                raw = json.dumps(data).encode() if data is not None else b''
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def signed(self, body):
                stamp = self.headers.get('RequestDate', '')
                operation = hmac.new(b'test-key', (self.command + self.path).encode(), hashlib.sha256).digest()
                dated = hmac.new(operation, stamp[:13].encode(), hashlib.sha256).digest()
                expected = base64.b64encode(hmac.new(dated, body, hashlib.sha256).digest()).decode()
                valid = self.headers.get('Signature') == expected and self.headers.get('Authorization') == 'bhesignature test-id'
                if not valid:
                    failures.append('bad signature')
                return valid
            def do_GET(self):
                if not self.signed(b''):
                    self.respond(401)
                elif self.path == '/api/version':
                    self.respond(200, {'data': {'server_version': 'fixture'}})
                else:
                    self.respond(200, {'data': [{'id': 7, 'status': 2, 'total_files': 3, 'failed_files': 0}]})
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if not self.signed(body):
                    return self.respond(401)
                seen.append(self.path)
                if self.path.endswith('/start'):
                    self.respond(201, {'data': {'id': 7}})
                elif self.path == '/api/v2/file-upload/7':
                    if self.headers['Content-Type'] != 'application/zip' or not body.startswith(b'PK'):
                        failures.append('wrong upload format')
                    self.respond(202)
                elif self.path.endswith('/end'):
                    self.respond(200)
                else:
                    query = json.loads(body)['query']
                    sid = SOURCE if SOURCE in query else TARGET
                    self.respond(200, {'data': {'nodes': {'n': {'objectId': sid}}, 'edges': []}})
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        settings = {'RMM_BLOODHOUND_URL': f'http://127.0.0.1:{http.server_port}', 'RMM_BLOODHOUND_TOKEN_ID': 'test-id', 'RMM_BLOODHOUND_TOKEN_KEY': 'test-key'}
        try:
            with tempfile.TemporaryDirectory() as tmp, patch('rmm_bloodhound.getenv', side_effect=lambda key, default='': settings.get(key, default)):
                store = bh.BloodHoundWorkflow(tmp)
                try:
                    run = store.create(OPTIONS)
                    path = collection_file(run['artifact_directory'])
                    run.update(state='collected', collection='complete', artifacts=[bh.validate_artifact(path, run)])
                    store.save(run)
                    store.launch(run['id'], 'import')
                    for worker in list(store.threads):
                        worker.join(3)
                    self.assertEqual(store.status(run['id'])['state'], 'ingested')
                    self.assertEqual(store.verify(run['id'])['verification'], 'passed')
                    summary, _ = store.query(run['id'], 'identity', {'sid': SOURCE})
                    self.assertTrue(Path(summary['artifact']).exists())
                    self.assertEqual(store.report(run['id'])['detection'], 'not_verified')
                finally:
                    store.db.close()
            self.assertEqual(failures, [])
            self.assertEqual(seen.count('/api/v2/file-upload/start'), 1)
            self.assertEqual(seen.count('/api/v2/file-upload/7'), 1)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(2)
