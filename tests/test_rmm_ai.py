"""Focused unit coverage for provider validation and provider protocol conversion."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import rmm_ai


class AiProviderTests(unittest.TestCase):
    """Validate provider-specific HTTP shapes without live provider credentials."""

    def test_truncated_responses_preserve_text_and_count_rounds(self):
        async def call_tool(name, arguments):
            self.fail("No tools should run")

        for provider, response in (
            ("openai", {"choices": [{"message": {"content": "Partial"}, "finish_reason": "length"}]}),
            ("mistral", {"choices": [{"message": {"content": "Partial"}, "finish_reason": "length"}]}),
            ("anthropic", {"content": [{"type": "text", "text": "Partial"}], "stop_reason": "max_tokens"}),
        ):
            with self.subTest(provider=provider), patch("rmm_ai._json_request", return_value=response):
                result = asyncio.run(rmm_ai._run_provider_loop(
                    provider=provider, api_key="test", messages=[], model="test",
                    system="test", tools=[], call_tool=call_tool, max_rounds=2, via="direct",
                ))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "provider_output_truncated")
                self.assertEqual(result["message"], "Partial")
                self.assertEqual(result["rounds_used"], 1)
                now = datetime.now(timezone.utc)
                report = rmm_ai._append_task_timing(result, now, now)
                self.assertIn("Rounds consumed: 1 /", report["message"])
                self.assertIn("Partial", report["message"])

    def test_malformed_tool_text_is_rejected_without_execution(self):
        async def call_tool(name, arguments):
            self.fail("No tools should run")

        for provider, response in (
            ("openai", {"choices": [{"message": {"content": "<tool_call>placeholder</arg_value>"}, "finish_reason": "stop"}]}),
            ("mistral", {"choices": [{"message": {"content": "<tool_call>placeholder</arg_value>"}, "finish_reason": "stop"}]}),
            ("anthropic", {"content": [{"type": "text", "text": "<tool_call>placeholder</arg_value>"}], "stop_reason": "end_turn"}),
        ):
            with self.subTest(provider=provider), patch("rmm_ai._json_request", return_value=response):
                result = asyncio.run(rmm_ai._run_provider_loop(
                    provider=provider, api_key="test", messages=[], model="test",
                    system="test", tools=[], call_tool=call_tool, max_rounds=2, via="direct",
                ))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "malformed_tool_call_text")
                self.assertEqual(result["message"], "<tool_call>placeholder</arg_value>")
                self.assertEqual(result["rounds_used"], 3)
                self.assertEqual(result["malformed_response_retries"], 2)
                now = datetime.now(timezone.utc)
                report = rmm_ai._append_task_timing(result, now, now)
                self.assertIn("Rounds consumed: 3 /", report["message"])
                self.assertIn("<tool_call>placeholder</arg_value>", report["message"])
                self.assertIn("That text was not executed", report["message"])

    def test_malformed_response_retry_recovers(self):
        async def call_tool(name, arguments):
            self.fail("Plain-text calls must never execute")

        for provider in ("openai", "mistral", "anthropic"):
            def response(text):
                if provider == "anthropic":
                    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
                return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}

            with self.subTest(provider=provider), patch("rmm_ai._json_request", side_effect=[
                response("<tool_call>placeholder"), response("Task could not be completed."),
            ]) as request:
                result = asyncio.run(rmm_ai._run_provider_loop(
                    provider=provider, api_key="test", messages=[], model="test",
                    system="test", tools=[], call_tool=call_tool, max_rounds=1, via="direct",
                ))
                self.assertTrue(result["ok"])
                self.assertEqual(result["rounds_used"], 2)
                self.assertEqual(result["malformed_response_retries"], 1)
                self.assertEqual(request.call_count, 2)

    def test_max_tool_rounds_reads_positive_environment_value(self):
        with patch.dict("os.environ", {"RMM_AI_MAX_TOOL_ROUNDS": "48"}):
            self.assertEqual(rmm_ai._max_tool_rounds(), 48)

    def test_max_tool_rounds_rejects_invalid_or_non_positive_values(self):
        with patch.dict("os.environ", {"RMM_AI_MAX_TOOL_ROUNDS": "invalid"}):
            self.assertEqual(rmm_ai._max_tool_rounds(), 32)
        with patch.dict("os.environ", {"RMM_AI_MAX_TOOL_ROUNDS": "0"}):
            self.assertEqual(rmm_ai._max_tool_rounds(), 1)

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

    def test_task_timing_is_appended_to_successful_response(self):
        result = rmm_ai._append_task_timing(
            {"ok": True, "message": "Completed task."},
            datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc),
            datetime(2026, 10, 8, 9, 31, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(
            result["message"],
            "Completed task.\n\n## Task timing\n"
            "- Started (UTC): 2026-10-08T09:30:00Z\n"
            "- Completed (UTC): 2026-10-08T09:31:02Z",
        )

    def test_task_timing_does_not_change_unsuccessful_response(self):
        result = {"ok": False, "error": "provider_error"}
        self.assertIs(
            rmm_ai._append_task_timing(
                result,
                datetime(2026, 10, 8, tzinfo=timezone.utc),
                datetime(2026, 10, 8, tzinfo=timezone.utc),
            ),
            result,
        )

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

    def test_tool_loop_auto_continues_after_round_checkpoint(self):
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-checkpoint",
                                    "function": {"name": "list_sessions", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "Task completed."}}]},
        ]

        async def tool(_name, _arguments):
            return "[{}]"

        with patch("rmm_ai._json_request", side_effect=responses) as request:
            result = asyncio.run(
                rmm_ai._run_provider_loop(
                    provider="openai",
                    api_key="test-key",
                    messages=[{"role": "user", "content": "Complete the task"}],
                    model="gpt-test",
                    system="Use RMM tools.",
                    tools=rmm_ai.OPENAI_TOOLS,
                    call_tool=tool,
                    max_rounds=1,
                    via="direct",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(request.call_args_list), 2)
        self.assertIn("Continue the original task", request.call_args_list[1].args[2]["messages"][-1]["content"])

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


class McpChatErrorLabelingTests(unittest.TestCase):
    """A provider failure after the MCP session connects must not be relabeled
    as an MCP installation/connection problem (e.g. a provider quota error
    must not read as "MCP server failed... install MCP support")."""

    def setUp(self):
        legacy = patch('rmm_ai.ai_engine', return_value='legacy_v2')
        legacy.start()
        self.addCleanup(legacy.stop)

    class DummyMcp:
        openai_tools = []
        server_instructions = None

        async def call_tool(self, name, arguments):
            raise AssertionError("No tools should run")  # pragma: no cover

    def test_provider_error_after_connection_is_not_relabeled(self):
        async def fake_run(rmm_base_url, rmm_token, fn, **kwargs):
            return await fn(self.DummyMcp())

        with patch("rmm_mcp_client.run_with_mcp_session",
                   side_effect=lambda *a, **k: asyncio.run(fake_run(*a, **k))), \
             patch("rmm_ai._run_provider_loop", side_effect=RuntimeError("mistral quota exceeded")):
            with self.assertRaises(RuntimeError) as ctx:
                rmm_ai._run_ai_chat_mcp(
                    rmm_base_url="http://local", rmm_token="tok", provider="mistral",
                    api_key="key", messages=[], model="mistral-test",
                    selected_session_id=None, max_rounds=2, workflow='legacy',
                )
        self.assertEqual(str(ctx.exception), "mistral quota exceeded")

    def test_connection_failure_before_chat_keeps_mcp_install_hint(self):
        with patch("rmm_mcp_client.run_with_mcp_session", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError) as ctx:
                rmm_ai._run_ai_chat_mcp(
                    rmm_base_url="http://local", rmm_token="tok", provider="mistral",
                    api_key="key", messages=[], model="mistral-test",
                    selected_session_id=None, max_rounds=2, workflow='legacy',
                )
        self.assertIn("MCP server failed", str(ctx.exception))
        self.assertIn("Install MCP support", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
