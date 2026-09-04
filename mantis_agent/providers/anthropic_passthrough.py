"""Anthropic — talk to Claude over the native Messages API.

Claude is a **first-class provider** here, alongside OpenAI, Gemini, Grok and
the open-source backends. ``Agent(model="claude-opus-5")`` with
``ANTHROPIC_API_KEY`` (or a subscription OAuth token in
``ANTHROPIC_AUTH_TOKEN``) set needs no other configuration: ``routing.py``
maps ``claude-*`` to the ``"anthropic"`` sentinel and
:func:`mantis_agent.providers.base.detect_provider` maps both the sentinel
and a bare ``claude-*`` name to this adapter.

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

Thinking
--------
The universal ``thinking`` config is translated per model generation, because
Anthropic changed the knob (see :func:`_claude_generation`):

* Haiku 4.5, Sonnet/Opus 4.5 and older, and the 4.6 pair: the fixed-budget
  form ``{"type": "enabled", "budget_tokens": N}`` (``max_tokens`` is raised
  above the budget when needed — the API requires ``budget_tokens <
  max_tokens``).
* Opus 4.7 / 4.8 / 5, Sonnet 5: ``{"type": "adaptive"}`` plus
  ``output_config: {"effort": …}`` derived from the budget (``budget_tokens``
  is rejected with a 400 there).
* Fable / Mythos: thinking is always on and an explicit block is rejected;
  only ``output_config.effort`` is sent.

Selecting this adapter
----------------------
Any of these forms works:

* A bare Claude model name (auto-routed)::

      agent = Agent(model="claude-opus-5")

* The literal sentinel ``"anthropic"`` as backend::

      agent = Agent(model="claude-opus-5", backend="anthropic")

* An Anthropic API URL, or an Anthropic-Messages gateway path
  (``…/anthropic/v1`` — Bedrock Access Gateway, Azure Foundry, LiteLLM)::

      agent = Agent(model="claude-opus-5", backend="https://api.anthropic.com/v1")

* An instance, when you want to set headers / beta flags yourself::

      provider = AnthropicPassthroughProvider(api_key=..., anthropic_beta="...")
      agent = Agent(model="claude-opus-5", provider=provider)
"""

from __future__ import annotations

import os
import re
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

#: The Claude Code identity string. A subscription OAuth token
#: (``sk-ant-oat…``) is entitled to the premium models only on requests that
#: present this identity: it must be the **first** ``system`` block and must
#: match verbatim. Without it, api.anthropic.com serves Haiku normally but
#: answers Opus/Sonnet/Fable with ``429 rate_limit_error {"message": "Error"}``
#: — an opaque body that reads like a quota problem and is not one. Folding the
#: string into the caller's own system text does *not* satisfy the check; it has
#: to stand alone as its own block, which is why this is prepended rather than
#: concatenated.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

