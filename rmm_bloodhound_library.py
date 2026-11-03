"""Search the pinned upstream query reference without admitting it for execution."""
from __future__ import annotations

import json
from pathlib import Path
import re

DIRECTORY = Path(__file__).resolve().parent / 'bloodhound-queries'
LIBRARY = DIRECTORY / 'upstream-library.json'
CATALOG = DIRECTORY / 'catalog.json'
MAX_SNAPSHOT = 16 * 1024 * 1024
MAX_QUERY = 256 * 1024


def _read_json(path):
    """Read only a fixed local document and expose safe configuration errors."""
    try:
        with path.open('rb') as handle:
            raw = handle.read(MAX_SNAPSHOT + 1)
        if len(raw) > MAX_SNAPSHOT:
            raise ValueError('oversize')
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError('invalid_document')
        return document
    except (OSError, ValueError, UnicodeError):
        raise ValueError('bloodhound_library_unavailable') from None


def _documents():
    """Load the reference snapshot and independently maintained executable mappings."""
    document = _read_json(LIBRARY)
    entries = document.get('entries')
    if (document.get('schema_version') != 1 or not isinstance(entries, list)
            or not isinstance(document.get('revision'), str)
            or not isinstance(document.get('repository'), str)
            or document.get('entry_count') != len(entries)):
        raise ValueError('bloodhound_library_unavailable')
    seen = set()
    for row in entries:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or row['id'] in seen or not isinstance(row.get('query'), str)
                or not isinstance(row.get('platforms'), list)
                or not all(isinstance(value, str) for value in row['platforms'])
                or not isinstance(row.get('source_url'), str)
                or not isinstance(row.get('upstream', {}), dict)):
            raise ValueError('bloodhound_library_unavailable')
        seen.add(row['id'])
    catalog = _read_json(CATALOG)
    mappings = {}
    templates = catalog.get('queries')
    if not isinstance(templates, list):
        raise ValueError('bloodhound_library_unavailable')
    for template in templates:
        if (not isinstance(template, dict) or not isinstance(template.get('id'), str)
                or not isinstance(template.get('sources'), list)
                or not all(isinstance(source, str) for source in template['sources'])):
            raise ValueError('bloodhound_library_unavailable')
        for source in template['sources']:
            mappings.setdefault(source, []).append(template['id'])
    return document, mappings


def _compatibility(row, template_ids):
    """Explain execution boundaries; inferred coverage hints are not capability checks."""
    content = ' '.join(str(row.get(key, '')) for key in ('name', 'description', 'category', 'query')).casefold()
    hints = []
    if any('azure' in str(platform).casefold() or 'entra' in str(platform).casefold()
           for platform in row['platforms']):
        hints.append('Azure/Entra data is outside the current AD DCOnly collector.')
    if any(term in content for term in ('adcs', 'certtemplate', 'enterpriseca', 'adc-sesc', 'adcse')):
        hints.append('Certificate-service data and schema support require separate validation.')
    if 'hassession' in content or 'session' in str(row.get('category', '')).casefold():
        hints.append('Host session evidence is not collected by DCOnly.')
    if any(term in content for term in ('adminto', 'canrdp', 'canpsremote', 'executedcom', 'local admin')):
        hints.append('Host-local access or administration evidence may require collection beyond DCOnly.')
    if any(term in content for term in ('system_tags', 'highvalue', 'tier zero', 'tier 0')):
        hints.append('Tier Zero/high-value classification may depend on instance configuration or annotations.')
    return {
        'status': 'adapted_template_available' if template_ids else 'reference_only',
        'upstream_executable': False,
        'blockers': [
            'Original upstream Cypher is reference material, not an execution allowlist entry.',
            'Collection completeness, CE capabilities, parameters, result shape, and bounds require review.',
        ],
        'coverage_hints': hints,
        'coverage_hints_basis': 'heuristic_metadata_and_query_text_not_verified_requirements',
        'next_action': ('Inspect a listed local template and its required coverage; it differs from the upstream query.'
                        if template_ids else 'Review and add a bounded local template before execution.'),
    }


def _metadata(row, mappings):
    """Return a compact reference descriptor with no Cypher or YAML body."""
    template_ids = sorted(set(mappings.get(row.get('source_url'), [])))
    return {
        'id': row['id'], 'name': str(row.get('name', ''))[:240],
        'platforms': [str(value)[:120] for value in row['platforms'][:8]],
        'category': str(row.get('category', ''))[:160],
        'description': str(row.get('description') or '')[:600],
        'source_url': row.get('source_url'), 'sha256': row.get('sha256'),
        'local_template_ids': template_ids,
        'compatibility': _compatibility(row, template_ids),
    }


def search_library(options):
    """Find reference metadata by text and exact category/platform filters with bounded paging."""
    if not isinstance(options, dict) or set(options) - {'text', 'platform', 'category', 'offset', 'limit'}:
        raise ValueError('invalid_library_search_options')
    filters = {}
    for key in ('text', 'platform', 'category'):
        value = options.get(key, '')
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError('invalid_library_search_options')
        filters[key] = value.strip().casefold()
    offset, limit = options.get('offset', 0), options.get('limit', 10)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('invalid_library_search_options')
    document, mappings = _documents()
    tokens = filters['text'].split()
    matches = []
    for row in document['entries']:
        if filters['platform'] and filters['platform'] not in [str(value).casefold() for value in row['platforms']]:
            continue
        if filters['category'] and filters['category'] != str(row.get('category', '')).casefold():
            continue
        text = ' '.join(str(row.get(key) or '') for key in ('name', 'description', 'category', 'query')).casefold()
        if all(token in text for token in tokens):
            matches.append(row)
    matches.sort(key=lambda row: (str(row.get('name', '')).casefold(), row['id']))
    return {
        'ok': True, 'revision': document['revision'], 'repository': document['repository'],
        'total': len(matches), 'offset': offset, 'limit': limit,
        'next_offset': offset + limit if offset + limit < len(matches) else None,
        'entries': [_metadata(row, mappings) for row in matches[offset:offset + limit]],
    }


def library_summary():
    """Advertise the pinned reference and discovery actions without returning entry bodies."""
    document, _ = _documents()
    return {'entry_count': document['entry_count'], 'revision': document['revision'],
            'repository': document['repository'], 'search_action': 'library_search',
            'detail_action': 'library_get'}


def get_library_entry(options):
    """Retrieve one exact reference query; never render or execute its original Cypher."""
    if (not isinstance(options, dict) or set(options) != {'id'} or not isinstance(options['id'], str)
            or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', options['id'])):
        raise ValueError('invalid_library_entry_id')
    document, mappings = _documents()
    row = next((row for row in document['entries'] if row['id'].lower() == options['id'].lower()), None)
    if row is None:
        raise ValueError('library_entry_not_found')
    if len(row['query'].encode('utf-8')) > MAX_QUERY:
        raise ValueError('library_entry_too_large_use_source')
    entry = _metadata(row, mappings)
    # Detail preserves the selected entry's prose and Cypher; search stays compact.
    entry.update(name=row.get('name', ''), description=row.get('description') or '',
                 platforms=row['platforms'], category=row.get('category', ''),
                 query=row['query'], source_path=row.get('source_path'))
    upstream = row.get('upstream') or {}
    entry['upstream_metadata'] = {key: upstream[key] for key in
                                  ('guid', 'prebuilt', 'revision', 'resources', 'acknowledgements') if key in upstream}
    return {'ok': True, 'revision': document['revision'], 'repository': document['repository'], 'entry': entry}
