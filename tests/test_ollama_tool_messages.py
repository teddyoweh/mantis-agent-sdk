"""Ollama tool results travel as ``role: "tool"`` messages on the native path.

Native-tool models' chat templates render tool responses from ``tool``-role
messages; flattening them into ``<tool_result>`` user text (the Hermes-prompt
shape) is out of distribution and makes models re-call tools or ignore results.
The prompt-engineered path keeps the tagged text its system prompt describes.
"""
from __future__ import annotations

import json
from typing import Any

import anyio
import httpx

from mantis_agent.capabilities import lookup_model
from mantis_agent.providers.ollama import OllamaProvider
from mantis_agent.types import (
    AssistantMessage,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

PNG = "iVBORw0KGgo="
TOOL = {"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}
_DONE = (
    '{"model":"x","message":{"role":"assistant","content":""},'
    '"done":true,"done_reason":"stop"}\n'
)


def _body(messages: list[Any], *, model: str = "qwen2.5", tools: Any = (TOOL,)) -> dict:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=_DONE, headers={"content-type": "application/x-ndjson"})

    p = OllamaProvider(base_url="http://localhost:11434", probe_model_info=False)
    p.client = httpx.AsyncClient(
        base_url="http://localhost:11434", transport=httpx.MockTransport(handler)
    )

    async def go() -> None:
        async for _ in p.stream(
            model=model, messages=messages, tools=list(tools) or None,
            max_tokens=64, model_capability=lookup_model(model),
        ):
            pass

    anyio.run(go)
    return captured["body"]


def _history() -> list[Any]:
    return [
        UserMessage(content="read both"),
        AssistantMessage(content=[
            TextBlock(text="reading"),
            ToolUseBlock(id="c1", name="read_file", input={"path": "a"}),
            ToolUseBlock(id="c2", name="grep", input={"q": "x"}),
        ]),
        # Results out of call order, with an error, a nested image, a sibling
        # reminder and a loose image in the same user message.
        UserMessage(content=[
            ToolResultBlock(tool_use_id="c2", content="no matches", is_error=True),
            ToolResultBlock(tool_use_id="c1", content=[
                TextBlock(text="file a"),
                ImageBlock(source={"type": "base64", "media_type": "image/png", "data": PNG}),
            ]),
            TextBlock(text="<system-reminder>be brief</system-reminder>"),
            ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "LOOSE"}),
        ]),
    ]


def test_native_path_sends_tool_role_messages_in_call_order() -> None:
    wire = _body(_history())["messages"]
    roles = [m["role"] for m in wire]
    assert roles == ["user", "assistant", "tool", "tool", "user"]

    assistant = wire[1]
    assert [c["id"] for c in assistant["tool_calls"]] == ["c1", "c2"]

    t1, t2 = wire[2], wire[3]
    assert t1 == {"role": "tool", "content": "file a", "tool_name": "read_file",
                  "tool_call_id": "c1"}
    assert t2["tool_name"] == "grep" and t2["tool_call_id"] == "c2"
    assert t2["content"] == "Error: no matches"
    assert "<tool_result" not in json.dumps(wire)

    # Everything else in that user message follows the tool messages.
    tail = wire[4]
    assert tail["images"] == [PNG, "LOOSE"]
    assert "be brief" in tail["content"] and "c1" in tail["content"]
    assert PNG not in tail["content"]
    assert "images" not in t1


def test_results_only_message_emits_no_trailing_user_message() -> None:
    wire = _body([
        UserMessage(content="go"),
        AssistantMessage(content=[ToolUseBlock(id="c1", name="read_file", input={})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="ok")]),
    ])["messages"]
    assert [m["role"] for m in wire] == ["user", "assistant", "tool"]


def test_tool_less_request_for_native_model_keeps_tool_messages() -> None:
    wire = _body(_history(), tools=())["messages"]
    assert [m["role"] for m in wire].count("tool") == 2


def test_prompt_engineered_path_keeps_tagged_text() -> None:
    wire = _body(_history(), model="phi3")["messages"]
    assert "tool" not in [m["role"] for m in wire]
    user = wire[-1]
    assert user["role"] == "user"
    assert '<tool_result tool_call_id="c2" is_error="true">no matches' in user["content"]
    assert '<tool_result tool_call_id="c1">file a' in user["content"]
    assert user["images"] == [PNG, "LOOSE"]
    assert "id" not in wire[-2]["tool_calls"][0]


def test_thinking_resent_only_within_the_current_turn() -> None:
    wire = _body([
        UserMessage(content="first"),
        AssistantMessage(content=[ThinkingBlock(thinking="old plan"), TextBlock(text="done")]),
        UserMessage(content="second"),
        AssistantMessage(content=[
            ThinkingBlock(thinking="new plan"),
            ToolUseBlock(id="c1", name="read_file", input={}),
        ]),
        UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="ok")]),
    ])["messages"]
    assistants = [m for m in wire if m["role"] == "assistant"]
    assert "thinking" not in assistants[0]
    assert assistants[1]["thinking"] == "new plan"


def test_user_message_with_nothing_sendable_keeps_its_slot() -> None:
    """A URL-only image (Ollama takes base64 only) must not vanish and leave
    two assistant messages back to back."""
    wire = _body([
        UserMessage(content="hi"),
        AssistantMessage(content=[TextBlock(text="hello")]),
        UserMessage(content=[ImageBlock(source={"type": "url", "url": "https://x/y.png"})]),
        AssistantMessage(content=[TextBlock(text="?")]),
        UserMessage(content="next"),
    ])["messages"]
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "assistant", "user"]
    assert wire[2]["content"] == "[image]"


def test_empty_tool_result_is_not_sent_empty() -> None:
    wire = _body([
        UserMessage(content="go"),
        AssistantMessage(content=[
            ToolUseBlock(id="c1", name="read_file", input={}),
            ToolUseBlock(id="c2", name="read_file", input={}),
            ToolUseBlock(id="c3", name="read_file", input={}),
        ]),
        UserMessage(content=[
            ToolResultBlock(tool_use_id="c1", content=""),
            ToolResultBlock(tool_use_id="c2", content=[
                ImageBlock(source={"type": "base64", "media_type": "image/png", "data": PNG}),
            ]),
            ToolResultBlock(tool_use_id="c3", content="", is_error=True),
        ]),
    ])["messages"]
    tools = [m for m in wire if m["role"] == "tool"]
    assert tools[0]["content"] == "(no output)"
    assert tools[1]["content"] == "(see images for call c2 below)"
    assert tools[2]["content"] == "Error"
    assert wire[-1]["role"] == "user" and wire[-1]["images"] == [PNG]
