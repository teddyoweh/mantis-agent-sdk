"""Anthropic passthrough — talk to Anthropic's native Messages API.

This adapter is **for parity testing only**. mantis-agent-sdk does not aim
to proxy Claude — if your production code targets Claude you should use
the real ``claude-agent-sdk``. The reason we ship a passthrough is the
inverse: when you build something on mantis-agent-sdk against an OSS model,
you almost always want to A/B it against Claude at some point ("does my
agent loop work as well on Claude as it does on Qwen?"). Going through
this adapter is the way to do that without rewriting your SDK calls.

Why it's a separate adapter, not folded into ``openai_compat``
-------------------------------------------------------------
Anthropic's Messages API at ``POST /v1/messages`` is *not* OpenAI-
compatible. The request body is different (``system`` is a top-level
field, ``tools`` use Anthropic's shape, ``messages`` exclude system),
the SSE event taxonomy is different (Anthropic emits
``message_start`` → ``content_block_*`` → ``message_delta`` →
``message_stop``, OpenAI emits flat ``chat.completion.chunk`` deltas),
and the auth header is ``x-api-key`` instead of ``Authorization: Bearer``.

Wire-format alignment
---------------------
Anthropic's SSE event taxonomy is in fact exactly the structural model
our :mod:`mantis_agent.events` exposes (it's where we cribbed the
shape from). So the streaming hot path is unusually clean: every
inbound Anthropic event maps to exactly one of our ``StreamEvent``
variants with a tiny amount of unwrapping.

Opt-in
------
This provider is **never** auto-selected by model name. ``routing.py``
still refuses ``claude-*`` model names with :class:`BackendRoutingError`,
preserving the "we don't proxy Anthropic" stance. To use this provider
you must explicitly opt in, in any of these forms:

* Pass an instance directly::

      provider = AnthropicPassthroughProvider(api_key=...)
      agent = Agent(model="claude-sonnet-4-5", provider=provider)

* Pass an Anthropic API URL as the backend::

      agent = Agent(
          model="claude-sonnet-4-5",
          backend="https://api.anthropic.com/v1",
      )

* Pass the literal sentinel ``"anthropic"`` as backend::

      agent = Agent(model="claude-sonnet-4-5", backend="anthropic")

Either of the URL / sentinel forms routes through
:func:`mantis_agent.providers.base.detect_provider` to this adapter
without invoking the bare-model routing path that would otherwise raise.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import uuid4

import httpx
import msgspec

from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
from ..errors import AuthError, ProviderError, StreamProtocolError
from ..events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    ErrorEvent,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
)
from ..http import make_client, raise_for_status
from ..types import (
    AssistantMessage,
    ContentBlock,
    Message,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)
from .base import HTTPProviderMixin

__all__ = [
    "ANTHROPIC_DEFAULT_BASE_URL",
    "ANTHROPIC_DEFAULT_VERSION",
    "AnthropicPassthroughProvider",
]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_DEFAULT_VERSION = "2023-06-01"

# Anthropic's Messages API requires a ``max_tokens`` value — there's no
# server-side default. We pick a generous-but-safe ceiling for tests so
# callers who forget to set it don't get a 400.
_DEFAULT_MAX_TOKENS = 1024

_JSON_DECODER = msgspec.json.Decoder()
_PAYLOAD_ENCODER = msgspec.json.Encoder()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AnthropicPassthroughProvider(HTTPProviderMixin):
    """Adapter for the native Anthropic Messages API.

    Parity-testing tool only. See module docstring for the rationale.

    Parameters
    ----------
    api_key:
        Anthropic API key. Falls back to ``$ANTHROPIC_API_KEY``. The
        provider refuses to construct without a key — there's no
        meaningful unauthenticated path against ``api.anthropic.com``.
    base_url:
        Override the API base. Default ``https://api.anthropic.com/v1``.
        Strips any trailing ``/messages`` if the caller pastes the full
        endpoint by mistake.
    anthropic_version:
        Value for the ``anthropic-version`` header. Defaults to the
        most-widely-supported stable version.
    anthropic_beta:
        Optional ``anthropic-beta`` header (comma-separated feature
        flags). Forwarded verbatim.
    default_headers:
        Extra request headers — merged last, so they override anything
        the adapter set up automatically.
    backend_capability:
        Override the capability profile. Default
        ``HOSTED_PROFILES["anthropic"]``.
    """

    name = "anthropic_passthrough"
    backend_capability: BackendCapability

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        anthropic_version: str | None = None,
        anthropic_beta: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise AuthError(
                "AnthropicPassthroughProvider needs an API key. "
                "Pass api_key=... or set $ANTHROPIC_API_KEY. This adapter "
                "talks to api.anthropic.com — there is no unauthenticated path."
            )

        url = _normalize_base_url(base_url or ANTHROPIC_DEFAULT_BASE_URL)
        version = anthropic_version or ANTHROPIC_DEFAULT_VERSION

        headers: dict[str, str] = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-api-key": key,
            "anthropic-version": version,
        }
        if anthropic_beta:
            headers["anthropic-beta"] = anthropic_beta
        if default_headers:
            headers.update(default_headers)

        self.client = make_client(base_url=url, headers=headers)
        self.base_url = url

        if backend_capability is None:
            self.backend_capability = HOSTED_PROFILES.get(
                "anthropic", HOSTED_PROFILES["mock"]
            )
        else:
            self.backend_capability = backend_capability

    # ------------------------------------------------------------------
    # Public stream entrypoint
    # ------------------------------------------------------------------

    async def stream(
        self,
        *,
        model: str,
        messages: Iterable[Message],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        extra: dict[str, Any] | None = None,
        model_capability: ModelCapability | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream events from Anthropic's Messages API."""

        if not model:
            raise ProviderError(
                "AnthropicPassthroughProvider.stream needs a model name "
                "(e.g. 'claude-sonnet-4-5')."
            )

        messages_list = list(messages)
        system_text, body_messages = _split_system(system, messages_list)

        payload: dict[str, Any] = {
            "model": model,
            "messages": [_encode_message(m) for m in body_messages],
            "max_tokens": int(max_tokens),
            "stream": True,
        }
        if system_text:
            payload["system"] = system_text
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if tools:
            payload["tools"] = _normalize_tools(tools)
        if extra:
            for k, v in extra.items():
                payload.setdefault(k, v)

        body = _PAYLOAD_ENCODER.encode(payload)

        async with self.client.stream(
            "POST",
            "/messages",
            content=body,
        ) as response:
            await _raise_if_error(response)
            async for ev in _iter_normalized_events(response):
                yield ev

    async def aclose(self) -> None:
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Outbound: convert internal types → Anthropic Messages API request shape
# ---------------------------------------------------------------------------


