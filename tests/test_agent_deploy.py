import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_rmm


TEMPLATE = (
    "$u = 'REPLACE-WITH-YOUR-CLOUDFLARED-URL'\n"
    "$beaconSecret = ''\n"
    "$sessionId = [System.Guid]::NewGuid().ToString()\n"
    "\n"
    "$baseSleepSeconds = 60\n"
    "$jitterPercent = 30\n"
    "\n"
    "$httpProxy = ''\n"
)


class RenderConfiguredClientScriptTests(unittest.TestCase):
    def test_renders_connection_and_timing(self):
        rendered = server_rmm.render_configured_client_script(
            TEMPLATE,
            "https://rmm.example.com/",
            "lab-secret",
            sleep_seconds=45,
            jitter_percent=20,
            http_proxy="http://proxy:8080",
        )
        self.assertIn("$u = 'https://rmm.example.com'", rendered)
        self.assertIn("$beaconSecret = 'lab-secret'", rendered)
        self.assertIn("$baseSleepSeconds = 45", rendered)
        self.assertIn("$jitterPercent = 20", rendered)
        self.assertIn("$httpProxy = 'http://proxy:8080'", rendered)
        self.assertNotIn("REPLACE-WITH-YOUR-CLOUDFLARED-URL", rendered)

    def test_quotes_single_quotes_and_keeps_generated_session_id(self):
        rendered = server_rmm.render_configured_client_script(
            TEMPLATE, "https://rmm.example.com", "s'cret"
        )
        self.assertIn("$beaconSecret = 's''cret'", rendered)
        self.assertIn("$sessionId = [System.Guid]::NewGuid().ToString()", rendered)

    def test_fixed_session_id_is_validated(self):
        rendered = server_rmm.render_configured_client_script(
            TEMPLATE, "https://rmm.example.com", "s", session_id="12345678-1234-1234-1234-123456789012"
        )
        self.assertIn("$sessionId = '12345678-1234-1234-1234-123456789012'", rendered)
        with self.assertRaises(ValueError):
            server_rmm.render_configured_client_script(
                TEMPLATE, "https://rmm.example.com", "s", session_id="../evil"
            )

    def test_rejects_missing_or_multiline_values(self):
        cases = [
            ("", "s"),
            ("https://rmm.example.com", ""),
            ("https://a\nb", "s"),
            ("https://rmm.example.com", "s\nt"),
        ]
        for url, secret in cases:
            with self.assertRaises(ValueError):
                server_rmm.render_configured_client_script(TEMPLATE, url, secret)

    def test_rejects_out_of_range_timing(self):
        with self.assertRaises(ValueError):
            server_rmm.render_configured_client_script(TEMPLATE, "https://a", "s", sleep_seconds=0)
        with self.assertRaises(ValueError):
            server_rmm.render_configured_client_script(TEMPLATE, "https://a", "s", jitter_percent=101)

    def test_rejects_template_drift(self):
        drifted = TEMPLATE.replace("$beaconSecret = ''\n", "$beaconSecret = \"\"\n")
        with self.assertRaises(ValueError):
            server_rmm.render_configured_client_script(drifted, "https://a", "s")


class DeployScriptEndpointTests(unittest.TestCase):
    def _handler(self, public_url="https://rmm.example.com"):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler.server_instance = SimpleNamespace(beacon_public_url=public_url)
        handler._json = Mock()
        return handler

    def test_renders_and_stores_configured_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server_rmm, "AGENT_DEPLOY_DIR", tmp), patch.object(
                server_rmm, "BEACON_SECRET", "active-secret"
            ):
                handler = self._handler()
                handler._handle_api_post(
                    "/api/v1/agent/deploy/script",
                    {"sleep_seconds": 45, "jitter_percent": 20},
                )
            status, payload = handler._json.call_args.args
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertIn("client_rmm-", payload["filename"])
            self.assertTrue(os.path.isfile(payload["path"]))
            self.assertTrue(os.path.realpath(payload["path"]).startswith(os.path.realpath(tmp)))
            self.assertTrue(payload["sha256"])
            self.assertEqual(payload["base_url"], "https://rmm.example.com")
            with open(payload["path"], encoding="utf-8") as handle:
                body = handle.read()
            self.assertIn("$u = 'https://rmm.example.com'", body)
            self.assertIn("$beaconSecret = 'active-secret'", body)
            self.assertIn("$baseSleepSeconds = 45", body)
            self.assertNotIn("active-secret", str(payload))

    def test_requires_active_beacon_url(self):
        with patch.object(server_rmm, "BEACON_SECRET", "active-secret"):
            handler = self._handler(public_url="")
            handler._handle_api_post("/api/v1/agent/deploy/script", {})
            status, payload = handler._json.call_args.args
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "no_active_beacon_url")

    def test_requires_active_beacon_secret(self):
        with patch.object(server_rmm, "BEACON_SECRET", ""):
            handler = self._handler()
            handler._handle_api_post("/api/v1/agent/deploy/script", {})
            status, payload = handler._json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "no_beacon_secret")

    def test_rejects_invalid_timing(self):
        with patch.object(server_rmm, "AGENT_DEPLOY_DIR", tempfile.mkdtemp()), patch.object(
            server_rmm, "BEACON_SECRET", "active-secret"
        ):
            handler = self._handler()
            handler._handle_api_post("/api/v1/agent/deploy/script", {"sleep_seconds": 0})
            status, payload = handler._json.call_args.args
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_timing")


class GoBuildServerConfigTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler._api_authorized = Mock(return_value=True)
        handler._workspace_scope = Mock(return_value=None)
        handler.server_instance = SimpleNamespace(
            beacon_public_url="https://current.example.com", builds=Mock()
        )
        handler._json = Mock()
        return handler

    def test_use_server_config_fills_missing_values(self):
        with patch.object(server_rmm, "BEACON_SECRET", "active-secret"), patch.object(
            server_rmm, "build_go_agent"
        ) as build:
            build.return_value = {"filename": "a.zip"}
            handler = self._handler()
            handler._handle_api_post(
                "/api/v1/agent/go/build",
                {"goos": "windows", "goarch": "amd64", "config": {}, "use_server_config": True},
            )
            status, _ = handler._json.call_args.args
            self.assertEqual(status, 200)
            agent_config = build.call_args.kwargs["agent_config"]
            self.assertEqual(agent_config["base_url"], "https://current.example.com")
            self.assertEqual(agent_config["beacon_secret"], "active-secret")
            self.assertNotIn("active-secret", str(handler._json.call_args.args[1]))

    def test_use_server_config_requires_active_url(self):
        handler = self._handler()
        handler.server_instance = SimpleNamespace(beacon_public_url="", builds=Mock())
        with patch.object(server_rmm, "BEACON_SECRET", "active-secret"):
            handler._handle_api_post(
                "/api/v1/agent/go/build",
                {"goos": "windows", "goarch": "amd64", "use_server_config": True},
            )
            status, payload = handler._json.call_args.args
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "no_active_beacon_url")

    def test_explicit_config_still_used_when_use_server_config_set(self):
        with patch.object(server_rmm, "BEACON_SECRET", "active-secret"), patch.object(
            server_rmm, "build_go_agent"
        ) as build:
            build.return_value = {"filename": "a.zip"}
            handler = self._handler()
            handler._handle_api_post(
                "/api/v1/agent/go/build",
                {
                    "goos": "windows",
                    "goarch": "amd64",
                    "use_server_config": True,
                    "config": {"base_url": "https://custom.example.com"},
                },
            )
            agent_config = build.call_args.kwargs["agent_config"]
            self.assertEqual(agent_config["base_url"], "https://custom.example.com")
            self.assertEqual(agent_config["beacon_secret"], "active-secret")



class DeploymentRegressionTests(unittest.TestCase):
    def test_invalid_config_returns_400_without_building(self):
        for config in (None, False, 42, "invalid", [], [["base_url", "x"]]):
            with self.subTest(config=config), patch.object(server_rmm, "build_go_agent") as build:
                handler = GoBuildServerConfigTests()._handler()
                handler._handle_api_post("/api/v1/agent/go/build", {"config": config})
                self.assertEqual(handler._json.call_args.args[0], 400)
                build.assert_not_called()

    def test_remote_artifact_upload_uses_authenticated_download(self):
        from rmm_cli import RmmApiClient
        import base64
        client = RmmApiClient("https://rmm.example.com", "operator-token")
        response = Mock()
        response.read.return_value = b"artifact bytes"
        with patch("rmm_cli.urllib.request.urlopen") as download, patch.object(client, "request", return_value=(200, {})) as upload:
            download.return_value.__enter__.return_value = response
            status, _ = client.upload_file("session", "artifact:agent-deploy/test.ps1", "C:/test.ps1")
            self.assertEqual(status, 200)
            request = download.call_args.args[0]
            self.assertEqual(request.full_url, "https://rmm.example.com/api/v1/artifacts/agent-deploy/test.ps1")
            self.assertEqual(request.get_header("Authorization"), "Bearer operator-token")
            self.assertEqual(base64.b64decode(upload.call_args.args[2]["content_b64"]), b"artifact bytes")

    def test_invalid_artifact_reference_never_accesses_network(self):
        from rmm_cli import RmmApiClient
        client = RmmApiClient("https://rmm.example.com")
        with patch("rmm_cli.urllib.request.urlopen") as download:
            for reference in ("artifact:../secret", "artifact:agent-deploy/../secret", "artifact:agent-deploy/%2e%2e", "artifact:https://other/file"):
                self.assertEqual(client.upload_file("session", reference, "target")[0], 400)
            download.assert_not_called()

    def test_tool_accepts_reference_without_local_file(self):
        import json
        import rmm_tools
        client = Mock()
        client.upload_file.return_value = (200, {})
        reference = "artifact:agent-deploy/test.ps1"
        with patch.object(rmm_tools, "_resolve_session_id", return_value=("sid", {})):
            result = json.loads(rmm_tools.tool_queue_upload(client, "sid", reference, "C:/test.ps1"))
        client.upload_file.assert_called_once_with("sid", reference, "C:/test.ps1")
        self.assertEqual(result["delivery_state"], "queued")
        self.assertIn("get_events", result["note"])


if __name__ == "__main__":
    unittest.main()
