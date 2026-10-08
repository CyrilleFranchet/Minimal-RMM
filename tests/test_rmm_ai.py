"""Focused unit coverage for provider validation and provider protocol conversion."""

import asyncio
import unittest
from unittest.mock import patch

import rmm_ai


class AiProviderTests(unittest.TestCase):
    """Validate provider-specific HTTP shapes without live provider credentials."""

    def test_validate_anthropic_provider_returns_model_ids(self):
        with patch(
            "rmm_ai._json_request",
            return_value={"data": [{"id": "claude-test"}, {"id": "claude-small"}]},
        ) as request:
            result = rmm_ai.validate_ai_provider("anthropic", "test-key")

        self.assertEqual(
            result,
            {
                "ok": True,
                "provider": "anthropic",
                "models": ["claude-small", "claude-test"],
            },
        )
        request.assert_called_once_with(
            rmm_ai.ANTHROPIC_MODELS_URL,
            {
                "x-api-key": "test-key",
                "anthropic-version": rmm_ai.ANTHROPIC_VERSION,
            },
            timeout=20,
        )

    def test_validate_mistral_provider_accepts_array_response(self):
        with patch(
            "rmm_ai._json_request",
            return_value=[
                {
                    "id": "mistral-small",
                    "capabilities": {"completion_chat": True, "function_calling": True},
                },
                {
                    "id": "mistral-embed",
                    "capabilities": {"completion_chat": False, "function_calling": False},
                },
                {
                    "id": "mistral-large",
                    "capabilities": {"completion_chat": True, "function_calling": True},
                },
            ],
        ):
            result = rmm_ai.validate_ai_provider("mistral", "test-key")

        self.assertEqual(result["models"], ["mistral-large", "mistral-small"])

    def test_validation_rejects_unsupported_provider_and_empty_key(self):
        with self.assertRaisesRegex(ValueError, "unsupported_provider"):
            rmm_ai.validate_ai_provider("unknown", "key")
        with self.assertRaisesRegex(ValueError, "missing_api_key"):
            rmm_ai.validate_ai_provider("openai", "")

    def test_anthropic_tool_loop_returns_tool_result_block(self):
        responses = [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "list_sessions",
                        "input": {},
                    }
                ]
            },
            {"content": [{"type": "text", "text": "One session is online."}]},
        ]
        calls = []

        def request(url, headers, body, timeout=180):
            calls.append((url, headers, body))
            return responses.pop(0)

        async def tool(name, arguments):
            self.assertEqual((name, arguments), ("list_sessions", {}))
            return "[{\"id\": \"abc\"}]"

        with patch("rmm_ai._json_request", side_effect=request):
            result = asyncio.run(
                rmm_ai._run_provider_loop(
                    provider="anthropic",
                    api_key="test-key",
                    messages=[{"role": "user", "content": "List sessions"}],
                    model="claude-test",
                    system="Use RMM tools.",
                    tools=rmm_ai.OPENAI_TOOLS,
                    call_tool=tool,
                    max_rounds=2,
                    via="direct",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "One session is online.")
        self.assertEqual(calls[0][1]["x-api-key"], "test-key")
        self.assertEqual(calls[0][2]["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(calls[1][2]["messages"][-1]["content"][0]["type"], "tool_result")

    def test_mistral_uses_chat_completions_shape(self):
        with patch(
            "rmm_ai._json_request",
            return_value={"choices": [{"message": {"content": "Ready."}}], "usage": {}},
        ) as request:
            result = asyncio.run(
                rmm_ai._run_provider_loop(
                    provider="mistral",
                    api_key="test-key",
                    messages=[{"role": "user", "content": "Hello"}],
                    model="mistral-test",
                    system="Use RMM tools.",
                    tools=[],
                    call_tool=lambda _name, _arguments: None,
                    max_rounds=1,
                    via="direct",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(request.call_args.args[0], rmm_ai.MISTRAL_CHAT_URL)
        self.assertEqual(request.call_args.args[2]["tool_choice"], "auto")

    def test_mistral_multi_message_completion_returns_text(self):
        response = {
            "choices": [
                {
                    "messages": [
                        {"content": [{"type": "text", "text": "First "}]},
                        {"content": [{"type": "text", "text": "response."}]},
                    ]
                }
            ]
        }
        with patch("rmm_ai._json_request", return_value=response):
            result = asyncio.run(
                rmm_ai._run_provider_loop(
                    provider="mistral",
                    api_key="test-key",
                    messages=[{"role": "user", "content": "Hello"}],
                    model="mistral-test",
                    system="Use RMM tools.",
                    tools=[],
                    call_tool=lambda _name, _arguments: None,
                    max_rounds=1,
                    via="direct",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "First response.")

    def test_openai_tool_loop_continues_after_a_function_call(self):
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "list_sessions", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "One session is online."}}]},
        ]

        async def tool(name, arguments):
            self.assertEqual((name, arguments), ("list_sessions", {}))
            return "[{}]"

        with patch("rmm_ai._json_request", side_effect=responses) as request:
            result = asyncio.run(
                rmm_ai._run_provider_loop(
                    provider="openai",
                    api_key="test-key",
                    messages=[{"role": "user", "content": "List sessions"}],
                    model="gpt-test",
                    system="Use RMM tools.",
                    tools=rmm_ai.OPENAI_TOOLS,
                    call_tool=tool,
                    max_rounds=2,
                    via="direct",
                )
            )

        self.assertTrue(result["ok"])
        second_body = request.call_args_list[1].args[2]
        self.assertEqual(second_body["messages"][-1]["tool_call_id"], "call-1")
        self.assertEqual(second_body["messages"][-1]["content"], "[{}]")

    def test_mistral_tool_loop_continues_after_a_function_call(self):
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-2",
                                    "function": {"name": "list_sessions", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "Ready."}}]},
        ]

        async def tool(_name, _arguments):
            return "[{}]"

        with patch("rmm_ai._json_request", side_effect=responses) as request:
            result = asyncio.run(
                rmm_ai._run_provider_loop(
                    provider="mistral",
                    api_key="test-key",
                    messages=[{"role": "user", "content": "List sessions"}],
                    model="mistral-test",
                    system="Use RMM tools.",
                    tools=rmm_ai.OPENAI_TOOLS,
                    call_tool=tool,
                    max_rounds=2,
                    via="direct",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(request.call_args_list[1].args[2]["messages"][-1]["tool_call_id"], "call-2")
        self.assertEqual(request.call_args_list[1].args[2]["messages"][-1]["name"], "list_sessions")

    def test_mistral_multi_message_completion_continues_after_a_function_call(self):
        responses = [
            {
                "choices": [
                    {
                        "messages": [
                            {
                                "tool_calls": [
                                    {
                                        "id": "call-3",
                                        "function": {
                                            "name": "list_sessions",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            {"choices": [{"message": {"content": "Ready."}}]},
        ]

        async def tool(_name, _arguments):
            return "[{}]"

        with patch("rmm_ai._json_request", side_effect=responses) as request:
            result = asyncio.run(
                rmm_ai._run_provider_loop(
                    provider="mistral",
                    api_key="test-key",
                    messages=[{"role": "user", "content": "List sessions"}],
                    model="mistral-test",
                    system="Use RMM tools.",
                    tools=rmm_ai.OPENAI_TOOLS,
                    call_tool=tool,
                    max_rounds=2,
                    via="direct",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(request.call_args_list[1].args[2]["messages"][-1]["tool_call_id"], "call-3")
        self.assertEqual(request.call_args_list[1].args[2]["messages"][-1]["name"], "list_sessions")


if __name__ == "__main__":
    unittest.main()
