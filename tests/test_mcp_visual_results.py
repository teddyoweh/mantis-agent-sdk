"""MCP screenshots use the same rich result convention as read_file."""

import anyio
import pytest

from mantis_agent.mcp.types import CallToolResult, MCPTool
from mantis_agent.streaming.executor import StreamingToolExecutor
from mantis_agent.tools import ToolRegistry
from mantis_agent.types import ImageBlock, TextBlock, ToolUseBlock

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def execute(result):
    class Client:
        async def call_tool(self, name, arguments):
            assert name == "screenshot"
            assert arguments == {"tab": 7}
            return result

    async def run():
        tool = MCPTool(
            name="screenshot", description="Capture a tab", input_schema={}, server_id="browser"
        ).to_mantis_agent_tool(Client())
        registry = ToolRegistry()
        registry.add(tool)
        async with StreamingToolExecutor(registry) as executor:
            executor.add_tool_call(ToolUseBlock(id="shot-1", name="screenshot", input={"tab": 7}))
            return (await executor.wait_all())[0]

    return anyio.run(run)


@pytest.mark.parametrize("mime_key", ["mimeType", "mime_type"])
def test_image_and_text_survive_mcp_dispatch(mime_key):
    result = execute(CallToolResult(content=[
        {"type": "text", "text": "Before"},
        {"type": "image", mime_key: "image/png", "data": PNG},
        {"type": "text", "text": "After"},
    ]))
    assert result.tool_use_id == "shot-1"
    assert not result.is_error
    assert result.content == [
        TextBlock(text="Before"),
        ImageBlock(source={"type": "base64", "media_type": "image/png", "data": PNG}),
        TextBlock(text="After"),
    ]


def test_text_only_keeps_string_result():
    result = execute(CallToolResult(content=[
        {"type": "text", "text": "one"}, {"type": "text", "text": "two"},
    ]))
    assert result.content == "one\ntwo"
    assert not result.is_error


@pytest.mark.parametrize("content, expected", [
    ([{"type": "text", "text": "Tab is closed"}], "Tab is closed"),
    ([], "MCP tool 'screenshot' returned an error"),
    ([{"type": "text", "text": "Capture failed"},
      {"type": "image", "mimeType": "image/png", "data": PNG}], "Capture failed"),
])
def test_server_errors_still_flag_dispatch_result(content, expected):
    result = execute(CallToolResult(content=content, is_error=True))
    assert result.is_error
    assert result.tool_use_id == "shot-1"
    assert isinstance(result.content, str)
    assert expected in result.content
    assert PNG not in result.content
