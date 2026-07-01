"""Prompt caching (T0.4): the Anthropic passthrough marks cache breakpoints on
the system prompt and the last message so Anthropic reads the prefix from cache
instead of re-billing it every turn."""

from __future__ import annotations

from mantis_agent.providers.anthropic_passthrough import (
    _encode_message,
    _mark_cache_breakpoint,
)
from mantis_agent.types import TextBlock, UserMessage


def test_mark_breakpoint_on_string_content() -> None:
    enc = {"role": "user", "content": "hello"}
    _mark_cache_breakpoint(enc)
    assert enc["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]


def test_mark_breakpoint_on_last_block() -> None:
    enc = _encode_message(UserMessage(content=[TextBlock(text="a"), TextBlock(text="b")]))
    _mark_cache_breakpoint(enc)
    blocks = enc["content"]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[0]   # only the last block is marked


def test_mark_breakpoint_empty_content_noop() -> None:
    enc = {"role": "user", "content": []}
    _mark_cache_breakpoint(enc)          # must not raise
    assert enc["content"] == []
