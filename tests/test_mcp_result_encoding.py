"""MCP result conversion must preserve structured-only protocol responses."""
import json
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    def test_registered_lateral_https_is_strict_boolean(self):
        import mcp_rmm_server as module

        async def run():
            with patch.object(module, "tool_lateral_move", Mock(return_value='{"ok":true}')) as helper, \
                    patch.object(module, "_client", return_value=object()):
                arguments = {"session_ref": "source", "method": "winrm", "host": "fixture"}
                for field in ["https", "insecure"]:
                    for value in ["true", "false", 0, 1, None]:
                        with self.subTest(field=field, value=value), self.assertRaisesRegex(Exception, "validation error"):
                            await module.mcp.call_tool("lateral_move", {**arguments, "https": True, field: value})
                        helper.assert_not_called()
                for value in [True, False]:
                    await module.mcp.call_tool("lateral_move", {**arguments, "https": value})
                    self.assertIs(helper.call_args.kwargs["https"], value)
                await module.mcp.call_tool("lateral_move", {**arguments, "https": True, "insecure": True})
                self.assertIs(helper.call_args.kwargs["insecure"], True)
        asyncio.run(run())

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
