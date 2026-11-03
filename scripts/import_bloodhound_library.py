"""Build a lossless JSON reference snapshot from a clean local official-library clone."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote
import uuid

REPOSITORY = 'https://github.com/SpecterOps/BloodHoundQueryLibrary'
PINNED_REVISION = 'b1fee8f5715f8fb3358e187ca3f1bddbfd125f93'
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / 'bloodhound-queries' / 'upstream-library.json'


def git(source, *arguments):
    """Read local Git state without fetching, executing hooks, or contacting a remote."""
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(source), *arguments], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout


def parse_entry(source_path, raw, revision):
    """Preserve all upstream content while validating discovery fields and unique keys."""
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        """Reject duplicate mapping keys rather than silently discarding source metadata."""

    def unique_mapping(loader, node, deep=False):
        """Construct one YAML mapping without accepting ambiguous duplicate keys."""
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError('duplicate YAML key: ' + str(key))
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    text = raw.decode('utf-8')
    try:
        upstream = yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ValueError('invalid YAML: ' + source_path) from exc
    if not isinstance(upstream, dict):
        raise ValueError('query must be a YAML object: ' + source_path)
    for field in ('name', 'guid', 'category', 'query'):
        if not isinstance(upstream.get(field), str) or not upstream[field].strip():
            raise ValueError('missing or invalid ' + field + ': ' + source_path)
    entry_id = str(uuid.UUID(upstream['guid']))
    platforms = upstream.get('platforms')
    platforms = [platforms] if isinstance(platforms, str) else platforms
    if not isinstance(platforms, list) or not platforms or any(not isinstance(p, str) or not p.strip() for p in platforms):
        raise ValueError('missing or invalid platforms: ' + source_path)
    description = upstream.get('description')
    if description is not None and not isinstance(description, str):
        raise ValueError('invalid description: ' + source_path)
    if type(upstream.get('revision')) is not int or upstream['revision'] < 1:
        raise ValueError('invalid revision: ' + source_path)
    if 'prebuilt' in upstream and type(upstream['prebuilt']) is not bool:
        raise ValueError('invalid prebuilt flag: ' + source_path)
    # Reject unsupported YAML values rather than changing their representation.
    json.dumps(upstream, allow_nan=False)
    return {
        'id': entry_id, 'name': upstream['name'], 'platforms': platforms,
        'category': upstream['category'], 'description': description or '',
        'query': upstream['query'], 'source_path': source_path,
        'source_url': REPOSITORY + '/blob/' + revision + '/' + quote(source_path, safe='/'),
        'sha256': hashlib.sha256(raw).hexdigest(), 'raw_yaml': text, 'upstream': upstream,
    }


def build_snapshot(source, revision=PINNED_REVISION):
    """Import every tracked query at the exact clean revision, failing on any omission."""
    source = Path(source).resolve()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('revision must be a full lowercase Git commit SHA')
    if Path(git(source, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != source:
        raise ValueError('source must be the repository root')
    if git(source, 'rev-parse', 'HEAD').decode().strip() != revision:
        raise ValueError('source HEAD does not match the requested revision')
    if git(source, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('source repository must be clean; no files will be silently omitted')
    inventory = git(source, 'ls-tree', '-rz', revision, '--', 'queries').split(b'\0')
    entries = []
    ids = set()
    for record in inventory:
        if not record:
            continue
        metadata, path_bytes = record.split(b'\t', 1)
        path = path_bytes.decode('utf-8')
        if not path.lower().endswith(('.yml', '.yaml')):
            raise ValueError('unexpected file in query inventory: ' + path)
        mode, kind, _object_id = metadata.split()
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('query must be a regular tracked file: ' + path)
        entry = parse_entry(path, git(source, 'show', revision + ':' + path), revision)
        if entry['id'] in ids:
            raise ValueError('duplicate query GUID: ' + entry['id'])
        ids.add(entry['id'])
        entries.append(entry)
    if not entries:
        raise ValueError('query inventory is empty')
    return {'schema_version': 1, 'repository': REPOSITORY, 'revision': revision,
            'entry_count': len(entries), 'entries': sorted(entries, key=lambda row: row['source_path'])}


def write_snapshot(snapshot, output):
    """Atomically write deterministic UTF-8 JSON, without source execution or downloads."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.upstream-library-', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    """Import a reviewed local clone at an explicit revision; never fetch or install."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--revision', default=PINNED_REVISION)
    parser.add_argument('--output', default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()
    try:
        snapshot = build_snapshot(args.source, args.revision)
        write_snapshot(snapshot, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(f"Imported {snapshot['entry_count']} queries at {snapshot['revision']}")


if __name__ == '__main__':
    main()
