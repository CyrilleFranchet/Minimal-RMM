"""Verify compiled debug mode is refreshed, persisted, and inherited as metadata."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import server_rmm
import test_agent_workspace_binding as fixtures


class DebugRegistrationTests(unittest.TestCase):
    def test_session_default_and_legacy_records_are_not_debug(self):
        self.assertIs(server_rmm.Session("fixture-session", "host", "user").to_dict()["agent_debug"], False)
        for record in ({}, {"agent_type": "go"}, {"agent_type": "go", "agent_debug": "true"},
                       {"agent_type": "powershell", "agent_debug": True}):
            with self.subTest(record=record):
                restored = server_rmm.RMMServer._session_from_record("fixture-session", record)
                self.assertIs(restored.agent_debug, False)

    def test_registration_refresh_clears_debug_for_normal_and_powershell_agents(self):
        server = fixtures.AgentRegistrationWorkspaceTests().server()
        for agent_type, reported, expected in (("go", True, True), ("go", False, False),
                                                ("go", True, True), ("powershell", True, False),
                                                ("go", "true", False), ("go", 1, False)):
            with self.subTest(agent_type=agent_type, reported=reported):
                server.register_session("fixture-session", "host", "user", agent_type=agent_type,
                                        agent_debug=reported)
                self.assertIs(server.sessions["fixture-session"].to_dict()["agent_debug"], expected)
        server.register_session("fixture-session", "host", "user", agent_type="go", agent_debug=True)
        server.save_session.reset_mock()
        server.register_session("fixture-session", "host", "user", agent_type="go")
        server.save_session.assert_called_once()
        self.assertIs(server.save_session.call_args.args[0].agent_debug, False)

    def test_debug_registration_round_trips_persisted_record(self):
        server = fixtures.AgentRegistrationWorkspaceTests().server()
        server.register_session("fixture-session", "host", "user", agent_type="go", agent_debug=True)
        session = server.sessions["fixture-session"]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server_rmm, "SESSION_FILE", str(Path(directory, "sessions.json"))):
            server_rmm.RMMServer.save_session(server, session)
            record = json.loads(Path(directory, "sessions.json").read_text())[session.id]
            self.assertIs(record["agent_debug"], True)
            self.assertIs(server._persisted_session_records[session.id]["agent_debug"], True)
            restored = server._session_from_record(session.id, record)
            self.assertIs(restored.to_dict()["agent_debug"], True)
            self.assertEqual(restored.agent_type, "go")

    def test_http_registration_accepts_only_exact_debug_build_true(self):
        server = fixtures.AgentRegistrationWorkspaceTests().server()
        for suffix, expected in (("&debug_build=true", True), ("&debug_build=false", False),
                                 ("&debug_build=TRUE", False), ("&debug_build=1", False),
                                 ("&debug_build=invalid", False), ("", False),
                                 ("&debug_build=true&debug_build=false", False)):
            with self.subTest(suffix=suffix):
                handler = object.__new__(server_rmm.RMMHandler)
                handler.path = "/register?id=fixture-session&h=host&u=user&agent=go" + suffix
                handler.client_address = ("127.0.0.1", 12345)
                handler._path_allowed = Mock(return_value=True)
                handler._beacon_authorized = Mock(return_value=True)
                handler._serve_web = Mock(return_value=False)
                handler._respond = Mock()
                handler.server_instance = server
                handler.do_GET()
                self.assertEqual(handler._respond.call_args.args[0], 200)
                self.assertIs(server.sessions["fixture-session"].agent_debug, expected)
        handler.path = "/register?id=fixture-session&h=host&u=user&debug_build=true"
        handler.do_GET()
        self.assertIs(server.sessions["fixture-session"].agent_debug, False)

    def test_debug_registration_still_requires_beacon_authentication(self):
        handler = object.__new__(server_rmm.RMMHandler)
        handler.path = "/register?id=fixture-session&agent=go&debug_build=true"
        handler._path_allowed = Mock(return_value=True)
        handler._beacon_authorized = Mock(return_value=False)
        handler._beacon_forbidden = Mock()
        handler._serve_web = Mock(return_value=False)
        handler.server_instance = Mock()
        handler.do_GET()
        handler._beacon_forbidden.assert_called_once()
        handler.server_instance.register_session.assert_not_called()
