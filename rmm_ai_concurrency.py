"""Bounded, conservative scheduling of one model round's tool calls."""

import asyncio

from rmm_settings import getenv


READ_TOOLS = frozenset({
    "health", "list_sessions", "get_session", "get_events",
    "list_plugins", "list_ai_jobs", "get_ai_job",
})
CONCURRENCY_GUIDANCE = (
    "\n\nRequest independent tool calls together in the same model round. "
    "Calls whose arguments or decisions depend on another result must wait for "
    "a later round. Command execution may overlap across different hosts; "
    "commands on the same host remain ordered. Never assume a queued command "
    "has completed. Existing authorization and skill requirements still apply."
)


def tool_concurrency():
    """Return a bounded per-conversation limit; one restores serial execution."""
    try:
        return max(1, min(16, int(getenv("RMM_AI_TOOL_CONCURRENCY", "4"))))
    except (TypeError, ValueError):
        return 4


async def run_tool_batch(calls, call_tool, *, resolve_session=None, progress=None, round_number=1):
    """Execute normalized calls with barriers, host exclusion, and ordered results."""
    if getattr(type(call_tool), 'begin_batch', None):
        call_tool.begin_batch(calls)
    limit = tool_concurrency()
    results = [None] * len(calls)

    async def invoke(index, arguments):
        """Report and execute one call without altering transcript order."""
        name = calls[index]["name"]
        if progress:
            progress({"phase": "tool", "round": round_number, "tool": name, "status": "running"})
        result = await call_tool(name, arguments)
        if progress:
            progress({"phase": "tool", "round": round_number, "tool": name, "status": "completed"})
        results[index] = result

    async def drain(segment):
        """Admit bounded work, retaining host order and draining on failure."""
        pending = list(segment)
        active = {}
        occupied = set()
        try:
            while pending or active:
                for item in list(pending):
                    if len(active) >= limit:
                        break
                    index, arguments, resources = item
                    if occupied.intersection(resources):
                        continue
                    pending.remove(item)
                    occupied.update(resources)
                    task = asyncio.create_task(invoke(index, arguments))
                    active[task] = resources
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    occupied.difference_update(active.pop(task))
                    task.result()
        finally:
            # Running remote operations cannot reliably be canceled. Finish them
            # before propagating errors instead of admitting more work or replaying.
            if active:
                await asyncio.shield(asyncio.gather(*active, return_exceptions=True))

    segment = []
    for index, call in enumerate(calls):
        name, arguments = call["name"], call["arguments"]
        resources = set()
        parallel = isinstance(arguments, dict) and name in READ_TOOLS
        if name == "get_events" and isinstance(arguments, dict) and arguments.get("forward", False):
            parallel = False
        if (limit > 1 and name == "exec_command" and isinstance(arguments, dict)
                and isinstance(arguments.get("session_ref"), str) and resolve_session):
            sid, info = await resolve_session(arguments.get("session_ref", ""))
            hostname = str(info.get("hostname") or "").strip().casefold()
            if sid and hostname:
                arguments = {**arguments, "session_ref": sid}
                resources = {("session", sid), ("host", hostname)}
                parallel = True
        if limit == 1 or not parallel:
            if segment:
                await drain(segment)
                segment = []
            await invoke(index, arguments)
        else:
            segment.append((index, arguments, resources))
    if segment:
        await drain(segment)
    return results
