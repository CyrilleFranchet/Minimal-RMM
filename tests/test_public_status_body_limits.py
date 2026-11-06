"""Reject unbounded public status bodies before touching the input stream."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from server_rmm import RMMHandler


class PublicStatusBodyLimitTests(unittest.TestCase):
    """Malformed lengths must never cause an unbounded or truncated read."""

    def test_invalid_lengths_are_rejected_without_reading(self):
        for length, status in (("-1", 400), ("0", 400), ("", 400),
                               ("invalid", 400), ("4097", 413)):
            with self.subTest(length=length):
                handler = SimpleNamespace(
                    command="POST", headers={"Content-Length": length},
                    rfile=Mock(), server_instance=Mock(), _respond=Mock(),
                )
                self.assertTrue(RMMHandler._serve_stage_tool(handler, "fixture-id"))
                self.assertEqual(handler._respond.call_args.args[0], status)
                handler.rfile.read.assert_not_called()
                handler.server_instance.staging_record_status.assert_not_called()
