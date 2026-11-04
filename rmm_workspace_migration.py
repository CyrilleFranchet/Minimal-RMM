"""Copy stopped legacy server storage into a fresh workspace without changing its source."""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from rmm_settings import SCHEMA, SettingsError, SettingsStore, definition
from rmm_workspace import valid_workspace_id
from workspace_service import WorkspaceStore


def read_object(path):
    """Read legacy JSON objects, refusing malformed data rather than silently skipping it."""
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object: ' + str(path))
    return value


def copy_storage(source, destination):
    """Copy regular files; materialize SQLite backups with WAL data in a temporary clone."""
    destination.mkdir(parents=True, exist_ok=True)
    entries = sorted(source.iterdir())
    databases = set()
    for entry in entries:
        if entry.is_file():
            with entry.open('rb') as handle:
                if handle.read(16) == b'SQLite format 3\x00':
                    databases.add(entry.name)
    for entry in entries:
        if entry.is_symlink():
            raise ValueError('Symlinks are not supported: ' + str(entry))
        target = destination / entry.name
        if entry.is_dir():
            copy_storage(entry, target)
        elif entry.name in databases:
            # A temporary clone avoids creating SQLite sidecars in the source.
            with tempfile.TemporaryDirectory(prefix='rmm-migration-sqlite-') as temp:
                clone = Path(temp) / entry.name
                shutil.copy2(entry, clone)
                for suffix in ('-wal', '-journal'):
                    sidecar = entry.with_name(entry.name + suffix)
                    if sidecar.exists():
                        shutil.copy2(sidecar, clone.with_name(clone.name + suffix))
                origin = sqlite3.connect(clone)
                output = sqlite3.connect(target)
                try:
                    if origin.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Invalid SQLite database: ' + str(entry))
                    origin.backup(output)
                finally:
                    output.close()
                    origin.close()
                target.chmod(0o600)
        elif any(entry.name == name + suffix for name in databases for suffix in ('-wal', '-shm', '-journal')):
            continue
        elif entry.is_file():
            shutil.copy2(entry, target)
            target.chmod(0o600)
        else:
            raise ValueError('Only regular files and directories are supported: ' + str(entry))


def import_history(archive, store, workspace_id):
    """Load archived sessions, transcripts, chats and download indexes into workspace SQLite."""
    records = read_object(archive / 'sessions.json') if (archive / 'sessions.json').exists() else {}
    history = archive / 'history'
    if history.exists():
        for folder in history.iterdir():
            if not folder.is_dir():
                raise ValueError('Invalid history entry: ' + str(folder))
            records[folder.name] = {**records.get(folder.name, {}),
                                    **(read_object(folder / 'meta.json') if (folder / 'meta.json').exists() else {})}
    stamp = datetime.now(timezone.utc).isoformat()
    for sid, record in records.items():
        if not isinstance(record, dict) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', sid):
            raise ValueError('Invalid session record')
        if record.get('workspace_id') not in (None, '', workspace_id):
            raise ValueError('Session belongs to a different workspace: ' + sid)
        record.update(id=sid, session_id=sid, workspace_id=workspace_id, active=False)
        store._db.execute('INSERT INTO sessions(session_id,payload,active,updated_at,ended_at) VALUES(?,?,0,?,?)',
                          (sid, json.dumps(record), stamp, record.get('ended_at') or stamp))
        folder = history / sid
        events = folder / 'events.jsonl'
        if events.exists():
            for line in events.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict) or type(event.get('id')) is not int or event['id'] <= 0:
                    raise ValueError('Invalid history event: ' + str(events))
                event.pop('workspace_payload', None)
                # Old links are retained in the archive; workspace URLs are scoped locally.
                artifact = event.get('artifact')
                event['artifact_url'] = None
                if isinstance(artifact, str) and Path(artifact).name == artifact:
                    for kind in ('downloads', 'screenshots', 'keylogs'):
                        if (store.artifact_dir / kind / artifact).is_file():
                            event['artifact_url'] = '/api/v1/artifacts/' + kind + '/' + artifact
                            break
                store._db.execute('INSERT INTO events(session_id,event_id,payload) VALUES(?,?,?)',
                                  (sid, event['id'], json.dumps(event)))
        chat = folder / 'ai_chat.json'
        if chat.exists():
            messages = read_object(chat).get('messages')
            if not isinstance(messages, list) or any(not isinstance(row, dict) for row in messages):
                raise ValueError('Invalid chat: ' + str(chat))
            store._db.execute('INSERT INTO ai_chats(session_id,payload) VALUES(?,?)', (sid, json.dumps(messages)))
        prefix = hashlib.sha256(sid.encode()).hexdigest()[:12] + '_'
        for artifact in store.download_dir.iterdir():
            if artifact.is_file() and artifact.name.startswith(prefix):
                store._db.execute('INSERT INTO downloads VALUES(?,?,?,?,?)',
                                  (artifact.name, sid, artifact.name[len(prefix):], artifact.stat().st_size, stamp))
    store._db.commit()
    return len(records)


