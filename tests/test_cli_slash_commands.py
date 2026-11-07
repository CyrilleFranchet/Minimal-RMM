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
        client.make_token.return_value = (200, {'ok': True})
        client.spawnas.return_value = (200, {'ok': True})
        client.runas.return_value = (200, {'ok': True, 'output': 'done'})
        client.creds_del.return_value = (200, {'message': 'deleted'})
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

    def test_creds_preserves_quoted_empty_and_literal_passwords(self):
        for password, expected in [("'two words\\path'", "two words\\path"), ("''", "")]:
            client = Mock(base_url='http://example.test')
            client.list_sessions.return_value = (200, {'sessions': []})
            client.get_session.return_value = (200, {})
            client.creds_add.return_value = (200, {'cred_id': 'cred-123'})
            prompt = Mock()
            prompt.prompt.side_effect = [f'/creds add LAB\\alice {password}', EOFError()]
            with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                    patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                    patch.object(rmm_cli.threading, 'Thread'), \
                    patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
                rmm_cli.run_interactive(client, {'current_session': 'session-id'})
            client.creds_add.assert_called_once_with('LAB', 'alice', expected)
            warn.assert_not_called()

    def test_creds_rejects_extra_arguments_and_bad_quoting(self):
        client, _, _ = self.run_lines(['/creds list extra', '/creds add alice pw extra',
                                      '/creds del id extra', "/creds add alice 'unterminated"])
        client.creds_add.assert_not_called()
        client.creds_del.assert_not_called()
        client.creds_list.assert_not_called()

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

    def test_spawn_dispatches_and_reports_verification(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.spawn_session.return_value = (200, {
            'ok': True, 'spawned_session_id': 'go-spawn-child-1', 'spawn_pid': 4242,
            'verify': True, 'registered': True, 'parent_responsive': True,
            'spawned_responsive': True, 'verified': True,
        })
        prompt = Mock()
        prompt.prompt.side_effect = ['/spawn', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt) as factory, \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.spawn_session.assert_called_once_with('session-id')
        warn.assert_not_called()

    def test_spawn_failure_warns_without_raising(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.spawn_session.return_value = (409, {'error': 'agent_type_unsupported'})
        prompt = Mock()
        prompt.prompt.side_effect = ['/spawn', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.spawn_session.assert_called_once_with('session-id')
        self.assertIn('agent_type_unsupported', warn.call_args.args[0])

    def test_make_token_passes_credentials_as_parameters(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.make_token.return_value = (200, {
            'ok': True, 'identity': 'LAB\\alice',
            'output': 'token created: LAB\\alice; spawned commands now use this network identity',
        })
        prompt = Mock()
        prompt.prompt.side_effect = ['/make_token LAB\\alice s3cret-pw', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.make_token.assert_called_once_with('session-id', 'LAB', 'alice', 's3cret-pw')
        warn.assert_not_called()

    def test_make_token_requires_identity_and_password(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        prompt = Mock()
        prompt.prompt.side_effect = ['/make_token', '/make_token LAB\\alice', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.make_token.assert_not_called()
        self.assertEqual(warn.call_count, 2)

    def test_make_token_accepts_stored_credential_id(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.make_token.return_value = (200, {'ok': True, 'output': 'token created'})
        prompt = Mock()
        prompt.prompt.side_effect = ['/make_token -i cred-123', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.make_token.assert_called_once_with('session-id', cred_id='cred-123')
        warn.assert_not_called()

    def test_all_stored_credentials_require_explicit_id(self):
        client, _, warn = self.run_lines([
            '/make_token -i cred-123', '/spawnas -i cred-123',
            '/runas -i cred-123 whoami /all', '/creds del -i cred-123',
        ])
        client.make_token.assert_called_once_with('session-id', cred_id='cred-123')
        client.spawnas.assert_called_once_with('session-id', cred_id='cred-123')
        client.runas.assert_called_once_with('session-id', 'whoami /all', cred_id='cred-123')
        client.creds_del.assert_called_once_with('cred-123')
        warn.assert_not_called()

    def test_domainless_direct_credentials_and_empty_password(self):
        client, _, warn = self.run_lines([
            '/make_token alice ""', '/spawnas alice "two words\\path"',
            '/runas alice "" whoami /all',
        ])
        client.make_token.assert_called_once_with('session-id', '', 'alice', '')
        client.spawnas.assert_called_once_with('session-id', user='alice', password='two words\\path')
        client.runas.assert_called_once_with('session-id', 'whoami /all', user='alice', password='')
        warn.assert_not_called()

    def test_invalid_credential_forms_never_send_requests(self):
        lines = [
            '/make_token cred-123', '/spawnas cred-123', '/runas cred-123 whoami',
            '/creds del cred-123', '/creds del -i', '/creds del -i ""',
            '/creds del -i cred-123 extra',
        ]
        for command in ('make_token', 'spawnas', 'runas'):
            lines.extend([f'/{command} -i', f'/{command} -i ""',
                          f'/{command} -i "" whoami', f'/{command} --user alice'])
        lines.extend(['/make_token -i cred-123 alice pw', '/spawnas -i cred-123 alice pw',
                      '/runas -i cred-123 ""'])
        client, _, warn = self.run_lines(lines)
        for name in ('make_token', 'spawnas', 'runas', 'creds_del'):
            getattr(client, name).assert_not_called()
        self.assertEqual(warn.call_count, len(lines))

    def test_make_token_failure_warns_with_agent_output(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.make_token.return_value = (200, {
            'ok': False, 'identity': 'LAB\\alice', 'output': 'Make token failed: LogonUserW failed: access denied',
        })
        prompt = Mock()
        prompt.prompt.side_effect = ['/make_token LAB\\alice wrong-pw', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.make_token.assert_called_once_with('session-id', 'LAB', 'alice', 'wrong-pw')
        self.assertIn('LogonUserW', warn.call_args.args[0])

    def test_rev2self_dispatches_to_selected_session(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.rev2self.return_value = (200, {'ok': True, 'output': 'token reverted: LAB\\alice'})
        prompt = Mock()
        prompt.prompt.side_effect = ['/rev2self', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.rev2self.assert_called_once_with('session-id')
        warn.assert_not_called()

    def test_lateral_dispatches_and_reports_verification(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.lateral_move.return_value = (200, {
            'ok': True, 'method': 'psexec', 'host': 'ws01',
            'target_session_id': 'go-src-lateral-1', 'verify': True,
            'registered': True, 'source_responsive': True,
            'target_responsive': True, 'verified': True,
            'output': 'lateral started: method=psexec host=ws01 target session=go-src-lateral-1',
        })
        prompt = Mock()
        prompt.prompt.side_effect = ['/lateral psexec ws01', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.lateral_move.assert_called_once_with('session-id', 'psexec', 'ws01')
        warn.assert_not_called()

    def test_lateral_rejects_wrong_arity_and_unknown_method(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.lateral_move.return_value = (400, {'error': 'invalid_method'})
        prompt = Mock()
        prompt.prompt.side_effect = ['/lateral', '/lateral psexec', '/lateral ssh ws01', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        self.assertEqual(warn.call_count, 3)
        client.lateral_move.assert_called_once_with('session-id', 'ssh', 'ws01')

    def test_uid_dispatches_and_prints_identity_lines(self):
        client = Mock(base_url='http://example.test')
        client.list_sessions.return_value = (200, {'sessions': []})
        client.get_session.return_value = (200, {})
        client.session_uid.return_value = (200, {
            'ok': True, 'output': 'agent user: LAB\\alice\nnetwork identity: none (spawned commands use the agent identity)',
        })
        prompt = Mock()
        prompt.prompt.side_effect = ['/uid', EOFError()]
        with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                patch.object(rmm_cli.threading, 'Thread'), \
                patch.object(rmm_cli, 'say') as say, patch.object(rmm_cli, 'warn') as warn:
            rmm_cli.run_interactive(client, {'current_session': 'session-id'})
        client.session_uid.assert_called_once_with('session-id')
        warn.assert_not_called()
        printed = [call.args[0] for call in say.call_args_list if call.args]
        self.assertTrue(any('agent user: LAB\\alice' in line for line in printed))

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


class IdentityQuotingTests(unittest.TestCase):
    def test_interactive_passwords_preserve_spaces_backslashes_and_empty_values(self):
        cases = [
            (r'''/make_token LAB\alice 'two words\path#1' ''', 'two words\\path#1'),
            ('/make_token LAB\\alice ""', ''),
            ('''/make_token LAB\\alice "it's a password"''', "it's a password"),
        ]
        for line, password in cases:
            with self.subTest(line=line):
                client = Mock(base_url='http://example.test')
                client.list_sessions.return_value = (200, {'sessions': []})
                client.make_token.return_value = (200, {'ok': True, 'output': 'token created'})
                prompt = Mock()
                prompt.prompt.side_effect = [line, EOFError()]
                with patch('prompt_toolkit.PromptSession', return_value=prompt), \
                        patch('prompt_toolkit.patch_stdout.patch_stdout', contextlib.nullcontext), \
                        patch.object(rmm_cli.threading, 'Thread'), \
                        patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'warn') as warn:
                    rmm_cli.run_interactive(client, {'current_session': 'session-id'})
                client.make_token.assert_called_once_with('session-id', 'LAB', 'alice', password)
                warn.assert_not_called()

    def test_unclosed_quotes_are_rejected(self):
        with self.assertRaises(ValueError):
            rmm_cli.parse_identity_arguments('/make_token alice "unfinished')

    def test_argument_completion(self):
        completer = rmm_cli.RmmCliPTKCompleter({'_completion_sessions': ['abc12345']})
        def matches(text):
            return [item.text for item in completer.get_completions(Document(text), CompleteEvent())]
        self.assertEqual(matches('/spawn a'), ['abc12345'])
        self.assertEqual(matches('/lateral '), ['psexec ', 'wmiexec ', 'winrm '])
        self.assertEqual(matches('/lateral w'), ['wmiexec ', 'winrm '])
        self.assertEqual(matches('/lateral winrm '), [])
        with patch.object(rmm_cli, 'readline') as readline:
            completer = rmm_cli.RmmCliCompleter({'_completion_sessions': ['abc12345']})
            readline.get_line_buffer.return_value = '/spawn a'
            self.assertEqual(completer.complete('a', 0), 'abc12345')
            readline.get_line_buffer.return_value = '/lateral w'
            self.assertEqual(completer.complete('w', 0), 'wmiexec ')
            self.assertEqual(completer.complete('w', 1), 'winrm ')


class DeploymentExitStatusTests(unittest.TestCase):
    def test_spawn_and_lateral_fail_without_registration_including_json(self):
        from types import SimpleNamespace
        for action in ('spawn', 'lateral'):
            for json_mode in (False, True):
                with self.subTest(action=action, json_mode=json_mode):
                    client = Mock()
                    response = {'ok': False, 'registered': False, 'verified': False,
                                'spawned_session_id': 'expected-child-session', 'spawn_pid': None}
                    getattr(client, 'spawn_session' if action == 'spawn' else 'lateral_move').return_value = (200, response)
                    args = SimpleNamespace(session='source-session', json=json_mode, no_verify=False,
                        spawn_timeout=120, exec_timeout=300, register_timeout=60, verify_timeout=120,
                        method='winrm', host='target')
                    with patch.object(rmm_cli, 'say'), patch.object(rmm_cli, 'print_json'), \
                            contextlib.redirect_stderr(__import__('io').StringIO()), self.assertRaises(SystemExit) as error:
                        getattr(rmm_cli, 'cmd_' + action)(client, {}, args)
                    self.assertEqual(error.exception.code, 1)

    def test_no_verify_is_accepted_as_pending_without_claiming_success(self):
        from types import SimpleNamespace
        client = Mock()
        client.spawn_session.return_value = (200, {'ok': False, 'accepted': True, 'pending': True,
            'registered': None, 'verify': False, 'spawned_session_id': 'expected-child-session', 'spawn_pid': None})
        args = SimpleNamespace(session='source-session', json=False, no_verify=True,
            spawn_timeout=120, register_timeout=60, verify_timeout=120)
        with patch.object(rmm_cli, 'say') as say:
            rmm_cli.cmd_spawn(client, {}, args)
        self.assertIn('unconfirmed', str(say.call_args_list))


if __name__ == "__main__":
    unittest.main()
