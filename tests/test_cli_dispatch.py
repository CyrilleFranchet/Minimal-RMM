"""Exercise real CLI parsing and dispatch without running a remote command."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import rmm_cli


class CliDispatchTests(unittest.TestCase):
    def invoke(self, arguments, client):
        """Capture the real CLI entry point with a deterministic API client."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['rmm_cli.py', *arguments]), \
                patch.object(rmm_cli, 'load_state', return_value={}), \
                patch.object(rmm_cli, 'ensure_client', return_value=(client, {})), \
                patch.object(rmm_cli, 'run_interactive') as interactive, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rmm_cli.main()
            interactive.assert_not_called()
        return stdout.getvalue(), stderr.getvalue()

    def test_run_payload_named_health_is_queued(self):
        client = Mock()
        client.queue_command.return_value = (200, {'ok': True})
        output, _ = self.invoke(['run', 'health', '--session', 'session-id', '--json'], client)
        client.queue_command.assert_called_once_with('session-id', 'health', 'oneshot')
        client.health.assert_not_called()
        self.assertEqual(json.loads(output), {'ok': True})

    def test_exec_file_dispatches_and_keeps_json_stdout_parseable(self):
        client = Mock()
        client.exec_command.return_value = (200, {'event': {'body': 'done'}})
        with tempfile.TemporaryDirectory() as root:
            command = Path(root) / 'command.txt'
            command.write_text('whoami\n')
            output, diagnostics = self.invoke(['exec', '--command-file', str(command),
                '--session', 'session-id', '--json'], client)
        client.exec_command.assert_called_once_with('session-id', 'whoami', timeout=120)
        self.assertEqual(json.loads(output), {'body': 'done'})
        self.assertIn('Waiting for agent', diagnostics)

    def test_health_subcommand_still_dispatches_health(self):
        client = Mock()
        client.health.return_value = (200, {'ok': True})
        output, _ = self.invoke(['health', '--json'], client)
        self.assertEqual(json.loads(output), {'status': 200, 'ok': True})
        client.health.assert_called_once_with()
