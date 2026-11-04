"""MCP result conversion must preserve structured-only protocol responses."""
import json
import unittest
from types import SimpleNamespace

from rmm_mcp_client import _tool_result_text, mcp_available, mcp_tool_to_openai
from rmm_exegol_runtime import execution_arguments


class McpSchemaTests(unittest.TestCase):
    def test_execution_schema_survives_both_sdk_field_names(self):
        schema = {'type': 'object', 'properties': {
            'container_name': {'type': 'string'}, 'command': {'type': 'string'}},
            'required': ['container_name', 'command']}
        for field in ('inputSchema', 'input_schema'):
            with self.subTest(field=field):
                tool = SimpleNamespace(name='execute_command_in_container',
                                       description='', **{field: schema})
                converted = mcp_tool_to_openai(tool)
                self.assertEqual(execution_arguments([converted], 'lab', 'hostname'),
                                 {'container_name': 'lab', 'command': 'hostname'})


@unittest.skipUnless(mcp_available(), 'MCP dependencies are not installed')
class McpResultEncodingTests(unittest.TestCase):
    def test_real_sdk_tool_schema_is_preserved(self):
        from mcp.types import Tool
        schema = {'type': 'object', 'properties': {'command': {'type': 'string'}}}
        tool = Tool(name='execute', inputSchema=schema)
        self.assertEqual(mcp_tool_to_openai(tool)['function']['parameters'], schema)

    def test_structured_only_tool_result_is_preserved(self):
        from mcp.types import CallToolResult
        result = CallToolResult(content=[], structuredContent={'containers': [{'name': 'lab'}]})
        self.assertEqual(json.loads(_tool_result_text(result)), {'containers': [{'name': 'lab'}]})

    def test_text_result_remains_unchanged(self):
        from mcp.types import CallToolResult, TextContent
        result = CallToolResult(content=[TextContent(type='text', text='plain result')],
                                structuredContent={'answer': 'structured'})
        self.assertEqual(_tool_result_text(result), 'plain result')
