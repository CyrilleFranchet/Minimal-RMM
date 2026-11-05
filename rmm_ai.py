"""Provider-neutral AI chat loop with RMM tools via MCP or direct calls."""

from __future__ import annotations

from rmm_settings import getenv as settings_getenv

import asyncio
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from rmm_ai_skills import compose_system_prompt, resolve_ai_skills, resolve_ai_skill_policy, resolve_ai_skill_snapshot
from rmm_ai_skill_context import CORE_SKILLS, prepare_skill_context, backend_enabled
from rmm_tools import OPENAI_TOOLS, SYSTEM_PROMPT, execute_tool, make_client, _resolve_session_id
from rmm_ai_concurrency import CONCURRENCY_GUIDANCE, run_tool_batch

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
ANTHROPIC_VERSION = "2023-06-01"


def ai_engine():
    """Select the v3 episode engine, with an explicit legacy rollback option."""
    engine = settings_getenv('RMM_AI_ENGINE', 'v3').strip()
    if engine not in {'v3', 'legacy_v2'}:
        raise ValueError('invalid_ai_engine')
    return engine


def legacy_admission(workflow, execution_scope, engine=None):
    """Require an explicit legacy workflow; never silently ignore v3 UI promises."""
    if (engine if engine is not None else ai_engine()) != 'legacy_v2':
        if workflow == 'legacy':
            return {'ok': False, 'error': 'legacy_engine_not_selected', 'message': 'Select the legacy_v2 engine before using the legacy workflow.'}
        return None
    if workflow != 'legacy':
        return {'ok': False, 'error': 'legacy_workflow_required', 'message': 'The legacy engine cannot enforce v3 workflows. Switch to v3, or explicitly select Legacy v2 (unrestricted tools).'}
    if execution_scope and (not isinstance(execution_scope, dict) or any(execution_scope.values())):
        return {'ok': False, 'error': 'legacy_scope_not_supported', 'message': 'Legacy execution does not enforce v3 scope fields. Clear them or use v3.'}
    return None


def _max_tool_rounds() -> int:
    """Return the configured AI tool-round limit with a safe lower bound."""
    raw = settings_getenv("RMM_AI_MAX_TOOL_ROUNDS", "32").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 32


MAX_TOOL_ROUNDS = _max_tool_rounds()
MAX_TOOL_CONTINUATIONS = 4

SUPPORTED_PROVIDERS = ("openai", "anthropic", "mistral")
DEFAULT_MODELS = {
    "openai": "gpt-5.2",
    "anthropic": "claude-sonnet-4-5-20250929",
    "mistral": "mistral-large-latest",
}


