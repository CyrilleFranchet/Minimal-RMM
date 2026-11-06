"""Exercise real queues, registration and result matching with simulated beacons."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

import test_agent_workspace_binding as binding_fixtures


class DeploymentVerificationTests(unittest.TestCase):
    def run_deployment(self, kind, *, acknowledge=True, register=True):
        server = binding_fixtures.AgentRegistrationWorkspaceTests().server()
        server.staging_lock = threading.Lock()
        server.staging_entries = {}
        server.running = True
        server._history_append_event = Mock()
        server.register_session('go-source-session', 'source', 'user', agent_type='go', workspace_id='alice')
        source_id = 'go-source-session'
        target_id = None
        stop = threading.Event()
        errors = []

        def beacon():
            nonlocal target_id
            try:
                while not stop.is_set():
                    for sid in [source_id] + ([target_id] if target_id and register else []):
                        command, _ = server.get_command(sid)
                        if not command:
                            continue
                        session = server.get_session(sid)
                        if command.startswith(('__SPAWN__\n', '__LATERAL__\n')):
                            payload = json.loads(command.split('\n', 1)[1])
                            target_id = payload['session_id']
                            if register:
                                server.register_session(target_id, 'target', 'user', agent_type='go', workspace_id=payload['workspace_id'])
                            if acknowledge:
                                reply = (f'spawned session={target_id} pid=42' if kind == 'spawn' else
                                         f'lateral started: method=psexec host=target target session={target_id}')
                                server._record_event(session, 'output', reply)
                        else:
                            raise AssertionError(f'unexpected automatic command: {command.split()[0]}')
                    stop.wait(.001)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=beacon, daemon=True)
        thread.start()
        try:
            if kind == 'spawn':
                result, status = server.spawn_session(source_id, spawn_timeout=.05, register_timeout=.05, verify_timeout=.2)
            else:
                # This suite tests registration proof, not executable builds.
                with patch('server_rmm.latest_go_agent_binary', return_value=__file__):
                    result, status = server.lateral_move(source_id, 'psexec', 'target', exec_timeout=.05, register_timeout=.05, verify_timeout=.2)
        finally:
            stop.set()
            thread.join(1)
        if errors:
            raise errors[0]
        self.assertEqual(status, 'ok')
        self.assertIsNotNone(target_id)
        return result, server, target_id

    def test_new_workspace_agent_and_fresh_response_confirm_deployment(self):
        for kind in ('spawn', 'lateral'):
            with self.subTest(kind=kind):
                result, server, target_id = self.run_deployment(kind)
                self.assertTrue(result['registered'])
                self.assertTrue(result['verified'])
                self.assertIsNotNone(server.resolve_workspace_session('alice', target_id))
                self.assertIsNone(server.resolve_workspace_session('bob', target_id))

    def test_new_registration_is_authoritative_when_launch_ack_is_lost(self):
        for kind in ('spawn', 'lateral'):
            with self.subTest(kind=kind):
                result, _, _ = self.run_deployment(kind, acknowledge=False)
                self.assertTrue(result['registered'])
                self.assertTrue(result['verified'])
                self.assertEqual(result['submission_status'], 'timeout')

    def test_acknowledgement_without_new_registration_is_not_success(self):
        result, _, _ = self.run_deployment('spawn', register=False)
        self.assertFalse(result['registered'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['verified'])



if __name__ == '__main__':
    unittest.main()
