"""Cover every inline-thinking tag convention the SDK normalizes to one
ThinkingBlock stream.

The OSS reasoning-model landscape uses different tag pairs that all mean
"this run of text is model-internal reasoning":

  <think>...</think>           DeepSeek-R1, QwQ, R1-Distill-*, Marco-o1
  <thought>...</thought>       Hermes-Pro reasoning mode
  <reasoning>...</reasoning>   some Mistral fine-tunes, IBM Granite
  <thinking>...</thinking>     Claude-style fine-tunes
  <reflection>...</reflection> Reflection-70B class

These tests verify the parser handles all of them, with case-insensitivity,
partial-tag boundary handling, and unterminated-block recovery.
"""

from __future__ import annotations

import pytest

from mantis_agent.streaming.thinking_parser import (
    DEFAULT_THINKING_TAGS,
    TextChunk,
    ThinkingChunk,
    ThinkingParser,
)


def _drain(parser: ThinkingParser, *deltas: str) -> list:
    """Feed each delta, finalize, return the flat event list."""

    out = []
    for d in deltas:
        out.extend(parser.feed(d))
    out.extend(parser.finalize())
    return out


def _flatten(events) -> list[tuple[str, str]]:
    """Compact event list as (kind, text) tuples for compact asserts."""

    flat: list[tuple[str, str]] = []
    for e in events:
        if isinstance(e, ThinkingChunk):
            flat.append(("thinking", e.text))
        elif isinstance(e, TextChunk):
            flat.append(("text", e.text))
    return flat


# ---------------------------------------------------------------------------
# Each tag pair, end to end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag_open, tag_close",
    [
        ("<think>", "</think>"),
        ("<thought>", "</thought>"),
        ("<reasoning>", "</reasoning>"),
        ("<thinking>", "</thinking>"),
        ("<reflection>", "</reflection>"),
    ],
)
def test_each_default_tag_pair_splits(tag_open: str, tag_close: str) -> None:
    text = f"Hi! {tag_open}Step 1: think{tag_close} Answer: 42."
    p = ThinkingParser()
    out = _flatten(_drain(p, text))
    assert ("thinking", "Step 1: think") in out
    # Text on either side appears
    assert any(k == "text" and "Hi!" in v for k, v in out)
    assert any(k == "text" and "Answer: 42." in v for k, v in out)


def test_case_insensitive_match() -> None:
    """OSS models sometimes capitalize. <Think> works the same as <think>."""

    p = ThinkingParser()
    out = _flatten(_drain(p, "<Think>reason</Think> reply"))
    assert ("thinking", "reason") in out


def test_streaming_partial_tag_holdback() -> None:
    """A partial opening tag at a chunk boundary must NOT flush as text."""

    p = ThinkingParser()
    # Split the opening tag across two chunks.
    out = _flatten(_drain(p, "intro <thi", "nk>reason</think> done"))
    # No spurious "<thi" text emitted.
    assert ("text", "<thi") not in out
    assert ("thinking", "reason") in out
    assert any(k == "text" and "intro" in v for k, v in out)


def test_unterminated_block_preserves_content() -> None:
    """If the stream ends mid-think, we'd rather keep the content than drop
    it — emit as thinking on finalize."""

    p = ThinkingParser()
    out = _flatten(_drain(p, "<think>truncated reason"))
    assert ("thinking", "truncated reason") in out


def test_multiple_blocks() -> None:
    """Multiple reasoning blocks interleaved with text — each captured."""

    p = ThinkingParser()
    out = _flatten(
        _drain(
            p,
            "<think>first</think> then <think>second</think> done",
        )
    )
    thinkings = [v for k, v in out if k == "thinking"]
    assert thinkings == ["first", "second"]


def test_restricted_tag_set_only_matches_configured_pair() -> None:
    """Caller-restricted tags: <thought> set, <think> must pass as text."""

    p = ThinkingParser(tags=(("<thought>", "</thought>"),))
    out = _flatten(_drain(p, "<think>ignored</think> rest"))
    # <think> is not in this parser's tag set — passes through as text.
    assert not any(k == "thinking" for k, v in out)
    assert any(k == "text" and "<think>" in v for k, v in out)


def test_streaming_one_char_at_a_time() -> None:
    """Hostile: feed one character at a time. Parser must still recover."""

    p = ThinkingParser()
    chunks = list("Hi <think>r1 r2</think> bye")
    out = _flatten(_drain(p, *chunks))
    # Coalesce by kind for the assertion (one-char-at-a-time emits many
    # small chunks of each kind, all of which we reduce).
    thinking_text = "".join(v for k, v in out if k == "thinking")
    text_text = "".join(v for k, v in out if k == "text")
    assert thinking_text == "r1 r2"
    assert text_text == "Hi  bye"


def test_capability_carries_default_tags() -> None:
    """Every ModelCapability ships with a default tag set so providers can
    just pass it to ThinkingParser(tags=…) without thinking about it."""

    from mantis_agent import lookup_model

    cap = lookup_model("deepseek-r1-distill-qwen-32b")
    assert cap.emits_inline_thinking is True
    assert ("<think>", "</think>") in cap.inline_thinking_tags
    # Non-reasoning models don't activate the parser even if they have tags.
    plain = lookup_model("qwen2.5-72b-instruct")
    assert plain.emits_inline_thinking is False
