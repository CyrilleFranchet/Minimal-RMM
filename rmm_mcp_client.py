"""
MCP clients for the web AI assistant — RMM (stdio) and optional Exegol (HTTP).

Requires: pip install -r requirements-mcp.txt (Python 3.10+).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, TypeVar

MCP_SCRIPT = Path(__file__).resolve().parent / "mcp_rmm_server.py"
DEFAULT_EXEGOL_MCP_URL = "http://127.0.0.1:8000/mcp"

T = TypeVar("T")


class McpBackend(Protocol):
    openai_tools: list[dict]
    server_instructions: str | None

    async def call_tool(self, name: str, arguments: dict) -> str: ...


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


def resolve_exegol_mcp_config(
    *,
    enabled: bool | None = None,
    url: str | None = None,
    token: str | None = None,
) -> tuple[bool, str, str | None]:
    """
    Resolve Exegol MCP settings from request body and RMM_EXEGOL_MCP_* env vars.

    Returns (enabled, url, token). When enabled with no URL, uses DEFAULT_EXEGOL_MCP_URL.
    """
    env_url = os.environ.get("RMM_EXEGOL_MCP_URL", "").strip()
    env_token = os.environ.get("RMM_EXEGOL_MCP_TOKEN", "").strip()

    resolved_url = (url or "").strip() or env_url
    resolved_token = (token or "").strip() or env_token or None

    if enabled is None:
        enabled = bool(resolved_url)

    if not enabled:
        return False, "", None

    if not resolved_url:
        resolved_url = DEFAULT_EXEGOL_MCP_URL

    return True, resolved_url.rstrip("/"), resolved_token


class _BaseMcpSession:
    """Shared MCP session state after initialize()."""

    def __init__(self) -> None:
        self._session: Any = None
        self._openai_tools: list[dict] = []
        self._server_instructions: str | None = None

    @property
    def openai_tools(self) -> list[dict]:
        return self._openai_tools

    @property
    def server_instructions(self) -> str | None:
        return self._server_instructions

    async def _load_tools_and_instructions(self) -> None:
        init = await self._session.initialize()
        instructions = getattr(init, "instructions", None) if init else None
        if instructions:
            self._server_instructions = instructions.strip()
        tools_resp = await self._session.list_tools()
        self._openai_tools = [mcp_tool_to_openai(t) for t in tools_resp.tools]

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self._session.call_tool(name, arguments=arguments)
        text = _tool_result_text(result)
        if text:
            return text
        return json.dumps({"ok": False, "error": "empty_tool_result"})


class McpRmmSession(_BaseMcpSession):
    """One stdio MCP connection to mcp_rmm_server.py (lifetime of a single AI chat)."""

    def __init__(self, rmm_base_url: str, rmm_token: str):
        super().__init__()
        self.rmm_base_url = rmm_base_url.rstrip("/")
        self.rmm_token = rmm_token
        self._stdio_stack: Any = None

    async def __aenter__(self) -> McpRmmSession:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env["RMM_SERVER_URL"] = self.rmm_base_url
        token = (self.rmm_token or "").strip()
        if token:
            env["RMM_API_TOKEN"] = token
        else:
            env.pop("RMM_API_TOKEN", None)
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
        await self._load_tools_and_instructions()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.__aexit__(exc_type, exc, tb)
            self._session = None
        if self._stdio_stack is not None:
            await self._stdio_stack.__aexit__(exc_type, exc, tb)
            self._stdio_stack = None


class McpHttpSession(_BaseMcpSession):
    """Streamable HTTP MCP connection (Exegol MCP server)."""

    def __init__(self, url: str, token: str | None = None):
        super().__init__()
        self.url = url.rstrip("/")
        self.token = (token or "").strip() or None
        self._stack = AsyncExitStack()
        self._http_client: Any = None
        self._transport_cm: Any = None

    async def __aenter__(self) -> McpHttpSession:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(300.0, connect=30.0),
        )
        await self._stack.enter_async_context(self._http_client)

        self._transport_cm = streamable_http_client(self.url, http_client=self._http_client)
        streams = await self._stack.enter_async_context(self._transport_cm)
        if len(streams) == 3:
            read, write, _ = streams
        else:
            read, write = streams

        self._session = ClientSession(read, write)
        await self._stack.enter_async_context(self._session)
        await self._load_tools_and_instructions()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._session = None
        await self._stack.aclose()


class McpCompositeSession:
    """Merge tools from multiple MCP backends (e.g. RMM + Exegol)."""

    def __init__(self, backends: list[McpBackend]) -> None:
        self._backends = backends
        self._tool_route: dict[str, McpBackend] = {}
        self._openai_tools: list[dict] = []
        self._server_instructions: str | None = None

    @property
    def openai_tools(self) -> list[dict]:
        return self._openai_tools

    @property
    def server_instructions(self) -> str | None:
        return self._server_instructions

    async def __aenter__(self) -> McpCompositeSession:
        instructions: list[str] = []
        for backend in self._backends:
            await backend.__aenter__()  # type: ignore[attr-defined]
            if backend.server_instructions:
                instructions.append(backend.server_instructions)
            for tool in backend.openai_tools:
                name = tool["function"]["name"]
                if name in self._tool_route:
                    raise RuntimeError(f"duplicate MCP tool name: {name}")
                self._tool_route[name] = backend
                self._openai_tools.append(tool)
        if instructions:
            self._server_instructions = "\n\n".join(instructions)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for backend in reversed(self._backends):
            await backend.__aexit__(exc_type, exc, tb)  # type: ignore[attr-defined]

    async def call_tool(self, name: str, arguments: dict) -> str:
        backend = self._tool_route.get(name)
        if backend is None:
            return json.dumps({"ok": False, "error": f"unknown_tool: {name}"})
        return await backend.call_tool(name, arguments)


def run_with_mcp_session(
    rmm_base_url: str,
    rmm_token: str,
    fn: Callable[[McpBackend], Awaitable[T]],
    *,
    exegol_enabled: bool | None = None,
    exegol_mcp_url: str | None = None,
    exegol_mcp_token: str | None = None,
) -> T:
    enabled, exegol_url, exegol_token = resolve_exegol_mcp_config(
        enabled=exegol_enabled,
        url=exegol_mcp_url,
        token=exegol_mcp_token,
    )

    async def _run() -> T:
        backends: list[Any] = [McpRmmSession(rmm_base_url, rmm_token)]
        if enabled:
            backends.append(McpHttpSession(exegol_url, exegol_token))

        if len(backends) == 1:
            session: McpBackend = backends[0]
        else:
            session = McpCompositeSession(backends)

        async with session:  # type: ignore[union-attr]
            return await fn(session)

    return asyncio.run(_run())


def use_mcp_for_ai() -> bool:
    flag = os.environ.get("RMM_AI_USE_MCP", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")
