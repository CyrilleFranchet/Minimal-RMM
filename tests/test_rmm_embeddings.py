"""Embedding profile persistence, owner isolation, event selection, and API parity."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer

from rmm_embeddings import EmbeddingError, EmbeddingStore, default_profile, embedding_api, profile_config, plugin_request, result_payload
from rmm_tools import tool_embedding_profiles, OPENAI_TOOLS
from rmm_cli import RmmApiClient, build_parser
from server_rmm import RMMHandler
from workspace_service import WorkspaceService, WorkspaceRequestHandler

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "examples/embedding-profile.json").read_text())


def output_event(event_id=4):
    """Return one embedding output fixture, independent of unrelated events."""
    return {"id": event_id, "type": "output", "body": json.dumps({
        "model": CONFIG["model"]["repo"], "revision": CONFIG["model"]["revision"],
        "results": [{"path": "C:\\Lab\\a.txt", "embedding": [0.6, 0.8]},
                    {"path": "C:\\Lab\\missing.txt", "error": "not found", "embedding": None}],
    })}


class EmbeddingValidationTests(unittest.TestCase):
    """Reject invalid model contracts, credential fields, and oversized requests."""

    def test_defaults_report_missing_or_invalid_repository_configuration(self):
        """A bad packaged preset returns a server error rather than hidden fallback values."""
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "preset.json"
            with patch("rmm_embeddings.DEFAULT_PROFILE_PATH", path):
                for contents in (None, "not JSON", '{"model": {}}'):
                    if contents is not None:
                        path.write_text(contents, encoding="utf-8")
                    self.assertEqual(embedding_api(None, "GET", ["embeddings", "defaults"]),
                                     (500, {"error": "default_embedding_profile_unavailable"}))
                path.write_text(json.dumps(CONFIG), encoding="utf-8")
                self.assertEqual(embedding_api(None, "GET", ["embeddings", "defaults"]),
                                 (200, {"config": profile_config(CONFIG)}))
                self.assertEqual(embedding_api(None, "POST", ["embeddings", "defaults"])[0], 405)

    def test_profile_round_trip_and_agent_environment_preference(self):
        config = profile_config(CONFIG)
        self.assertEqual(config["model"], CONFIG["model"])
        config["auth"]["method"] = "env"
        request = plugin_request(config, ["C:\\Lab\\a.txt"])
        self.assertEqual(json.loads(request["input"])["auth"], {"method": "env"})
        self.assertLessEqual(len(request["input"].encode()), 4096)

    def test_rejects_secrets_unknown_fields_paths_and_nonfinite_values(self):
        mutations = [
            lambda c: c.update(token="secret"),
            lambda c: c["auth"].update(token="secret"),
            lambda c: c.update(workspace_id="bob"),
            lambda c: c["model"].update(onnx_file="../model.onnx"),
            lambda c: c["model"].update(revision="main"),
            lambda c: c["model"].update(input_names=["x", "x"]),
            lambda c: c.update(max_tokens=True),
            lambda c: c["auth"].update(method="token"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                config = copy.deepcopy(CONFIG); mutate(config)
                with self.assertRaises(EmbeddingError):
                    profile_config(config)
        event = output_event(); data = json.loads(event["body"])
        data["results"][0]["embedding"] = [float("nan")]
        event["body"] = json.dumps(data)
        with self.assertRaises(EmbeddingError):
            result_payload(event)
        data["results"][0]["embedding"] = [10 ** 400]
        event["body"] = json.dumps(data)
        with self.assertRaises(EmbeddingError):
            result_payload(event)

    def test_accepts_enumeration_failures_alongside_embeddings(self):
        """Directory enumeration may add per-path error rows to embedded files."""
        event = output_event()
        data = json.loads(event["body"])
        data["results"] = [{"path": f"C:\\Lab\\f{i}.txt", "embedding": [0.6, 0.8]} for i in range(32)]
        data["results"] += [{"path": f"C:\\Lab\\d{i}", "error": "directory contains no supported documents"}
                            for i in range(4)]
        event["body"] = json.dumps(data)
        self.assertEqual(len(result_payload(event)["results"]), 36)
        data["results"].append({"path": "C:\\Lab\\extra.txt", "embedding": [0.6, 0.8]})
        event["body"] = json.dumps(data)
        with self.assertRaisesRegex(EmbeddingError, "too many embedded"):
            result_payload(event)

    def test_copy_documents_profile_and_result_fields(self):
        """Local copying is an explicit profile switch with dedicated result fields."""
        config = copy.deepcopy(CONFIG)
        config["copy_documents"] = True
        request = plugin_request(profile_config(config), ["C:\\Lab\\a.txt"])
        self.assertIs(json.loads(request["input"])["copy_documents"], True)
        event = output_event()
        data = json.loads(event["body"])
        data["copy_directory"] = "C:\\Windows\\Temp\\rmm-embeddings-x"
        data["results"][0]["copied_to"] = "C:\\Windows\\Temp\\rmm-embeddings-x\\sensitive\\a.txt"
        event["body"] = json.dumps(data)
        value = result_payload(event)
        self.assertEqual(value["copy_directory"], "C:\\Windows\\Temp\\rmm-embeddings-x")
        data["results"][1]["copied_to"] = "C:\\Windows\\Temp\\rmm-embeddings-x\\a.txt"
        event["body"] = json.dumps(data)
        with self.assertRaisesRegex(EmbeddingError, "failed document"):
            result_payload(event)
        config["copy_documents"] = "yes"
        with self.assertRaises(EmbeddingError):
            profile_config(config)

    def test_copy_filters_select_labels_and_threshold(self):
        """Copies stay on documents matching the selected label names."""
        config = copy.deepcopy(CONFIG)
        config["copy_documents"] = True
        names = [label["name"] for label in config["labels"]]
        config["copy_labels"] = names
        config["copy_threshold"] = 0.4
        request = plugin_request(profile_config(config), ["C:\\Lab\\a.txt"])
        payload = json.loads(request["input"])
        self.assertEqual(payload["copy_labels"], names)
        self.assertEqual(payload["copy_threshold"], 0.4)

        config = copy.deepcopy(CONFIG)
        config["copy_documents"] = True
        config["copy_labels"] = ["absent"]
        with self.assertRaisesRegex(EmbeddingError, "defined label"):
            profile_config(config)

        config = copy.deepcopy(CONFIG)
        config["copy_documents"] = False
        config["copy_labels"] = names
        with self.assertRaisesRegex(EmbeddingError, "require copy_documents"):
            profile_config(config)

        config = copy.deepcopy(CONFIG)
        config["copy_documents"] = True
        config["copy_threshold"] = 1.5
        with self.assertRaisesRegex(EmbeddingError, "between 0 and 1"):
            profile_config(config)

    def test_utf8_byte_limit_and_empty_batches(self):
        with self.assertRaisesRegex(EmbeddingError, "4096 bytes"):
            plugin_request(CONFIG, ["C:\\" + "é" * 400 + str(i) for i in range(5)])
        for docs in ([], ["a"] * 33, "a"):
            with self.assertRaises(EmbeddingError):
                plugin_request(CONFIG, docs)

    def test_profiles_and_results_survive_reopen_and_delete(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "embeddings.db"
            store = EmbeddingStore(path)
            self.assertEqual(embedding_api(store, "POST", ["embeddings", "profiles", "strategy"], {"config": CONFIG})[0], 200)
            status, saved = embedding_api(store, "POST", ["embeddings", "results"], {"session_ref": "abc", "event_id": 4}, event=lambda ref, eid: ("abc-full", output_event(eid)))
            self.assertEqual(status, 200)
            name = saved["record"]["name"]
            store.close(); store = EmbeddingStore(path)
            self.assertEqual(store.get("profiles", "strategy")["config"]["model"], CONFIG["model"])
            self.assertEqual(store.get("results", name)["event_id"], 4)
            self.assertNotIn("result", store.get("results")[0])
            self.assertTrue(store.delete("results", name)); self.assertFalse(store.delete("results", name))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            for suffix in ("-wal", "-shm"):
                self.assertEqual(Path(str(path) + suffix).stat().st_mode & 0o777, 0o600)
            store.close()


class EmbeddingWorkspaceTests(unittest.TestCase):
    """Exercise real local HTTP routing, authentication, and storage ownership."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = WorkspaceService("http://unused.invalid", "alice", "upstream-secret", self.temp.name, local_token="local-secret")
        self.service.store.sync_sessions([{"id": "alice-session", "hostname": "lab", "agent_type": "go", "capabilities": ["pe"]}])
        self.service.store.apply_event("alice-session", output_event())
        self.service.store.apply_event("alice-session", {"id": 5, "type": "output", "body": "unrelated command"})
        handler = type("EmbeddingTestHandler", (WorkspaceRequestHandler,), {"service": self.service})
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.client = RmmApiClient(f"http://127.0.0.1:{self.http.server_port}", "local-secret")

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        self.service.pivots.stop_all(); self.service.store.close(); self.temp.cleanup()

    def test_local_http_crud_run_save_export_and_isolation(self):
        with patch.object(self.service, "_proxy", return_value=(200, {"session_id": "alice-session", "ok": True}, "application/json")) as proxy:
            self.assertEqual(self.client.embedding_profiles("save", "strategy", CONFIG)[0], 200)
            proxy.assert_not_called()
            self.assertEqual(self.client.run_embeddings("alice-session", "strategy", ["C:\\Lab\\a.txt"])[0], 200)
            self.assertEqual(proxy.call_args.args[1], "/api/v1/sessions/alice-session/pe")
            self.assertEqual(json.loads(proxy.call_args.args[2]["input"])["documents"], ["C:\\Lab\\a.txt"])
            self.assertEqual(self.client.run_embeddings("bob-session", "strategy", ["a"])[0], 404)
            proxy.reset_mock()
            code, payload = self.client.embedding_results("save", session_ref="alice-session", event_id=4)
            self.assertEqual(code, 200)
            name = payload["record"]["name"]
            self.assertEqual(self.client.embedding_results("get", name)[1]["record"]["result"]["results"][0]["embedding"], [0.6, 0.8])
            self.assertEqual(self.client.embedding_results("save", session_ref="alice-session", event_id=5)[0], 400)
            self.assertEqual(self.client.embedding_results("save", session_ref="bob-session", event_id=4)[0], 404)
            self.assertEqual(self.client.embedding_results("save", session_ref="alice-session", event_id=3)[0], 404)
            self.assertEqual(self.client.embedding_results("delete", name)[0], 200)
            self.assertEqual(self.client.embedding_profiles("delete", "strategy")[0], 200)
            proxy.assert_not_called()
        with tempfile.TemporaryDirectory() as bob:
            store = EmbeddingStore(Path(bob) / "workspace.db")
            self.assertEqual(store.get("results"), []); self.assertEqual(store.get("profiles"), [])
            store.close()

    def test_authentication_and_central_workspace_rejection(self):
        self.client.token = "wrong"
        self.assertEqual(self.client.embedding_profiles()[0], 401)
        self.assertEqual(self.client.embedding_profiles("defaults")[0], 401)
        handler = SimpleNamespace(server_instance=SimpleNamespace(storage_mode="workspace", embedding_store=Mock()), _json=Mock())
        for method in ("GET", "POST", "DELETE"):
            RMMHandler._handle_embeddings(handler, method, ["embeddings", "profiles"])
            self.assertEqual(handler._json.call_args.args[0], 409)
        handler.server_instance.embedding_store.assert_not_called()
        handler.server_instance.storage_mode = "server"
        handler._workspace_scope = lambda: "alice"
        RMMHandler._handle_embeddings(handler, "GET", ["embeddings", "results"])
        self.assertEqual(handler._json.call_args.args[0], 409)
        handler.server_instance.embedding_store.assert_not_called()

    def test_cli_import_and_export(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "export.json"
            args = parser.parse_args(["embeddings", "profiles", "save", "strategy", "--file", str(Path(__file__).resolve().parents[1] / "examples/embedding-profile.json")])
            with patch("rmm_cli.print_json"):
                args.func(self.client, {}, args)
            args = parser.parse_args(["embeddings", "profiles", "get", "strategy", "--output", str(output)])
            args.func(self.client, {}, args)
            self.assertEqual(json.loads(output.read_text())["record"]["config"]["model"], CONFIG["model"])

    def test_shared_defaults_through_cli_and_mcp_do_not_change_workspace_profiles(self):
        """CLI and MCP read the same repository preset without implicitly saving it."""
        expected = profile_config(CONFIG)
        self.assertEqual(default_profile(), expected)
        code, response = self.client.embedding_profiles("defaults")
        self.assertEqual(code, 200)
        self.assertEqual(response, {"config": expected})
        mcp = json.loads(tool_embedding_profiles(self.client, action="defaults"))
        self.assertTrue(mcp["ok"])
        self.assertEqual(mcp["data"], response)
        tool = next(item["function"] for item in OPENAI_TOOLS if item["function"]["name"] == "embedding_profiles")
        self.assertIn("defaults", tool["parameters"]["properties"]["action"]["enum"])
        self.assertEqual(self.client.embedding_profiles()[1]["profiles"], [])
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "defaults.json"
            args = build_parser().parse_args(["embeddings", "profiles", "defaults", "--output", str(output)])
            args.func(self.client, {}, args)
            self.assertEqual(json.loads(output.read_text()), response)
            args = build_parser().parse_args(["embeddings", "profiles", "save", "financial", "--file", str(output)])
            with patch("rmm_cli.print_json"):
                args.func(self.client, {}, args)
            changed = copy.deepcopy(expected)
            changed.update(copy_documents=False, copy_labels=[], copy_threshold=0)
            self.client.embedding_profiles("save", "financial", changed)
            self.client.embedding_profiles("defaults")
            self.assertEqual(self.client.embedding_profiles("get", "financial")[1]["record"]["config"], changed)

    def test_cli_copy_profile_directory_run_and_result_round_trip(self):
        """CLI JSON workflows preserve copy filters, UNC paths, and error rows."""
        parser = build_parser()
        config = copy.deepcopy(CONFIG)
        config.update(copy_documents=True, copy_labels=[config["labels"][0]["name"]], copy_threshold=0.4)
        with tempfile.TemporaryDirectory() as root, patch("rmm_cli.print_json"):
            source = Path(root) / "profile.json"
            exported = Path(root) / "export.json"
            for value in (config, {"config": config}, {"record": {"config": config}}):
                source.write_text(json.dumps(value), encoding="utf-8")
                args = parser.parse_args(["embeddings", "profiles", "save", "copy-profile", "--file", str(source)])
                args.func(self.client, {}, args)
                args = parser.parse_args(["embeddings", "profiles", "get", "copy-profile", "--output", str(exported)])
                args.func(self.client, {}, args)
                self.assertEqual(json.loads(exported.read_text())["record"]["config"], profile_config(config))

            args = parser.parse_args(["embeddings", "profiles", "save", "reimported", "--file", str(exported)])
            args.func(self.client, {}, args)
            paths = [r"C:\Lab Docs", r"\\fileserver\share\Lab Docs"]
            with patch.object(self.service, "_proxy", return_value=(200, {"session_id": "alice-session", "ok": True}, "application/json")) as proxy:
                args = parser.parse_args(["embeddings", "run", "reimported", "--session", "alice-session",
                                         "--document", paths[0], "--document", paths[1]])
                args.func(self.client, {}, args)
                payload = proxy.call_args.args[2]
                self.assertEqual(payload["plugin"], "document-embeddings-autonomous.dll")
                self.assertEqual(json.loads(payload["input"]), {**profile_config(config), "documents": paths})

            event = output_event(6)
            result = json.loads(event["body"])
            result["copy_directory"] = r"C:\Temp\rmm-embeddings-test"
            result["results"][0].update(copied_to=r"C:\Temp\rmm-embeddings-test\lab\a.txt",
                                        classification={"label": config["labels"][0]["name"], "score": 0.8})
            result["results"][1]["path"] = r"\\fileserver\missing"
            event["body"] = json.dumps(result)
            self.service.store.apply_event("alice-session", event)
            args = parser.parse_args(["embeddings", "results", "save", "--session", "alice-session", "--event", "6"])
            args.func(self.client, {}, args)
            code, listing = self.client.embedding_results()
            self.assertEqual(code, 200)
            name = listing["results"][0]["name"]
            args = parser.parse_args(["embeddings", "results", "get", name, "--output", str(exported)])
            args.func(self.client, {}, args)
            record = json.loads(exported.read_text())["record"]
            self.assertEqual(record["result"], result)
            self.assertEqual(record["event_id"], 6)


class EmbeddingServerTests(unittest.TestCase):
    """Exercise server-mode dispatch, plugin prerequisites, and archived output."""

    def test_server_routes_share_profile_validation_and_exact_result_selection(self):
        with tempfile.TemporaryDirectory() as root:
            store = EmbeddingStore(Path(root) / "embeddings.db")
            self.addCleanup(store.close)
            session = SimpleNamespace(id="server-session", agent_type="go", capabilities=["pe"])
            server = SimpleNamespace(storage_mode="server", embedding_store=lambda: store,
                                     resolve_session=lambda ref: session, set_command=Mock(return_value=True),
                                     record_operator_action=Mock(), get_history_events=Mock(return_value=(session.id, [output_event()])))
            handler = SimpleNamespace(server_instance=server, _json=Mock(), _api_authorized=lambda *args: True,
                                      _api_path_parts=lambda path: path.removeprefix("/api/v1/").split("/"))
            handler._handle_embeddings = RMMHandler._handle_embeddings.__get__(handler)
            RMMHandler._handle_api_get(handler, "/api/v1/embeddings/defaults", {})
            self.assertEqual(handler._json.call_args.args, (200, {"config": profile_config(CONFIG)}))
            RMMHandler._handle_api_post(handler, "/api/v1/embeddings/profiles/test", {"config": CONFIG})
            self.assertEqual(handler._json.call_args.args[0], 200)
            with patch("server_rmm.plugin_path_for_session", return_value=None):
                RMMHandler._handle_api_post(handler, "/api/v1/embeddings/run", {"session_ref": session.id, "profile": "test", "documents": ["a.txt"]})
                self.assertEqual(handler._json.call_args.args[0], 409)
                server.set_command.assert_not_called()
            with patch("server_rmm.plugin_path_for_session", return_value="/fixture/plugin.dll"):
                RMMHandler._handle_api_post(handler, "/api/v1/embeddings/run", {"session_ref": session.id, "profile": "test", "documents": ["a.txt"]})
                self.assertEqual(handler._json.call_args.args[0], 200)
                self.assertTrue(server.set_command.call_args.args[1].startswith("__PE_LOAD__ document-embeddings-autonomous.dll Run "))
            RMMHandler._handle_api_post(handler, "/api/v1/embeddings/results", {"session_ref": session.id, "event_id": 4})
            self.assertEqual(handler._json.call_args.args[0], 200)
            server.get_history_events.assert_called_once_with(session.id, since_id=3, limit=0)
            RMMHandler._handle_api_get(handler, "/api/v1/embeddings/results", {})
            self.assertEqual(len(handler._json.call_args.args[1]["results"]), 1)
            RMMHandler._handle_api_delete(handler, "/api/v1/embeddings/profiles/test")
            self.assertEqual(handler._json.call_args.args[0], 200)
