"""Provider-neutral AI chat loop with RMM tools via MCP or direct calls."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable

from rmm_ai_skills import compose_system_prompt
from rmm_tools import OPENAI_TOOLS, SYSTEM_PROMPT, execute_tool, make_client

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOOL_ROUNDS = 12

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


def _build_openai_convo(messages: list[dict], system_content: str) -> list[dict]:
    """Convert saved user and assistant messages into Chat Completions messages."""
    convo: list[dict] = [{"role": "system", "content": system_content}]
    for message in messages:
        if message.get("role") in ("user", "assistant") and message.get("content"):
            convo.append({"role": message["role"], "content": str(message["content"])})
    return convo


def _build_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Convert saved user and assistant messages into Anthropic Messages API turns."""
    return [
        {"role": message["role"], "content": str(message["content"])}
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


async def _run_provider_loop(
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
) -> dict:
    """Run one provider's native chat and tool-calling protocol until a final reply."""
    tool_log: list[dict] = []
    if provider in ("openai", "mistral"):
        url = OPENAI_CHAT_URL if provider == "openai" else MISTRAL_CHAT_URL
        convo = _build_openai_convo(messages, system)
        for _ in range(max_rounds):
            response = _json_request(
                url,
                _provider_headers(provider, api_key),
                {
                    "model": model,
                    "messages": convo,
                    "tools": tools,
                    "tool_choice": "auto",
                },
            )
            message = ((response.get("choices") or [{}])[0]).get("message") or {}
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return {
                    "ok": True,
                    "message": message.get("content") or "",
                    "tool_calls_made": tool_log,
                    "usage": response.get("usage"),
                    "via": via,
                    "provider": provider,
                }
            convo.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                name = str(function.get("name") or "")
                result = await call_tool(name, arguments)
                tool_log.append(
                    {"name": name, "arguments": arguments, "result_preview": result[:500]}
                )
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": result,
                    }
                )
    else:
        convo = _build_anthropic_messages(messages)
        for _ in range(max_rounds):
            response = _json_request(
                ANTHROPIC_MESSAGES_URL,
                _provider_headers(provider, api_key),
                {
                    "model": model,
                    "max_tokens": 4096,
                    "system": system,
                    "messages": convo,
                    "tools": _anthropic_tools(tools),
                },
            )
            blocks = response.get("content") or []
            calls = [block for block in blocks if block.get("type") == "tool_use"]
            if not calls:
                text = "".join(
                    str(block.get("text", ""))
                    for block in blocks
                    if block.get("type") == "text"
                )
                return {
                    "ok": True,
                    "message": text,
                    "tool_calls_made": tool_log,
                    "usage": response.get("usage"),
                    "via": via,
                    "provider": provider,
                }
            convo.append({"role": "assistant", "content": blocks})
            results = []
            for call in calls:
                name = str(call.get("name") or "")
                arguments = call.get("input") if isinstance(call.get("input"), dict) else {}
                result = await call_tool(name, arguments)
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
        "tool_calls_made": tool_log,
        "via": via,
        "provider": provider,
    }


def _run_ai_chat_mcp(
    *,
    rmm_base_url: str,
    rmm_token: str,
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
) -> dict:
    """Run a provider chat loop using the local RMM MCP server and optional Exegol MCP."""
    from rmm_mcp_client import run_with_mcp_session

    async def chat(mcp) -> dict:
        system = compose_system_prompt(
            mcp.server_instructions or SYSTEM_PROMPT,
            skill_ids=skill_ids,
            session_context=_selected_session_context(selected_session_id),
        )
        return await _run_provider_loop(
            provider=provider,
            api_key=api_key,
            messages=messages,
            model=model,
            system=system,
            tools=mcp.openai_tools,
            call_tool=mcp.call_tool,
            max_rounds=max_rounds,
            via="mcp",
        )

    try:
        return run_with_mcp_session(
            rmm_base_url,
            rmm_token,
            chat,
            exegol_enabled=exegol_mcp_enabled,
            exegol_mcp_url=exegol_mcp_url,
            exegol_mcp_token=exegol_mcp_token,
        )
    except Exception as e:
        raise RuntimeError(
            f"MCP server failed: {e}. Install MCP support: "
            "pip install -r requirements-mcp.txt (Python 3.10+)."
        ) from e


def _run_ai_chat_direct(
    *,
    rmm_base_url: str,
    rmm_token: str,
    provider: str,
    api_key: str,
    messages: list[dict],
    model: str,
    selected_session_id: str | None,
    max_rounds: int,
    skill_ids: list[str] | None = None,
) -> dict:
    """Run a provider chat loop with direct in-process RMM tool execution."""
    client = make_client(rmm_base_url, rmm_token)
    system = compose_system_prompt(
        SYSTEM_PROMPT,
        skill_ids=skill_ids,
        session_context=_selected_session_context(selected_session_id),
    )

    async def call_tool(name: str, arguments: dict) -> str:
        return execute_tool(client, name, arguments)

    return asyncio.run(
        _run_provider_loop(
            provider=provider,
            api_key=api_key,
            messages=messages,
            model=model,
            system=system,
            tools=OPENAI_TOOLS,
            call_tool=call_tool,
            max_rounds=max_rounds,
            via="direct",
        )
    )


def run_ai_chat(
    *,
    rmm_base_url: str,
    rmm_token: str,
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
) -> dict:
    """Run an AI provider agent loop and return its final response and tool activity."""
    provider = (provider or "").strip().lower()
    api_key = (api_key or "").strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported_provider")
    if not api_key:
        raise ValueError("missing_api_key")
    rmm_token = (rmm_token or "").strip()
    if not rmm_token:
        raise ValueError(
            "RMM API token required for tool calls (same as RMM_API_TOKEN / web UI login)"
        )
    resolved_model = (model or "").strip() or DEFAULT_MODELS[provider]

    from rmm_mcp_client import mcp_available, use_mcp_for_ai

    common = {
        "rmm_base_url": rmm_base_url,
        "rmm_token": rmm_token,
        "provider": provider,
        "api_key": api_key,
        "messages": messages,
        "model": resolved_model,
        "selected_session_id": selected_session_id,
        "max_rounds": max_rounds,
        "skill_ids": skill_ids,
    }
    if use_mcp_for_ai() and mcp_available():
        return _run_ai_chat_mcp(
            **common,
            exegol_mcp_enabled=exegol_mcp_enabled,
            exegol_mcp_url=exegol_mcp_url,
            exegol_mcp_token=exegol_mcp_token,
        )
    return _run_ai_chat_direct(**common)
