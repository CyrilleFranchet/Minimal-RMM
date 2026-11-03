"""Deterministic concurrency and provider/transport integration coverage."""
import asyncio
import importlib.util
import json
import threading
import unittest
from unittest.mock import patch

import rmm_ai
from rmm_ai_concurrency import run_tool_batch, tool_concurrency


def call(name="health", **arguments):
    return {"name": name, "arguments": arguments}


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        setting = patch("rmm_ai_concurrency.getenv", return_value="4")
        setting.start()
        self.addCleanup(setting.stop)

    async def test_bound_and_overlap(self):
        active = peak = 0
        release = asyncio.Event()
        full = asyncio.Event()

        async def execute(name, arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                full.set()
            await release.wait()
            active -= 1
            return str(arguments["index"])

        task = asyncio.create_task(run_tool_batch([call(index=i) for i in range(9)], execute))
        await asyncio.wait_for(full.wait(), 2)
        self.assertEqual(peak, 4)
        release.set()
        self.assertEqual(await task, list(map(str, range(9))))
        self.assertEqual(peak, 4)

    async def test_aliases_and_same_host_sessions_are_serial_different_hosts_overlap(self):
        started = []
        release = asyncio.Event()
        other_started = asyncio.Event()
        inventory = {"host": ("id1", {"hostname": "HOST"}),
                     "id1": ("id1", {"hostname": "host"}),
                     "second-session": ("id2", {"hostname": "Host"}),
                     "other": ("id3", {"hostname": "other"})}

        async def resolve(ref):
            return inventory[ref]

        async def execute(name, arguments):
            started.append(arguments["command"])
            if arguments["command"] == "first":
                self.assertEqual(arguments["session_ref"], "id1")
                await release.wait()
            if arguments["command"] == "other":
                other_started.set()
            return arguments["command"]

        calls = [call("exec_command", session_ref=ref, command=cmd) for ref, cmd in
                 [("host", "first"), ("id1", "second"), ("second-session", "third"), ("other", "other")]]
        task = asyncio.create_task(run_tool_batch(calls, execute, resolve_session=resolve))
        await asyncio.wait_for(other_started.wait(), 2)
        self.assertEqual(started, ["first", "other"])
        release.set()
        self.assertEqual(await task, ["first", "second", "third", "other"])
        self.assertEqual(started, ["first", "other", "second", "third"])

    async def test_barriers_and_failed_resolution(self):
        for barrier in [call("unknown"), call("patch_config"), call("load_skill"),
                        call("bloodhound", action="collect"), call("get_events", forward=True),
                        call("exec_command", session_ref="ambiguous"),
                        {"name": "get_events", "arguments": []}]:
            with self.subTest(barrier=barrier):
                order = []

                async def resolve(ref):
                    return None, {"error": "ambiguous_session_ref"}

                async def execute(name, arguments):
                    order.append((name, "start"))
                    await asyncio.sleep(0)
                    order.append((name, "end"))
                    return "ok"

                await run_tool_batch([call(), barrier, call("list_sessions")], execute, resolve_session=resolve)
                self.assertEqual([state for _, state in order], ["start", "end"] * 3)

    async def test_raised_error_drains_started_work_and_does_not_admit_more(self):
        other_started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        started = []

        async def execute(name, arguments):
            index = arguments["index"]
            started.append(index)
            if index == 0:
                await other_started.wait()
                raise RuntimeError("failed")
            other_started.set()
            await release.wait()
            finished.set()
            return "ok"

        with patch("rmm_ai_concurrency.getenv", return_value="2"):
            task = asyncio.create_task(run_tool_batch([call(index=i) for i in range(5)], execute))
            await asyncio.wait_for(other_started.wait(), 2)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await task
        self.assertTrue(finished.is_set())
        self.assertEqual(started, [0, 1])

    async def test_serial_rollback_does_not_resolve(self):
        order = []

        async def execute(name, arguments):
            order.append("start")
            await asyncio.sleep(0)
            order.append("end")
            return "ok"

        async def resolve(ref):
            self.fail("Serial rollback must not add resolution calls")

        with patch("rmm_ai_concurrency.getenv", return_value="1"):
            await run_tool_batch([call("exec_command"), call()], execute, resolve_session=resolve)
        self.assertEqual(order, ["start", "end", "start", "end"])

    async def test_cancellation_drains_active_calls(self):
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def execute(name, arguments):
            started.set()
            await release.wait()
            finished.set()
            return "ok"

        task = asyncio.create_task(run_tool_batch([call()], execute))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_progress_interruption_stops_admission_and_drains(self):
        second_started = asyncio.Event()
        interrupted = asyncio.Event()
        release = asyncio.Event()
        started = []

        async def execute(name, arguments):
            started.append(name)
            if name == "health":
                await second_started.wait()
            else:
                second_started.set()
                await release.wait()
            return "ok"

        def progress(event):
            if event["tool"] == "health" and event["status"] == "completed":
                interrupted.set()
                raise RuntimeError("job interrupted")

        with patch("rmm_ai_concurrency.getenv", return_value="2"):
            task = asyncio.create_task(run_tool_batch(
                [call(), call("list_sessions"), call("list_plugins")], execute, progress=progress))
            await asyncio.wait_for(interrupted.wait(), 2)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaisesRegex(RuntimeError, "job interrupted"):
                await task
        self.assertEqual(started, ["health", "list_sessions"])

    async def test_provider_protocol_order_and_error_results(self):
        for provider in ("openai", "mistral", "anthropic"):
            with self.subTest(provider=provider):
                release = asyncio.Event()
                completed = []

                async def execute(name, arguments):
                    if name == "health":
                        await release.wait()
                    else:
                        release.set()
                    completed.append(name)
                    return json.dumps({"ok": name == "health"})

                calls = [{"id": "a", "type": "function", "function": {"name": "health", "arguments": "{}"}},
                         {"id": "b", "type": "function", "function": {"name": "list_sessions", "arguments": "{}"}}]
                if provider == "anthropic":
                    responses = [{"content": [{"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": {}} for c in calls]},
                                 {"content": [{"type": "text", "text": "Done"}]}]
                else:
                    responses = [{"choices": [{"message": {"tool_calls": calls}}]},
                                 {"choices": [{"message": {"content": "Done"}}]}]
                with patch("rmm_ai._json_request", side_effect=responses) as request:
                    result = await rmm_ai._run_provider_loop(provider=provider, api_key="test", messages=[],
                        model="test", system="test", tools=[], call_tool=execute, max_rounds=2, via="direct")
                self.assertTrue(result["ok"])
                self.assertEqual(completed, ["list_sessions", "health"])
                self.assertEqual([x["name"] for x in result["tool_calls_made"]], ["health", "list_sessions"])
                convo = request.call_args.args[2]["messages"]
                if provider == "anthropic":
                    self.assertEqual([x["tool_use_id"] for x in convo[-1]["content"]], ["a", "b"])
                else:
                    self.assertEqual([x["tool_call_id"] for x in convo[-2:]], ["a", "b"])
                    if provider == "mistral":
                        self.assertEqual([x["name"] for x in convo[-2:]], ["health", "list_sessions"])


class TransportTests(unittest.TestCase):
    def setUp(self):
        setting = patch("rmm_ai_concurrency.getenv", return_value="4")
        setting.start()
        self.addCleanup(setting.stop)

    def test_direct_real_resolver_and_dispatch_preserve_scope_and_host_order(self):
        rendezvous = threading.Barrier(2)
        started = []
        lock = threading.Lock()

        class Client:
            def list_sessions(self):
                return 200, {"sessions": [{"id": "aaaa-full", "hostname": "HOST-A"},
                                           {"id": "bbbb-full", "hostname": "HOST-B"}]}

            def exec_command(self, sid, command, timeout):
                with lock:
                    started.append((sid, command))
                if command != "a2":
                    rendezvous.wait(timeout=3)
                return 200, {"event": {"body": command}}

        async def loop(**kwargs):
            calls = [call("exec_command", session_ref=ref, command=cmd) for ref, cmd in
                     [("host-a", "a1"), ("aaaa", "a2"), ("HOST-B", "b1")]]
            return await run_tool_batch(calls, kwargs["call_tool"], resolve_session=kwargs["resolve_session"])

        headers = {"X-RMM-Workspace": "visible-workspace", "X-RMM-Workspace-Token": "scoped-token"}
        with patch("rmm_ai.make_client", return_value=Client()) as make_client, patch("rmm_ai._run_provider_loop", side_effect=loop):
            results = rmm_ai._run_ai_chat_direct(rmm_base_url="http://localhost", rmm_token="test", rmm_headers=headers,
                provider="openai", api_key="test", messages=[], model="test", selected_session_id=None, max_rounds=2)
        make_client.assert_called_once_with("http://localhost", "test", extra_headers=headers)
        self.assertEqual([json.loads(result)["output"] for result in results], ["a1", "a2", "b1"])
        self.assertEqual(started[-1], ("aaaa-full", "a2"))
        self.assertEqual(set(started[:2]), {("aaaa-full", "a1"), ("bbbb-full", "b1")})

    def test_malformed_reference_retains_direct_tool_error(self):
        async def loop(**kwargs):
            return await run_tool_batch([call("exec_command", session_ref=123, command="test")],
                                        kwargs["call_tool"], resolve_session=kwargs["resolve_session"])

        with patch("rmm_ai._resolve_session_id", side_effect=AssertionError("Must not pre-resolve malformed input")), \
                patch("rmm_ai._run_provider_loop", side_effect=loop):
            results = rmm_ai._run_ai_chat_direct(rmm_base_url="http://localhost", rmm_token="test",
                provider="openai", api_key="test", messages=[], model="test", selected_session_id=None, max_rounds=2)
        self.assertFalse(json.loads(results[0])["ok"])

    def test_configuration_bounds(self):
        for raw, expected in [("0", 1), ("1", 1), ("999", 16), ("bad", 4)]:
            with patch("rmm_ai_concurrency.getenv", return_value=raw):
                self.assertEqual(tool_concurrency(), expected)

    def test_parity_scanner_recognizes_stacked_and_async_tools(self):
        from scripts.check_operator_parity import mcp_tools_from_server
        source = """
@mcp.tool()
@offload
def sync_tool(): pass
@mcp.tool()
async def async_tool(): pass
def ordinary_function(): pass
"""
        with patch("pathlib.Path.read_text", return_value=source):
            self.assertEqual(mcp_tools_from_server(), {"sync_tool", "async_tool"})

    def test_direct_dispatch_overlaps_blocking_tools(self):
        rendezvous = threading.Barrier(2)

        def execute(client, name, arguments):
            rendezvous.wait(timeout=3)
            return "ok"

        async def loop(**kwargs):
            self.assertIsNotNone(kwargs["resolve_session"])
            return {"results": await run_tool_batch([call(), call("list_sessions")], kwargs["call_tool"])}

        with patch("rmm_ai.execute_tool", side_effect=execute), patch("rmm_ai._run_provider_loop", side_effect=loop):
            result = rmm_ai._run_ai_chat_direct(rmm_base_url="http://localhost", rmm_token="test", provider="openai",
                api_key="test", messages=[], model="test", selected_session_id=None, max_rounds=2)
        self.assertEqual(result["results"], ["ok", "ok"])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK not installed")
    def test_registered_mcp_dispatch_overlaps_and_preserves_schema(self):
        import mcp_rmm_server
        rendezvous = threading.Barrier(2)

        def blocking(client):
            rendezvous.wait(timeout=3)
            return "ok"

        async def run():
            definitions = {t.name: t for t in await mcp_rmm_server.mcp.list_tools()}
            self.assertEqual(definitions["exec_command"].inputSchema["required"], ["session_ref", "command"])
            self.assertEqual(definitions["exec_command"].inputSchema["properties"]["timeout"]["default"], 120)
            results = await asyncio.gather(mcp_rmm_server.mcp.call_tool("health", {}),
                                           mcp_rmm_server.mcp.call_tool("list_sessions", {}))
            self.assertEqual(len(results), 2)
            self.assertIn("ok", str(results))
            self.assertNotIn("BrokenBarrierError", str(results))

        with patch.object(mcp_rmm_server, "tool_health", side_effect=blocking), patch.object(mcp_rmm_server, "tool_list_sessions", side_effect=blocking):
            asyncio.run(run())
