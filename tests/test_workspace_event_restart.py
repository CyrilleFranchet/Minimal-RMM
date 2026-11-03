"""Restarted central streams cannot hide or overwrite migrated workspace events."""
import base64
import tempfile
import unittest
from unittest.mock import Mock, patch

from rmm_workspace import WorkspaceEventBroker
from workspace_service import WorkspaceService, WorkspaceStore


class WorkspaceRestartTests(unittest.TestCase):
    def test_final_upload_chunk_replays_after_interrupted_receipt_without_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory, 'alice')
            def chunk(identifier, offset, data, eof):
                return {'id': identifier, 'type': 'file_upload', 'workspace_payload': {
                    'kind': 'file_upload', 'remote_path': 'C:/report.txt', 'data': {
                        'filename': 'report.txt', 'upload_id': 'upload-1', 'offset': offset,
                        'content': base64.b64encode(data).decode(), 'eof': eof}}}
            first = chunk(1, 0, b'first-', False)
            final = chunk(2, 6, b'last', True)
            try:
                store.sync_sessions([{'id': 'session'}])
                store.apply_stream_event('session', first, 'epoch', 1)
                original = store.apply_event
                def interrupted(*args, **kwargs):
                    original(*args, **kwargs)
                    raise OSError('Interrupted before receipt commit')
                with patch.object(store, 'apply_event', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        store.apply_stream_event('session', final, 'epoch', 2)
                self.assertEqual(store.get_events('session'), [])
                self.assertEqual(store.list_downloads('session'), [])
                store.close()
                store = WorkspaceStore(directory, 'alice')
                store.apply_stream_event('session', final, 'epoch', 2)
                store.apply_stream_event('session', final, 'epoch', 2)
                rows = store.get_events('session')
                self.assertEqual(len(rows), 1)
                self.assertEqual(store.artifact_path('downloads', rows[0]['artifact']).read_bytes(), b'first-last')
                self.assertEqual(list(store.parts_dir.glob('*.part')), [])
            finally:
                store.close()

    def test_missing_upload_prefix_does_not_acknowledge_or_publish_corrupt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory, 'alice')
            try:
                event = {'id': 1, 'workspace_payload': {'kind': 'file_upload', 'data': {
                    'filename': 'report.txt', 'offset': 100, 'eof': True,
                    'content': base64.b64encode(b'last').decode()}}}
                with self.assertRaisesRegex(ValueError, 'preceding bytes'):
                    store.apply_stream_event('session', event, 'epoch', 1)
                self.assertEqual(store._db.execute('SELECT COUNT(*) FROM event_receipts').fetchone()[0], 0)
                self.assertEqual(list(store.download_dir.iterdir()), [])
            finally:
                store.close()

    def test_broker_epoch_restarts_cursor_without_losing_new_queue(self):
        old, new = WorkspaceEventBroker(), WorkspaceEventBroker()
        self.assertFalse(new.read('alice', epoch='')['reset_required'])
        new.publish('alice', 'session', {'id': 1, 'body': 'new'})
        reply = new.read('alice', since=900, epoch=old.epoch)
        self.assertTrue(reply['reset_required'])
        self.assertEqual(reply['events'][0]['event']['body'], 'new')
        self.assertEqual(reply['next_cursor'], 1)
        self.assertEqual(new.read('alice', since=1, epoch=new.epoch)['events'], [])

    def test_sync_after_restart_preserves_history_and_deduplicates_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory, 'alice')
            self.addCleanup(store.close)
            store.sync_sessions([{'id': 'session'}])
            store.apply_event('session', {'id': 50, 'body': 'migrated'})
            store.set_event_checkpoint('previous', 900)
            broker = WorkspaceEventBroker()
            broker.publish('alice', 'session', {'id': 1, 'body': 'after restart'})
            service = WorkspaceService.__new__(WorkspaceService)
            service.store = store
            service._running = True
            service.auto_claim = False
            service.log = Mock()
            service._sync_pivot_state = Mock()
            service.upstream = Mock()

            def request(method, path, *args, **kwargs):
                if path == '/api/v1/workspace/sessions':
                    return 200, {'sessions': [{'id': 'session'}]}, 'application/json'
                from urllib.parse import parse_qs, urlsplit
                qs = parse_qs(urlsplit(path).query, keep_blank_values=True)
                return 200, broker.read('alice', int(qs['since'][0]), epoch=qs['epoch'][0]), 'application/json'

            service.upstream.request.side_effect = request
            service.sync_once()
            self.assertEqual(store.get_cursor(), 1)
            self.assertEqual(store.get_event_epoch(), broker.epoch)
            # A lost checkpoint after durable processing causes an envelope replay.
            store.set_event_checkpoint('previous', 900)
            service.sync_once()
            rows = store.get_events('session', since=0, limit=100)
            self.assertEqual([row['body'] for row in rows], ['migrated', 'after restart'])
            self.assertEqual([row['id'] for row in rows], [50, 51])
            self.assertEqual(rows[1]['source_event_id'], 1)
