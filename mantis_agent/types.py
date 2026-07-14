"""Universal message + content-block types.

Every provider adapter converts its native shapes to/from these. Built on
``msgspec.Struct`` — typed, immutable-by-default, ~5–10× faster to encode/decode
than Pydantic v2 and uses ~3× less memory.

Design notes
------------
* Content blocks are a *tagged union* via ``msgspec.Struct, tag_field="type"``.
  This means msgspec dispatches on a single string compare at decode time —
  no reflection, no isinstance ladders.
* ``frozen=True`` for blocks; that lets us hash, share across tasks, and
  prevents accidental mutation in the streaming hot path.
* ``Message`` is *not* frozen — assistant messages grow during streaming.
  Mutation is confined to streaming assembly; consumers should treat finalized
  messages as immutable.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

import msgspec

# ---------------------------------------------------------------------------
# Content blocks (tagged union)
# ---------------------------------------------------------------------------


class TextBlock(
    msgspec.Struct,
    frozen=True,
    tag="text",
    tag_field="type",
    omit_defaults=True,
):
    """Plain text. Streamed as a sequence of TextDelta events."""

    text: str
    # Anthropic-style cache control. Other providers ignore this field.
    cache_control: dict[str, str] | None = None


class ThinkingBlock(
    msgspec.Struct,
    frozen=True,
    tag="thinking",
    tag_field="type",
    omit_defaults=True,
):
    """Model-internal reasoning. Only emitted by providers that support it."""

    thinking: str
    signature: str | None = None


class ToolUseBlock(
    msgspec.Struct,
    frozen=True,
    tag="tool_use",
    tag_field="type",
    omit_defaults=True,
):
    """Assistant requests a tool call."""

    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(
    msgspec.Struct,
    frozen=True,
    tag="tool_result",
    tag_field="type",
    omit_defaults=True,
):
    """Result of a tool call, sent back as part of a user message."""

    tool_use_id: str
    content: str | list["ContentBlock"]
    is_error: bool = False


class ImageBlock(
    msgspec.Struct,
    frozen=True,
    tag="image",
    tag_field="type",
    omit_defaults=True,
):
    """Image content. ``source`` follows the Anthropic shape; adapters
    convert to OpenAI/Gemini equivalents."""

    source: dict[str, Any]


# Tagged union over all block kinds. msgspec dispatches on the ``type`` field.
ContentBlock = Annotated[
    Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock, ImageBlock],
    msgspec.Meta(description="A single content block in a message."),
]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class SystemMessage(msgspec.Struct, omit_defaults=True):
    """System prompt. Some providers want this in a dedicated field; the
    adapter is responsible for placement. We model it uniformly here."""

    content: str | list[TextBlock]
    role: Literal["system"] = "system"


class UserMessage(msgspec.Struct, omit_defaults=True):
    """User-authored or tool-result-bearing message.

    ``isMeta=True`` flags synthetic messages produced by the SDK itself
    (e.g. the ``<system-reminder>``-wrapped user-context block prepended
    at session start, or attachment-surface reminders mid-turn). Mirrors
    Claude SDK's ``SDKUserMessage.isSynthetic`` semantics — transcript
    readers can skip these when counting visible turns.
    """

    content: str | list[ContentBlock]
    role: Literal["user"] = "user"
    isMeta: bool = False


class AssistantMessage(msgspec.Struct, omit_defaults=True):
    """Assistant turn. Content is a list of blocks; mutated during streaming."""

    content: list[ContentBlock]
    role: Literal["assistant"] = "assistant"
    # Populated when the message finalizes.
    stop_reason: str | None = None
    usage: Usage | None = None


# ``Message`` is an *untagged* union: its members are discriminated by a plain
# ``role`` Literal, not a msgspec ``tag``. msgspec requires every Struct in a
# union to be tagged, so ``msgspec.json.Decoder(Message)`` raises TypeError at
# construction — the union is encode-only. Tagging the structs would drop the
# ``role`` attribute (msgspec doesn't expose the tag field as an attribute),
# breaking every ``msg.role`` consumer, so to decode a persisted message use
# ``decode_message`` below, which dispatches on the ``role`` discriminator.
Message = Union[SystemMessage, UserMessage, AssistantMessage]


# ---------------------------------------------------------------------------
# Usage / metadata
# ---------------------------------------------------------------------------


class Usage(msgspec.Struct, frozen=True, omit_defaults=True):
    """Token counts. Fields are optional because not every provider reports
    every metric.

    OSS backend semantics:
      * Ollama exposes ``prompt_eval_count`` → ``input_tokens`` and
        ``eval_count`` → ``output_tokens``. No cache fields.
      * vLLM / Together / Fireworks / Groq emit OpenAI-shape ``usage``:
        ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``. No cache.
      * DeepSeek's hosted API exposes prefix-cache token counts that map to
        ``cache_read_input_tokens``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ModelUsage(msgspec.Struct, frozen=True, omit_defaults=True):
    """Per-model usage record matching the Claude Agent SDK's ``ModelUsage``
    schema verbatim (camelCase fields, since the wire format goes out to JS
    consumers expecting Claude SDK output).

    Aggregated into ``SDKResultMessage.modelUsage`` (a dict keyed by model
    id) so multi-model runs (primary + fallback + summarizer) report each
    model's contribution separately. All fields default to 0 so partial
    reporting from OSS backends Just Works.
    """

    inputTokens: int = 0
    outputTokens: int = 0
    cacheReadInputTokens: int = 0
    cacheCreationInputTokens: int = 0
    webSearchRequests: int = 0
    costUSD: float = 0.0
    contextWindow: int = 0
    maxOutputTokens: int = 0


