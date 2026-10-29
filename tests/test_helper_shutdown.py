"""Resource cleanup for sockets, child helpers, and partial MCP startup."""
import asyncio
import socket
import subprocess
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from rmm_mcp_client import McpCompositeSession, McpHttpSession, McpRmmSession
from rmm_socks import SessionSocksBridge, SocksManager
from rmm_tun import ServerTunRelay, TunManager, TunError
from rmm_ws import OperatorEventHub, WebSocketConnection
from rmm_profile import ProfileManager, ProfileError


class SocketShutdownTests(unittest.TestCase):
    def test_websocket_closes_transport_after_receive_failure(self):
        local, peer = socket.socketpair()
        self.addCleanup(peer.close)
        ws = WebSocketConnection(local)
        peer.close()
        self.assertIsNone(ws.recv_json())
        self.assertTrue(ws.closed)
        ws.close()
        ws.close()
        self.assertEqual(local.fileno(), -1)

    def test_hub_close_wakes_receiver_and_rejects_new_clients(self):
        local, peer = socket.socketpair()
        self.addCleanup(peer.close)
        ws = WebSocketConnection(local)
        hub = OperatorEventHub()
        hub.add(ws)
        reader = threading.Thread(target=ws.recv_json, daemon=True)
        reader.start()
        hub.close()
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive())
        self.assertEqual(local.fileno(), -1)
        late = Mock()
        hub.add(late)
        late.close.assert_called_once()
        hub.close()
        self.assertEqual(hub._snapshot(), [])

    def test_socks_stop_closes_client_stalled_before_handshake(self):
        bridge = SessionSocksBridge("test", bind_host="127.0.0.1", port=0)
        bridge.start()
        self.addCleanup(bridge.stop)
        client = socket.create_connection(("127.0.0.1", bridge.port), timeout=1)
        self.addCleanup(client.close)
        deadline = time.monotonic() + 2
        while not bridge._client_sockets and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(bridge._client_sockets)
        threads = list(bridge._client_threads)
        bridge.stop()
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(client.recv(1), b"")
        self.assertFalse(bridge._client_sockets)
        self.assertFalse(bridge._wait_connect("late-connect"))
        self.assertFalse(bridge.connect_events)


class TunShutdownTests(unittest.TestCase):
    def test_manager_close_prevents_new_helpers_and_listeners(self):
        socks = SocksManager()
        socks.start_external("existing")
        socks.close()
        socks.close()
        self.assertEqual(socks.bridges, {})
        with self.assertRaises(RuntimeError):
            socks.start_external("late")
        with self.assertRaises(RuntimeError):
            socks.start("late")
        tun = TunManager("unused", socks)
        tun.close()
        with self.assertRaises(TunError):
            tun.start("late", {})
        profiles = ProfileManager()
        profiles.close()
        with patch("rmm_profile.discover_profiles", return_value=[{"name": "fake"}]):
            with self.assertRaisesRegex(ProfileError, "stopping"):
                profiles.start("fake", listen_host="127.0.0.1", listen_port=8081)

    def test_forced_termination_kills_group_and_reaps_helper(self):
        relay = ServerTunRelay("test", {"device_name": "test0"}, "unused", Mock())
        process = Mock(pid=98765, stderr=None)
        process.wait.side_effect = [subprocess.TimeoutExpired("helper", 5), 0]
        relay.process = process
        with patch("rmm_tun.platform.system", return_value="Linux"), patch.object(relay, "_remove_route"), patch("rmm_tun.os.killpg") as killpg:
            relay.stop()
            relay.stop()
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(killpg.call_count, 2)
        self.assertIsNone(relay.process)


class Context:
    """Minimal async resource that records entry and cleanup ordering."""
    def __init__(self, events, name, value=None, fail_enter=False, fail_exit=False):
        self.events, self.name, self.value = events, name, value
        self.fail_enter, self.fail_exit = fail_enter, fail_exit
        self.server_instructions = None
        self.openai_tools = []

    async def __aenter__(self):
        self.events.append("enter:" + self.name)
        if self.fail_enter:
            raise RuntimeError("entry failed")
        return self if self.value is None else self.value

    async def __aexit__(self, *_args):
        self.events.append("exit:" + self.name)
        if self.fail_exit:
            raise RuntimeError("exit failed")

    async def initialize(self):
        raise RuntimeError("initialize failed")


class McpShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_composite_rolls_back_previous_backends_on_entry_failure(self):
        events = []
        composite = McpCompositeSession([Context(events, "first"), Context(events, "second", fail_enter=True)])
        with self.assertRaises(RuntimeError):
            await composite.__aenter__()
        self.assertEqual(events, ["enter:first", "enter:second", "exit:first"])

    async def test_composite_closes_remaining_backends_when_exit_raises(self):
        events = []
        composite = McpCompositeSession([Context(events, "first"), Context(events, "second", fail_exit=True)])
        await composite.__aenter__()
        with self.assertRaises(RuntimeError):
            await composite.__aexit__(None, None, None)
        self.assertEqual(events[-2:], ["exit:second", "exit:first"])

    async def test_stdio_initialization_failure_releases_session_and_child(self):
        events = []
        module = types.ModuleType("mcp")
        module.ClientSession = lambda *_a: Context(events, "session")
        module.StdioServerParameters = lambda **kwargs: kwargs
        stdio = types.ModuleType("mcp.client.stdio")
        stdio.stdio_client = lambda *_a: Context(events, "stdio", (None, None))
        with patch.dict("sys.modules", {"mcp": module, "mcp.client": types.ModuleType("mcp.client"), "mcp.client.stdio": stdio}):
            backend = McpRmmSession("http://localhost", "")
            with self.assertRaises(RuntimeError):
                await backend.__aenter__()
            await backend.__aexit__(None, None, None)
        self.assertEqual(events, ["enter:stdio", "enter:session", "exit:session", "exit:stdio"])
        self.assertIsNone(backend._session)

    async def test_http_initialization_failure_releases_all_contexts(self):
        events = []
        module = types.ModuleType("mcp")
        module.ClientSession = lambda *_a: Context(events, "session")
        transport = types.ModuleType("mcp.client.streamable_http")
        transport.streamable_http_client = lambda *_a, **_k: Context(events, "transport", (None, None))
        httpx = types.ModuleType("httpx")
        httpx.AsyncClient = lambda **_k: Context(events, "http")
        httpx.Timeout = lambda *_a, **_k: None
        with patch.dict("sys.modules", {"mcp": module, "mcp.client": types.ModuleType("mcp.client"), "mcp.client.streamable_http": transport, "httpx": httpx}):
            backend = McpHttpSession("http://localhost")
            with self.assertRaises(RuntimeError):
                await backend.__aenter__()
        self.assertEqual(events, ["enter:http", "enter:transport", "enter:session", "exit:session", "exit:transport", "exit:http"])


if __name__ == "__main__":
    unittest.main()
