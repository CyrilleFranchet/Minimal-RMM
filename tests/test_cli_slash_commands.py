"""Exercise interactive routing and live terminal completion without an agent."""
import asyncio
import contextlib
import unittest
from unittest.mock import Mock, patch

import rmm_cli
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput


class SlashCommandTests(unittest.TestCase):
    def run_lines(self, lines):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.queue_command.return_value = (200, {})
        client.queue_screenshot.return_value = (200, {})
        client.health.return_value = (200, {'sessions': 0})
        prompt = Mock()
        prompt.prompt.side_effect = [*lines, EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt) as factory, \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        return client, factory.call_args.kwargs, warn

    def test_slash_dispatch_and_bare_command_collisions(self):
        client, _, _ = self.run_lines(['/screenshot', 'screenshot', 'help', 'quit', '/health'])
        client.queue_screenshot.assert_called_once_with('session-id')
        self.assertEqual([call.args for call in client.queue_command.call_args_list],
                         [('session-id', 'screenshot'), ('session-id', 'help'), ('session-id', 'quit')])
        client.health.assert_called_once_with()

    def test_unknown_and_malformed_slashes_never_reach_agent(self):
        client, _, warn = self.run_lines(['/', '//screenshot', '/ screenshot', '/unknown', '/assets'])
        client.queue_command.assert_not_called()
        client.queue_screenshot.assert_not_called()
        self.assertEqual(warn.call_count, 5)

    def test_explicit_run_preserves_leading_slash_and_internal_bloodhound_parser(self):
        with patch.object(rmm_cli, 'dispatch_interactive_bloodhound') as dispatch:
            client, _, _ = self.run_lines(['/run /bin/echo hello', '/bloodhound status --run-id abc'])
        client.queue_command.assert_called_once_with('session-id', '/bin/echo hello')
        self.assertEqual(dispatch.call_args.args[2], 'bloodhound status --run-id abc')

    def test_case_and_whitespace(self):
        client, _, _ = self.run_lines(['  /ScReEnShOt  ', '  whoami  '])
        client.queue_screenshot.assert_called_once_with('session-id')
        client.queue_command.assert_called_once_with('session-id', 'whoami')

    def test_completer_prefixes_and_arguments(self):
        completer = rmm_cli.RmmCliPTKCompleter({'_completion_sessions': ['abc12345']})
        def matches(text):
            return [item.text for item in completer.get_completions(Document(text), CompleteEvent())]
        self.assertEqual(matches('/'), ['/' + cmd + ' ' for cmd in rmm_cli.RMM_CLI_COMMANDS])
        self.assertEqual(matches('/SC'), ['/screenshot '])
        self.assertEqual(matches('/use a'), ['abc12345 '])
        self.assertEqual(matches('screenshot'), [])
        self.assertEqual(matches('use a'), [])
        self.assertEqual(matches('/unknown'), [])

    def test_live_keyboard_menu(self):
        _, options, _ = self.run_lines([])
        options.pop('history')

        async def scenario():
            with create_pipe_input() as pipe:
                prompt = PromptSession(input=pipe, output=DummyOutput(), **options)
                task = asyncio.create_task(prompt.prompt_async('RMM > '))
                async def wait_for(predicate):
                    for _ in range(300):
                        if predicate():
                            return
                        await asyncio.sleep(.01)
                    self.fail('Keyboard state did not converge')
                try:
                    pipe.send_text('/')
                    await wait_for(lambda: prompt.default_buffer.complete_state is not None)
                    self.assertEqual(len(prompt.default_buffer.complete_state.completions), len(rmm_cli.RMM_CLI_COMMANDS))
                    pipe.send_text('sc')
                    await wait_for(lambda: prompt.default_buffer.text == '/sc' and
                                   prompt.default_buffer.complete_state is not None and
                                   len(prompt.default_buffer.complete_state.completions) == 1)
                    pipe.send_text('\x1b')
                    await wait_for(lambda: prompt.default_buffer.complete_state is None)
                    self.assertEqual(prompt.default_buffer.text, '/sc')
                    pipe.send_text('r')
                    await wait_for(lambda: prompt.default_buffer.complete_state is not None)
                    pipe.send_text('\x1b[B')
                    await wait_for(lambda: prompt.default_buffer.text == '/screenshot ')
                    pipe.send_text('\t')
                    await wait_for(lambda: prompt.default_buffer.complete_state is None)
                    self.assertFalse(task.done())
                    pipe.send_text('\r')
                    self.assertEqual(await asyncio.wait_for(task, 2), '/screenshot ')
                finally:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
        asyncio.run(scenario())
