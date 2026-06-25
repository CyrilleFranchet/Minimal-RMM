"""
OpenAI chat loop with RMM tools via MCP (mcp_rmm_server.py) or direct rmm_tools fallback.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from rmm_tools import OPENAI_TOOLS, SYSTEM_PROMPT, execute_tool, make_client
from rmm_ai_skills import compose_system_prompt

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
MAX_TOOL_ROUNDS = 12


def _openai_request(api_key: str, body: dict, timeout: float = 180) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_CHAT_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            err = json.loads(raw)
        except json.JSONDecodeError:
            err = {"error": raw or str(e)}
        raise RuntimeError(err.get("error", {}).get("message", raw or str(e))) from e


def _selected_session_context(selected_session_id: str | None) -> str:
    if not selected_session_id:
        return ""
    return f"\nThe operator currently has this session selected in the UI: {selected_session_id}"


def _build_convo(
    messages: list[dict],
    system_content: str,
) -> list[dict]:
    convo: list[dict] = [{"role": "system", "content": system_content}]
    for m in messages:
        role = m.get("role")
        if role in ("user", "assistant") and m.get("content"):
            convo.append({"role": role, "content": m["content"]})
        elif role == "tool" and m.get("tool_call_id") and m.get("content"):
            convo.append({
                "role": "tool",
                "tool_call_id": m["tool_call_id"],
                "content": m["content"],
            })
    return convo


def _run_ai_chat_mcp(
    *,
    rmm_base_url: str,
    rmm_token: str,
    openai_api_key: str,
    messages: list[dict],
    model: str,
    selected_session_id: str | None,
    max_rounds: int,
    exegol_mcp_enabled: bool | None = None,
    exegol_mcp_url: str | None = None,
    exegol_mcp_token: str | None = None,
    skill_ids: list[str] | None = None,
) -> dict:
    from rmm_mcp_client import run_with_mcp_session

    context = _selected_session_context(selected_session_id)

    async def _chat(mcp) -> dict:
        base = mcp.server_instructions or SYSTEM_PROMPT
        system = compose_system_prompt(
            base,
            skill_ids=skill_ids,
            session_context=context,
        )
        convo = _build_convo(messages, system)
        tools = mcp.openai_tools

        tool_log: list[dict] = []
        for _ in range(max_rounds):
            body = {
                "model": model,
                "messages": convo,
                "tools": tools,
                "tool_choice": "auto",
            }
            resp = _openai_request(openai_api_key.strip(), body)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                return {
                    "ok": True,
                    "message": msg.get("content") or "",
                    "tool_calls_made": tool_log,
                    "usage": resp.get("usage"),
                    "via": "mcp",
                }

            convo.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    args = {}
                result = await mcp.call_tool(name, args)
                tool_log.append({
                    "name": name,
                    "arguments": args,
                    "result_preview": result[:500],
                })
                convo.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": result,
                })

        return {
            "ok": False,
            "error": "max_tool_rounds_exceeded",
            "tool_calls_made": tool_log,
            "via": "mcp",
        }

    try:
        return run_with_mcp_session(
            rmm_base_url,
            rmm_token,
            _chat,
            exegol_enabled=exegol_mcp_enabled,
            exegol_mcp_url=exegol_mcp_url,
            exegol_mcp_token=exegol_mcp_token,
        )
    except Exception as e:
        raise RuntimeError(
            f"MCP server failed: {e}. "
            "Install MCP support: pip install -r requirements-mcp.txt (Python 3.10+). "
            "For Exegol: run `exegol-mcp` locally and set URL/token in the AI panel or "
            "RMM_EXEGOL_MCP_URL / RMM_EXEGOL_MCP_TOKEN."
        ) from e


def _run_ai_chat_direct(
    *,
    rmm_base_url: str,
    rmm_token: str,
    openai_api_key: str,
    messages: list[dict],
    model: str,
    selected_session_id: str | None,
    max_rounds: int,
    skill_ids: list[str] | None = None,
) -> dict:
    client = make_client(rmm_base_url, rmm_token)
    system = compose_system_prompt(
        SYSTEM_PROMPT,
        skill_ids=skill_ids,
        session_context=_selected_session_context(selected_session_id),
    )
    convo = _build_convo(messages, system)

    tool_log: list[dict] = []
    for _ in range(max_rounds):
        body = {
            "model": model,
            "messages": convo,
            "tools": OPENAI_TOOLS,
            "tool_choice": "auto",
        }
        resp = _openai_request(openai_api_key.strip(), body)
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            return {
                "ok": True,
                "message": msg.get("content") or "",
                "tool_calls_made": tool_log,
                "usage": resp.get("usage"),
                "via": "direct",
            }

        convo.append({
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(client, name, args)
            tool_log.append({"name": name, "arguments": args, "result_preview": result[:500]})
            convo.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "content": result,
            })

    return {
        "ok": False,
        "error": "max_tool_rounds_exceeded",
        "tool_calls_made": tool_log,
        "via": "direct",
    }


def run_ai_chat(
    *,
    rmm_base_url: str,
    rmm_token: str,
    openai_api_key: str,
    messages: list[dict],
    model: str = "gpt-5.2",
    selected_session_id: str | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    exegol_mcp_enabled: bool | None = None,
    exegol_mcp_url: str | None = None,
    exegol_mcp_token: str | None = None,
    skill_ids: list[str] | None = None,
) -> dict:
    """
    Run agent loop; returns {message, tool_calls_made, via, ...} for the client.

    By default uses the MCP server (mcp_rmm_server.py). Set RMM_AI_USE_MCP=0 to call
    rmm_tools directly without spawning MCP.
    """
    if not openai_api_key or not openai_api_key.strip():
        raise ValueError("openai_api_key required")

    rmm_token = (rmm_token or "").strip()
    if not rmm_token:
        raise ValueError(
            "RMM API token required for tool calls (same as RMM_API_TOKEN / web UI login)"
        )

    from rmm_mcp_client import mcp_available, use_mcp_for_ai

    if use_mcp_for_ai() and mcp_available():
        return _run_ai_chat_mcp(
            rmm_base_url=rmm_base_url,
            rmm_token=rmm_token,
            openai_api_key=openai_api_key,
            messages=messages,
            model=model,
            selected_session_id=selected_session_id,
            max_rounds=max_rounds,
            exegol_mcp_enabled=exegol_mcp_enabled,
            exegol_mcp_url=exegol_mcp_url,
            exegol_mcp_token=exegol_mcp_token,
            skill_ids=skill_ids,
        )

    return _run_ai_chat_direct(
        rmm_base_url=rmm_base_url,
        rmm_token=rmm_token,
        openai_api_key=openai_api_key,
        messages=messages,
        model=model,
        selected_session_id=selected_session_id,
        max_rounds=max_rounds,
        skill_ids=skill_ids,
    )
