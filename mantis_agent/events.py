"""Normalized streaming events.

Every provider adapter — Anthropic, OpenAI, Gemini, Bedrock, local — emits
exactly this set of events. The agent loop and any user-facing stream consumer
sees a uniform interface regardless of provider.

The variants mirror Anthropic's SSE event taxonomy because it's the cleanest
*structural* model — events scope to ``(message → content_block → delta)``.
OpenAI's ``ChatCompletion.chunk`` flattens everything into ``choices[0].delta``,
which adapters here re-expand into the structured form.

Events are ``msgspec.Struct`` tagged unions; consumers can dispatch on
``isinstance`` or match the ``type`` field.
"""

from __future__ import annotations

from typing import Any, Union

import msgspec

from .types import ContentBlock, Usage


class MessageStart(msgspec.Struct, frozen=True, tag="message_start", tag_field="type"):
    """Stream is starting. ``message_id`` and ``model`` are populated from the
    first provider event."""

    message_id: str
    model: str
    role: str = "assistant"


class ContentBlockStart(
    msgspec.Struct,
    frozen=True,
    tag="content_block_start",
    tag_field="type",
    omit_defaults=True,
):
    """A new content block (text, tool_use, thinking, ...) is beginning."""

    index: int
    block: ContentBlock


class TextDelta(msgspec.Struct, frozen=True, tag="text_delta", tag_field="type"):
    """Incremental text. Pass through as a ``str`` — no concatenation here."""

    text: str


class ThinkingDelta(msgspec.Struct, frozen=True, tag="thinking_delta", tag_field="type"):
    """Incremental thinking text."""

    thinking: str


class InputJsonDelta(
    msgspec.Struct,
    frozen=True,
    tag="input_json_delta",
    tag_field="type",
):
    """Incremental tool input JSON. Adapters that don't stream tool input
    natively (OpenAI in some modes) buffer the full JSON and emit one delta."""

    partial_json: str


Delta = Union[TextDelta, ThinkingDelta, InputJsonDelta]


class ContentBlockDelta(
    msgspec.Struct,
    frozen=True,
    tag="content_block_delta",
    tag_field="type",
):
    """A delta against an in-flight content block."""

    index: int
    delta: Delta


class ContentBlockStop(
    msgspec.Struct,
    frozen=True,
    tag="content_block_stop",
    tag_field="type",
):
    """The content block at ``index`` is finalized."""

    index: int


class MessageDelta(
    msgspec.Struct,
    frozen=True,
    tag="message_delta",
    tag_field="type",
    omit_defaults=True,
):
    """Update to message-level metadata mid-stream (usage, stop reason)."""

    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: Usage | None = None


class MessageStop(msgspec.Struct, frozen=True, tag="message_stop", tag_field="type"):
    """Stream is complete."""


class ErrorEvent(msgspec.Struct, frozen=True, tag="error", tag_field="type"):
    """Provider returned an error mid-stream (rare but real)."""

    error_type: str
    message: str
    raw: dict[str, Any] | None = None


StreamEvent = Union[
    MessageStart,
    ContentBlockStart,
    ContentBlockDelta,
    ContentBlockStop,
    MessageDelta,
    MessageStop,
    ErrorEvent,
]
