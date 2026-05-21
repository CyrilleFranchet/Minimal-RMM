"""
stdio MCP client for the web AI assistant — talks to mcp_rmm_server.py.

Requires: pip install -r requirements-mcp.txt (Python 3.10+).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

MCP_SCRIPT = Path(__file__).resolve().parent / "mcp_rmm_server.py"

T = TypeVar("T")


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


def mcp_tool_to_openai(tool: Any) -> dict:
    schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "parameters": schema,
        },
    }


def _tool_result_text(result: Any) -> str:
    from mcp import types

    parts: list[str] = []
    for block in result.content or []:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, indent=2, default=str)
    return ""


class McpRmmSession:
    """One stdio MCP connection to mcp_rmm_server.py (lifetime of a single AI chat)."""

    def __init__(self, rmm_base_url: str, rmm_token: str):
        self.rmm_base_url = rmm_base_url.rstrip("/")
        self.rmm_token = rmm_token
        self._stdio_stack: Any = None
        self._session: Any = None
        self._openai_tools: list[dict] = []
        self._server_instructions: str | None = None

    async def __aenter__(self) -> McpRmmSession:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env["RMM_SERVER_URL"] = self.rmm_base_url
        env["RMM_API_TOKEN"] = self.rmm_token
        env.pop("RMM_BASE_URL", None)

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SCRIPT)],
            env=env,
            cwd=str(MCP_SCRIPT.parent),
        )
        self._stdio_stack = stdio_client(params)
        read, write = await self._stdio_stack.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        init = await self._session.initialize()
        instructions = getattr(init, "instructions", None) if init else None
        if instructions:
            self._server_instructions = instructions.strip()
        tools_resp = await self._session.list_tools()
        self._openai_tools = [mcp_tool_to_openai(t) for t in tools_resp.tools]
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.__aexit__(exc_type, exc, tb)
            self._session = None
        if self._stdio_stack is not None:
            await self._stdio_stack.__aexit__(exc_type, exc, tb)
            self._stdio_stack = None

    @property
    def openai_tools(self) -> list[dict]:
        return self._openai_tools

    @property
    def server_instructions(self) -> str | None:
        return self._server_instructions

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self._session.call_tool(name, arguments=arguments)
        text = _tool_result_text(result)
        if text:
            return text
        return json.dumps({"ok": False, "error": "empty_tool_result"})


def run_with_mcp_session(
    rmm_base_url: str,
    rmm_token: str,
    fn: Callable[[McpRmmSession], Awaitable[T]],
) -> T:
    async def _run() -> T:
        async with McpRmmSession(rmm_base_url, rmm_token) as mcp:
            return await fn(mcp)

    return asyncio.run(_run())


def use_mcp_for_ai() -> bool:
    flag = os.environ.get("RMM_AI_USE_MCP", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")
