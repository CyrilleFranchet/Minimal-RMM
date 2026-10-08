"""Unit coverage for AI provider API request handling."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server_rmm import API_PREFIX, RMMHandler


class FakeHandler:
    """Minimal handler surface required by RMMHandler._handle_api_post."""

    def __init__(self):
        self.responses = []
        self.server_instance = SimpleNamespace(save_ai_chat=lambda *_args: None)

    def _api_authorized(self):
        return True

    def _api_unauthorized(self):
        self.responses.append((401, {"error": "unauthorized"}))

    def _api_path_parts(self, path):
        return [part for part in path[len(API_PREFIX) + 1 :].split("/") if part]

    def _api_token_from_request(self):
        return "operator-token"

    def _operator_api_base_url(self):
        return "http://127.0.0.1:8080/api/v1"

    def _json(self, status, body):
        self.responses.append((status, body))


class AiProviderRouteTests(unittest.TestCase):
    """Verify validation and chat route parameter forwarding and failures."""

    def call(self, path, body):
        handler = FakeHandler()
        RMMHandler._handle_api_post(handler, path, body)
        return handler.responses[-1]

    def test_validation_forwards_provider_and_key(self):
        with patch("rmm_ai.validate_ai_provider", return_value={"ok": True, "models": ["m"]}) as validate:
            status, body = self.call(
                f"{API_PREFIX}/ai/providers/validate",
                {"provider": "mistral", "api_key": "key"},
            )

        self.assertEqual((status, body), (200, {"ok": True, "models": ["m"]}))
        validate.assert_called_once_with("mistral", "key")

    def test_validation_maps_provider_failures(self):
        with patch("rmm_ai.validate_ai_provider", side_effect=RuntimeError("rejected")):
            status, body = self.call(
                f"{API_PREFIX}/ai/providers/validate",
                {"provider": "anthropic", "api_key": "key"},
            )

        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "provider_validation_failed")

    def test_chat_forwards_new_provider_contract(self):
        response = {"ok": True, "message": "Ready", "tool_calls_made": []}
        with patch("rmm_ai.run_ai_chat", return_value=response) as run:
            status, body = self.call(
                f"{API_PREFIX}/ai/chat",
                {
                    "provider": "anthropic",
                    "api_key": "key",
                    "model": "claude-test",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        self.assertEqual((status, body), (200, response))
        self.assertEqual(run.call_args.kwargs["provider"], "anthropic")
        self.assertEqual(run.call_args.kwargs["api_key"], "key")
        self.assertEqual(run.call_args.kwargs["model"], "claude-test")

    def test_chat_accepts_legacy_openai_key(self):
        with patch("rmm_ai.run_ai_chat", return_value={"ok": True, "message": "Ready"}) as run:
            status, _ = self.call(
                f"{API_PREFIX}/ai/chat",
                {
                    "openai_api_key": "legacy-key",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(run.call_args.kwargs["provider"], "openai")
        self.assertEqual(run.call_args.kwargs["api_key"], "legacy-key")

    def test_chat_rejects_missing_key(self):
        status, body = self.call(
            f"{API_PREFIX}/ai/chat",
            {"provider": "mistral", "messages": [{"role": "user", "content": "Hello"}]},
        )
        self.assertEqual((status, body), (400, {"error": "missing_api_key"}))


if __name__ == "__main__":
    unittest.main()
