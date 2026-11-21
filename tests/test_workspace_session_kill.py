"""Acknowledged kills must leave workspace sessions archived immediately."""

import tempfile
import threading
import unittest
from unittest.mock import Mock

from workspace_pivot import WorkspacePivotManager
from workspace_service import WorkspaceService, WorkspaceStore
from rmm_tun import TunError


class WorkspaceSessionKillTests(unittest.TestCase):
    """Exercise local kill acknowledgement and stale central snapshots."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(self.directory.name, "alice")
        self.session = {"id": "session-1234", "hostname": "HOST"}
        self.store.sync_sessions([self.session])
        self.service = WorkspaceService.__new__(WorkspaceService)
        self.service.store = self.store
        self.service.pivots = Mock()
        self.service._proxy = Mock(return_value=(
            200, {"ok": True, "session_id": self.session["id"]}, "application/json"))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_success_archives_full_id_and_clears_chat_before_returning(self):
        self.store.save_ai_chat(self.session["id"], [{"role": "user", "content": "hello"}])
        result = self.service.delete_json("/api/v1/sessions/sess")
        self.assertEqual(result[0], 200)
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.list_history()[0]["end_reason"], "killed")
        self.assertEqual(self.store.get_ai_chat(self.session["id"])[1], [])
        self.service.pivots.end_session.assert_called_once_with(self.session["id"])
        self.service._proxy.assert_called_once_with("DELETE", "/api/v1/sessions/sess")

    def test_failed_delete_keeps_session_live_and_chat(self):
        self.store.save_ai_chat(self.session["id"], [{"role": "user", "content": "hello"}])
        for status in (0, 403, 404, 500):
            with self.subTest(status=status):
                response = (status, {"error": "failed"}, "application/json")
                self.service._proxy.return_value = response
                self.assertEqual(self.service.delete_json("/api/v1/sessions/session-1234"), response)
                self.assertEqual(self.store.list_sessions(), [self.session])
                self.assertEqual(self.store.list_history(), [])
                self.assertTrue(self.store.get_ai_chat(self.session["id"])[1])
        self.service.pivots.end_session.assert_not_called()

    def test_snapshot_started_before_delete_cannot_reactivate_session(self):
        snapshot_ready = threading.Event()
        release_snapshot = threading.Event()
        errors = []

        def delayed_sync():
            snapshot = [dict(self.session)]
            snapshot_ready.set()
            release_snapshot.wait(5)
            try:
                self.store.sync_sessions(snapshot)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=delayed_sync)
        worker.start()
        try:
            self.assertTrue(snapshot_ready.wait(5))
            self.service.delete_json("/api/v1/sessions/session-1234")
        finally:
            release_snapshot.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual([row["id"] for row in self.store.list_history()], [self.session["id"]])

    def test_tombstone_survives_restart_and_archive_deletion(self):
        self.service.delete_json("/api/v1/sessions/session-1234")
        self.assertTrue(self.store.delete_history(self.session["id"])[0])
        self.store.close()
        self.store = WorkspaceStore(self.directory.name, "alice")
        self.store.sync_sessions([self.session, {"id": "new-session"}])
        self.assertEqual(self.store.list_sessions(), [{"id": "new-session"}])
        self.assertEqual(self.store.list_history(), [])

    def test_restart_does_not_restore_pivots_for_durable_kill(self):
        self.store.mark_session_killed(self.session["id"])
        self.store.close()
        self.store = WorkspaceStore(self.directory.name, "alice")
        self.service.store = self.store
        self.service._running = True
        self.service.upstream = Mock()
        self.service.upstream.request.return_value = (
            200, {"relays": [{"session_id": self.session["id"], "port": 1080}]}, "application/json")
        self.service.pivots.list_socks_status.return_value = []
        self.service.pivots.list_tun_status.return_value = []
        self.service._sync_pivot_state()
        self.service.pivots.start_socks.assert_not_called()
        self.service.pivots.start_tun.assert_not_called()
        self.assertEqual(self.service.pivots.end_session.call_count, 2)

    def test_cleanup_failure_does_not_mask_acknowledged_kill(self):
        manager = self.pivot_manager()
        manager.stop_socks = Mock(side_effect=OSError("private details"))
        manager.stop_tun = Mock()
        self.service.pivots = manager
        self.assertEqual(self.service.delete_json("/api/v1/sessions/session-1234")[0], 200)
        manager.stop_tun.assert_called_once_with(self.session["id"])
        self.assertEqual(self.store.list_sessions(), [])
        self.assertIn("cleanup failed", manager.log_fn.call_args.args[0])
        self.assertNotIn("private details", manager.log_fn.call_args.args[0])
        with self.assertRaisesRegex(RuntimeError, "session has ended"):
            manager.start_socks(self.session["id"])
        with self.assertRaisesRegex(TunError, "session has ended"):
            manager.start_tun(self.session["id"], {})

    def test_snapshot_checked_before_kill_cannot_recreate_listeners(self):
        manager = self.pivot_manager()
        manager.socks.get.return_value = None
        manager.socks.list_relays.return_value = []
        self.service.pivots = manager
        self.service._running = True
        self.service.log = Mock()
        self.service.upstream = Mock()
        self.service.upstream.request.return_value = (
            200, {"relays": [{"session_id": self.session["id"]}]}, "application/json")
        is_killed = self.store.is_session_killed

        def kill_after_check(session_id):
            stale_result = is_killed(session_id)
            self.service.delete_json("/api/v1/sessions/session-1234")
            return stale_result

        self.store.is_session_killed = Mock(side_effect=kill_after_check)
        self.service._sync_pivot_state()
        manager.socks.start.assert_not_called()
        self.assertEqual(manager.tun_relays, {})
        self.assertEqual(self.store.list_sessions(), [])

    @staticmethod
    def pivot_manager():
        """Construct the manager without starting a background data-plane thread."""
        manager = WorkspacePivotManager.__new__(WorkspacePivotManager)
        manager._lock = threading.RLock()
        manager._ended_sessions = set()
        manager._running = True
        manager.socks = Mock()
        manager.tun_relays = {}
        manager.log_fn = Mock()
        return manager

    def test_in_flight_restore_is_stopped_and_cannot_restart_after_kill(self):
        manager = self.pivot_manager()
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def delayed_start(*args, **kwargs):
            entered.set()
            release.wait(5)
            return Mock(bind_host="127.0.0.1", port=1080)

        manager.socks.start.side_effect = delayed_start

        def restore():
            try:
                manager.start_socks(self.session["id"])
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=restore)
        killer = None
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            # end_session waits for the restore's lock, then stops its listener.
            killer = threading.Thread(target=manager.end_session, args=(self.session["id"],))
            killer.start()
        finally:
            release.set()
            worker.join(5)
        if killer is not None:
            killer.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(killer.is_alive())
        self.assertEqual(errors, [])
        manager.socks.stop.assert_called_once_with(self.session["id"])
        with self.assertRaisesRegex(RuntimeError, "session has ended"):
            manager.start_socks(self.session["id"])
        manager.socks.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
