"""MCP result conversion must preserve structured-only protocol responses."""
import json
import unittest

from rmm_mcp_client import _tool_result_text, mcp_available


@unittest.skipUnless(mcp_available(), 'MCP dependencies are not installed')
class McpResultEncodingTests(unittest.TestCase):
    def test_structured_only_tool_result_is_preserved(self):
        from mcp.types import CallToolResult
        result = CallToolResult(content=[], structuredContent={'containers': [{'name': 'lab'}]})
        self.assertEqual(json.loads(_tool_result_text(result)), {'containers': [{'name': 'lab'}]})

    def test_text_result_remains_unchanged(self):
        from mcp.types import CallToolResult, TextContent
        result = CallToolResult(content=[TextContent(type='text', text='plain result')],
                                structuredContent={'answer': 'structured'})
        self.assertEqual(_tool_result_text(result), 'plain result')