def _split_system(
    explicit_system: str | None, messages: list[Message]
) -> tuple[str | None, list[Message]]:
    """Return ``(system_text, messages_without_system)``.

    Anthropic's Messages API takes ``system`` as a top-level field, not
    a message with ``role="system"``. If the caller supplied
    ``system=...`` we use that verbatim. Otherwise we hoist any
    :class:`SystemMessage` instances out of the messages list and
    concatenate them.
    """

    if explicit_system is not None:
        body_only = [m for m in messages if not isinstance(m, SystemMessage)]
        return explicit_system, body_only

    system_pieces: list[str] = []
    body: list[Message] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            system_pieces.append(_system_text(m))
        else:
            body.append(m)
    if not system_pieces:
        return None, body
    return "\n\n".join(p for p in system_pieces if p), body


def _system_text(m: SystemMessage) -> str:
    """Flatten a SystemMessage's content to a plain string."""

    if isinstance(m.content, str):
        return m.content
    return "\n\n".join(
        block.text for block in m.content if isinstance(block, TextBlock)
    )


def _encode_message(m: Message) -> dict[str, Any]:
    """Convert a user/assistant ``Message`` to Anthropic's wire shape."""

    if isinstance(m, UserMessage):
        return {"role": "user", "content": _encode_content(m.content)}
    if isinstance(m, AssistantMessage):
        return {"role": "assistant", "content": _encode_content(m.content)}
    # System messages should have been hoisted out already.
    raise ProviderError(
        f"AnthropicPassthroughProvider can't encode {type(m).__name__} as a "
        "messages-array entry (system messages should be hoisted to the "
        "top-level system field)."
    )