def _json_request(
    url: str,
    headers: dict[str, str],
    body: dict | None = None,
    timeout: float = 180,
) -> dict:
    """Send a JSON HTTP request and turn provider failures into RuntimeError values."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **headers}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            error = json.loads(raw)
        except json.JSONDecodeError:
            error = raw or str(e)
        if isinstance(error, dict):
            error = error.get("error", error)
            if isinstance(error, dict):
                error = error.get("message") or error.get("type") or raw
        raise RuntimeError(str(error or e)) from e
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise RuntimeError(str(e)) from e


def _provider_headers(provider: str, api_key: str) -> dict[str, str]:
    """Build authentication headers for a supported AI provider."""
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {api_key}"}


def _provider_models_url(provider: str) -> str:
    """Return the model-list endpoint for a supported AI provider."""
    return {
        "openai": OPENAI_MODELS_URL,
        "anthropic": ANTHROPIC_MODELS_URL,
        "mistral": MISTRAL_MODELS_URL,
    }[provider]


def validate_ai_provider(provider: str, api_key: str) -> dict:
    """Validate a provider credential and return the models available to that key."""
    provider = (provider or "").strip().lower()
    api_key = (api_key or "").strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported_provider")
    if not api_key:
        raise ValueError("missing_api_key")
    response = _json_request(
        _provider_models_url(provider),
        _provider_headers(provider, api_key),
        timeout=20,
    )
    rows = response if isinstance(response, list) else response.get("data") or []
    def supports_rmm_tools(row: object) -> bool:
        if not isinstance(row, dict) or not row.get("id"):
            return False
        if provider != "mistral":
            return True
        capabilities = row.get("capabilities")
        return isinstance(capabilities, dict) and bool(
            capabilities.get("completion_chat") and capabilities.get("function_calling")
        )

    models = sorted({str(row["id"]).strip() for row in rows if supports_rmm_tools(row)})
    return {"ok": True, "provider": provider, "models": models}


def _selected_session_context(selected_session_id: str | None) -> str:
    if not selected_session_id:
        return ""
    return f"\nThe operator currently has this session selected in the UI: {selected_session_id}"


def _message_with_checkpoint(message):
    """Carry prior execution checkpoints as untrusted evidence, never active policy."""
    text = str(message['content'])
    state = message.get('step_state')
    if isinstance(state, dict):
        text += '\nPrior execution checkpoint (historical evidence; reselect enabled skills):\n' + json.dumps(state)
    return text


def _build_openai_convo(messages: list[dict], system_content: str) -> list[dict]:
    """Convert saved user and assistant messages into Chat Completions messages."""
    convo: list[dict] = [{"role": "system", "content": system_content}]
    for message in messages:
        if message.get("role") in ("user", "assistant") and message.get("content"):
            convo.append({"role": message["role"], "content": _message_with_checkpoint(message)})
    return convo


def _build_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Convert saved user and assistant messages into Anthropic Messages API turns."""
    return [
        {"role": message["role"], "content": _message_with_checkpoint(message)}
        for message in messages
        if message.get("role") in ("user", "assistant") and message.get("content")
    ]


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    """Convert OpenAI function schemas to Anthropic tool schemas."""
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"].get(
                "parameters", {"type": "object", "properties": {}}
            ),
        }
        for tool in tools
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict)
    ]


