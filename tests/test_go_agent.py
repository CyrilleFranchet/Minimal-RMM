import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm


class GoAgentTests(unittest.TestCase):
    def test_source_manifest_is_repository_scoped(self):
        files = server_rmm.go_agent_source_files()
        names = {item["filename"] for item in files}
        self.assertEqual(
            names,
            {
                "go.mod",
                "main.go",
                "debug_log.go",
                "debug_disabled.go",
                "debug_crash_go123.go",
                "debug_crash_legacy.go",
                "debug_dir_windows.go",
                "debug_dir_other.go",
                "pe_loader_windows.go",
                "plugin_format.go",
                "plugin_library_windows.go",
                "platform_other.go",
                "platform_windows.go",
                "spawn.go",
                "runas_output.go",
                "make_token.go",
                "make_token_windows.go",
                "lateral.go",
                "lateral_results.go",
                "lateral_windows.go",
                "service_windows.go",
                "uid.go",
                "wmi_windows.go",
                "wsman_windows.go",
                "spawnas_windows.go",
                "spawnas_other.go",
                "stage1/main.go",
            },
        )
        self.assertTrue(all(".." not in item["filename"] for item in files))

    def test_build_rejects_unallowlisted_target(self):
        for target in ("darwin", "linux"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                server_rmm.build_go_agent(target, "amd64")
        self.assertEqual(server_rmm.GO_AGENT_TARGETS, {("windows", "amd64"), ("windows", "arm64")})

    def test_build_rejects_wrong_secret_before_starting_compiler(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler.server_instance = SimpleNamespace(beacon_public_url="https://current.trycloudflare.com", builds=Mock())
        handler._json = Mock()
        with patch.object(server_rmm, "BEACON_SECRET", "active-secret"), patch.object(server_rmm, "build_go_agent") as build:
            handler._handle_api_post("/api/v1/agent/go/build", {"goos": "windows", "goarch": "amd64", "config": {"beacon_secret": "outdated-secret"}})
            build.assert_not_called()
            self.assertEqual(handler._json.call_args.args[0], 400)
            response = str(handler._json.call_args.args[1])
            self.assertIn("active RMM_BEACON_SECRET", response)
            self.assertNotIn("active-secret", response)
            self.assertNotIn("outdated-secret", response)

    def test_connection_validation_accepts_current_and_custom_routes(self):
        with patch.object(server_rmm, "BEACON_SECRET", "active-secret"):
            for url in ("https://current.trycloudflare.com", "https://custom.example.com"):
                server_rmm.validate_agent_connection({"base_url": url, "beacon_secret": "active-secret"}, "https://current.trycloudflare.com")
            server_rmm.validate_agent_connection({}, "https://current.trycloudflare.com")
            with self.assertRaisesRegex(ValueError, "Quick Tunnel URL is stale"):
                server_rmm.validate_agent_connection({"base_url": "https://old.trycloudflare.com", "beacon_secret": "active-secret"}, "https://current.trycloudflare.com")


if __name__ == "__main__":
    unittest.main()