def _encode_content(content: str | list[ContentBlock]) -> Any:
    """Encode either a bare-string content or a list of ContentBlocks."""

    if isinstance(content, str):
        return content
    encoded: list[dict[str, Any]] = []
    for block in content:
        encoded.append(_encode_block(block))
    return encoded


def _encode_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        out: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache_control:
            out["cache_control"] = block.cache_control
        return out
    if isinstance(block, ThinkingBlock):
        out = {"type": "thinking", "thinking": block.thinking}
        if block.signature:
            out["signature"] = block.signature
        return out
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input),
        }
    if isinstance(block, ToolResultBlock):
        # Anthropic accepts either a string or a list of content blocks.
        if isinstance(block.content, str):
            inner: Any = block.content
        else:
            inner = [_encode_block(b) for b in block.content]
        out = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": inner,
        }
        if block.is_error:
            out["is_error"] = True
        return out
    # ImageBlock + future variants — encode their msgspec dict shape.
    return msgspec.to_builtins(block)


def _normalize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept OpenAI-shape ``[{type:'function', function:{...}}]`` OR
    Anthropic-shape ``[{name, description, input_schema}]`` and emit
    Anthropic's shape.

    The agent loop currently produces OpenAI shape because it's also
    what local backends expect. We translate here.
    """

    out: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object"}),
                }
            )
        elif isinstance(t, dict) and "name" in t and ("input_schema" in t or "parameters" in t):
            out.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema") or t.get("parameters", {"type": "object"}),
                }
            )
        else:
            # Unknown shape — pass through and let the API yell at the caller.
            out.append(t)  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------------------
# Inbound: HTTP errors + SSE → StreamEvent normalization
# ---------------------------------------------------------------------------


async def _raise_if_error(response: httpx.Response) -> None:
    """Translate HTTP-level failures into the SDK's error hierarchy.

    Reads the body once (Anthropic always returns small JSON error bodies
    for non-2xx) so callers see the original message verbatim.
    """

    if response.is_success:
        return

    try:
        text = (await response.aread()).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — best effort
        text = ""

    payload: dict[str, Any] | None = None
    if text:
        try:
            payload = _JSON_DECODER.decode(text)
        except msgspec.DecodeError:
            payload = None

    msg = text or response.reason_phrase or "request failed"
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        if "message" in err:
            msg = err["message"]

    status = response.status_code
    if status in (401, 403):
        raise AuthError(f"Anthropic auth error ({status}): {msg}")
    raise ProviderError(f"Anthropic API error ({status}): {msg}")


async def _iter_normalized_events(
    response: httpx.Response,
) -> AsyncIterator[StreamEvent]:
    """Parse Anthropic's SSE stream and yield normalized StreamEvents.

    Anthropic's SSE frames look like::

        event: message_start
        data: {"type": "message_start", "message": {...}}

        event: content_block_delta
        data: {"type": "content_block_delta", ...}

    The blank-line separator terminates a frame. We don't trust the
    ``event:`` line — the ``type`` field inside ``data`` is the canonical
    discriminator and Anthropic always sets both.
    """

    current_event: str | None = None
    data_lines: list[str] = []

    async for line in response.aiter_lines():
        if line == "" or line == "\n":
            # End of an SSE frame — emit if we have data.
            if data_lines:
                payload_str = "\n".join(data_lines)
                data_lines = []
                event_name = current_event
                current_event = None
                async for ev in _frame_to_events(event_name, payload_str):
                    yield ev
            continue
        # Some SSE servers also send lines without explicit \n stripping.
        line = line.rstrip("\r")
        if line.startswith(":"):
            # Comment / heartbeat — ignore.
            continue
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
            continue
        # Unknown SSE field — Anthropic doesn't use any, so don't choke.

    # If the connection closes without a final blank line, flush whatever's left.
    if data_lines:
        payload_str = "\n".join(data_lines)
        async for ev in _frame_to_events(current_event, payload_str):
            yield ev


async def _frame_to_events(
    event_name: str | None, payload_str: str
) -> AsyncIterator[StreamEvent]:
    """Decode one SSE frame payload into 0..n StreamEvents."""

    if payload_str == "[DONE]":  # rare belt-and-suspenders — Anthropic uses message_stop
        return

    try:
        payload = _JSON_DECODER.decode(payload_str)
    except msgspec.DecodeError as exc:
        raise StreamProtocolError(
            f"Anthropic SSE frame was not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        return

    kind = payload.get("type") or event_name
    if kind == "ping":
        return
    if kind == "message_start":
        msg = payload.get("message", {}) or {}
        yield MessageStart(
            message_id=str(msg.get("id") or f"msg_{uuid4().hex}"),
            model=str(msg.get("model") or ""),
            role=str(msg.get("role") or "assistant"),
        )
        usage = _decode_usage(msg.get("usage"))
        if usage is not None:
            yield MessageDelta(usage=usage)
        return
    if kind == "content_block_start":
        index = int(payload.get("index", 0))
        block_payload = payload.get("content_block") or payload.get("block") or {}
        block = _decode_block(block_payload)
        if block is None:
            return
        yield ContentBlockStart(index=index, block=block)
        return
    if kind == "content_block_delta":
        index = int(payload.get("index", 0))
        delta = payload.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            yield ContentBlockDelta(
                index=index, delta=TextDelta(text=str(delta.get("text", "")))
            )
        elif dtype == "thinking_delta":
            yield ContentBlockDelta(
                index=index,
                delta=ThinkingDelta(thinking=str(delta.get("thinking", ""))),
            )
        elif dtype == "input_json_delta":
            yield ContentBlockDelta(
                index=index,
                delta=InputJsonDelta(
                    partial_json=str(delta.get("partial_json", ""))
                ),
            )
        # Unknown delta types — silently skip.
        return
    if kind == "content_block_stop":
        yield ContentBlockStop(index=int(payload.get("index", 0)))
        return
    if kind == "message_delta":
        delta = payload.get("delta") or {}
        usage = _decode_usage(payload.get("usage"))
        yield MessageDelta(
            stop_reason=delta.get("stop_reason"),
            stop_sequence=delta.get("stop_sequence"),
            usage=usage,
        )
        return
    if kind == "message_stop":
        yield MessageStop()
        return
    if kind == "error":
        err = payload.get("error") or {}
        yield ErrorEvent(
            error_type=str(err.get("type") or "error"),
            message=str(err.get("message") or ""),
            raw=payload,
        )
        return
    # Unknown event — skip silently. Anthropic occasionally adds new
    # event types behind beta flags; ignoring them is safer than crashing.


def _decode_block(payload: dict[str, Any]) -> ContentBlock | None:
    """Build a typed ContentBlock from Anthropic's content_block dict."""

    btype = payload.get("type")
    if btype == "text":
        return TextBlock(text=str(payload.get("text", "")))
    if btype == "thinking":
        return ThinkingBlock(
            thinking=str(payload.get("thinking", "")),
            signature=payload.get("signature"),
        )
    if btype == "tool_use":
        return ToolUseBlock(
            id=str(payload.get("id") or f"toolu_{uuid4().hex}"),
            name=str(payload.get("name") or ""),
            input=dict(payload.get("input") or {}),
        )
    return None


def _decode_usage(payload: Any) -> Usage | None:
    """Translate Anthropic's usage block into our :class:`Usage`."""

    if not isinstance(payload, dict):
        return None
    return Usage(
        input_tokens=int(payload.get("input_tokens", 0) or 0),
        output_tokens=int(payload.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(
            payload.get("cache_creation_input_tokens", 0) or 0
        ),
        cache_read_input_tokens=int(
            payload.get("cache_read_input_tokens", 0) or 0
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_base_url(url: str) -> str:
    """Strip trailing ``/`` and ``/messages`` so the client base is at ``/v1``."""

    s = url.rstrip("/")
    if s.endswith("/messages"):
        s = s[: -len("/messages")]
    return s