def _message_text(content: object) -> str:
    """Convert a provider message's string or text blocks into displayable text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _chat_completion_message(provider: str, response: dict) -> dict:
    """Normalize a provider chat-completion choice to one message dictionary."""
    choices = response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    if not isinstance(choice, dict):
        return {}
    message = choice.get("message")
    if isinstance(message, dict):
        return message

    # Mistral connector completions may return multiple output messages instead
    # of the standard single `message` object used by custom function calls.
    messages = choice.get("messages") if provider == "mistral" else None
    if not isinstance(messages, list):
        return {}
    content_parts: list[str] = []
    tool_calls: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = _message_text(item.get("content"))
        if text:
            content_parts.append(text)
        calls = item.get("tool_calls")
        if isinstance(calls, list):
            tool_calls.extend(call for call in calls if isinstance(call, dict))
    return {"content": "".join(content_parts), "tool_calls": tool_calls}


def _final_response_error(text: str, truncated: bool) -> str | None:
    """Reject output-limit stops and tool-call markup returned as plain text."""
    if truncated:
        return "provider_output_truncated"
    if re.search(r"<\s*/?\s*(?:tool_call|arg_key|arg_value)\b", text, re.IGNORECASE):
        return "malformed_tool_call_text"
    return None


def _append_task_timing(result: dict, started_at: datetime, completed_at: datetime) -> dict:
    """Append authoritative timing and available round counts to an AI response."""
    message = result.get("message")
    if not isinstance(message, str):
        return result
    started = (
        started_at.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    completed = (
        completed_at.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    enriched = dict(result)
    if result.get("error") == "malformed_tool_call_text":
        message += (
            "\n\n## Response status\n"
            "Incomplete: the provider returned tool-call markup as plain text. "
            "That text was not executed. Inspect prior tool history before retrying."
        )
    enriched["message"] = (
        f"{message.rstrip()}\n\n## Task timing\n"
        f"- Started (UTC): {started}\n"
        f"- Completed (UTC): {completed}"
    )
    if "rounds_used" in result:
        enriched["message"] += (
            f"\n- Rounds consumed: {result['rounds_used']} / {result['max_tool_rounds']}"
            " (provider requests, including the final response)"
        )
    return enriched


async def _model_request(url, headers, body, *, progress, round_number):
    """Retry only transport timeouts, preserving the current model request."""
    for attempt in range(3):
        if attempt and progress:
            progress({"phase": "model", "round": round_number, "status": "retrying"})
        try:
            return _json_request(url, headers, body)
        except RuntimeError as exc:
            cause = exc.__cause__
            if isinstance(cause, urllib.error.URLError):
                cause = cause.reason
            if not isinstance(cause, TimeoutError) or attempt == 2:
                raise
            if progress:
                progress({"phase": "model", "round": round_number, "status": "retry_wait"})
            await asyncio.sleep(2 ** attempt)


async def _run_provider_loop(**kwargs):
    """Apply one context budget and evidence archive to either provider protocol."""
    from rmm_ai_context import ContextWindow, ContextDispatcher, ContextBudgetExceeded, READ_CONTEXT_TOOL
    runtime = kwargs['call_tool']
    if getattr(type(runtime), 'activate_explicit', None):
        runtime.require_from_messages(kwargs['messages'])
        try:
            runtime.activate_explicit()
        except ValueError as error:
            return {'ok': False, 'error': str(error),
                    'message': 'The explicitly invoked skill cannot run: an enabled skill, dependency, or required tool is unavailable.'}
    scope = kwargs.pop('context_scope', None)
    window = ContextWindow(kwargs.get('progress'), scope=scope)
    dispatcher = ContextDispatcher(kwargs['call_tool'], window)
    window.anchor = next((dict(m) for m in reversed(kwargs['messages']) if m.get('role') == 'user' and isinstance(m.get('content'), str)), None)
    window.operator_requests = [{'role': 'user', 'content': _message_with_checkpoint(m)}
                                for m in kwargs['messages'] if m.get('role') == 'user' and isinstance(m.get('content'), str)]
    try:
        result = await _run_provider_conversation(**{**kwargs, 'call_tool': dispatcher,
            'tools': [*kwargs['tools'], READ_CONTEXT_TOOL], 'context_window': window})
    except ContextBudgetExceeded:
        result = {'ok': False, 'error': 'context_budget_exceeded',
                  'message': 'The configured context budget cannot fit the current instructions and working state. Split the task or adjust the budget. Completed operations were not replayed.',
                  'tool_calls_made': dispatcher.operations}
    except Exception as error:
        from rmm_ai_jobs import JobInterrupted, _classify_failure, _failure_diagnostics, _CLASSIFIED_ERRORS, _GENERIC_FAILURE_MESSAGE
        if isinstance(error, JobInterrupted):
            raise
        classification = _classify_failure(error)
        result = {'ok': False, 'error': classification or 'ai_execution_failed',
                  'message': _CLASSIFIED_ERRORS.get(classification, _GENERIC_FAILURE_MESSAGE),
                  'diagnostics': _failure_diagnostics(error)}
    if getattr(type(runtime), 'instructions', None):
        result['step_state'] = {'step': runtime.step, 'active_skills': runtime.active,
                                'versions': {sid: runtime.versions[sid] for sid in runtime.active},
                                'checkpoint': runtime.checkpoint, 'summary': runtime.summary,
                                'returned_calls': [{'name': call['name'], 'artifact_id': call['artifact_id']}
                                                   for call in dispatcher.operations]}
    result['context_artifacts'] = window.manifest()
    result['context_compactions'] = window.compactions
    result['context_budget'] = window.diagnostics
    return result


async def _run_provider_conversation(
    *,
    provider: str,
    api_key: str,
    messages: list[dict],
    model: str,
    system: str,
    tools: list[dict],
    call_tool: Callable[[str, dict], Awaitable[str]],
    max_rounds: int,
    via: str,
    resolve_session=None,
    progress: Callable[[dict], None] | None = None,
    context_window=None,
) -> dict:
    """Run one provider's native chat and tool-calling protocol until a final reply."""
    system += CONCURRENCY_GUIDANCE
    tool_log: list[dict] = []
    total_rounds = max_rounds * (MAX_TOOL_CONTINUATIONS + 1)
    malformed_retries = 0

    async def request(url, headers, body, **options):
        """Pin active instructions and budget every request before contacting a provider."""
        if context_window:
            body['tools'] = call_tool.request_tools(body.get('tools', []))
            body = context_window.prepare(body, call_tool.instructions())
            body['max_completion_tokens' if provider == 'openai' else 'max_tokens'] = context_window.reserve
        return await _model_request(url, headers, body, **options)

    def add_continuation_prompt(convo: list[dict]) -> None:
        """Tell the provider to continue a long task after an internal checkpoint."""
        convo.append(
            {
                "role": "user",
                "content": (
                    "Continue the original task using the tool results already obtained. "
                    "Avoid repeating successful calls and keep working until the task is "
                    "complete. Do not provide a final summary while more work is needed."
                ),
            }
        )

    if provider in ("openai", "mistral"):
        url = OPENAI_CHAT_URL if provider == "openai" else MISTRAL_CHAT_URL
        convo = _build_openai_convo(messages, system)
        for round_index in range(total_rounds):
            if progress:
                progress({"phase": "model", "round": round_index + 1})
            if round_index and round_index % max_rounds == 0:
                add_continuation_prompt(convo)
            response = await request(
                url,
                _provider_headers(provider, api_key),
                {
                    "model": model,
                    "messages": convo,
                    "tools": tools,
                    "tool_choice": "auto",
                },
                progress=progress, round_number=round_index + 1,
            )
            message = _chat_completion_message(provider, response)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                truncated = response["choices"][0].get("finish_reason") == "length"
                text = _message_text(message.get("content"))
                error = _final_response_error(text, truncated)
                if error == "malformed_tool_call_text" and malformed_retries < 2 and round_index + 1 < total_rounds:
                    malformed_retries += 1
                    convo.append({"role": "assistant", "content": text})
                    convo.append({"role": "user", "content": (
                        "Your last response contained tool-call markup as plain text; "
                        "that text was not executed. Use the native structured tool-call "
                        "interface if further work is required, or provide a factual final report. "
                        "Preserve prior results and do not repeat completed operations. "
                        "All existing authorization and operator-consent boundaries still apply."
                    )})
                    continue
                return {
                    "ok": error is None,
                    **({"error": error} if error else {}),
                    "rounds_used": round_index + 1,
                    "malformed_response_retries": malformed_retries,
                    "max_tool_rounds": total_rounds,
                    "message": text,
                    "tool_calls_made": tool_log,
                    "usage": response.get("usage"),
                    "via": via,
                    "provider": provider,
                }
            convo.append(
                {
                    "role": "assistant",
                    "content": _message_text(message.get("content")) or None,
                    "tool_calls": tool_calls,
                }
            )
            normalized = []
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                normalized.append({"name": str(function.get("name") or ""), "arguments": arguments})
            batch_results = await run_tool_batch(
                normalized, call_tool, resolve_session=resolve_session,
                progress=progress, round_number=round_index + 1,
            )
            for tool_call, call, result in zip(tool_calls, normalized, batch_results):
                name, arguments = call["name"], call["arguments"]
                tool_log.append(
                    {"name": name, "arguments": arguments, "result_preview": result[:500]}
                )
                tool_result = {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id"),
                    "content": result,
                }
                # Mistral requires the called function name with each tool
                # result so it can associate the response with the invocation.
                if provider == "mistral":
                    tool_result["name"] = name
                convo.append(tool_result)
    else:
        convo = _build_anthropic_messages(messages)
        for round_index in range(total_rounds):
            if progress:
                progress({"phase": "model", "round": round_index + 1})
            if round_index and round_index % max_rounds == 0:
                add_continuation_prompt(convo)
            response = await request(
                ANTHROPIC_MESSAGES_URL,
                _provider_headers(provider, api_key),
                {
                    "model": model,
                    "max_tokens": 4096,
                    "system": system,
                    "messages": convo,
                    "tools": _anthropic_tools(tools),
                },
                progress=progress, round_number=round_index + 1,
            )
            blocks = response.get("content") or []
            calls = [block for block in blocks if block.get("type") == "tool_use"]
            if not calls:
                truncated = response.get("stop_reason") == "max_tokens"
                text = "".join(
                    str(block.get("text", ""))
                    for block in blocks
                    if block.get("type") == "text"
                )
                error = _final_response_error(text, truncated)
                if error == "malformed_tool_call_text" and malformed_retries < 2 and round_index + 1 < total_rounds:
                    malformed_retries += 1
                    convo.append({"role": "assistant", "content": text})
                    convo.append({"role": "user", "content": (
                        "Your last response contained tool-call markup as plain text; "
                        "that text was not executed. Use the native structured tool-call "
                        "interface if further work is required, or provide a factual final report. "
                        "Preserve prior results and do not repeat completed operations. "
                        "All existing authorization and operator-consent boundaries still apply."
                    )})
                    continue
                return {
                    "ok": error is None,
                    **({"error": error} if error else {}),
                    "rounds_used": round_index + 1,
                    "malformed_response_retries": malformed_retries,
                    "max_tool_rounds": total_rounds,
                    "message": text,
                    "tool_calls_made": tool_log,
                    "usage": response.get("usage"),
                    "via": via,
                    "provider": provider,
                }
            convo.append({"role": "assistant", "content": blocks})
            results = []
            normalized = [{"name": str(call.get("name") or ""),
                           "arguments": call.get("input") if isinstance(call.get("input"), dict) else {}}
                          for call in calls]
            batch_results = await run_tool_batch(
                normalized, call_tool, resolve_session=resolve_session,
                progress=progress, round_number=round_index + 1,
            )
            for call, normalized_call, result in zip(calls, normalized, batch_results):
                name, arguments = normalized_call["name"], normalized_call["arguments"]
                tool_log.append(
                    {"name": name, "arguments": arguments, "result_preview": result[:500]}
                )
                results.append(
                    {"type": "tool_result", "tool_use_id": call.get("id"), "content": result}
                )
            convo.append({"role": "user", "content": results})
    return {
        "ok": False,
        "error": "max_tool_rounds_exceeded",
        "rounds_used": total_rounds,
        "malformed_response_retries": malformed_retries,
        "message": (
            "## Task report\n\n"
            f"- Work completed: {len(tool_log)} tool calls returned results.\n"
            "- Result: Task completion is unconfirmed; the round limit was reached.\n"
            "- Evidence: Inspect the tool history before retrying; returned tool results "
            "do not establish that each operation succeeded."
        ),
        "tool_calls_made": tool_log,
        "max_tool_rounds": total_rounds,
        "via": via,
        "provider": provider,
    }


