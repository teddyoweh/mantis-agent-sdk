"""Screenshots must reach the HTTP image input, not a JSON/text placeholder."""
import json

import anyio
import httpx
import pytest

from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.providers.ollama import OllamaProvider
from mantis_agent.types import AssistantMessage, ImageBlock, ToolResultBlock, ToolUseBlock, UserMessage
from tests.test_mcp_visual_results import PNG, execute
from mantis_agent.mcp.types import CallToolResult

TOOL = {"name": "screenshot", "description": "Capture", "input_schema": {"type": "object"}}
URI = f"data:image/png;base64,{PNG}"


def history():
    result = execute(CallToolResult(content=[
        {"type": "text", "text": "Universe screenshot"},
        {"type": "image", "mimeType": "image/png", "data": PNG},
        {"type": "text", "text": "After capture"},
    ]))
    return [AssistantMessage(content=[
        ToolUseBlock(id="shot-1", name="screenshot", input={"tab": 7}),
        ToolUseBlock(id="shot-2", name="screenshot", input={}),
    ]), UserMessage(content=[result, ToolResultBlock(tool_use_id="shot-2", content=[
        ImageBlock(source={"type": "base64", "media_type": "image/png", "data": PNG}),
    ])])]


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-6-astra"])
def test_images_reach_openai_http_request(model):
    messages = history()
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if model == "gpt-6-astra":
            assert request.url.path.endswith("/responses")
            return httpx.Response(200, json={"id": "r", "output": []})
        return httpx.Response(200, text='data: [DONE]\n\n', headers={"content-type": "text/event-stream"})

    async def run():
        p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
        await p.client.aclose()
        async with httpx.AsyncClient(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handle)) as client:
            p.client = client
            return [event async for event in p.stream(model=model, messages=messages, tools=[TOOL])]

    anyio.run(run)
    assert len(calls) == 1
    if model == "gpt-6-astra":
        wire = calls[0]["input"]
        outputs = [m for m in wire if m.get("type") == "function_call_output"]
        assert [m["call_id"] for m in outputs] == ["shot-1", "shot-2"]
        assert outputs[0]["output"] == "Universe screenshot\nAfter capture"
        assert wire[-3:-1] == outputs
        images = [p["image_url"] for p in wire[-1]["content"] if p["type"] == "input_image"]
    else:
        wire = calls[0]["messages"]
        outputs = [m for m in wire if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in outputs] == ["shot-1", "shot-2"]
        assert outputs[0]["content"] == "Universe screenshot\nAfter capture"
        assert wire[-3:-1] == outputs
        images = [p["image_url"]["url"] for p in wire[-1]["content"] if p["type"] == "image_url"]
    assert wire[-1]["role"] == "user"
    assert images == [URI, URI]
    assert PNG not in json.dumps(outputs)


@pytest.mark.parametrize("path", ["A", "B", "C"])
def test_chat_payload_all_tool_paths(path):
    p = OpenAICompatProvider(base_url="https://example.com/v1", api_key="x")
    payload = p._build_payload(model="gpt-6-astra", messages=history(), system=None,
                               tools=[TOOL], max_tokens=100, temperature=None, extra=None, path=path)
    anyio.run(p.client.aclose)
    content = payload["messages"][-1]["content"]
    assert [part["image_url"]["url"] for part in content if part["type"] == "image_url"] == [URI, URI]
    text = "\n".join(part["text"] for part in content if part["type"] == "text")
    assert "shot-1" in text and "shot-2" in text
    if path != "A":
        assert "Universe screenshot" in text and "After capture" in text
    assert PNG not in text


def test_ollama_nested_images_use_native_array():
    wire = OllamaProvider._encode_messages(history(), None)
    assert wire[-1]["images"] == [PNG, PNG]
    assert 'tool_call_id="shot-1"' in wire[-1]["content"]
    assert "Universe screenshot\nAfter capture" in wire[-1]["content"]
    assert PNG not in wire[-1]["content"]