_JSON_DECODER = msgspec.json.Decoder()
_PAYLOAD_ENCODER = msgspec.json.Encoder()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AnthropicPassthroughProvider(HTTPProviderMixin):
    """Adapter for the native Anthropic Messages API — the Claude provider.

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
        # A subscription OAuth token against api.anthropic.com is the one auth
        # style that also constrains the *body* — see CLAUDE_CODE_IDENTITY.
        # Gateways (non-anthropic.com base) mint their own Bearer tokens, handle
        # auth themselves, and must not have an identity block injected.
        self._oauth_subscription = bool(
            token and token.startswith("sk-ant-oat") and "anthropic.com" in url
        )
        if token:
            headers["authorization"] = f"Bearer {token}"
            # A subscription OAuth token against api.anthropic.com requires the
            # oauth beta header (Claude Code sends OAUTH_BETA_HEADER). Skip it for
            # gateways (non-anthropic.com base) which handle auth themselves.
            if "anthropic.com" in url:
                headers["anthropic-beta"] = "oauth-2025-04-20"
        else:
            headers["x-api-key"] = key
        if anthropic_beta:
            # Append rather than replace: a caller asking for an extra beta
            # (e.g. a context-window flag) must not silently drop the oauth beta
            # header, which would turn a working OAuth token into a 401.
            existing = headers.get("anthropic-beta")
            wanted = [f.strip() for f in anthropic_beta.split(",") if f.strip()]
            merged = [f for f in (existing or "").split(",") if f.strip()]
            merged += [f for f in wanted if f not in merged]
            headers["anthropic-beta"] = ",".join(merged)
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
        It maps to Anthropic's native knob for the model's generation — the
        ``{"type": "enabled", "budget_tokens": N}`` block on Haiku 4.5 /
        4.6-and-older, ``{"type": "adaptive"}`` + ``output_config.effort`` on
        4.7+ / 5-series, effort only on Fable/Mythos (see module docstring).
        An explicit ``extra["thinking"]`` wins. When ``thinking`` is ``None``
        the request body is byte-for-byte unchanged.
        """

        if not model:
            raise ProviderError(
                "AnthropicPassthroughProvider.stream needs a model name "
                "(e.g. 'claude-opus-5')."
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
        # Prepend the Claude Code identity block when the credential is a
        # subscription OAuth token, unless the caller already leads with it.
        # It has to be a standalone first block, so the caller's own system text
        # follows as a second one instead of being merged in.
        identity = getattr(self, "_oauth_subscription", False) and not (
            system_text or ""
        ).startswith(CLAUDE_CODE_IDENTITY)
        if system_text or identity:
            blocks: list[dict[str, Any]] = []
            if identity:
                blocks.append({"type": "text", "text": CLAUDE_CODE_IDENTITY})
            if system_text:
                blocks.append({"type": "text", "text": system_text})
            if cache:
                # A cache breakpoint on the (stable) system prompt lets Anthropic
                # read the whole prefix from cache on every later turn instead of
                # re-billing it. The marker goes on the last block so it covers
                # the identity block too.
                blocks[-1]["cache_control"] = {"type": "ephemeral"}
            # A single un-cached text block can stay a plain string; anything
            # with the identity block in front must be an array.
            payload["system"] = (
                system_text if (not cache and len(blocks) == 1) else blocks
            )
        if cache and encoded:
            _mark_cache_breakpoint(encoded[-1])  # cache the conversation so far
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if tools:
            payload["tools"] = _normalize_tools(tools)
        # Universal thinking config -> Anthropic thinking block. An explicit
        # extra["thinking"] is already in Anthropic's native block shape, so it
        # wins outright; extra["max_thinking_tokens"] is the Claude-SDK alias
        # for a fixed budget and becomes one here (in whichever form this
        # model generation accepts). extra["effort"] / ["reasoning_effort"]
        # become ``output_config.effort`` on generations that have it.
        # Everything else in the control set is an SDK-level knob with no
        # Anthropic wire field — translated above or dropped, never forwarded
        # (the Messages API 400s on any unrecognized top-level key).
        generation = _claude_generation(model)
        block: dict[str, Any] | None = None
        effort: str | None = None
        if extra and isinstance(extra.get("thinking"), dict):
            block = dict(extra["thinking"])
        else:
            cfg: dict[str, Any] | None = None
            if extra and extra.get("max_thinking_tokens") is not None:
                cfg = {"type": "enabled",
                       "budget_tokens": int(extra["max_thinking_tokens"])}
            elif thinking is not None:
                cfg = thinking
            if cfg is not None:
                block, effort, payload["max_tokens"] = _thinking_plan(
                    cfg, generation, payload["max_tokens"]
                )
        if extra:
            word = extra.get("reasoning_effort", extra.get("effort"))
            if word is not None:
                effort = _normalize_claude_effort(str(word), generation) or effort
        if block is not None:
            payload["thinking"] = block
        if effort is not None and generation != "budget" and (
            block is None or "budget_tokens" not in block
        ) and (block is None or block.get("type") != "disabled" or effort in ("low", "medium", "high")):
            # ``output_config.effort`` is GA on 4.6+; never paired with the
            # deprecated budget form, and never sent to a pre-4.6 model.
            payload.setdefault("output_config", {})["effort"] = effort
        # With thinking on, Anthropic requires the default sampling
        # temperature — a non-default temperature is a 400 on the models
        # that take a thinking block. Opus 4.7+ / the 5-series / Fable have
        # removed sampling parameters outright. Drop it so enabling thinking
        # (or picking a current model) can't turn a valid request into a
        # rejected one.
        if (block is not None and block.get("type") != "disabled") or (
            generation == "always_on" or _sampling_removed(model)
        ):
            payload.pop("temperature", None)
        for k, v in strip_control_keys(extra).items():
            payload.setdefault(k, v)

        body = _PAYLOAD_ENCODER.encode(payload)

        for attempt in range(2):
            async with self.client.stream(
                "POST",
                "/messages",
                content=body,
            ) as response:
                if (attempt == 0 and response.status_code == 400
                        and "thinking" in payload):
                    # The generation table is a best guess for an id we have
                    # never seen. If the API rejects the thinking form we
                    # picked (``budget_tokens`` on a model that dropped it, or
                    # ``adaptive`` on one that never had it), swap to the other
                    # form once rather than fail the turn.
                    text = (await response.aread()).decode("utf-8", "replace")
                    swapped = _swap_thinking_form(payload, text)
                    if swapped:
                        body = _PAYLOAD_ENCODER.encode(payload)
                        continue
                    _raise_for_body(response, text)
                await _raise_if_error(response)
                async for ev in _iter_normalized_events(response):
                    yield ev
                return

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
    _raise_for_body(response, text)


def _raise_for_body(response: httpx.Response, text: str) -> None:
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


# ``claude-opus-4-8``, ``claude-3-5-sonnet-20241022``, ``claude-sonnet-4-20250514``:
# the minor version is at most two digits and never runs into a date suffix.
_CLAUDE_ID_RE = re.compile(
    r"claude-(?:(opus|sonnet|haiku|fable|mythos)-)?(\d+)(?:[-.](\d{1,2})(?!\d))?"
)

#: Fixed thinking budget used when a "budget" generation is asked for
#: ``adaptive`` (which it doesn't understand) or ``enabled`` with no budget.
_DEFAULT_BUDGET = 8192
#: Anthropic's floor for ``budget_tokens``.
_MIN_BUDGET = 1024
#: Room left for the visible answer above the thinking budget when
#: ``max_tokens`` has to be raised to satisfy ``budget_tokens < max_tokens``.
_ANSWER_HEADROOM = 1024


def _claude_generation(model: str) -> str:
    """Which thinking dialect a Claude id speaks.

    * ``"budget"``    — ``{"type": "enabled", "budget_tokens": N}`` only:
      Haiku 4.5 and everything ≤ 4.5. No ``adaptive``, no ``effort``.
    * ``"hybrid"``    — the 4.6 pair (Opus 4.6 / Sonnet 4.6): ``adaptive`` and
      ``output_config.effort`` work, and the budget form is deprecated but
      still accepted — an explicit budget keeps its exact meaning there.
    * ``"adaptive"``  — ``{"type": "adaptive"}`` + ``output_config.effort``:
      Opus 4.7 / 4.8 / 5, Sonnet 5, and any newer id (``budget_tokens`` 400s).
    * ``"always_on"`` — Fable / Mythos: thinking can't be configured at all;
      only ``output_config.effort`` is sent.

    Unknown / unparseable Claude ids default to ``"adaptive"`` — the form
    every current model accepts — and the request-time fallback swaps once if
    the API disagrees.
    """

    bare = model.lower().rsplit("/", 1)[-1]
    if "fable" in bare or "mythos" in bare:
        return "always_on"
    m = _CLAUDE_ID_RE.search(bare)
    if not m:
        return "adaptive"
    family, major_s, minor_s = m.group(1), m.group(2), m.group(3)
    major, minor = int(major_s), int(minor_s or 0)
    if family == "haiku":
        return "budget" if major <= 4 else "adaptive"
    if major >= 5:
        return "adaptive"
    if major == 4 and minor >= 7:
        return "adaptive"
    if major == 4 and minor == 6:
        return "hybrid"
    return "budget"


def _sampling_removed(model: str) -> bool:
    """Opus 4.7+, the 5-series and Fable/Mythos reject ``temperature`` /
    ``top_p`` / ``top_k`` outright (400), so a default temperature must not
    be sent to them."""

    bare = model.lower().rsplit("/", 1)[-1]
    if "fable" in bare or "mythos" in bare:
        return True
    m = _CLAUDE_ID_RE.search(bare)
    if not m:
        return False
    family, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if family == "haiku":
        return major >= 5
    return major >= 5 or (major == 4 and minor >= 7)


def _budget_to_effort(budget: int) -> str:
    """The ``output_config.effort`` level a fixed token budget stands for.
    Mirrors the agent's effort ladder (2048 / 12288 / 24576)."""

    if budget <= 2048:
        return "low"
    if budget <= 8192:
        return "medium"
    if budget <= 16384:
        return "high"
    return "max"


def _normalize_claude_effort(word: str, generation: str) -> str | None:
    """An SDK effort word as Anthropic spells it. ``ultra`` (a multi-agent
    mode) and anything unknown clamp to ``max``; ``minimal`` → ``low``;
    ``xhigh`` only exists from Opus 4.7 / Sonnet 5 on and drops to ``high``
    on the 4.6 pair. Pre-4.6 models have no effort field at all."""

    w = word.strip().lower()
    if w == "minimal":
        w = "low"
    if w == "ultra":
        w = "max"
    if w == "none":
        w = "low"
    if w not in ("low", "medium", "high", "xhigh", "max"):
        return None
    if w == "xhigh" and generation in ("budget", "hybrid"):
        return "high"
    return w


def _thinking_plan(
    thinking: dict[str, Any], generation: str, max_tokens: int
) -> tuple[dict[str, Any] | None, str | None, int]:
    """Translate the universal config for one generation.

    Returns ``(thinking_block, effort, max_tokens)`` — the block to send (or
    ``None`` to omit), the ``output_config.effort`` to send (or ``None``), and
    a possibly-raised ``max_tokens`` (the budget form needs ``budget_tokens <
    max_tokens``, and a budget with no room for an answer is useless anyway).

    Universal shape: ``{"type": "adaptive"|"enabled"|"disabled",
    "budget_tokens": int|None}``.
    """

    if not isinstance(thinking, dict):
        return None, None, max_tokens
    ttype = thinking.get("type")
    budget = thinking.get("budget_tokens")
    if ttype not in ("adaptive", "enabled", "disabled"):
        return None, None, max_tokens

    if generation == "always_on":
        # Fable / Mythos: any explicit block is a 400. Effort is the only lever.
        if ttype == "disabled":
            return None, "low", max_tokens
        return None, (_budget_to_effort(int(budget)) if budget is not None else None), max_tokens

    if generation == "adaptive":
        if ttype == "disabled":
            return {"type": "disabled"}, None, max_tokens
        effort = _budget_to_effort(int(budget)) if budget is not None else None
        return {"type": "adaptive"}, effort, max_tokens

    if ttype == "disabled":
        return {"type": "disabled"}, None, max_tokens

    if generation == "hybrid" and budget is None:
        # 4.6: ``adaptive`` is the modern form and a bare ``enabled`` (no
        # budget) is not valid, so both map to adaptive.
        return {"type": "adaptive"}, None, max_tokens

    # The fixed-budget form — required on ≤ 4.5, honoured (deprecated) on 4.6.
    # ``adaptive`` without a budget on a pre-4.6 model gets a real budget.
    budget = max(_MIN_BUDGET, int(budget if budget is not None else _DEFAULT_BUDGET))
    if budget >= max_tokens:
        max_tokens = budget + _ANSWER_HEADROOM
    return {"type": "enabled", "budget_tokens": budget}, None, max_tokens


def _swap_thinking_form(payload: dict[str, Any], error_text: str) -> bool:
    """Flip ``payload["thinking"]`` to the other form after a 400 that names
    the thinking config. Returns True when the payload changed and the
    request should be retried once.

    * budget form rejected (a model that dropped ``budget_tokens``) →
      ``adaptive`` + the equivalent ``output_config.effort``.
    * ``adaptive`` rejected (a pre-4.6 id the table didn't recognise) → the
      budget form, sized from the effort that was going to be sent.
    * ``disabled`` rejected (an always-on model) → omit the block.
    """

    text = error_text.lower()
    if "thinking" not in text and "budget_tokens" not in text:
        return False
    block = payload.get("thinking")
    if not isinstance(block, dict):
        return False
    if block.get("type") == "disabled":
        payload.pop("thinking", None)
        return True
    if "budget_tokens" in block:
        effort = _budget_to_effort(int(block["budget_tokens"]))
        payload["thinking"] = {"type": "adaptive"}
        payload.setdefault("output_config", {})["effort"] = effort
        return True
    if block.get("type") == "adaptive":
        oc = payload.get("output_config") or {}
        effort = oc.pop("effort", None)
        if not oc:
            payload.pop("output_config", None)
        budget = {"low": 2048, "medium": 8192, "high": 12288, "xhigh": 16384,
                  "max": 24576}.get(effort or "", _DEFAULT_BUDGET)
        if budget >= payload.get("max_tokens", 0):
            payload["max_tokens"] = budget + _ANSWER_HEADROOM
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return True
    return False


def _normalize_base_url(url: str) -> str:
    """Strip trailing ``/`` and ``/messages`` so the client base is at ``/v1``."""

    s = url.rstrip("/")
    if s.endswith("/messages"):
        s = s[: -len("/messages")]
    return s
