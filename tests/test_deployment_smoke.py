"""Start real single-preset services on loopback without Exegol or lab traffic."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from rmm_deployment import prepare, CODE
from rmm_settings import SettingsStore


class DeploymentSmokeTests(unittest.TestCase):
    def test_real_single_preset_workspace_cli_auth_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'deployment'
            reservations = [socket.socket() for _ in range(3)]
            for reservation in reservations:
                reservation.bind(('127.0.0.1', 0))
            admin, beacon, workspace = [sock.getsockname()[1] for sock in reservations]
            for reservation in reservations:
                reservation.close()
            prepare(root, 'single', None, f'http://127.0.0.1:{admin}')
            central_settings = SettingsStore(root / 'central/settings.json', 'server', environment={})
            central_settings.update({'settings': {'RMM_ADMIN_PORT': admin, 'RMM_BEACON_PORT': beacon,
                                     'RMM_PROFILE_AUTOSTART': False}})
            local_settings = SettingsStore(root / 'workspaces/default/settings.json', 'workspace', environment={})
            local_settings.update({'settings': {'RMM_WORKSPACE_PORT': workspace}})
            local_token = local_settings.saved_at_start['RMM_WORKSPACE_LOCAL_TOKEN']
            admin_token = central_settings.saved_at_start['RMM_API_TOKEN']
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith('RMM_') and key not in ('OPENAI_API_KEY', 'CLOUDFLARED_BIN')}
            environment['PYTHONUNBUFFERED'] = '1'
            log_path = Path(directory) / 'service.log'
            with log_path.open('wb') as output:
                process = subprocess.Popen([sys.executable, str(CODE / 'rmm_deployment.py'), 'run',
                    '--root', str(root), '--component', 'single'], env=environment,
                    stdout=output, stderr=subprocess.STDOUT)
                try:
                    def get(port, token):
                        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/v1/health',
                                                         headers={'Authorization': 'Bearer ' + token})
                        with urllib.request.urlopen(request, timeout=1) as response:
                            return json.load(response)
                    deadline = time.monotonic() + 15
                    while True:
                        self.assertIsNone(process.poll(), 'Single preset exited before listeners became ready')
                        try:
                            central = get(admin, admin_token)
                            local = get(workspace, local_token)
                            break
                        except (OSError, urllib.error.URLError):
                            if time.monotonic() >= deadline:
                                self.fail('Single preset listeners did not become ready')
                            time.sleep(.1)
                    self.assertEqual(central['service_role'], 'control_plane')
                    self.assertEqual(local['workspace_id'], 'default')
                    with self.assertRaises(urllib.error.HTTPError) as rejected:
                        get(workspace, admin_token)
                    self.assertEqual(rejected.exception.code, 401)
                    result = subprocess.check_output([sys.executable, str(CODE / 'rmm_cli.py'),
                        '--url', f'http://127.0.0.1:{workspace}', '--token', local_token, 'health', '--json'],
                        env=environment, timeout=5)
                    self.assertEqual(json.loads(result)['workspace_id'], 'default')
                    self.assertFalse((root / 'central/bloodhound').exists())
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        self.fail('Single preset did not stop its services promptly')
                self.assertEqual(process.returncode, 0)
