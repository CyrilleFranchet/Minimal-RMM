"""Async AI route integration without a live provider or browser."""
import tempfile
import json
import urllib.request
from http.server import ThreadingHTTPServer
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm
from rmm_ai_jobs import AIJobManager
from workspace_service import WorkspaceService


class AIJobRouteTests(unittest.TestCase):
    def test_http_disconnect_and_new_connection_recover_finished_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = AIJobManager(tmp + "/jobs.db")
            release = threading.Event()
            entered = threading.Event()

            class Handler(server_rmm.RMMHandler):
                server_instance = SimpleNamespace(ai_jobs=lambda: manager, save_ai_chat=Mock(), session_lock=threading.RLock(), killed_sessions=set())
                def _api_authorized(self, *args):
                    return True
                def _workspace_scope(self):
                    return None
                def _api_token_from_request(self):
                    return "test-token"
                def _path_allowed(self, *args, **kwargs):
                    return True
                def log_message(self, *args):
                    pass

            def runner(**kwargs):
                entered.set()
                release.wait(5)
                return {"ok": True, "message": "finished after disconnect"}

            http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{http.server_port}"
            try:
                with patch("rmm_ai.run_ai_chat", side_effect=runner) as run:
                    body = {"request_id": "http-request-123", "api_key": "provider-secret", "messages": [{"role": "user", "content": "test"}], "skill_documents": []}
                    request = urllib.request.Request(base + "/api/v1/ai/jobs", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 202)
                        job_id = json.load(response)["job"]["id"]
                    # The submitting connection is closed while the worker remains active.
                    self.assertTrue(entered.wait(1))
                    workers = list(manager._threads)
                    release.set()
                    for worker in workers:
                        worker.join(timeout=3)
                    with urllib.request.urlopen(base + "/api/v1/ai/jobs/" + job_id, timeout=2) as response:
                        job = json.load(response)["job"]
                    self.assertEqual(job["status"], "succeeded")
                    self.assertEqual(job["result"]["chat_messages"][-1]["content"], "finished after disconnect")
                    self.assertEqual(run.call_count, 1)
            finally:
                release.set()
                manager.close()
                http.shutdown()
                http.server_close()
                thread.join(timeout=3)

    def handler(self, manager, workspace=None):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=workspace)
        handler._api_token_from_request = Mock(return_value="operator-secret")
        handler._operator_api_base_url = Mock(return_value="http://127.0.0.1:8080")
        handler.headers = {"X-RMM-Workspace-Token": "workspace-secret"}
        handler.server_instance = SimpleNamespace(ai_jobs=lambda: manager, save_ai_chat=Mock(), session_lock=threading.RLock(), killed_sessions=set())
        handler._json = Mock()
        return handler

    def test_submit_returns_before_work_and_polling_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = AIJobManager(tmp + "/jobs.db")
            release = threading.Event()
            entered = threading.Event()
            def runner(**kwargs):
                entered.set()
                release.wait(3)
                return {"ok": True, "message": "done"}
            handler = self.handler(manager, "workspace-a")
            body = {"request_id": "request-123", "api_key": "provider-secret", "messages": [{"role": "user", "content": "test"}], "skill_documents": []}
            try:
                with patch("rmm_ai.run_ai_chat", side_effect=runner) as run:
                    handler._handle_api_post("/api/v1/ai/jobs", body)
                    status, payload = handler._json.call_args.args
                    self.assertEqual(status, 202)
                    self.assertTrue(entered.wait(1))
                    job_id = payload["job"]["id"]
                    self.assertEqual(manager.get(job_id, "workspace:workspace-a")["status"], "running")
                    handler._handle_api_post("/api/v1/ai/jobs", body)
                    self.assertEqual(handler._json.call_args.args[0], 200)
                    self.assertEqual(run.call_count, 1)
                    other = self.handler(manager, "workspace-b")
                    other._handle_api_get("/api/v1/ai/jobs/" + job_id, {})
                    self.assertEqual(other._json.call_args.args[0], 404)
                    handler._handle_api_get("/api/v1/ai/jobs/" + job_id, {})
                    self.assertEqual(handler._json.call_args.args[0], 200)
            finally:
                release.set()
                workers = list(manager._threads)
                manager.close()
                for worker in workers:
                    worker.join(timeout=3)

    def test_rejects_session_outside_scope_before_submission(self):
        manager = Mock()
        handler = self.handler(manager, "workspace-a")
        body = {"request_id": "request-123", "api_key": "key", "messages": [{}], "selected_session_id": "other"}
        with patch.object(server_rmm, "_resolve_api_session", return_value=None):
            handler._handle_api_post("/api/v1/ai/jobs", body)
        self.assertEqual(handler._json.call_args.args[0], 404)
        manager.submit.assert_not_called()

    def test_workspace_forwards_job_filter_and_preserves_status(self):
        service = object.__new__(WorkspaceService)
        service._proxy = Mock(return_value=(200, {"jobs": []}, "application/json"))
        result = service.get_json("/api/v1/ai/jobs", {"session_id": ["sid & other"]})
        self.assertEqual(result[0], 200)
        service._proxy.assert_called_once_with("GET", "/api/v1/ai/jobs?session_id=sid+%26+other")

    def test_submission_canonicalizes_session_alias(self):
        manager = Mock()
        manager.submit.return_value = ({"id": "job"}, True)
        handler = self.handler(manager, "workspace-a")
        body = {"request_id": "request-123", "api_key": "key", "messages": [{}],
                "selected_session_id": "hostname", "skill_documents": []}
        with patch.object(server_rmm, "_resolve_api_session", return_value=SimpleNamespace(id="canonical-id")):
            handler._handle_api_post("/api/v1/ai/jobs", body)
        self.assertEqual(handler._json.call_args.args[0], 202)
        self.assertEqual(manager.submit.call_args.kwargs["session_id"], "canonical-id")

    def test_reset_rejects_active_job_then_dismisses_terminal_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = AIJobManager(tmp + "/jobs.db")
            release = threading.Event()
            job, _ = manager.submit(owner="operator", session_id="sid", request_id="request-123",
                                    run=lambda progress: (release.wait(3), {"ok": True})[1])
            handler = self.handler(manager)
            handler.server_instance.get_ai_chat = Mock(return_value=("sid", []))
            handler.server_instance.clear_ai_chat = Mock(return_value=True)
            try:
                handler._handle_api_delete("/api/v1/sessions/sid/ai/chat")
                self.assertEqual(handler._json.call_args.args[0], 409)
                handler.server_instance.clear_ai_chat.assert_not_called()
                workers = list(manager._threads)
                release.set()
                for worker in workers:
                    worker.join(timeout=3)
                handler._handle_api_delete("/api/v1/sessions/sid/ai/chat")
                self.assertEqual(handler._json.call_args.args[0], 200)
                self.assertTrue(manager.get(job["id"], "operator")["dismissed"])
                handler.server_instance.clear_ai_chat.assert_called_once_with("sid")
            finally:
                release.set()
                manager.close()

    def test_workspace_reset_waits_for_central_success(self):
        service = object.__new__(WorkspaceService)
        service.store = Mock()
        service._proxy = Mock(return_value=(409, {"error": "ai_job_active"}, "application/json"))
        path = "/api/v1/sessions/sid/ai/chat"
        self.assertEqual(service.delete_json(path)[0], 409)
        service.store.clear_ai_chat.assert_not_called()
        service._proxy.return_value = (200, {"ok": True}, "application/json")
        self.assertEqual(service.delete_json(path)[0], 200)
        service.store.clear_ai_chat.assert_called_once_with("sid")


if __name__ == "__main__":
    unittest.main()