# ---------------------------------------------------------------------------
# Encoder / decoder singletons
# ---------------------------------------------------------------------------
# Reusing one encoder/decoder per type beats constructing one per call by ~30%
# for our message sizes. msgspec's encoders are thread-safe.

ENCODE_MESSAGE = msgspec.json.Encoder()
DECODE_ASSISTANT_MESSAGE = msgspec.json.Decoder(AssistantMessage)

# Per-role decoders for the untagged ``Message`` union (see the note by its
# definition). Keyed by the ``role`` discriminator so ``decode_message`` can
# route without a union decoder (which msgspec refuses to build for untagged
# Struct members).
_MESSAGE_DECODERS = {
    "system": msgspec.json.Decoder(SystemMessage),
    "user": msgspec.json.Decoder(UserMessage),
    "assistant": DECODE_ASSISTANT_MESSAGE,
}


class _RoleTag(msgspec.Struct):
    """Minimal peek struct: reads only ``role`` (msgspec ignores other fields).

    ``role`` is optional because the message structs use ``omit_defaults=True``,
    which drops ``role`` when it equals its Literal default. The session store
    forces ``role`` back into persisted blobs, so persisted rows always carry it.
    """

    role: str | None = None


_DECODE_ROLE = msgspec.json.Decoder(_RoleTag)


def to_json(obj: Any) -> bytes:
    """Fast JSON encode using the shared encoder."""

    return ENCODE_MESSAGE.encode(obj)


def decode_message(data: bytes | str) -> Message:
    """Decode a persisted JSON :data:`Message`, dispatching on its ``role`` field.

    ``Message`` is an untagged union, so ``msgspec.json.Decoder(Message)`` raises
    ``TypeError`` at construction. This helper peeks the ``role`` discriminator and
    routes to the matching per-type decoder, giving consumers a working round-trip
    without tagging the structs (which would drop the ``role`` attribute).

    Expects the persisted wire format, which carries ``role`` (the session store
    forces it in even though ``omit_defaults`` would otherwise drop it). Raises
    ``ValueError`` when ``role`` is absent or unrecognized rather than guessing."""

    role = _DECODE_ROLE.decode(data).role
    decoder = _MESSAGE_DECODERS.get(role) if role is not None else None
    if decoder is None:
        raise ValueError(
            f"cannot decode message: missing or unknown 'role' discriminator ({role!r})"
        )
    return decoder.decode(data)


# ---------------------------------------------------------------------------
# Claude Agent SDK compat — mirror ``claude_agent_sdk.types`` re-exports.
# The canonical Claude SDK examples ``from claude_agent_sdk.types import
# HookContext, HookInput, HookJSONOutput, HookMatcher, Message,
# ResultMessage, AssistantMessage, TextBlock`` — we expose the same names
# here so callers can swap the import and nothing else.
# ---------------------------------------------------------------------------


def __getattr__(name: str) -> Any:
    """Module-level ``__getattr__`` for Claude-compat lazy attribute resolution.

    Triggered when something does ``from mantis_agent.types import HookContext``
    and the name isn't already a module global. We populate the compat names
    on demand and re-resolve.
    """

    if name in {
        "HookContext",
        "HookInput",
        "HookJSONOutput",
        "HookMatcher",
        "ResultMessage",
        "MessageType",
        "MantisAgentOptions",
        "PermissionResult",
        "PermissionResultAllow",
        "PermissionResultDeny",
        "AgentDefinition",
        "StreamEvent",
    }:
        from .claude_compat import (  # noqa: PLC0415
            AgentDefinition as _AgentDefinition,
            MantisAgentOptions as _MantisAgentOptions,
            HookContext as _HookContext,
            HookInput as _HookInput,
            HookJSONOutput as _HookJSONOutput,
            HookMatcher as _HookMatcher,
            Message as _MessageType,
            PermissionResult as _PermissionResult,
            PermissionResultAllow as _PermissionResultAllow,
            PermissionResultDeny as _PermissionResultDeny,
            ResultMessage as _ResultMessage,
        )
        from .events import StreamEvent as _StreamEvent  # noqa: PLC0415

        mapping = {
            "HookContext": _HookContext,
            "HookInput": _HookInput,
            "HookJSONOutput": _HookJSONOutput,
            "HookMatcher": _HookMatcher,
            "ResultMessage": _ResultMessage,
            "MessageType": _MessageType,
            "MantisAgentOptions": _MantisAgentOptions,
            "PermissionResult": _PermissionResult,
            "PermissionResultAllow": _PermissionResultAllow,
            "PermissionResultDeny": _PermissionResultDeny,
            "AgentDefinition": _AgentDefinition,
            "StreamEvent": _StreamEvent,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
