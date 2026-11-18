"""Execute the container supervisor protocol against isolated host fixtures."""
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import rmm_exegol_supervisor as module


class RequestTests(unittest.TestCase):
    def test_request_is_encoded_and_round_trips_shell_metacharacters(self):
        command = 'echo "secret $(touch /never-created)"; printf "\\xff"'
        parts = shlex.split(module.build_request('a' * 64, 'b' * 32, 'start', command=command))
        self.assertEqual(parts[:2], ['python3', '-c'])
        self.assertNotIn(command, parts[2])
        self.assertEqual(json.loads(base64.b64decode(parts[3]))['command'], command)

    def test_invalid_ids_pages_deadlines_and_commands_are_rejected(self):
        for arguments in [
                {'scope': '../escape'}, {'execution_id': '../escape'}, {'action': '_supervise'},
                {'offset': -1}, {'offset': True}, {'limit': 65537}, {'limit': 0},
                {'timeout_seconds': 0}, {'timeout_seconds': 86401}, {'timeout_seconds': True},
                {'command': ''}, {'command': 'nul\x00'}, {'command': 'x' * 65537}]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                module.build_request(**{'scope': 'a' * 64, 'execution_id': 'b' * 32,
                                        'action': 'start', 'command': 'true', **arguments})

    def test_unicode_request_avoids_ascii_expansion_and_controls_obey_encoded_limit(self):
        command = '\U0001f600' * 16000
        encoded = module.build_request('a' * 64, 'b' * 32, 'start', command=command)
        self.assertLessEqual(len(encoded.encode()), 120000)
        self.assertEqual(json.loads(base64.b64decode(shlex.split(encoded)[3]))['command'], command)
        with self.assertRaisesRegex(ValueError, 'encoded execution request'):
            module.build_request('a' * 64, 'b' * 32, 'start', command='\x01' * 30000)

    def test_encoded_control_boundary_is_enforced_after_shell_wrapping(self):
        low, high = 1, 30000
        while low + 1 < high:
            count = (low + high) // 2
            try:
                result = module.build_request('a' * 64, 'b' * 32, 'start', command='\x01' * count)
            except ValueError:
                high = count
            else:
                self.assertLessEqual(len(result.encode()), 120000)
                low = count
        with self.assertRaisesRegex(ValueError, 'encoded execution request'):
            module.build_request('a' * 64, 'b' * 32, 'start', command='\x01' * high)


