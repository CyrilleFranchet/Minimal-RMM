"""Opt-in real MCP HTTP and Linux supervisor round trips; no lab targets."""
import asyncio
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rmm_ai_jobs import JobInterrupted
from rmm_exegol_execution import DurableExegolBackend
from rmm_mcp_client import McpHttpSession


CONTAINER = os.environ.get('RMM_TEST_EXEGOL_CONTAINER', '')


@unittest.skipUnless(re.fullmatch(r'rmm-exegol-test-[a-z0-9-]+', CONTAINER),
                     'requires a disposable RMM_TEST_EXEGOL_CONTAINER with Python3 and zsh')
class ExegolHttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        try:
            from mcp.server.fastmcp import FastMCP
            server = FastMCP('Exegol integration fixture', json_response=True, stateless_http=False)
            app = server.streamable_http_app()
        except ModuleNotFoundError as error:
            if error.name != 'mcp.server.fastmcp':
                raise
            from mcp.server.mcpserver import MCPServer
            server = MCPServer('Exegol integration fixture')
            app = server.streamable_http_app(json_response=True, stateless_http=False)

        @server.tool()
        async def execute_command_in_container(container_name: str, command: str) -> dict:
            """Match Exegol's synchronous Docker-exec envelope and zsh initialization."""
            if container_name != CONTAINER:
                raise ValueError('unexpected test container')
            result = subprocess.run(
                ['docker', 'exec', '-e', 'CMD=' + command, '-e', 'DISABLE_AUTO_UPDATE=true',
                 container_name, 'zsh', '-c',
                 'autoload -Uz compinit; compinit; source ~/.zshrc; eval "$CMD"'],
                capture_output=True, timeout=10)
            return {'exit_code': result.returncode,
                    'output': (result.stdout + result.stderr).decode('utf-8', errors='replace')}

        cls.socket = socket.socket()
        cls.socket.bind(('127.0.0.1', 0))
        cls.socket.listen()
        cls.url = 'http://127.0.0.1:' + str(cls.socket.getsockname()[1]) + '/mcp'
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={'sockets': [cls.socket]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        if not cls.server.started:
            cls.server.should_exit = True
            cls.thread.join(5)
            cls.socket.close()
            raise RuntimeError('MCP HTTP fixture failed to start')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)
        cls.socket.close()
        if cls.thread.is_alive():
            raise RuntimeError('MCP HTTP fixture did not stop')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.timeout = 10
        config = patch('rmm_exegol_execution.getenv', side_effect=lambda key, default=None:
                       self.temp.name if key == 'RMM_LOG_DIR' else
                       str(self.timeout) if key == 'RMM_EXEGOL_EXECUTION_TIMEOUT' else default)
        config.start()
        self.addCleanup(config.stop)
        self.scope = __import__('hashlib').sha256(self.temp.name.encode()).hexdigest()

    def backend(self, progress=None):
        return DurableExegolBackend(McpHttpSession(self.url), self.scope, progress)

    def test_real_http_command_and_reconnect_recover_exactly_one_linux_execution(self):
        async def exercise():
            command = 'printf x >> /tmp/' + self.scope + '; sleep 1; printf collection-finished'
            args = {'container_name': CONTAINER, 'command': command}
            async with self.backend() as first:
                original = first.backend.call_tool
                async def lose_acknowledgment(name, arguments):
                    await original(name, arguments)
                    raise TimeoutError('fixture loses the successful launch acknowledgment')
                first.backend.call_tool = lose_acknowledgment
                pending = json.loads(await first.call_tool('execute_command_in_container', args))
            self.assertEqual(pending['state'], 'outcome_unknown')
            async with self.backend() as second:
                result = json.loads(await second.call_tool('execute_command_in_container', args))
                self.assertEqual(result['execution_id'], pending['execution_id'])
                self.assertEqual((result['state'], result['exit_code']), ('completed', 0))
                self.assertIn('collection-finished', result['output'])
                recovered = json.loads(await second.call_tool('get_exegol_execution',
                                       {'execution_id': result['execution_id']}))
                self.assertEqual(recovered['output'], result['output'])
            marker = subprocess.run(['docker', 'exec', CONTAINER, 'cat', '/tmp/' + self.scope],
                                    capture_output=True, check=True, timeout=5)
            self.assertEqual(marker.stdout, b'x')
        asyncio.run(exercise())

    def test_real_http_cancel_after_disconnect_confirms_linux_termination(self):
        def stop_waiting(event):
            if event['status'] == 'running':
                raise JobInterrupted('fixture stops AI polling')
        async def exercise():
            async with self.backend(stop_waiting) as first:
                with self.assertRaises(JobInterrupted):
                    await first.call_tool('execute_command_in_container',
                                          {'container_name': CONTAINER, 'command': 'sleep 30'})
                execution_id = first.store.list()[0]['execution_id']
                self.assertEqual(first.store.get(execution_id)['state'], 'running')
            async with self.backend() as second:
                result = json.loads(await second.call_tool('cancel_exegol_execution', {'execution_id': execution_id}))
                deadline = time.monotonic() + 8
                while result['state'] in ('starting', 'running') and time.monotonic() < deadline:
                    await asyncio.sleep(.1)
                    result = json.loads(await second.call_tool('get_exegol_execution', {'execution_id': execution_id}))
                self.assertEqual(result['state'], 'cancelled')
                self.assertTrue(result['cancel_requested'])
        asyncio.run(exercise())

    def test_real_http_command_deadline_has_known_outcome(self):
        self.timeout = 1
        async def exercise():
            async with self.backend() as backend:
                result = json.loads(await backend.call_tool('execute_command_in_container',
                                    {'container_name': CONTAINER, 'command': 'sleep 30'}))
                self.assertEqual(result['state'], 'timed_out')
                self.assertLess(result['exit_code'], 0)
        asyncio.run(exercise())

    def test_production_session_combines_real_rmm_stdio_and_exegol_http(self):
        from rmm_mcp_client import run_with_mcp_session
        async def chat(session):
            names = {tool['function']['name'] for tool in session.openai_tools}
            self.assertTrue({'list_sessions', 'execute_command_in_container',
                             'get_exegol_execution'} <= names)
            self.assertIn('get_exegol_execution', session.external_tool_names)
            result = json.loads(await session.call_tool('execute_command_in_container',
                                {'container_name': CONTAINER, 'command': 'printf composite-session-ready'}))
            self.assertEqual((result['state'], result['exit_code']), ('completed', 0))
            self.assertIn('composite-session-ready', result['output'])
            return result
        result = run_with_mcp_session('http://127.0.0.1:1', 'fixture-token', chat,
                                      exegol_enabled=True, exegol_mcp_url=self.url)
        self.assertEqual(result['state'], 'completed')
