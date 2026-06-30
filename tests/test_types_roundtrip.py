"""msgspec encode/decode roundtrip for every message + content-block variant.

The point is to lock in tagged-union dispatch: we encode a struct, decode the
JSON back into the union type, and confirm we got the same variant + payload.
If a future refactor breaks the ``tag``/``tag_field`` convention this test
will catch it immediately.
"""

from __future__ import annotations

import msgspec
import pytest

from mantis_agent.types import (
    AssistantMessage,
    ContentBlock,
    ImageBlock,
    Message,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


_BLOCK_DECODER = msgspec.json.Decoder(ContentBlock)
_BLOCK_ENCODER = msgspec.json.Encoder()


@pytest.mark.parametrize(
    "block",
    [
        TextBlock(text="hello"),
        ThinkingBlock(thinking="step 1: ...", signature="sig123"),
        ToolUseBlock(id="tu_1", name="search", input={"q": "spawn"}),
        ToolResultBlock(tool_use_id="tu_1", content="ok"),
        ToolResultBlock(tool_use_id="tu_2", content="err", is_error=True),
        ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "abc"}),
    ],
    ids=["text", "thinking", "tool_use", "tool_result_ok", "tool_result_err", "image"],
)
def test_content_block_roundtrip(block: ContentBlock) -> None:
    encoded = _BLOCK_ENCODER.encode(block)
    decoded = _BLOCK_DECODER.decode(encoded)
    assert type(decoded) is type(block)
    assert decoded == block


def test_tool_result_with_nested_content_blocks() -> None:
    # The ``content`` field on ToolResultBlock can be a list of ContentBlocks
    # for image / mixed-mode results. Roundtrip that shape too.
    inner = [TextBlock(text="partial"), ImageBlock(source={"url": "data:..."})]
    block = ToolResultBlock(tool_use_id="tu_3", content=inner)
    encoded = _BLOCK_ENCODER.encode(block)
    decoded = _BLOCK_DECODER.decode(encoded)
    assert isinstance(decoded, ToolResultBlock)
    assert isinstance(decoded.content, list)
    assert len(decoded.content) == 2
    assert isinstance(decoded.content[0], TextBlock)
    assert isinstance(decoded.content[1], ImageBlock)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


# Message variants share a ``role`` field but the Struct types themselves
# don't carry msgspec tags (so users can hand-construct messages without
# noise). We dispatch decode by the Python type of the original message —
# the test owns both sides, so this is fine.
_USER_DECODER = msgspec.json.Decoder(UserMessage)
_ASSISTANT_DECODER = msgspec.json.Decoder(AssistantMessage)
_SYSTEM_DECODER = msgspec.json.Decoder(SystemMessage)


def _decode_like(original, payload: bytes):
    """Decode ``payload`` into the same Struct type as ``original``."""

    if isinstance(original, UserMessage):
        return _USER_DECODER.decode(payload)
    if isinstance(original, AssistantMessage):
        return _ASSISTANT_DECODER.decode(payload)
    if isinstance(original, SystemMessage):
        return _SYSTEM_DECODER.decode(payload)
    raise TypeError(f"unknown message type: {type(original).__name__}")


@pytest.mark.parametrize(
    "message",
    [
        SystemMessage(content="be terse"),
        UserMessage(content="hi"),
        UserMessage(content=[TextBlock(text="hi"), TextBlock(text="!")]),
        AssistantMessage(content=[TextBlock(text="hello back")]),
        AssistantMessage(
            content=[
                TextBlock(text="thinking..."),
                ToolUseBlock(id="tu_x", name="echo", input={"text": "hi"}),
            ],
            stop_reason="tool_use",
            usage=Usage(input_tokens=10, output_tokens=4),
        ),
    ],
    ids=["system", "user_str", "user_blocks", "assistant_text", "assistant_tool_use"],
)
def test_message_roundtrip(message: Message) -> None:
    encoded = _BLOCK_ENCODER.encode(message)
    decoded = _decode_like(message, encoded)
    assert type(decoded) is type(message)
    assert decoded == message


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_usage_roundtrip() -> None:
    u = Usage(
        input_tokens=42,
        output_tokens=7,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=11,
    )
    encoded = _BLOCK_ENCODER.encode(u)
    decoded = msgspec.json.Decoder(Usage).decode(encoded)
    assert decoded == u


def test_usage_omits_zero_defaults() -> None:
    # ``omit_defaults=True`` should give us a compact encoding for the common
    # case (most providers report only input + output tokens).
    encoded = _BLOCK_ENCODER.encode(Usage(input_tokens=5, output_tokens=2))
    raw = encoded.decode()
    assert "cache_creation_input_tokens" not in raw
    assert "cache_read_input_tokens" not in raw