@unittest.skipUnless(shlex and os.name == 'posix' and Path('/bin/zsh').exists(), 'POSIX zsh fixture required')
class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote_root = self.root / 'executions'
        self.home = self.root / 'home'
        self.home.mkdir()
        (self.home / '.zshrc').write_text('alias fixture_alias="printf alias-ready"\n')
        self.source = module.REMOTE_SOURCE.replace(
            "ROOT = Path('/var/tmp') / ('minimal-rmm-executions-' + str(os.getuid()))",
            'ROOT = Path(' + repr(str(self.remote_root)) + ')')
        if not hasattr(os, 'waitid'):
            # macOS Python omits waitid; exercise the remaining real subprocess
            # protocol with a fixture-only shim. Linux retains the production
            # unreaped-child identity guarantee and group-completion test below.
            self.source = self.source.replace(
                'os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)',
                'process.poll()')
            # The macOS test sandbox prohibits killpg even for an owned child.
            # Exercise direct-child termination here; group cleanup is asserted
            # separately against the unmodified Linux worker.
            self.source = self.source.replace('os.killpg(process.pid, sig)', 'os.kill(process.pid, sig)')
            self.source = self.source.replace('os.killpg(process.pid, signal.SIGKILL)', 'None')
        self.scope = 'a' * 64
        self.execution_id = 'b' * 32
        self.environment = dict(os.environ, HOME=str(self.home))

    def request(self, action, **kwargs):
        execution_id = kwargs.pop('execution_id', self.execution_id)
        success = kwargs.pop('success', True)
        with patch.object(module, 'REMOTE_SOURCE', self.source):
            parts = shlex.split(module.build_request(self.scope, execution_id, action, **kwargs))
        result = subprocess.run([sys.executable, *parts[1:]], env=self.environment,
                                capture_output=True, text=True, timeout=5)
        self.assertTrue(result.stdout, result.stderr)
        payload = result.stdout
        marker = 'RMM_EXECUTION:' + execution_id + ':'
        if success:
            self.assertEqual(payload.count(marker), 1, payload)
            payload = payload.split(marker, 1)[1]
        response = json.loads(payload)
        if success:
            self.assertEqual(result.returncode, 0, response)
        else:
            self.assertNotEqual(result.returncode, 0, response)
        return response

    def finish(self, **kwargs):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.request('status', **kwargs)
            if result['state'] in {'completed', 'failed', 'timed_out', 'cancelled', 'outcome_unknown'}:
                return result
            time.sleep(0.05)
        self.fail('supervisor did not complete')

    def test_detached_execution_aliases_output_exit_and_recovery(self):
        started = time.monotonic()
        result = self.request('start', command='sleep 0.3; fixture_alias; printf "stderr" >&2; exit 7')
        self.assertIn(result['state'], {'starting', 'running'})
        self.assertLess(time.monotonic() - started, 2)
        # Each status is a fresh process, independent of the submission process.
        result = self.finish()
        self.assertEqual((result['state'], result['exit_code']), ('failed', 7))
        self.assertEqual(base64.b64decode(result['output_b64']), b'alias-readystderr')
        page = self.request('status', offset=6, limit=4)
        self.assertEqual(base64.b64decode(page['output_b64']), b'read')
        self.assertEqual(page['next_offset'], 10)

    def test_binary_output_and_byte_offsets(self):
        self.request('start', command="printf '\\377\\303\\251'", timeout_seconds=4)
        result = self.finish(limit=2)
        self.assertEqual(base64.b64decode(result['output_b64']), b'\xff\xc3')
        self.assertEqual(result['next_offset'], 2)
        result = self.request('status', offset=2)
        self.assertEqual(base64.b64decode(result['output_b64']), b'\xa9')

    def test_short_output_writes_preserve_every_byte(self):
        self.source = self.source.replace('import time\n',
            'import time\n_original_write = os.write\n'
            'def _short_write(fd, data):\n'
            '    return _original_write(fd, data[:3])\n'
            'os.write = _short_write\n', 1)
        payload = 'short-writes-must-preserve-this-entire-output'
        self.request('start', command='printf %s ' + shlex.quote(payload))
        result = self.finish()
        self.assertEqual((result['state'], result['exit_code']), ('completed', 0))
        self.assertEqual(base64.b64decode(result['output_b64']), payload.encode())
        self.assertEqual(result['output_bytes'], len(payload))

    def test_output_write_without_progress_fails_execution(self):
        self.source = self.source.replace('import time\n',
            'import time\nos.write = lambda fd, data: 0\n', 1)
        self.request('start', command='printf output')
        result = self.finish()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error'], 'supervisor_failed')

    def test_simultaneous_duplicate_start_never_replays(self):
        marker = self.root / 'marker'
        command = 'printf x >> ' + shlex.quote(str(marker)) + '; sleep 0.2'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request('start', command=command), range(2)))
        self.assertEqual(len(results), 2)
        self.assertEqual(self.finish()['exit_code'], 0)
        self.request('start', command=command)
        self.assertEqual(marker.read_text(), 'x')

    def test_execution_deadline_terminates_command(self):
        self.request('start', command='sleep 30', timeout_seconds=1)
        result = self.finish()
        self.assertEqual(result['state'], 'timed_out')
        self.assertLess(result['exit_code'], 0)

    def test_status_probe_lock_during_launch_does_not_drop_execution(self):
        directory = self.remote_root / self.scope / self.execution_id
        self.remote_root.mkdir(mode=0o700)
        directory.parent.mkdir(mode=0o700)
        directory.mkdir(mode=0o700)
        lock = os.open(directory / 'worker.lock', os.O_RDWR | os.O_CREAT, 0o600)
        marker = self.root / 'marker'
        command = 'printf x >> ' + shlex.quote(str(marker))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.request('start', command=command)
            time.sleep(0.2)
            self.assertFalse(marker.exists())
        finally:
            os.close(lock)
        self.assertEqual(self.finish()['exit_code'], 0)
        self.request('start', command=command)
        self.assertEqual(marker.read_text(), 'x')

    def test_conflicting_repeated_id_is_rejected_without_execution(self):
        self.request('start', command='true')
        self.finish()
        marker = self.root / 'must-not-exist'
        result = self.request('start', command='touch ' + shlex.quote(str(marker)), success=False)
        self.assertEqual(result['state'], 'error')
        self.assertFalse(marker.exists())

    def test_foreign_scope_cannot_read_execution(self):
        self.request('start', command='printf private')
        self.finish()
        self.scope = 'c' * 64
        self.assertEqual(self.request('status')['state'], 'not_found')
        self.assertFalse((self.remote_root / self.scope).exists())

    def test_cancel_request_is_observed_by_supervisor(self):
        self.request('start', command='sleep 30', timeout_seconds=10)
        self.request('cancel')
        result = self.finish()
        self.assertEqual(result['state'], 'cancelled')
        self.assertTrue(result['cancel_requested'])

    def test_missing_status_does_not_create_directories(self):
        self.assertEqual(self.request('status')['state'], 'not_found')
        self.assertFalse(self.remote_root.exists())

    def test_private_permissions_and_symlink_rejection(self):
        self.request('start', command='true')
        self.finish()
        directory = self.remote_root / self.scope / self.execution_id
        for path in [self.remote_root, directory.parent, directory]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        state = directory / 'state.json'
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        state.unlink()
        state.symlink_to(self.home / '.zshrc')
        self.assertEqual(self.request('status', success=False)['state'], 'error')

    def test_stale_dispatch_intent_is_unknown_and_not_replayed(self):
        self.request('start', command='true')
        self.finish()
        state = self.remote_root / self.scope / self.execution_id / 'state.json'
        data = json.loads(state.read_text())
        data.update(state='starting', created_at=time.time() - 60)
        state.write_text(json.dumps(data))
        self.assertEqual(self.request('status')['state'], 'outcome_unknown')
        self.assertEqual(self.request('start', command='true')['state'], 'outcome_unknown')

    def test_dead_running_worker_is_unknown_and_not_replayed(self):
        marker = self.root / 'once'
        command = 'printf x >> ' + shlex.quote(str(marker))
        self.request('start', command=command)
        self.finish()
        state = self.remote_root / self.scope / self.execution_id / 'state.json'
        data = json.loads(state.read_text())
        data.update(state='running', exit_code=None)
        state.write_text(json.dumps(data))
        self.assertEqual(self.request('status')['state'], 'outcome_unknown')
        self.assertEqual(self.request('start', command=command)['state'], 'outcome_unknown')
        self.assertEqual(marker.read_text(), 'x')

    def test_output_is_capped_without_limiting_command_artifact_sizes(self):
        self.source = self.source.replace('64 * 1024 * 1024', '1024')
        self.request('start', command='while true; do printf 1234567890; done')
        result = self.finish()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error'], 'output_limit_exceeded')
        self.assertEqual(result['output_bytes'], 1024)

    @unittest.skipUnless(hasattr(os, 'waitid'), 'Linux unreaped-child process-group contract')
    def test_background_descendant_cannot_write_after_completion(self):
        marker = self.root / 'late-marker'
        self.request('start', command='(sleep 1; touch ' + shlex.quote(str(marker)) + ') & exit 0')
        self.assertEqual(self.finish()['state'], 'completed')
        time.sleep(1.2)
        self.assertFalse(marker.exists())