def _run_ai_chat_mcp(
    *,
    rmm_base_url: str,
    rmm_token: str,
    rmm_headers: dict[str, str] | None = None,
    provider: str,
    api_key: str,
    messages: list[dict],
    model: str,
    selected_session_id: str | None,
    max_rounds: int,
    exegol_mcp_enabled: bool | None = None,
    exegol_mcp_url: str | None = None,
    exegol_mcp_token: str | None = None,
    skill_ids: list[str] | None = None,
    skill_documents: list[dict] | None = None,
    skill_policy: list[dict] | None = None,
    context_scope: str | None = None,
    workflow: str = "auto",
    execution_scope: dict | None = None,
    request_id: str | None = None,
    state_owner: str | None = None,
    exegol_container: str | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Run a provider chat loop using the local RMM MCP server and optional Exegol MCP."""
    engine = ai_engine()
    rejection = legacy_admission(workflow, execution_scope, engine)
    if rejection:
        return rejection
    from rmm_mcp_client import run_with_mcp_session
    connected = False
    client = make_client(rmm_base_url, rmm_token, extra_headers=rmm_headers)

    async def resolve_session(ref):
        """Resolve visible session aliases outside the model event loop."""
        return await asyncio.to_thread(_resolve_session_id, client, ref)

    async def chat(mcp) -> dict:
        nonlocal connected
        connected = True
        if engine == 'v3':
            from rmm_ai_v3 import run_v3
            sid = selected_session_id
            if sid and workflow not in {'auto', 'explain'}:
                sid, _ = await resolve_session(sid)
                if not sid:
                    return {'ok': False, 'error': 'selected_session_unavailable', 'message': 'Select an available scoped agent.'}
            if progress:
                progress({'phase': 'ready', 'mcp_status': 'connected', 'tool_count': len(mcp.openai_tools)})
            return await run_v3(provider=provider, api_key=api_key, model=model, messages=messages,
                documents=skill_documents or [], tools=mcp.openai_tools, call_tool=mcp.call_tool,
                scope=hashlib.sha256(json.dumps([rmm_base_url, state_owner, sid]).encode()).hexdigest() if state_owner else context_scope, selected_session_id=sid, container=exegol_container,
                external_tool_names=getattr(mcp, 'external_tool_names', ()), workflow=workflow,
                max_tasks=min(max_rounds, 32), progress=progress, policy=skill_policy, execution_scope=execution_scope, request_id=request_id)
        core, catalog, tools, call_tool = prepare_skill_context(
            skill_documents, mcp.openai_tools, mcp.call_tool, progress,
            external_tool_names=getattr(mcp, 'external_tool_names', ()), policy=skill_policy,
        )
        if progress:
            progress({"phase": "ready", "mcp_status": "connected", "tool_count": len(tools)})
        system = compose_system_prompt(
            mcp.server_instructions or SYSTEM_PROMPT,
            skill_ids=skill_ids,
            session_context=_selected_session_context(selected_session_id),
            skill_documents=core,
        ) + catalog
        return await _run_provider_loop(
            provider=provider,
            api_key=api_key,
            messages=messages,
            model=model,
            system=system,
            tools=tools,
            call_tool=call_tool,
            max_rounds=max_rounds,
            via="mcp",
            context_scope=context_scope,
            resolve_session=resolve_session,
            progress=progress,
        )

    try:
        return run_with_mcp_session(
            rmm_base_url,
            rmm_token,
            chat,
            extra_headers=rmm_headers,
            exegol_enabled=exegol_mcp_enabled,
            exegol_mcp_url=exegol_mcp_url,
            exegol_mcp_token=exegol_mcp_token,
        )
    except Exception as e:
        if connected:
            # The MCP session was established; `chat()` (the provider call or
            # a tool execution) is what failed. Re-raise as-is so the actual
            # provider error (e.g. an AI provider quota/rate-limit message)
            # reaches the operator instead of being relabeled as an MCP
            # connection problem.
            raise
        if progress:
            progress({"phase": "failed", "mcp_status": "error"})
        raise RuntimeError(
            f"MCP server failed: {e}. Install MCP support: "
            "pip install -r requirements-mcp.txt (Python 3.10+)."
        ) from e


def _run_ai_chat_direct(
    *,
    rmm_base_url: str,
    rmm_token: str,
    rmm_headers: dict[str, str] | None = None,
    provider: str,
    api_key: str,
    messages: list[dict],
    model: str,
    selected_session_id: str | None,
    max_rounds: int,
    skill_ids: list[str] | None = None,
    skill_documents: list[dict] | None = None,
    skill_policy: list[dict] | None = None,
    context_scope: str | None = None,
    workflow: str = "auto",
    execution_scope: dict | None = None,
    request_id: str | None = None,
    state_owner: str | None = None,
    exegol_container: str | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Run a provider chat loop with direct in-process RMM tool execution."""
    engine = ai_engine()
    rejection = legacy_admission(workflow, execution_scope, engine)
    if rejection:
        return rejection
    client = make_client(rmm_base_url, rmm_token, extra_headers=rmm_headers)
    async def execute(name: str, arguments: dict) -> str:
        return await asyncio.to_thread(execute_tool, client, name, arguments)

    async def resolve_session(ref):
        """Resolve visible session aliases without blocking concurrent tools."""
        return await asyncio.to_thread(_resolve_session_id, client, ref)

    if engine == 'v3':
        from rmm_ai_v3 import run_v3
        async def run_episode_engine():
            """Resolve canonical session identity before creating the durable run."""
            sid = selected_session_id
            if sid and workflow not in {'auto', 'explain'}:
                sid, _ = await resolve_session(sid)
                if not sid:
                    return {'ok': False, 'error': 'selected_session_unavailable', 'message': 'Select an available scoped agent.'}
            return await run_v3(provider=provider, api_key=api_key, model=model, messages=messages,
                documents=skill_documents or [], tools=OPENAI_TOOLS, call_tool=execute,
                scope=hashlib.sha256(json.dumps([rmm_base_url, state_owner, sid]).encode()).hexdigest() if state_owner else context_scope, selected_session_id=sid, workflow=workflow,
                max_tasks=min(max_rounds, 32), progress=progress, policy=skill_policy, execution_scope=execution_scope, request_id=request_id)
        return asyncio.run(run_episode_engine())

    core, catalog, tools, call_tool = prepare_skill_context(
        skill_documents, OPENAI_TOOLS, execute, progress, policy=skill_policy,
    )
    system = compose_system_prompt(
        SYSTEM_PROMPT,
        skill_ids=skill_ids,
        session_context=_selected_session_context(selected_session_id),
        skill_documents=core,
    ) + catalog

    return asyncio.run(
        _run_provider_loop(
            provider=provider,
            api_key=api_key,
            messages=messages,
            model=model,
            system=system,
            tools=tools,
            call_tool=call_tool,
            max_rounds=max_rounds,
            via="direct",
            context_scope=context_scope,
            resolve_session=resolve_session,
            progress=progress,
        )
    )


def run_ai_chat(
    *,
    rmm_base_url: str,
    rmm_token: str,
    rmm_headers: dict[str, str] | None = None,
    provider: str = "openai",
    api_key: str = "",
    messages: list[dict],
    model: str | None = None,
    selected_session_id: str | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    exegol_mcp_enabled: bool | None = None,
    exegol_mcp_url: str | None = None,
    exegol_mcp_token: str | None = None,
    skill_ids: list[str] | None = None,
    skill_documents: list[dict] | None = None,
    skill_policy: list[dict] | None = None,
    context_scope: str | None = None,
    workflow: str = "auto",
    execution_scope: dict | None = None,
    request_id: str | None = None,
    state_owner: str | None = None,
    exegol_container: str | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Run an AI provider agent loop and return its final response and tool activity."""
    started_at = datetime.now(timezone.utc)
    if not isinstance(workflow, str) or workflow not in {'legacy', 'auto', 'observe', 'network', 'directory', 'collect', 'files', 'embeddings', 'agent_commands', 'explain'}:
        raise ValueError('invalid_ai_workflow')
    rejection = legacy_admission(workflow, execution_scope)
    if rejection:
        return _append_task_timing(rejection, started_at, datetime.now(timezone.utc))
    provider = (provider or "").strip().lower()
    api_key = (api_key or "").strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported_provider")
    if not api_key:
        raise ValueError("missing_api_key")
    rmm_token = (rmm_token or "").strip()
    if not rmm_token and not (
        isinstance(rmm_headers, dict)
        and rmm_headers.get("X-RMM-Workspace")
        and rmm_headers.get("X-RMM-Workspace-Token")
    ):
        raise ValueError(
            "RMM API or workspace token required for tool calls"
        )
    resolved_model = (model or "").strip() or DEFAULT_MODELS[provider]

    from rmm_mcp_client import mcp_available, use_mcp_for_ai, resolve_exegol_mcp_config

    exegol_enabled, _, _ = resolve_exegol_mcp_config(
        enabled=exegol_mcp_enabled, url=exegol_mcp_url, token=exegol_mcp_token,
    )
    mcp_selected = use_mcp_for_ai()
    mcp_installed = mcp_available()
    use_mcp = mcp_selected and mcp_installed
    if skill_documents is None:
        skill_documents, snapshot_policy = resolve_ai_skill_snapshot(skill_ids)
        if skill_policy is None:
            skill_policy = snapshot_policy
    if skill_policy is None:
        skill_policy = resolve_ai_skill_policy()
    wants_exegol = (ai_engine() == 'legacy_v2' or workflow in {'network', 'directory', 'collect'}) and backend_enabled('external', skill_documents, skill_policy)
    exegol_enabled = exegol_enabled and wants_exegol
    execution = {
        "tool_path": "mcp" if use_mcp else "direct",
        "mcp_status": "connecting" if use_mcp else ("unavailable" if mcp_selected else "disabled"),
        "exegol_status": "requested" if exegol_enabled else "disabled",
        "skills": [s.get("id", "") for s in skill_documents if isinstance(s, dict) and s.get("id") in CORE_SKILLS and str(s.get("body") or "").strip()],
    }
    original_progress = progress

    def report_execution(event):
        """Attach the resolved execution context to every job checkpoint."""
        execution.update({k: event[k] for k in ("mcp_status", "skills", "active_skills", "step",
                                               "step_summary", "step_checkpoint", "context_compactions", "run_id", "task_id", "role") if k in event})
        if event.get("mcp_status") == "connected" and exegol_enabled:
            execution["exegol_status"] = "connected"
        if original_progress:
            original_progress({**execution, **event})

    progress = report_execution
    progress({"phase": "initializing"})
    if wants_exegol and not exegol_enabled:
        return {"ok": False, "error": "exegol_required",
                "message": "The enabled Exegol workflow requires its configured MCP connection. Configure the owner service or disable the Exegol skill."}
    if exegol_enabled and not use_mcp:
        return {"ok": False, "error": "exegol_requires_mcp",
                "message": "Exegol requires MCP. Enable RMM_AI_USE_MCP and install MCP support in the server environment."}

    # Derive scope from authenticated credentials and canonical session, never
    # accept an operator-supplied archive path or another workspace's scope.
    context_scope = hashlib.sha256(json.dumps([rmm_base_url, rmm_token,
        rmm_headers or {}, selected_session_id], sort_keys=True).encode()).hexdigest()
    common = {
        "rmm_base_url": rmm_base_url,
        "rmm_token": rmm_token,
        "rmm_headers": rmm_headers,
        "provider": provider,
        "api_key": api_key,
        "messages": messages,
        "model": resolved_model,
        "selected_session_id": selected_session_id,
        "max_rounds": max_rounds,
        "skill_ids": skill_ids,
        "skill_documents": skill_documents,
        "skill_policy": skill_policy,
        "context_scope": context_scope,
        "workflow": workflow,
        "execution_scope": execution_scope,
        "request_id": request_id,
        "state_owner": state_owner,
        "exegol_container": exegol_container,
        "progress": progress,
    }
    if use_mcp:
        result = _run_ai_chat_mcp(
            **common,
            exegol_mcp_enabled=exegol_enabled,
            exegol_mcp_url=exegol_mcp_url,
            exegol_mcp_token=exegol_mcp_token,
        )
    else:
        result = _run_ai_chat_direct(**common)
    return _append_task_timing(result, started_at, datetime.now(timezone.utc))
