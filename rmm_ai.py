"""
OpenAI chat loop with RMM tool execution (used by web UI /api/v1/ai/chat).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from rmm_tools import OPENAI_TOOLS, SYSTEM_PROMPT, execute_tool, make_client

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


def run_ai_chat(
    *,
    rmm_base_url: str,
    rmm_token: str,
    openai_api_key: str,
    messages: list[dict],
    model: str = "gpt-4o-mini",
    selected_session_id: str | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> dict:
    """
    Run agent loop; returns {message, tool_calls_made, messages} for the client.
    """
    if not openai_api_key or not openai_api_key.strip():
        raise ValueError("openai_api_key required")

    client = make_client(rmm_base_url, rmm_token)
    context = ""
    if selected_session_id:
        context = f"\nThe operator currently has this session selected in the UI: {selected_session_id}"

    convo: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT + context},
    ]
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
            content = msg.get("content") or ""
            return {
                "ok": True,
                "message": content,
                "tool_calls_made": tool_log,
                "usage": resp.get("usage"),
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
    }
