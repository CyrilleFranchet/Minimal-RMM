"""Lossless local snapshot import; fixtures never access upstream services."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.import_bloodhound_library import build_snapshot, parse_entry, write_snapshot


class LibraryImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'source'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        (self.root / 'queries').mkdir()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def write_query(self, name='first.yml', guid='0596dba7-9180-49a0-aa54-00243240037c'):
        raw = ('# Preserve this source comment\r\nname: Example query\r\nguid: ' + guid + '\r\n'
               'platforms: Active Directory\r\ncategory: Fixture\r\ndescription: null\r\n'
               'query: |-\r\n  MATCH (n) RETURN n LIMIT 1\r\nrevision: 1\r\n'
               'resources: [https://example.invalid/reference]\r\nfuture_field: retained\r\n').encode()
        (self.root / 'queries' / name).write_bytes(raw)
        return raw

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture')
        return self.git('rev-parse', 'HEAD')

    def test_complete_lossless_and_deterministic(self):
        raw = self.write_query()
        self.write_query('second.yml', '1596dba7-9180-49a0-aa54-00243240037c')
        revision = self.commit()
        snapshot = build_snapshot(self.root, revision)
        self.assertEqual(snapshot['entry_count'], 2)
        first = snapshot['entries'][0]
        self.assertEqual(first['raw_yaml'].encode(), raw)
        self.assertEqual(first['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(first['platforms'], ['Active Directory'])
        self.assertEqual(first['description'], '')
        self.assertIsNone(first['upstream']['description'])
        self.assertEqual(first['upstream']['future_field'], 'retained')
        self.assertEqual(first['query'], first['upstream']['query'])
        self.assertIn(revision, first['source_url'])
        output = Path(self.directory.name) / 'snapshot.json'
        write_snapshot(snapshot, output)
        original = output.read_bytes()
        write_snapshot(build_snapshot(self.root, revision), output)
        self.assertEqual(output.read_bytes(), original)

    def test_wrong_revision_and_dirty_inventory_rejected(self):
        self.write_query()
        revision = self.commit()
        with self.assertRaisesRegex(ValueError, 'does not match'):
            build_snapshot(self.root, '0' * 40)
        (self.root / 'queries' / 'untracked.yml').write_text('name: not silently omitted')
        with self.assertRaisesRegex(ValueError, 'must be clean'):
            build_snapshot(self.root, revision)

    def test_duplicate_guid_fails_instead_of_dropping_entry(self):
        self.write_query()
        self.write_query('second.yml')
        revision = self.commit()
        with self.assertRaisesRegex(ValueError, 'duplicate query GUID'):
            build_snapshot(self.root, revision)

    def test_invalid_and_ambiguous_yaml_rejected(self):
        with self.assertRaises(ValueError):
            parse_entry('queries/broken.yml', b'name: [unterminated', 'a' * 40)
        with self.assertRaisesRegex(ValueError, 'duplicate YAML key'):
            parse_entry('queries/broken.yml', b'name: first\nname: second\n', 'a' * 40)
        with self.assertRaisesRegex(ValueError, 'missing or invalid'):
            parse_entry('queries/incomplete.yml', b'name: missing required metadata\n', 'a' * 40)

    def test_committed_snapshot_has_full_inventory_and_lossless_checksums(self):
        path = Path(__file__).resolve().parents[1] / 'bloodhound-queries' / 'upstream-library.json'
        snapshot = json.loads(path.read_text())
        self.assertEqual(snapshot['revision'], 'b1fee8f5715f8fb3358e187ca3f1bddbfd125f93')
        self.assertEqual(snapshot['entry_count'], 236)
        self.assertEqual(len(snapshot['entries']), snapshot['entry_count'])
        self.assertEqual(len({entry['id'] for entry in snapshot['entries']}), snapshot['entry_count'])
        for entry in snapshot['entries']:
            self.assertEqual(entry['sha256'], hashlib.sha256(entry['raw_yaml'].encode()).hexdigest())
            self.assertEqual(entry['query'], entry['upstream']['query'])
            self.assertEqual(entry['name'], entry['upstream']['name'])


if __name__ == '__main__':
    unittest.main()
