"""Local BloodHound CE collection, ingestion, and bounded graph evidence workflow."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from collections import deque
import hashlib
import hmac
import ipaddress
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from rmm_settings import getenv

CATALOG = Path(__file__).resolve().parent / 'bloodhound-queries' / 'catalog.json'
MAX_FILE = 64 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
JOB_STATUS = {0: 'ready', 1: 'running', 2: 'complete', 3: 'canceled', 4: 'timed_out',
              5: 'failed', 6: 'ingesting', 7: 'analyzing', 8: 'partial'}


class WorkflowError(ValueError):
    """Safe public failure code; raw HTTP/provider output is never returned."""


def now():
    """Return an authoritative UTC observation timestamp."""
    return datetime.now(timezone.utc).isoformat()


def validate(value, kind):
    """Validate query and collection inputs before rendering or execution."""
    if not isinstance(value, str):
        raise WorkflowError('invalid_' + kind)
    if kind == 'sid':
        if not re.fullmatch(r'S-1-[0-9]{1,10}(?:-[0-9]{1,10}){1,15}', value):
            raise WorkflowError('invalid_sid')
        return value
    if kind == 'username' and re.fullmatch(r'[A-Za-z0-9_.$-]{1,64}', value):
        return value
    if kind == 'domain':
        if len(value) > 253 or '.' not in value or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', p) for p in value.split('.')):
            raise WorkflowError('invalid_domain')
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise WorkflowError('hostname_required')
        return value.lower()
    if kind == 'ip':
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise WorkflowError('invalid_ip') from None
    if kind == 'run_id' and re.fullmatch(r'[a-f0-9]{32}', value):
        return value
    raise WorkflowError('invalid_' + kind)


def query_catalog():
    """Read the maintained query catalog without accepting user-supplied templates."""
    return json.loads(CATALOG.read_text(encoding='utf-8'))['queries']


def deferred_query_catalog():
    """Expose coverage/schema blockers without making deferred ideas executable."""
    return json.loads(CATALOG.read_text(encoding='utf-8')).get('deferred_queries', [])


def render_query(query_id, parameters):
    """Render fixed Cypher templates with validated string literals only."""
    query = next((q for q in query_catalog() if q['id'] == query_id), None)
    if query is None or not isinstance(parameters, dict) or set(parameters) != set(query['parameters']):
        raise WorkflowError('invalid_query_parameters')
    values = {}
    for name, kind in query['parameters'].items():
        value = validate(parameters[name], kind)
        values[name] = json.dumps(value.upper() if kind == 'domain' else value)
    return query, query['query'].format(**values)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward BloodHound authentication through redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BloodHoundClient:
    """Bounded CE HTTP client with exact-byte HMAC signing and safe errors."""
    def __init__(self):
        endpoint = getenv('RMM_BLOODHOUND_URL', 'http://127.0.0.1:1030').rstrip('/')
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            _ = parsed.port
        except ValueError:
            raise WorkflowError('invalid_bloodhound_endpoint') from None
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise WorkflowError('invalid_bloodhound_endpoint')
        if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise WorkflowError('bloodhound_requires_https_or_loopback')
        self.endpoint = endpoint
        self.token_id = getenv('RMM_BLOODHOUND_TOKEN_ID', '')
        self.token_key = getenv('RMM_BLOODHOUND_TOKEN_KEY', '')
        if not self.token_id or not self.token_key or any(c in self.token_id for c in '\r\n'):
            raise WorkflowError('bloodhound_api_credentials_missing')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=b'', content_type='application/json', expected=200):
        """Send one request; never automatically repeat a mutation after failure."""
        stamp = now()
        operation = hmac.new(self.token_key.encode(), (method + path).encode(), hashlib.sha256).digest()
        dated = hmac.new(operation, stamp[:13].encode(), hashlib.sha256).digest()
        signature = base64.b64encode(hmac.new(dated, body, hashlib.sha256).digest()).decode()
        headers = {'Authorization': 'bhesignature ' + self.token_id, 'RequestDate': stamp,
                   'Signature': signature, 'Content-Type': content_type, 'Accept': 'application/json'}
        req = urllib.request.Request(self.endpoint + path, data=body if method != 'GET' else None, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                if response.status != expected:
                    raise WorkflowError('unexpected_bloodhound_status')
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise WorkflowError('bloodhound_response_too_large')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise WorkflowError('bloodhound_http_' + str(exc.code)) from None
        except (OSError, urllib.error.URLError):
            raise WorkflowError('bloodhound_transport_uncertain') from None
        except WorkflowError:
            raise
        except (ValueError, UnicodeError):
            raise WorkflowError('invalid_bloodhound_response') from None

    def graph(self, query):
        """Execute a maintained, read-only query and validate its graph envelope."""
        result = self.request('POST', '/api/v2/graphs/cypher', json.dumps({'query': query, 'include_properties': True}).encode())
        data = result.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('nodes'), dict) or not isinstance(data.get('edges'), list):
            raise WorkflowError('unsupported_graph_response')
        return data


def collector_info():
    """Probe the installed CE collector help without contacting AD."""
    executable = shutil.which(getenv('RMM_BLOODHOUND_COLLECTOR', 'bloodhound-ce-python'))
    if not executable:
        raise WorkflowError('ce_collector_missing')
    if Path(executable).name != 'bloodhound-ce-python':
        raise WorkflowError('ce_collector_executable_required')
    try:
        result = subprocess.run([executable, '--help'], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkflowError('ce_collector_probe_failed') from None
    help_text = (result.stdout + result.stderr).decode(errors='replace')
    if result.returncode or not all(flag in help_text for flag in ('--collectionmethod', '--domain', '--kerberos', '-no-pass', '--auth-method', '--dns-tcp')):
        raise WorkflowError('unsupported_collector_cli')
    try:
        version = importlib.metadata.version('bloodhound-ce')
    except importlib.metadata.PackageNotFoundError:
        version = 'unverified'
    # Keep a reproducible fingerprint, not potentially sensitive console text.
    return {'executable': executable, 'help_sha256': hashlib.sha256(help_text.encode()).hexdigest(),
            'edition': 'CE', 'service_environment_package_version': version,
            'executable_sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest()}


def validate_cache(username, domain):
    """Require a matching unexpired TGT in the configured file cache, without a bind."""
    cache_path = os.environ.get('KRB5CCNAME', '')
    if not cache_path or not Path(cache_path).is_file():
        raise WorkflowError('kerberos_file_cache_required')
    try:
        from impacket.krb5.ccache import CCache
        cache = CCache.loadFile(cache_path)
        expected = (username + '@' + domain).casefold()
        if cache.principal.prettyPrint().decode().casefold() != expected:
            raise WorkflowError('kerberos_cache_principal_mismatch')
        target = ('krbtgt/' + domain + '@' + domain).casefold()
        current = time.time()
        if not any(c['server'].prettyPrint().decode().casefold() == target
                   and c['client'].prettyPrint().decode().casefold() == expected
                   and (c['time']['starttime'] or c['time']['authtime']) <= current < c['time']['endtime'] - 60
                   for c in cache.credentials):
            raise WorkflowError('valid_domain_tgt_required')
    except WorkflowError:
        raise
    except ImportError:
        raise WorkflowError('cache_validation_dependency_missing') from None
    except Exception:
        raise WorkflowError('kerberos_cache_validation_failed') from None


def validate_artifact(path, expected=None):
    """Validate bounded CE JSON or ZIP records without extraction or side effects."""
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE:
        raise WorkflowError('invalid_collection_artifact')
    raw = path.read_bytes()
    types = set()
    count = 0
    present = set()
    def inspect(content):
        nonlocal count
        try:
            data = json.loads(content)
            meta = data['meta']
            rows = data['data']
            if not isinstance(meta, dict) or not isinstance(rows, list) or meta.get('type') not in {'users', 'groups', 'computers', 'domains', 'gpos', 'ous', 'containers'} or meta.get('count') != len(rows) or meta.get('version') != 6:
                raise ValueError()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('ObjectIdentifier'), str):
                    raise ValueError()
                if expected:
                    for key in ('source_sid', 'target_sid'):
                        if row['ObjectIdentifier'] == expected[key]:
                            present.add(key)
                    if meta['type'] == 'domains' and str(row.get('Properties', {}).get('name', '')).lower() == expected['domain']:
                        present.add('domain')
            types.add(meta['type'])
            count += len(rows)
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise WorkflowError('unsupported_collection_format') from None
    if path.suffix == '.zip':
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if not entries or len(entries) > 32 or sum(e.file_size for e in entries) > MAX_FILE:
                    raise WorkflowError('oversized_collection_archive')
                for entry in entries:
                    if entry.is_dir() or not entry.filename.endswith('.json') or entry.flag_bits & 1:
                        raise WorkflowError('unsupported_collection_archive')
                    inspect(archive.read(entry))
        except zipfile.BadZipFile:
            raise WorkflowError('invalid_collection_archive') from None
    elif path.suffix == '.json':
        inspect(raw)
    else:
        raise WorkflowError('unsupported_collection_artifact')
    return {'name': path.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
            'types': sorted(types), 'objects': count, 'expected_present': sorted(present)}


def ordered_path(graph, source_sid, target_sid):
    """Reconstruct a bounded directed chain from graph references, never array order."""
    nodes = graph['nodes']
    sources = [str(key) for key, node in nodes.items() if isinstance(node, dict) and node.get('objectId') == source_sid]
    targets = {str(key) for key, node in nodes.items() if isinstance(node, dict) and node.get('objectId') == target_sid}
    adjacency = {}
    for edge in graph['edges']:
        if isinstance(edge, dict):
            adjacency.setdefault(str(edge.get('source')), []).append(edge)
    pending = deque((key, []) for key in sources)
    seen = set(sources)
    while pending:
        key, path = pending.popleft()
        if key in targets:
            return path
        if len(path) == 6:
            continue
        for edge in adjacency.get(key, []):
            target = str(edge.get('target'))
            if target not in seen and target in nodes:
                seen.add(target)
                step = {'source': nodes[key].get('objectId'), 'target': nodes[target].get('objectId'),
                        'relationship': edge.get('kind') or edge.get('label')}
                pending.append((target, path + [step]))
    return None


class BloodHoundWorkflow:
    """One storage owner's persisted runs and bounded asynchronous workers."""
    def __init__(self, root, client_factory=BloodHoundClient):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.threads = set()
        self.client_factory = client_factory
        db_path = self.root / 'runs.sqlite3'
        descriptor = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.commit()
        for run_id, raw in self.db.execute('SELECT id,data FROM runs').fetchall():
            run = json.loads(raw)
            if run.get('state') in ('collecting', 'uploading'):
                run['collection' if run['state'] == 'collecting' else 'ingestion'] = 'uncertain'
                run.update(state='uncertain', error='owner_restarted_inspect_before_retry')
                self.save(run)

    def save(self, run):
        """Persist a checkpoint before the next externally visible operation."""
        run['updated_at'] = now()
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO runs(id,data) VALUES (?,?)', (run['id'], json.dumps(run)))

    def get(self, run_id):
        """Read only a validated ID within this storage owner."""
        validate(run_id, 'run_id')
        with self.lock:
            row = self.db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise WorkflowError('run_not_found')
        return json.loads(row[0])

    def create(self, options):
        """Create an isolated artifact directory and an explicit analysis objective."""
        if set(options) != {'domain', 'dc', 'nameserver', 'username', 'source_sid', 'target_sid'}:
            raise WorkflowError('create_requires_domain_dc_nameserver_username_source_sid_target_sid')
        run = {name: validate(value, 'domain' if name in ('domain', 'dc') else 'ip' if name == 'nameserver' else 'username' if name == 'username' else 'sid')
               for name, value in options.items()}
        run_id = uuid.uuid4().hex
        directory = self.root / run_id
        directory.mkdir(mode=0o700)
        (directory / 'collection').mkdir(mode=0o700)
        run.update(id=run_id, created_at=now(), state='created', collection='not_started',
                   ingestion='not_started', verification='not_started', detection='not_verified',
                   artifacts=[], queries=[], coverage=['Group', 'ACL', 'Trusts', 'ObjectProps', 'Container'],
                   artifact_directory=str(directory / 'collection'))
        self.save(run)
        return run

    def launch(self, run_id, action):
        """Admit one phase once; repeat submissions return the existing run."""
        with self.lock:
            run = self.get(run_id)
            expected = 'created' if action == 'collect' else 'collected'
            if run['state'] != expected:
                if (action == 'collect' and run.get('collection_started_at')) or (action == 'collect' and run['state'] == 'collecting') or (action == 'import' and run.get('ingestion') != 'not_started') or (action == 'import' and run['state'] == 'uploading'):
                    return run
                raise WorkflowError('invalid_phase_transition')
            if not self.slots.acquire(blocking=False):
                raise WorkflowError('workflow_capacity_reached')
            worker = None
            try:
                run['state'] = 'collecting' if action == 'collect' else 'uploading'
                self.save(run)
                worker = threading.Thread(target=self._work, args=(run, action), daemon=True)
                self.threads.add(worker)
                worker.start()
            except Exception:
                if worker is not None:
                    self.threads.discard(worker)
                self.slots.release()
                run.update(state=expected, error='worker_start_failed')
                self.save(run)
                raise WorkflowError('worker_start_failed') from None
            return run

    def _work(self, run, action):
        """Run a phase without the model request's lifetime or HTTP wait budget."""
        try:
            if action == 'collect':
                self.collect(run)
            else:
                self.import_artifacts(run)
        except Exception as exc:
            run.update(state='uncertain' if action == 'import' else 'collection_failed',
                       error=str(exc) if isinstance(exc, WorkflowError) else 'workflow_failed')
            if action == 'collect':
                run['collection'] = 'failed'
            else:
                run['ingestion'] = 'uncertain'
            self.save(run)
        finally:
            with self.lock:
                self.threads.discard(threading.current_thread())
            self.slots.release()

    def collect(self, run):
        """Run the CE collector with an existing Kerberos cache, no shell or password."""
        info = collector_info()
        validate_cache(run['username'], run['domain'])
        directory = self.root / run['id'] / 'collection'
        if any(directory.iterdir()):
            raise WorkflowError('collection_directory_not_empty')
        command = [info['executable'], '-u', run['username'], '-k', '-no-pass', '--auth-method', 'kerberos',
                   '-d', run['domain'], '-dc', run['dc'], '-ns', run['nameserver'], '--dns-tcp',
                   '-c', 'DCOnly', '--zip', '-op', run['id']]
        run.update(collector=info, collection='running', collection_started_at=now())
        self.save(run)
        log_path = self.root / run['id'] / 'collector.log'
        # Raw collector diagnostics remain local; no credentials or command output enter chat.
        with open(log_path, 'xb') as output:
            os.chmod(log_path, 0o600)
            try:
                result = subprocess.run(command, cwd=directory, stdout=output, stderr=subprocess.STDOUT,
                                        timeout=1800, check=False)
            except subprocess.TimeoutExpired:
                raise WorkflowError('collection_deadline_exceeded') from None
        run['collection_finished_at'] = now()
        run['collector_exit_code'] = result.returncode
        if result.returncode:
            raise WorkflowError('collector_failed_inspect_local_log')
        files = sorted(directory.glob('*.zip')) or sorted(directory.glob('*.json'))
        if not files or len(files) > 16:
            raise WorkflowError('collection_artifacts_missing_or_excessive')
        artifacts = [validate_artifact(path, run) for path in files]
        if sum(a['bytes'] for a in artifacts) > MAX_FILE:
            raise WorkflowError('collection_total_too_large')
        run.update(artifacts=artifacts, collection='complete', state='collected')
        self.save(run)

    def import_artifacts(self, run):
        """Checkpoint every upload, never replaying an ambiguous import automatically."""
        client = self.client_factory()
        run['bloodhound_instance'] = client.endpoint
        run['bloodhound_version'] = client.request('GET', '/api/version').get('data', {})
        self.save(run)
        # Persist intent before a POST whose response might be lost.
        run['ingestion'] = 'starting'
        self.save(run)
        result = client.request('POST', '/api/v2/file-upload/start', expected=201)
        job_id = result.get('data', {}).get('id')
        if type(job_id) is not int or job_id < 1:
            raise WorkflowError('invalid_upload_job_id')
        run.update(ingestion_id=job_id, ingestion='uploading', uploaded=[])
        self.save(run)
        for artifact in run['artifacts']:
            path = self.root / run['id'] / 'collection' / artifact['name']
            checked = validate_artifact(path, run)
            if checked != artifact:
                raise WorkflowError('collection_artifact_changed')
            run['upload_pending'] = artifact['name']
            self.save(run)
            client.request('POST', f'/api/v2/file-upload/{job_id}', path.read_bytes(),
                           'application/zip' if path.suffix == '.zip' else 'application/json', expected=202)
            run['uploaded'].append(artifact['name'])
            run.pop('upload_pending', None)
            self.save(run)
        run['ingestion'] = 'finalizing'
        run['end_pending'] = True
        run['ingestion_started_at'] = now()
        self.save(run)
        client.request('POST', f'/api/v2/file-upload/{job_id}/end')
        run.update(ingestion='pending', state='ingesting')
        self.save(run)

    def client_for(self, run):
        """Refuse to silently switch an imported run to another instance."""
        client = self.client_factory()
        if client.endpoint != run.get('bloodhound_instance'):
            raise WorkflowError('bloodhound_instance_changed')
        return client

    def status(self, run_id):
        """Poll a known ingestion once without reuploading or waiting indefinitely."""
        with self.lock:
            run = self.get(run_id)
            if run['state'] != 'ingesting' and not (run['state'] == 'uncertain' and run.get('end_pending')):
                return run
            client = self.client_for(run)
            result = client.request('GET', f"/api/v2/file-upload?id=eq:{run['ingestion_id']}&limit=1")
            jobs = result.get('data')
            if not isinstance(jobs, list) or len(jobs) != 1 or jobs[0].get('id') != run['ingestion_id']:
                raise WorkflowError('ingestion_job_not_found')
            job = jobs[0]
            state = JOB_STATUS.get(job.get('status'), 'unknown')
            run.update(ingestion=state, ingestion_checked_at=now(),
                       ingestion_files=job.get('total_files'), ingestion_failed_files=job.get('failed_files'),
                       ingestion_partial_failed_files=job.get('partial_failed_files', 0))
            if (state == 'complete' and job.get('failed_files') == 0
                    and job.get('partial_failed_files', 0) == 0
                    and type(job.get('total_files')) is int and job['total_files'] > 0):
                run['state'] = 'ingested'
            elif state in ('complete', 'partial', 'failed', 'canceled', 'timed_out', 'unknown'):
                run['state'] = 'ingestion_incomplete'
            elif time.time() - datetime.fromisoformat(run['ingestion_started_at']).timestamp() > 1800:
                run.update(state='uncertain', error='ingestion_deadline_exceeded')
            self.save(run)
            return run

    def query(self, run_id, query_id, parameters, verifying=False):
        """Save bounded graph evidence with its exact query and run provenance."""
        with self.lock:
            run = self.get(run_id)
            if len(run['queries']) >= 100:
                raise WorkflowError('run_query_limit_reached')
            if run['state'] not in (('ingested', 'verified') if verifying else ('verified',)):
                raise WorkflowError('verify_ingestion_before_queries')
            definition, rendered = render_query(query_id, parameters)
            missing = set(definition['required_collection']) - set(run.get('coverage', []))
            if missing:
                raise WorkflowError('query_collection_coverage_missing:' + ','.join(sorted(missing)))
            graph = self.client_for(run).graph(rendered)
            evidence_id = uuid.uuid4().hex
            filename = 'query-' + evidence_id + '.json'
            evidence = {'query_id': query_id, 'query': rendered, 'parameters': parameters,
                        'run_id': run_id, 'ingestion_id': run['ingestion_id'], 'observed_at': now(),
                        'limitations': definition['limitations'], 'graph': graph,
                        'interpretation': definition['interpretation'],
                        'sources': definition.get('sources', []),
                        'required_collection': definition['required_collection'],
                        'required_properties': definition.get('required_properties', []),
                        'required_edges': definition.get('required_edges', []),
                        'catalog_sha256': hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
                        'coverage_basis': 'requested_collection_methods_not_per_property_completeness',
                        'provenance_warning': 'Instance-wide graph may contain older data beyond this run.'}
            path = self.root / run_id / filename
            raw = json.dumps(evidence).encode()
            with open(path, 'xb') as output:
                os.chmod(path, 0o600)
                output.write(raw)
            summary = {'id': evidence_id, 'query_id': query_id, 'artifact': str(path),
                       'sha256': hashlib.sha256(raw).hexdigest(), 'node_count': len(graph['nodes']),
                       'edge_count': len(graph['edges']), 'limitations': definition['limitations'],
                       'observed_at': evidence['observed_at'],
                       'interpretation': evidence['interpretation'], 'sources': evidence['sources'],
                       'required_collection': evidence['required_collection'],
                       'required_properties': evidence['required_properties'],
                       'required_edges': evidence['required_edges'],
                       'catalog_sha256': evidence['catalog_sha256'],
                       'coverage_basis': evidence['coverage_basis'],
                       'provenance_warning': evidence['provenance_warning'],
                       'result_status': 'graph_evidence_returned' if graph['nodes'] else 'no_matches_in_query_scope'}
            if query_id == 'candidate_path':
                summary['ordered_path'] = ordered_path(graph, parameters['source_sid'], parameters['target_sid'])
                summary['path_status'] = 'candidate' if summary['ordered_path'] else 'not_established'
            run['queries'].append(summary)
            self.save(run)
            return summary, graph

    def verify(self, run_id):
        """Check expected domain and endpoint identities after confirmed ingestion."""
        with self.lock:
            run = self.get(run_id)
            present = {value for artifact in run['artifacts'] for value in artifact.get('expected_present', [])}
            if not {'domain', 'source_sid', 'target_sid'}.issubset(present):
                raise WorkflowError('expected_objects_missing_from_collection')
            checks = [('domain', {'domain': run['domain']}), ('identity', {'sid': run['source_sid']}),
                      ('identity', {'sid': run['target_sid']})]
            passed = True
            for query_id, parameters in checks:
                _, graph = self.query(run_id, query_id, parameters, verifying=True)
                expected = parameters.get('sid')
                if expected:
                    found = any(node.get('objectId') == expected or node.get('properties', {}).get('objectid') == expected
                                for node in graph['nodes'].values() if isinstance(node, dict))
                else:
                    found = bool(graph['nodes'])
                passed = passed and found
            run = self.get(run_id)
            run.update(verification='passed' if passed else 'missing_expected_objects', state='verified' if passed else 'ingested',
                       provenance_warning='Graph is instance-wide and may contain older data; import does not isolate a dataset.')
            self.save(run)
            return run

    def report(self, run_id):
        """Return a secret-free handoff, with discovery distinct from detection."""
        run = self.get(run_id)
        return {'run': run, 'candidate_paths': 'graph evidence only; no path executed',
                'detection': 'not_verified', 'next_action': {'created': 'collect', 'collected': 'import',
                 'ingesting': 'status', 'ingested': 'verify', 'verified': 'query'}.get(run['state'], 'inspect recorded evidence'),
                'limitations': ['LDAP-oriented collection does not collect live sessions or local admin membership.',
                                'The query catalog covers selected edge types and bounded depths only.',
                                'BloodHound merges data; existing graph records are not attributed solely to this run.']}

    def preflight(self):
        """Report independent service and collector prerequisites without collecting."""
        result = {'collector_ready': False, 'bloodhound_ready': False,
                  'kerberos_cache_configured': bool(os.environ.get('KRB5CCNAME')),
                  'kerberos_cache_validity': 'checked_for_run_at_collection',
                  'ad_connectivity': 'not_tested', 'blockers': []}
        try:
            result['collector'] = collector_info()
            result['collector_ready'] = True
        except WorkflowError as exc:
            result['blockers'].append(str(exc))
        try:
            client = self.client_factory()
            result['bloodhound_version'] = client.request('GET', '/api/version').get('data', {})
            # Unlike /api/version, the ingestion list checks authentication and access.
            client.request('GET', '/api/v2/file-upload?limit=1')
            result['bloodhound_ready'] = True
        except WorkflowError as exc:
            result['blockers'].append(str(exc))
        if not result['kerberos_cache_configured']:
            result['blockers'].append('kerberos_cache_not_configured')
        return result


