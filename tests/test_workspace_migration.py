"""Copy-only migration preserves source evidence and rejects unsafe inputs."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from rmm_settings import SCHEMA, SettingsStore
from rmm_workspace_migration import migrate
from workspace_service import WorkspaceStore


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'legacy'
        self.source.mkdir()
        self.destination = self.root / 'workspace'
        self.sid = 'session-1234'
        self.history = self.source / 'history' / self.sid
        self.history.mkdir(parents=True)
        (self.history / 'meta.json').write_text(json.dumps({'id': self.sid, 'hostname': 'fixture'}))

    def test_copy_import_and_source_unchanged(self):
        name = hashlib.sha256(self.sid.encode()).hexdigest()[:12] + '_report.txt'
        (self.source / 'downloads').mkdir()
        (self.source / 'downloads' / name).write_text('evidence')
        event = {'id': 1, 'body': 'done', 'artifact': name}
        (self.history / 'events.jsonl').write_text(json.dumps(event) + '\n')
        (self.history / 'ai_chat.json').write_text(json.dumps({'messages': [{'role': 'assistant', 'content': 'done', 'tool_calls_made': ['kept']}]}))
        (self.source / 'settings.json').write_text(json.dumps({'schema': SCHEMA, 'scope': 'server', 'settings': {
            'RMM_EXEGOL_MCP_URL': 'http://localhost:8000/mcp', 'RMM_API_TOKEN': 'private-server-token',
            'RMM_PLUGIN_TOKEN_FIXTURE': 'named-plugin-token'}}))
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        result = migrate(self.source, self.destination, 'alice')
        self.assertEqual(result['sessions'], 1)
        self.assertEqual(before, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        store = WorkspaceStore(self.destination, 'alice')
        self.addCleanup(store.close)
        self.assertEqual(store.list_sessions(), [])
        self.assertEqual(store.list_history()[0]['id'], self.sid)
        self.assertIn('/api/v1/artifacts/downloads/', store.get_events(self.sid)[0]['artifact_url'])
        self.assertEqual((store.download_dir / name).read_text(), 'evidence')
        self.assertEqual(store.get_ai_chat(self.sid)[1][0]['tool_calls_made'], ['kept'])
        settings = SettingsStore(self.destination / 'settings.json', 'workspace', environment={}).saved_at_start
        self.assertNotIn('RMM_API_TOKEN', settings)
        self.assertEqual(settings['RMM_WORKSPACE_ID'], 'alice')
        self.assertEqual(settings['RMM_PLUGIN_TOKEN_FIXTURE'], 'named-plugin-token')

    def test_existing_destination_and_nested_destination_refused(self):
        self.destination.mkdir()
        (self.destination / 'keep').write_text('untouched')
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            migrate(self.source, self.destination, 'alice')
        self.assertEqual((self.destination / 'keep').read_text(), 'untouched')
        with self.assertRaisesRegex(ValueError, 'separate trees'):
            migrate(self.source, self.source / 'nested', 'alice')

    def test_symlinks_refused_before_destination_creation(self):
        (self.source / 'link').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            migrate(self.source, self.destination, 'alice')
        self.assertFalse(self.destination.exists())

    def test_malformed_history_rolls_back_new_destination(self):
        (self.history / 'events.jsonl').write_text('{broken')
        with self.assertRaises(ValueError):
            migrate(self.source, self.destination, 'alice')
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.history / 'events.jsonl').read_text(), '{broken')

    def test_sqlite_wal_is_backed_up_without_touching_source(self):
        database = self.source / 'ai-jobs.sqlite3'
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE evidence(value TEXT)')
        connection.execute("INSERT INTO evidence VALUES('committed in WAL')")
        connection.commit()
        raw = database.read_bytes()
        wal = database.with_name(database.name + '-wal').read_bytes()
        migrate(self.source, self.destination, 'alice')
        self.assertEqual(database.read_bytes(), raw)
        self.assertEqual(database.with_name(database.name + '-wal').read_bytes(), wal)
        result = sqlite3.connect(self.destination / 'legacy-server' / database.name)
        self.addCleanup(result.close)
        self.assertEqual(result.execute('SELECT value FROM evidence').fetchone()[0], 'committed in WAL')

    def test_prepared_settings_preserved_and_merged(self):
        self.destination.mkdir()
        settings = {'schema': SCHEMA, 'scope': 'workspace', 'settings': {
            'RMM_WORKSPACE_ID': 'alice', 'RMM_WORKSPACE_TOKEN': 'prepared-secret'}}
        (self.destination / 'settings.json').write_text(json.dumps(settings))
        (self.source / 'settings.json').write_text(json.dumps({'schema': SCHEMA, 'scope': 'server', 'settings': {
            'RMM_EXEGOL_MCP_URL': 'http://localhost:8000/mcp', 'RMM_WORKSPACE_TOKEN': 'old-token'}}))
        migrate(self.source, self.destination, 'alice')
        values = SettingsStore(self.destination / 'settings.json', 'workspace', environment={}).saved_at_start
        self.assertEqual(values['RMM_WORKSPACE_TOKEN'], 'prepared-secret')
        self.assertEqual(values['RMM_EXEGOL_MCP_URL'], 'http://localhost:8000/mcp')

    def test_crashed_sqlite_rollback_journal_is_recovered_without_touching_source(self):
        database = self.source / 'crashed.sqlite3'
        subprocess.run([sys.executable, '-c', '''
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=DELETE')
db.execute('PRAGMA cache_size=1')
db.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)')
db.executemany('INSERT INTO evidence VALUES(?,?)', [(i, 'committed' * 512) for i in range(100)])
db.commit()
db.execute("UPDATE evidence SET value=?", ('uncommitted' * 512,))
os._exit(0)
''', str(database)], check=True)
        journal = database.with_name(database.name + '-journal')
        self.assertTrue(journal.exists())
        before = (database.read_bytes(), journal.read_bytes())
        migrate(self.source, self.destination, 'alice')
        self.assertEqual(before, (database.read_bytes(), journal.read_bytes()))
        with sqlite3.connect(self.destination / 'legacy-server' / database.name) as imported:
            self.assertEqual(imported.execute('SELECT value FROM evidence WHERE id=0').fetchone()[0], 'committed' * 512)

    def test_failed_import_preserves_prepared_settings_exactly(self):
        self.destination.mkdir()
        raw = json.dumps({'schema': SCHEMA, 'scope': 'workspace', 'settings': {'RMM_WORKSPACE_ID': 'alice'}})
        (self.destination / 'settings.json').write_text(raw)
        (self.history / 'events.jsonl').write_text('malformed')
        with self.assertRaises(ValueError):
            migrate(self.source, self.destination, 'alice')
        self.assertEqual([p.name for p in self.destination.iterdir()], ['settings.json'])
        self.assertEqual((self.destination / 'settings.json').read_text(), raw)
