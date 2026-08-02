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
from ..http import make_client
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
from .base import HTTPProviderMixin, normalize_messages, strip_control_keys

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
        auth_token: str | None = None,
        base_url: str | None = None,
        anthropic_version: str | None = None,
        anthropic_beta: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
    ) -> None:
        # Two auth styles, mirroring Claude Code (services/api/client.ts):
        #   * x-api-key      — a direct Anthropic API key (ANTHROPIC_API_KEY)
        #   * Authorization: Bearer — an OAuth access token / gateway token
        #     (ANTHROPIC_AUTH_TOKEN). This is what a subscription OAuth login,
        #     or a Bedrock/Vertex/Azure/LiteLLM gateway in front of Anthropic,
        #     issues. auth_token wins when both are present.
        # Refresh a subscription OAuth token if it's expired/near-expiry, so a
        # long-lived process doesn't 401 forever once the short-lived access
        # token lapses. No-op unless a refresh token was stored at login, and
        # only when no explicit auth_token was passed in.
        if not auth_token:
            try:
                from ..anthropic_oauth import ensure_fresh_anthropic_token  # noqa: PLC0415
                ensure_fresh_anthropic_token()
            except Exception:  # noqa: BLE001 — never block construction on refresh
                pass
        # Strip stray whitespace/newlines — .env files and copy-paste often add a
        # trailing \n, which would otherwise poison the auth header and 401.
        token = (auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip() or None
        key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None
        if not token and not key:
            raise AuthError(
                "AnthropicPassthroughProvider needs credentials. Pass api_key=… "
                "(or $ANTHROPIC_API_KEY) for a direct key, or auth_token=… (or "
                "$ANTHROPIC_AUTH_TOKEN) for an OAuth / gateway Bearer token."
            )

        url = _normalize_base_url(base_url or ANTHROPIC_DEFAULT_BASE_URL)
        version = anthropic_version or ANTHROPIC_DEFAULT_VERSION

        headers: dict[str, str] = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": version,
        }
        if token:
            headers["authorization"] = f"Bearer {token}"
            # A subscription OAuth token against api.anthropic.com requires the
            # oauth beta header (Claude Code sends OAUTH_BETA_HEADER). Skip it for
            # gateways (non-anthropic.com base) which handle auth themselves.
            if not anthropic_beta and "anthropic.com" in url:
                headers["anthropic-beta"] = "oauth-2025-04-20"
        else:
            headers["x-api-key"] = key
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
        thinking: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream events from Anthropic's Messages API.

        ``thinking`` is the universal reasoning config —
        ``{"type": "adaptive"|"enabled"|"disabled", "budget_tokens": int|None}``.
        It maps to Anthropic's native ``thinking`` block (``{"type": "enabled",
        "budget_tokens": N}`` when a budget is given, else ``{"type": "adaptive"}``
        / ``{"type": "disabled"}``). An explicit ``extra["thinking"]`` wins. When
        ``thinking`` is ``None`` the request body is byte-for-byte unchanged.
        """

        if not model:
            raise ProviderError(
                "AnthropicPassthroughProvider.stream needs a model name "
                "(e.g. 'claude-sonnet-4-5')."
            )

        messages_list = list(messages)
        system_text, body_messages = _split_system(system, messages_list)

        encoded = [_encode_message(m) for m in body_messages]
        payload: dict[str, Any] = {
            "model": model,
            "messages": encoded,
            "max_tokens": int(max_tokens),
            "stream": True,
        }
        cache = getattr(self, "cache_prompts", True)
        if system_text:
            # A cache breakpoint on the (stable) system prompt lets Anthropic
            # read the whole prefix from cache on every later turn instead of
            # re-billing it. System becomes a content array carrying the marker.
            payload["system"] = (
                [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
                if cache else system_text
            )
        if cache and encoded:
            _mark_cache_breakpoint(encoded[-1])  # cache the conversation so far
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if tools:
            payload["tools"] = _normalize_tools(tools)
        # Universal thinking config -> Anthropic thinking block. An explicit
        # extra["thinking"] takes precedence (respected via the guard + the
        # setdefault merge below).
        # An explicit extra["thinking"] is already in Anthropic's native block
        # shape, so it wins outright; extra["max_thinking_tokens"] is the
        # Claude-SDK alias for a fixed budget and becomes one here. Everything
        # else in the control set is an SDK-level knob with no Anthropic wire
        # field — it is translated above or dropped, never forwarded (the
        # Messages API 400s on any unrecognized top-level key).
        block: dict[str, Any] | None = None
        if extra and isinstance(extra.get("thinking"), dict):
            block = dict(extra["thinking"])
        elif extra and extra.get("max_thinking_tokens") is not None:
            block = {"type": "enabled",
                     "budget_tokens": int(extra["max_thinking_tokens"])}
        elif thinking is not None:
            block = _thinking_to_anthropic(thinking)
        if block is not None:
            payload["thinking"] = block
            # With thinking on, Anthropic requires the default sampling
            # temperature — a non-default temperature is a 400 on the models
            # that take a thinking block. Drop it so enabling thinking can't
            # turn a valid request into a rejected one.
            if block.get("type") != "disabled":
                payload.pop("temperature", None)
        for k, v in strip_control_keys(extra).items():
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

    # Compaction boundaries become SystemMessages here, so BOTH paths below
    # place them instead of handing the encoder a type it can't serialize.
    messages = normalize_messages(messages)
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


def _mark_cache_breakpoint(encoded: dict[str, Any]) -> None:
    """Add ``cache_control: ephemeral`` to the last content block of an already
    encoded message, so Anthropic caches the conversation prefix up to here.
    Normalizes string content to a single text block so the marker has a home."""
    content = encoded.get("content")
    if isinstance(content, str):
        encoded["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = {"type": "ephemeral"}


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
    # Per-index streaming state shared across frames. Used to re-attach a
    # thinking block's signature — Anthropic delivers it via a trailing
    # ``signature_delta`` frame, AFTER the block already started with an empty
    # signature — so the reconstructed ThinkingBlock round-trips the signature
    # Anthropic requires when the prior thinking is echoed on a follow-up turn.
    stream_state: dict[int, dict[str, Any]] = {}

    async for line in response.aiter_lines():
        if line == "" or line == "\n":
            # End of an SSE frame — emit if we have data.
            if data_lines:
                payload_str = "\n".join(data_lines)
                data_lines = []
                event_name = current_event
                current_event = None
                async for ev in _frame_to_events(event_name, payload_str, stream_state):
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
        async for ev in _frame_to_events(current_event, payload_str, stream_state):
            yield ev


async def _frame_to_events(
    event_name: str | None,
    payload_str: str,
    stream_state: dict[int, dict[str, Any]] | None = None,
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

    state = stream_state if stream_state is not None else {}

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
        if isinstance(block, ThinkingBlock):
            # Anthropic starts a thinking block with an empty signature and
            # streams the real one later via ``signature_delta``. Track the
            # accumulating text + signature so we can re-attach it at block stop.
            state[index] = {
                "thinking": [block.thinking],
                "signature": block.signature,
                "sig_from_delta": False,
            }
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
            text = str(delta.get("thinking", ""))
            st = state.get(index)
            if st is not None:
                st["thinking"].append(text)
            yield ContentBlockDelta(
                index=index,
                delta=ThinkingDelta(thinking=text),
            )
        elif dtype == "signature_delta":
            # Signature for the in-flight thinking block. There's no signature
            # StreamEvent to carry it live; stash it and re-emit the block start
            # (with the full thinking text) at content_block_stop so the
            # assembler's ThinkingBlock keeps the signature. Discarding it would
            # make a follow-up request echoing this thinking fail Anthropic's
            # signature validation.
            st = state.get(index)
            if st is not None:
                sig = str(delta.get("signature", "")) or st.get("signature")
                st["signature"] = sig
                st["sig_from_delta"] = True
            return
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
        index = int(payload.get("index", 0))
        st = state.pop(index, None)
        if st is not None and st.get("sig_from_delta") and st.get("signature"):
            # Re-issue the thinking block start carrying the finalized text +
            # signature. The assembler rebuilds its block from this start, so
            # the reconstructed ThinkingBlock (and any follow-up request that
            # echoes it) now carries the signature Anthropic validates against.
            yield ContentBlockStart(
                index=index,
                block=ThinkingBlock(
                    thinking="".join(st["thinking"]),
                    signature=st["signature"],
                ),
            )
        yield ContentBlockStop(index=index)
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
    # Anthropic reports cache tokens SEPARATELY from ``input_tokens`` — its
    # ``input_tokens`` counts only the fresh (uncached) prompt tokens. The rest
    # of the SDK follows the OpenAI convention, where ``prompt_tokens`` already
    # INCLUDES cached tokens (see openai_compat._decode_usage) and the cache
    # fields are a *subset* of ``input_tokens``. budget.CostModel.cost and
    # BudgetState.total_tokens both rely on that subset invariant. Fold the
    # cache tokens into ``input_tokens`` here so cost/token accounting is
    # correct — the cache fields are still carried for the per-cache rate
    # adjustment, without being double-counted or lost.
    cache_creation = int(payload.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(payload.get("cache_read_input_tokens", 0) or 0)
    return Usage(
        input_tokens=int(payload.get("input_tokens", 0) or 0)
        + cache_creation
        + cache_read,
        output_tokens=int(payload.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _thinking_to_anthropic(thinking: dict[str, Any]) -> dict[str, Any] | None:
    """Translate the universal thinking config to an Anthropic ``thinking`` block.

    Universal shape: ``{"type": "adaptive"|"enabled"|"disabled",
    "budget_tokens": int|None}``.

    * ``disabled``                     -> ``{"type": "disabled"}``
    * ``enabled``/``adaptive`` + budget -> ``{"type": "enabled", "budget_tokens": N}``
      (the fixed-budget form; required on pre-4.6 models, honored elsewhere)
    * ``adaptive`` without a budget      -> ``{"type": "adaptive"}`` (Claude paces
      itself; the modern default on 4.6+)

    Returns ``None`` for an unrecognized/empty config so the caller leaves the
    payload untouched.
    """

    if not isinstance(thinking, dict):
        return None
    ttype = thinking.get("type")
    budget = thinking.get("budget_tokens")
    if ttype == "disabled":
        return {"type": "disabled"}
    if ttype in ("enabled", "adaptive"):
        if budget is not None:
            return {"type": "enabled", "budget_tokens": int(budget)}
        # No budget: adaptive is a valid standalone block; a bare "enabled" is
        # not (it needs budget_tokens), so fall through to adaptive for it too.
        return {"type": "adaptive"}
    return None


def _normalize_base_url(url: str) -> str:
    """Strip trailing ``/`` and ``/messages`` so the client base is at ``/v1``."""

    s = url.rstrip("/")
    if s.endswith("/messages"):
        s = s[: -len("/messages")]
    return s