_STORES = {}
_STORES_LOCK = threading.Lock()


def bloodhound_api(root, body):
    """Dispatch authenticated owner-local API actions with bounded typed options."""
    try:
        if not isinstance(body, dict) or set(body) - {'action', 'run_id', 'options'}:
            raise WorkflowError('invalid_request_fields')
        action = body.get('action', 'preflight')
        if not isinstance(action, str):
            raise WorkflowError('invalid_action')
        options = body.get('options', {})
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise WorkflowError('invalid_options')
        if action in ('library_search', 'library_get'):
            if body.get('run_id'):
                raise WorkflowError('unexpected_library_run_id')
            from rmm_bloodhound_library import search_library, get_library_entry
            try:
                result = search_library(options) if action == 'library_search' else get_library_entry(options)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from None
            return 200, {'ok': True, **result}
        if action in ('ui', 'health', 'ensure'):
            if options or body.get('run_id'):
                raise WorkflowError('unexpected_ui_options')
            from rmm_exegol_runtime import get_runtime
            runtime = get_runtime(root)
            snapshot = runtime.status() if action == 'health' else runtime.ensure()
            if action == 'health':
                return 200, {'ok': True, 'runtime': snapshot}
            if action == 'ensure' or snapshot.get('state') != 'ready':
                code = 202 if snapshot.get('state') in ('checking', 'starting') else (200 if snapshot.get('state') == 'ready' else 503)
                payload = {'ok': code < 400, 'runtime': snapshot, 'retry_after': 2}
                if code >= 400:
                    payload['error'] = snapshot.get('error') or 'bloodhound_runtime_unavailable'
                return code, payload
            from rmm_bloodhound_proxy import open_ui
            return open_ui()
        if action in ('create', 'collect', 'import', 'verify', 'query'):
            if options and action in ('collect', 'import', 'verify'):
                raise WorkflowError('unexpected_options')
            from rmm_exegol_runtime import get_runtime
            snapshot = get_runtime(root).status()
            if snapshot.get('exegol_status') != 'ready':
                return 503, {'ok': False, 'error': 'exegol_workspace_not_ready', 'runtime': snapshot}
        key = str(Path(root).resolve())
        if action == 'result':
            request_id = validate(body.get('run_id', ''), 'run_id')
            path = Path(key) / 'relay-results' / (request_id + '.json')
            if not path.is_file():
                raise WorkflowError('workspace_request_not_found_inspect_before_retry')
            cached = json.loads(path.read_text())
            if cached.get('state') == 'complete':
                return cached['status'], cached['payload']
            return 409, {'error': 'workspace_operation_uncertain_inspect_local_runs'}
        with _STORES_LOCK:
            store = _STORES.setdefault(key, None)
            if store is None:
                store = _STORES[key] = BloodHoundWorkflow(key)
        if action == 'create':
            return 201, {'ok': True, 'run': store.create(options)}
        if action == 'query':
            if set(options) != {'query_id', 'parameters'}:
                raise WorkflowError('query_requires_query_id_and_parameters')
            summary, graph = store.query(body.get('run_id', ''), options['query_id'], options['parameters'])
            nodes = list(graph['nodes'].items())[:25]
            preview = {key: {field: node.get(field) for field in ('objectId', 'label', 'kind')}
                       for key, node in nodes if isinstance(node, dict)}
            edges = [{field: edge.get(field) for field in ('source', 'target', 'kind', 'label')}
                     for edge in graph['edges'][:50] if isinstance(edge, dict)]
            return 200, {'ok': True, 'evidence': summary, 'graph_preview': {'nodes': preview, 'edges': edges},
                         'preview_truncated': len(nodes) < len(graph['nodes']) or len(graph['edges']) > 50}
        if options:
            raise WorkflowError('unexpected_options')
        if action == 'preflight':
            return 200, {'ok': True, 'preflight': store.preflight()}
        if action == 'queries':
            from rmm_bloodhound_library import library_summary
            return 200, {'ok': True, 'queries': query_catalog(), 'deferred_queries': deferred_query_catalog(),
                         'reference_library': library_summary()}
        run_id = body.get('run_id', '')
        if action in ('collect', 'import'):
            return 202, {'ok': True, 'run': store.launch(run_id, action)}
        if action == 'status':
            return 200, {'ok': True, 'run': store.status(run_id)}
        if action == 'verify':
            return 200, {'ok': True, 'run': store.verify(run_id)}
        if action == 'report':
            return 200, {'ok': True, **store.report(run_id)}
        raise WorkflowError('unknown_action')
    except WorkflowError as exc:
        return 400, {'ok': False, 'error': str(exc)}
    except Exception:
        return 500, {'ok': False, 'error': 'bloodhound_workflow_failed'}
