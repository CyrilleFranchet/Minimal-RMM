"""Verify connection-persistence choices survive AI/MCP tool dispatch."""
import json
import unittest
from unittest.mock import Mock

import rmm_tools


class GoPersistenceToolTests(unittest.TestCase):
    def test_dispatch_preserves_default_and_explicit_boolean(self):
        client = Mock()
        client.build_go_agent.return_value = (200, {})
        for arguments, expected in (({}, False), ({"persistent_http": True}, True),
                                    ({"persistent_http": False}, False)):
            with self.subTest(arguments=arguments):
                result = rmm_tools.TOOL_HANDLERS["build_go_agent"](client, arguments)
                self.assertTrue(json.loads(result)["ok"])
                self.assertIs(client.build_go_agent.call_args.kwargs["config"]["persistent_http"], expected)

    def test_nonboolean_is_rejected_before_api_call(self):
        client = Mock()
        for value in ("false", "true", 0, 1, None, {}):
            with self.subTest(value=value):
                result = rmm_tools.TOOL_HANDLERS["build_go_agent"](client, {"persistent_http": value})
                self.assertFalse(json.loads(result)["ok"])
        client.build_go_agent.assert_not_called()