def migrate(source, destination, workspace_id):
    """Create a fresh copy-only workspace; remove only our new destination on failure."""
    source, destination = Path(source).expanduser(), Path(destination).expanduser()
    if source.is_symlink() or destination.is_symlink():
        raise ValueError('Migration roots must not be symlinks')
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir() or not valid_workspace_id(workspace_id):
        raise ValueError('A source directory and valid workspace ID are required')
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must be separate trees')
    prepared = None
    prepared_values = {}
    if destination.exists():
        if not destination.is_dir() or {p.name for p in destination.iterdir()} != {'settings.json'}:
            raise ValueError('Destination must not exist or contain only prepared settings.json')
        settings_path = destination / 'settings.json'
        if settings_path.is_symlink() or not settings_path.is_file():
            raise ValueError('Prepared settings must be a regular file')
        prepared_values = SettingsStore(settings_path, 'workspace', environment={}).saved_at_start
        if prepared_values.get('RMM_WORKSPACE_ID') != workspace_id:
            raise ValueError('Prepared settings workspace ID does not match')
        prepared = (settings_path.read_bytes(), settings_path.stat().st_mode & 0o777)
    for root, dirs, files in os.walk(source):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                raise ValueError('Source contains a symlink: ' + str(Path(root) / name))
    if prepared is None:
        destination.mkdir(parents=True, mode=0o700)
    store = None
    try:
        archive = destination / 'legacy-server'
        copy_storage(source, archive)
        store = WorkspaceStore(destination, workspace_id)
        for kind in ('downloads', 'screenshots', 'keylogs'):
            if (archive / kind).exists():
                copy_storage(archive / kind, store.artifact_dir / kind)
        if (archive / 'assets').exists():
            copy_storage(archive / 'assets', destination / 'assets')
        count = import_history(archive, store, workspace_id)
        store.close()
        store = None
        settings = archive / 'settings.json'
        saved = SettingsStore(settings, 'server', environment={}).saved_at_start if settings.exists() else {}
        transferred = {}
        for key, value in saved.items():
            try:
                definition(key, 'workspace')
            except SettingsError:
                continue
            transferred[key] = value
        transferred['RMM_WORKSPACE_ID'] = workspace_id
        transferred.update(prepared_values)
        (destination / 'settings.json').write_text(json.dumps({'schema': SCHEMA, 'scope': 'workspace', 'settings': transferred}, indent=2) + '\n')
        (destination / 'settings.json').chmod(0o600)
        manifest = {'source': str(source), 'workspace_id': workspace_id, 'sessions': count,
                    'copied_at': datetime.now(timezone.utc).isoformat(),
                    'archive_only': ['bloodhound', 'ai-jobs.sqlite3', 'embeddings.db', 'ui-state databases', 'server-only settings'],
                    'note': 'Source untouched. Review archived state and configure workspace connection before starting.'}
        (destination / 'migration.json').write_text(json.dumps(manifest, indent=2) + '\n')
        (destination / 'migration.json').chmod(0o600)
        return manifest
    except BaseException:
        if store is not None:
            store.close()
        if prepared is None:
            shutil.rmtree(destination)
        else:
            for entry in destination.iterdir():
                if entry.name != 'settings.json':
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            (destination / 'settings.json').write_bytes(prepared[0])
            (destination / 'settings.json').chmod(prepared[1])
        raise


def main():
    """Require an explicit stopped-service acknowledgement before copying legacy storage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--services-stopped', action='store_true', help='Confirm source services are stopped for a consistent snapshot')
    args = parser.parse_args()
    if not args.services_stopped:
        parser.error('Stop source services and supply --services-stopped before migration')
    try:
        result = migrate(args.source, args.destination, args.workspace_id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
