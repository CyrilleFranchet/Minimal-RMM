"""Session deletion interrupts server-owned work without contacting a remote host."""
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import server_rmm
from server_rmm import RMMServer, Session


class SessionKillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch.object(server_rmm, 'LOG_DIR', self.directory.name)
        self.patch.start()
        with patch.object(RMMServer, '_archive_orphaned_sessions_on_startup'):
            self.runtime = RMMServer()
        self.runtime.event_hub = Mock()
        self.runtime.socks = Mock()
        self.runtime.tun = Mock()
        self.runtime._finalize_session_history = Mock()
        self.runtime.clear_ai_chat = Mock()
        self.runtime.remove_persisted_session = Mock()
        self.session = Session('test-session', 'host', 'user')
        self.runtime.sessions[self.session.id] = self.session

    def tearDown(self):
        self.runtime.close()
        self.patch.stop()
        self.directory.cleanup()

    def test_kill_releases_waiter_and_interrupts_bound_jobs(self):
        manager = self.runtime.ai_jobs()
        release = threading.Event()
        entered = threading.Event()
        def run(progress):
            entered.set()
            release.wait(3)
            progress({'phase': 'tool'})
            return {'ok': True}
        job, _ = manager.submit(owner='operator', session_id=self.session.id, request_id='kill-test', run=run)
        self.assertTrue(entered.wait(1))
        queued = threading.Event()
        original = self.runtime.set_command
        def queue(*args):
            result = original(*args)
            queued.set()
            return result
        self.runtime.set_command = queue
        results = []
        worker = threading.Thread(target=lambda: results.append(self.runtime.exec_and_wait(self.session.id, 'echo test', 30)))
        worker.start()
        self.assertTrue(queued.wait(1))
        self.assertEqual(self.runtime.exec_and_wait(self.session.id, 'second', 1), (None, 'session_busy'))
        self.assertTrue(self.runtime.kill_session(self.session.id)[0])
        worker.join(1)
        release.set()
        for thread in list(manager._threads):
            thread.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [(None, 'session_killed')])
        self.assertEqual(manager.get(job['id'], 'operator')['status'], 'interrupted')
        self.assertEqual(self.runtime.get_command(self.session.id), ('__EXIT__', 'execute'))
        self.assertFalse(self.runtime.set_command(self.session.id, 'late'))

    def test_operator_and_unrelated_results_do_not_complete_waiter(self):
        session = self.session
        session.wait_event = threading.Event()
        session.wait_command = 'echo wanted'
        self.runtime._history_append_event = Mock()
        self.runtime._history_write_meta = Mock()
        self.runtime.workspace_publish_event = Mock()
        self.runtime._record_event(session, 'operator', 'other action')
        self.runtime._record_event(session, 'output', 'other output', command='echo other')
        self.assertFalse(session.wait_event.is_set())
        self.runtime._record_event(session, 'output', 'wanted', command='echo wanted')
        self.assertTrue(session.wait_event.is_set())
        self.assertEqual(session.wait_result['body'], 'wanted')

    def test_workspace_publication_preserves_event_order(self):
        entered = threading.Event()
        release = threading.Event()
        published = []
        def persist(session, event):
            if event['id'] == 1:
                entered.set()
                release.wait(2)
        self.runtime._history_append_event = persist
        self.runtime._history_write_meta = Mock()
        self.runtime.workspace_publish_event = lambda session, event: published.append(event['id'])
        first = threading.Thread(target=lambda: self.runtime._record_event(self.session, 'output', 'first'))
        second = threading.Thread(target=lambda: self.runtime._record_event(self.session, 'output', 'second'))
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        try:
            second.join(.05)
            self.assertTrue(second.is_alive())
            self.assertEqual(published, [])
        finally:
            release.set()
            first.join(2)
            second.join(2)
        self.assertEqual(published, [1, 2])
