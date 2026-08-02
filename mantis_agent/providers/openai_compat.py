"""OpenAI-compatible adapter — the workhorse.

One adapter, every provider that speaks ``POST /v1/chat/completions``: vLLM,
Together, Fireworks, Groq, OpenRouter, Cerebras, DeepInfra, Anyscale, DeepSeek,
Mistral's own API.

Responsibilities
----------------
1. Auth + base-URL resolution (env-key fallback chain, vLLM localhost default).
2. Backend capability detection (hosted profile match or generic vLLM-style).
3. Translates universal ``Message`` / ``ContentBlock`` to the OpenAI chat shape
   for both Path A (native ``tools`` + emitted ``tool_calls``) and Paths B/C
   (prompt-engineered ``<tool_call>`` XML in the system prompt).
4. Issues the SSE stream and normalizes chunks into ``StreamEvent``. Path A
   passes through; B/C run content deltas through ``ToolCallTextParser`` (and
   ``ThinkingParser`` when the model emits inline ``<think>`` tags).
5. Surfaces ``usage`` from the final chunk via ``stream_options.include_usage``.

Perf (plan §9): no msgspec on the per-chunk hot path beyond the SSE decode;
one ``httpx.AsyncClient`` per provider; the input message iterable is
materialized once (no deep copies of blocks).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_quoteattr

import httpx
import msgspec

from ..capabilities import (
    BackendCapability,
    ModelCapability,
    ToolUsePath,
    hosted_profile_from_url,
    resolve_tool_use_path,
)
from ..errors import ProviderError, StreamProtocolError
from ..events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
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
from .base import PROVIDER_CONTROL_KEYS, HTTPProviderMixin, normalize_messages

# Soft import — thinking parser ships in a later milestone. The provider still
# works without it; we just won't split <think>...</think> out of content
# when ``emits_inline_thinking`` is True.
try:
    from ..streaming.thinking_parser import ThinkingParser  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — parser not yet shipped
    ThinkingParser = None  # type: ignore[assignment]

from ..streaming.text_tool_parser import (
    TextChunk,
    ToolCallInputDelta,
    ToolCallStart,
    ToolCallStop,
    ToolCallTextParser,
)

__all__ = ["OpenAICompatProvider"]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://localhost:8000/v1"  # vLLM's default OpenAI-compat URL

# Order matters: first env var that's set wins. OPENAI_API_KEY first because
# it's the de-facto standard and many users export it once for everything.
_ENV_KEY_CHAIN: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPINFRA_API_KEY",
    "CEREBRAS_API_KEY",
    "ANYSCALE_API_KEY",
    "MOONSHOT_API_KEY",
)

# Fallback when we can't match a hosted provider — model the most common self-
# hosted case (vLLM-style) and let actual feature detection happen lazily via
# the resolver.
_GENERIC_VLLM_PROFILE = BackendCapability(
    kind="openai_compat",
    supports_native_tools=True,
    supports_grammar=True,
    supports_logprobs=True,
    supports_prefix_caching=True,
    provider_hint="vllm",
)

# Shared msgspec encoder for outbound payloads. ~30% faster than json.dumps for
# our message shapes; thread-safe.
_PAYLOAD_ENCODER = msgspec.json.Encoder()
_JSON_DECODER = msgspec.json.Decoder()


# ---------------------------------------------------------------------------
# Prompt-engineered tool protocol (Path B / C)
# ---------------------------------------------------------------------------

_TOOL_PROTOCOL_PREAMBLE = (
    "You have access to the following tools. To call a tool, emit a single\n"
    "<tool_call> block in your response. You can call multiple tools in one\n"
    "response by emitting multiple <tool_call> blocks back-to-back.\n"
    "\n"
    "<tool_call>\n"
    '{"name": "<tool_name>", "arguments": {<JSON object>}}\n'
    "</tool_call>\n"
    "\n"
    "Available tools:\n"
)

_TOOL_PROTOCOL_TRAILER = (
    "\n"
    "When you receive <tool_result> messages, continue your response using\n"
    "the new information. If you have completed the user's request, respond\n"
    "without any <tool_call> blocks.\n"
)


def _render_prompt_engineered_tools(tools: list[dict[str, Any]]) -> str:
    """System-prompt block teaching a non-native model the ``<tool_call>``
    protocol. Accepts OpenAI function-tool format OR flattened Anthropic-shaped.

    Each tool is rendered as its own readable, pretty-printed section rather than
    one dense minified JSON blob — weak OSS models (the only ones routed through
    Paths B/C) parse a spaced per-tool listing far more reliably, losing track of
    which ``required``/``properties`` belong to which tool much less often."""

    flattened = [t["function"] if isinstance(t.get("function"), dict) else t for t in tools]
    sections: list[str] = []
    for fn in flattened:
        name = fn.get("name", "")
        description = fn.get("description", "")
        schema = fn.get("parameters") or fn.get("input_schema") or {}
        lines = [f"## {name}"]
        if description:
            lines.append(description)
        lines.append("Parameters (JSON schema):")
        lines.append(json.dumps(schema, indent=2))
        required = schema.get("required") if isinstance(schema, dict) else None
        if required:
            lines.append("Required: " + ", ".join(str(r) for r in required))
        sections.append("\n".join(lines))
    return _TOOL_PROTOCOL_PREAMBLE + "\n\n".join(sections) + _TOOL_PROTOCOL_TRAILER


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAICompatProvider(HTTPProviderMixin):
    """Adapter for any backend that speaks the OpenAI Chat Completions wire
    format. Handles all three tool-use paths (native, prompt-engineered,
    grammar-constrained) by dispatching on the resolved ``ToolUsePath``."""

    name = "openai_compat"
    backend_capability: BackendCapability

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
        model_capability: ModelCapability | None = None,
    ) -> None:
        url = base_url or DEFAULT_BASE_URL
        # api_key semantics: a non-empty string is used verbatim; ``None`` means
        # "discover a key from the env chain"; an empty string ``""`` means
        # "explicitly NO auth — do not read the env" (used by providers like
        # Modal that authenticate with their own headers).
        key = api_key
        if key is None:
            for var in _ENV_KEY_CHAIN:
                v = os.environ.get(var)
                if v and v.strip():
                    key = v
                    break
        # Strip stray whitespace/newlines from an env/.env key so it doesn't
        # poison the Bearer header and 401 confusingly.
        if key:
            key = key.strip()

        headers: dict[str, str] = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if key:
            headers["authorization"] = f"Bearer {key}"
        # OpenRouter wants identifying headers for analytics; harmless elsewhere.
        if "openrouter" in url.lower():
            headers.setdefault("http-referer", "https://github.com/teddyoweh/mantis-agent-sdk")
            headers.setdefault("x-title", "mantis-agent-sdk")
        if default_headers:
            headers.update(default_headers)

        self.client = make_client(base_url=url, headers=headers)

        # Backend capability: explicit > URL match > generic vLLM-style.
        if backend_capability is not None:
            self.backend_capability = backend_capability
        else:
            profile = hosted_profile_from_url(url)
            self.backend_capability = profile if profile is not None else _GENERIC_VLLM_PROFILE

        # Stashed; used only when caller omits ``model_capability`` on stream().
        self._default_model_capability = model_capability

    # ------------------------------------------------------------------
    # Streaming entrypoint
    # ------------------------------------------------------------------

    async def stream(
        self,
        *,
        model: str,
        messages: Iterable[Message],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        extra: dict[str, Any] | None = None,
        model_capability: ModelCapability | None = None,
        thinking: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a chat completion as normalized ``StreamEvent``s.

        ``thinking`` is the universal reasoning config —
        ``{"type": "adaptive"|"enabled"|"disabled", "budget_tokens": int|None}``.
        It is translated to ``reasoning_effort`` / ``max_thinking_tokens`` (only
        for models that accept a request-side knob) and never overrides an
        explicit reasoning field the caller already put in ``extra``. When
        ``thinking`` is ``None`` the built payload is byte-for-byte unchanged.
        """

        cap = model_capability or self._default_model_capability
        # Without tools the path doesn't matter, but we keep Path A semantics
        # so the (text-only) translator runs the simpler hot loop.
        path: ToolUsePath = (
            resolve_tool_use_path(cap, self.backend_capability)
            if (cap is not None and tools)
            else "A"
        )
        # Path C promises grammar-constrained sampling (server-enforced JSON),
        # but this adapter does not build/inject a guided_json/GBNF grammar for
        # the ``<tool_call>`` protocol — and it can't safely, since guided_json
        # would forbid the interleaved prose the protocol requires. Rather than
        # mislabel C while behaving identically to B on the wire (a false
        # enforcement claim), downgrade to B honestly.
        if path == "C":
            path = "B"

        payload = self._build_payload(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=extra,
            path=path,
            thinking=thinking,
            model_capability=cap,
        )

        # why: ``client.stream(...)`` is the only httpx call that doesn't drain
        # the body up front; we need that to keep the SSE channel open.
        # One retry: some recent OpenAI models require ``max_completion_tokens``
        # instead of ``max_tokens`` (and reject a custom temperature) but aren't
        # matched by name — e.g. the bare ``chat-latest`` alias. Rather than fail
        # a valid model, swap the field on that specific 400 and retry once.
        # Track which param repairs we've already applied so each can fire on
        # whatever attempt first surfaces it — keying them all on ``_attempt == 0``
        # meant a first-attempt context-length lowering disabled every later
        # field/temperature/reasoning repair.
        _swapped_token_field = False
        _dropped_temperature = False
        _disabled_reasoning_effort = False
        for _attempt in range(4):
            async with self.client.stream(
                "POST",
                "/chat/completions",
                content=_PAYLOAD_ENCODER.encode(payload),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    # Handle the "recent OpenAI model" param rejections
                    # independently (OpenAI may report any first): swap
                    # max_tokens→max_completion_tokens, and/or drop a temperature
                    # the model won't accept. Retry if we changed anything.
                    body = response.content
                    retry = False
                    if (not _swapped_token_field
                            and "max_tokens" in payload and b"max_completion_tokens" in body):
                        payload["max_completion_tokens"] = payload.pop("max_tokens")
                        # Models that require max_completion_tokens (gpt-5.x /
                        # o-series) also reject a non-default temperature — drop
                        # it too so the retry doesn't just trip the next error.
                        payload.pop("temperature", None)
                        _swapped_token_field = True
                        _dropped_temperature = True
                        retry = True
                    if (not _dropped_temperature
                            and "temperature" in payload and b"temperature" in body
                            and (b"nsupported" in body or b"does not support" in body
                                 or b"only the default" in body or b"only supports" in body)):
                        payload.pop("temperature", None)
                        _dropped_temperature = True
                        retry = True
                    if (not _disabled_reasoning_effort
                            and "tools" in payload and b"reasoning_effort" in body
                            and b"none" in body):
                        payload["reasoning_effort"] = "none"
                        _disabled_reasoning_effort = True
                        retry = True
                    if retry:
                        continue
                    if _lower_max_tokens_for_context_error(payload, body):
                        continue
                    raise_for_status(response)

                if path == "A":
                    async for ev in _translate_native(response, model):
                        yield ev
                else:
                    emits_thinking = bool(cap and cap.emits_inline_thinking)
                    async for ev in _translate_prompt_engineered(
                        response, model, emits_thinking=emits_thinking
                    ):
                        yield ev
                return

    # ------------------------------------------------------------------
    # Payload construction
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        *,
        model: str,
        messages: Iterable[Message],
        system: str | None,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float | None,
        extra: dict[str, Any] | None,
        path: ToolUsePath,
        thinking: dict[str, Any] | None = None,
        model_capability: ModelCapability | None = None,
    ) -> dict[str, Any]:
        """Translate universal messages -> OpenAI chat shape and assemble the
        full request body."""

        # One-time materialize. We need two passes (split out system,
        # then serialize body). No deep copy of the underlying blocks.
        msg_list = list(messages)
        pulled_system, body_msgs = _split_system(msg_list)

        sys_string: str | None = system
        if pulled_system is not None:
            sys_string = (
                pulled_system if sys_string is None else f"{sys_string}\n\n{pulled_system}"
            )

        if path in ("B", "C") and tools:
            # Inject Hermes-Pro-style tool protocol *before* the user's system
            # prompt — the model has to see the rules before reading the task.
            protocol = _render_prompt_engineered_tools(tools)
            sys_string = protocol if not sys_string else f"{sys_string}\n\n{protocol}"

        wire_messages: list[dict[str, Any]] = []
        if sys_string is not None:
            wire_messages.append({"role": "system", "content": sys_string})
        for m in body_msgs:
            wire_messages.extend(_encode_message(m, path=path))

        # OpenAI's GPT-5 / o-series reject the legacy ``max_tokens`` (they want
        # ``max_completion_tokens``) and only accept the default temperature.
        # Other OpenAI-compat backends (vLLM, Groq, Together, …) never serve
        # these ids, so keying off the model name is safe and self-contained.
        _bare = model.lower().rsplit("/", 1)[-1]
        _new_openai = _bare.startswith(("gpt-5", "o1", "o3", "o4"))
        token_field = "max_completion_tokens" if _new_openai else "max_tokens"

        payload: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
            token_field: max_tokens,
            "stream": True,
            # why: this is the OpenAI-compat way to get token counts from the
            # final SSE chunk. Without it, ``usage`` is silently dropped.
            "stream_options": {"include_usage": True},
        }
        if temperature is not None and not _new_openai:
            payload["temperature"] = temperature

        if extra:
            effort = extra.get("reasoning_effort", extra.get("effort"))
            if effort == "minimal":
                effort = "low"
            if effort in ("ultra", "max"):
                # Ultra is a multi-agent orchestration mode; 'max' requires the
                # Responses API. Neither is a valid Chat Completions
                # reasoning_effort value (400 with no recovery), so clamp to the
                # highest value the endpoint accepts.
                effort = "high"
            if effort is not None:
                payload["reasoning_effort"] = effort
            # NB: a local name, NOT the ``thinking`` parameter. Rebinding it
            # here meant any non-empty ``extra`` without a "thinking" key
            # silently erased the universal thinking config below — so
            # extra={"verbosity": …} quietly turned reasoning off.
            extra_thinking = extra.get("thinking")
            if isinstance(extra_thinking, dict):
                thinking_effort = extra_thinking.get("effort")
                if thinking_effort == "minimal":
                    thinking_effort = "low"
                if thinking_effort is not None:
                    payload["reasoning_effort"] = thinking_effort
            # ``verbosity`` is a real GPT-5 Chat Completions field, but only
            # there — gpt-4o and every OSS server 400 on it.
            if extra.get("verbosity") is not None and _new_openai:
                payload["verbosity"] = extra["verbosity"]

        # Universal thinking config -> OpenAI reasoning knobs. Only applied when
        # the caller didn't already set a reasoning field (extra wins), and only
        # for models that accept a request-side knob — sending reasoning_effort to
        # a plain chat model (gpt-4o, most local checkpoints) is a hard 400.
        #
        # ``budget_tokens`` is deliberately dropped: Chat Completions has no
        # per-request thinking budget. ``reasoning_effort`` is the only knob, and
        # reasoning tokens are already billed against max_completion_tokens.
        if (thinking
                and "reasoning_effort" not in payload
                and _supports_request_reasoning(model, model_capability)):
            ttype = thinking.get("type")
            if ttype == "disabled":
                payload["reasoning_effort"] = "none"
            else:
                # "enabled" is an explicit ask for deeper reasoning; "adaptive"
                # lets the model self-pace — map to a middle setting. There's no
                # OpenAI reasoning_effort value for "adaptive", so pick sane
                # defaults the Chat Completions endpoint accepts.
                payload["reasoning_effort"] = "high" if ttype == "enabled" else "medium"

        if path == "A" and tools:
            payload["tools"] = _normalize_tool_defs(tools)
            payload["tool_choice"] = "auto"
            if _bare.startswith("gpt-5.6") and "reasoning_effort" not in payload:
                payload["reasoning_effort"] = "none"
        # why: Path C grammar (GBNF / guided_json / response_format) is built
        # elsewhere — caller injects it via ``extra``. Without it, Path C
        # degrades to Path B at the wire; the Hermes-Pro prompt is strict
        # enough that this is safe for most models.

        if extra:
            # Shallow-merge last so callers can override anything above (e.g.
            # guided_grammar, response_format, vendor knobs). Claude-style
            # reasoning aliases were normalized above; don't leak them as
            # unknown OpenAI parameters.
            passthrough = {
                k: v for k, v in extra.items()
                if k not in PROVIDER_CONTROL_KEYS and k not in {
                    # Never let opaque passthrough clobber the structural fields
                    # the translator owns — doing so would silently break the
                    # request (e.g. extra={'stream': False} or a stray messages).
                    "model", "messages", "stream", "stream_options", "tools",
                }
            }
            payload.update(passthrough)
        return payload


# ---------------------------------------------------------------------------
# Message encoding (universal -> OpenAI chat shape)
# ---------------------------------------------------------------------------


def _split_system(messages: list[Message]) -> tuple[str | None, list[Message]]:
    """Pull SystemMessages out. Multiple system messages concatenate with
    blank-line separators — last-wins would silently drop user intent."""

    sys_parts: list[str] = []
    body: list[Message] = []
    for m in normalize_messages(messages):
        if isinstance(m, SystemMessage):
            sys_parts.append(_system_to_string(m))
        else:
            body.append(m)
    return ("\n\n".join(sys_parts) if sys_parts else None), body


def _system_to_string(m: SystemMessage) -> str:
    return m.content if isinstance(m.content, str) else "".join(b.text for b in m.content)


def _encode_message(m: Message, *, path: ToolUsePath) -> list[dict[str, Any]]:
    """Encode one universal message into one or more OpenAI wire messages.
    A UserMessage with N ToolResultBlocks expands to N ``tool``-role messages
    in Path A (OpenAI requires one per call id); B/C folds them as XML in user
    text."""

    if isinstance(m, UserMessage):
        if isinstance(m.content, str):
            return [{"role": "user", "content": m.content}]
        return _encode_user_blocks(m.content, path=path)
    if isinstance(m, AssistantMessage):
        return _encode_assistant_blocks(m.content, path=path)
    if isinstance(m, SystemMessage):  # only reachable if caller bypassed _split_system
        return [{"role": "system", "content": _system_to_string(m)}]
    raise TypeError(f"unsupported message type: {type(m).__name__}")


def _encode_user_blocks(
    blocks: list[ContentBlock], *, path: ToolUsePath,
) -> list[dict[str, Any]]:
    """Plain text, images, and tool results coexist on a user message. Path A
    splits tool results into their own ``tool`` role messages (OpenAI requires
    they immediately follow the requesting assistant message); B/C keeps them
    inline as ``<tool_result>`` XML."""

    text_pieces: list[str] = []
    image_parts: list[dict[str, Any]] = []
    tool_results: list[ToolResultBlock] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            text_pieces.append(b.text)
        elif isinstance(b, ToolResultBlock):
            tool_results.append(b)
        else:
            image_parts.append(_image_block_to_openai_part(b))

    out: list[dict[str, Any]] = []
    if path == "A":
        for tr in tool_results:
            out.append({
                "role": "tool",
                "tool_call_id": tr.tool_use_id,
                "content": _tool_result_content_to_string(tr.content),
            })
    else:
        # Prepend results as one block in call order. Repeated ``insert(0, ...)``
        # reversed multi-result turns relative to the matching <tool_call> blocks.
        # Escape both the id (attribute) and the payload (element text) so
        # result content containing a literal ``</tool_result>`` (or any markup)
        # can't break out of the block and inject a spoofed tool call. The model
        # reads XML entities fine; this is context-only, never parsed back.
        rendered = [
            f"<tool_result tool_call_id={_xml_quoteattr(tr.tool_use_id)}>"
            f"{_xml_escape(_tool_result_content_to_string(tr.content))}</tool_result>"
            for tr in tool_results
        ]
        text_pieces[:0] = rendered

    if text_pieces or image_parts:
        if image_parts:
            parts: list[dict[str, Any]] = []
            if text_pieces:
                parts.append({"type": "text", "text": "\n".join(text_pieces)})
            parts.extend(image_parts)
            out.append({"role": "user", "content": parts})
        else:
            out.append({"role": "user", "content": "\n".join(text_pieces)})
    return out


def _image_block_to_openai_part(block: ContentBlock) -> dict[str, Any]:
    """Convert an Anthropic-shaped ``ImageBlock`` to OpenAI's ``image_url``
    content part. Anthropic carries images as ``source={type, media_type, data}``
    (base64) or ``source={type:'url', url}``; OpenAI wants a single
    ``{"type":"image_url","image_url":{"url": ...}}`` where the url is either a
    ``data:<media_type>;base64,<data>`` URI or a plain URL. Emitting the raw
    Anthropic shape makes images fail on OpenAI-compatible endpoints."""

    src = getattr(block, "source", None)
    if not isinstance(src, dict):
        # Unknown shape — fall back to a passthrough so we don't crash, but this
        # path is not expected for well-formed ImageBlocks.
        return msgspec.to_builtins(block)

    src_type = src.get("type")
    if src_type == "url" or (src_type is None and "url" in src):
        url = src.get("url", "")
    elif src.get("data") is not None:
        media_type = src.get("media_type") or "image/png"
        url = f"data:{media_type};base64,{src['data']}"
    else:
        # Already a data: URI stashed under url, or otherwise best-effort.
        url = src.get("url", "")
    return {"type": "image_url", "image_url": {"url": url}}


def _encode_assistant_blocks(
    blocks: list[ContentBlock], *, path: ToolUsePath,
) -> list[dict[str, Any]]:
    """Encode an assistant turn. Native emits ``tool_calls``; prompt-engineered
    paths re-serialize tool uses as ``<tool_call>`` XML so the model sees its
    own prior calls in the same shape it produced them."""

    text_pieces: list[str] = []
    thinking_pieces: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_call_xml: list[str] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            text_pieces.append(b.text)
        elif isinstance(b, ThinkingBlock):
            thinking_pieces.append(b.thinking)
        elif isinstance(b, ToolUseBlock):
            if path == "A":
                tool_calls.append({
                    "id": b.id,
                    "type": "function",
                    "function": {
                        "name": b.name,
                        "arguments": json.dumps(b.input, separators=(",", ":")),
                    },
                })
            else:
                tool_call_xml.append(
                    "<tool_call>\n"
                    + json.dumps({"name": b.name, "arguments": b.input}, separators=(",", ":"))
                    + "\n</tool_call>"
                )

    # why: re-inline <think> for OSS models that learned it — round-tripping
    # preserves chain-of-thought across turns.
    content_parts: list[str] = []
    if thinking_pieces and path in ("B", "C"):
        content_parts.append("<think>" + "".join(thinking_pieces) + "</think>")
    content_parts.extend(text_pieces)
    content_parts.extend(tool_call_xml)
    content = "\n".join(p for p in content_parts if p) or None

    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return [msg]


def _tool_result_content_to_string(content: str | list[ContentBlock]) -> str:
    """Stringify tool results for OpenAI's ``tool`` role messages."""

    if isinstance(content, str):
        return content
    parts = [
        b.text if isinstance(b, TextBlock)
        else json.dumps(msgspec.to_builtins(b), separators=(",", ":"))
        for b in content
    ]
    return "\n".join(parts)


def _normalize_tool_defs(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce tool defs to OpenAI ``{type:'function', function:{...}}`` shape.
    Accepts Anthropic-shaped ``{name, description, input_schema}`` too."""

    out: list[dict[str, Any]] = []
    for t in tools:
        if t.get("type") == "function" and "function" in t:
            out.append(t)
            continue
        name = t.get("name")
        if not name:
            raise ProviderError(f"tool def missing 'name': {t!r}")
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or t.get("input_schema") or {},
            },
        })
    return out


# ---------------------------------------------------------------------------
# SSE iteration — local because OpenAI uses ``data: [DONE]``
# ---------------------------------------------------------------------------


async def _iter_openai_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded JSON chunks from an OpenAI-style SSE response.

    The shared ``iter_sse`` in ``http.py`` raises on non-JSON data fields, but
    OpenAI-compat servers terminate with the literal ``data: [DONE]``. We
    swallow that and convert decode errors on other lines to
    ``StreamProtocolError`` so the agent loop sees them.
    """

    data_chunks: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_chunks:
                payload = "\n".join(data_chunks)
                data_chunks = []
                if payload == "[DONE]":
                    return
                try:
                    yield _JSON_DECODER.decode(payload)
                except msgspec.DecodeError as e:
                    raise StreamProtocolError(f"bad JSON in SSE chunk: {payload[:200]}") from e
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            chunk = line[5:]
            if chunk.startswith(" "):
                chunk = chunk[1:]
            data_chunks.append(chunk)

    if data_chunks:
        payload = "\n".join(data_chunks)
        if payload != "[DONE]":
            try:
                yield _JSON_DECODER.decode(payload)
            except msgspec.DecodeError as e:
                raise StreamProtocolError(f"bad JSON in trailing SSE chunk: {payload[:200]}") from e


# ---------------------------------------------------------------------------
# Native streaming translation (Path A)
# ---------------------------------------------------------------------------


async def _translate_native(
    response: httpx.Response,
    requested_model: str,
) -> AsyncIterator[StreamEvent]:
    """Walk the OpenAI SSE stream and emit normalized events for the native
    tool-call path. Events are emitted as soon as they materialize so the
    streaming tool executor (plan §5) can dispatch tools mid-stream."""

    started = False
    text_open = False
    text_index = 0
    thinking_open = False
    thinking_index = 0
    next_index = 0
    # Tool calls keyed by a canonical key. OpenAI always sends ``index``, but
    # many OpenAI-*compatible* backends stream deltas with only ``id`` (no
    # index) — keying purely on ``index`` (default 0) merged distinct calls.
    # Value: {sdk_index, id, name_buf, opened, args_pending}
    tool_state: dict[Any, dict[str, Any]] = {}
    # Map a provider ``index`` -> canonical key so later index-only continuation
    # deltas resolve back to the entry created when the ``id`` first arrived.
    index_to_key: dict[int, Any] = {}
    synthetic_counter = 0  # last resort when a delta carries neither id nor index
    last_key: Any = None
    stop_reason: str | None = None
    usage: Usage | None = None

    async for data in _iter_openai_sse(response):
        # Mid-stream provider error (rare but real — e.g. Together kills a
        # connection with a JSON error payload instead of an HTTP status).
        if "error" in data and "choices" not in data:
            raise ProviderError(_err_message(data["error"]), raw=data)

        if not started:
            yield MessageStart(
                message_id=data.get("id") or f"chatcmpl-{uuid4().hex[:12]}",
                model=data.get("model") or requested_model,
            )
            started = True

        # ``usage`` arrives on the final chunk when stream_options.include_usage
        # is set; some providers (Groq) emit it every chunk with growing
        # counts — last-write-wins is correct in both cases.
        u = data.get("usage")
        if u:
            usage = _decode_usage(u)

        choices = data.get("choices") or ()
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}

        # Reasoning_content arrives out-of-band from some R1-style providers
        # (Fireworks, DeepSeek). Surface as ThinkingBlock — same channel as
        # Anthropic's thinking_delta so the agent UI doesn't care.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            if text_open:
                yield ContentBlockStop(index=text_index)
                text_open = False
            if not thinking_open:
                thinking_index = next_index
                next_index += 1
                thinking_open = True
                yield ContentBlockStart(
                    index=thinking_index, block=ThinkingBlock(thinking="")
                )
            yield ContentBlockDelta(
                index=thinking_index, delta=ThinkingDelta(thinking=reasoning)
            )

        # Plain text content.
        content_piece = delta.get("content")
        if content_piece:
            if thinking_open:
                yield ContentBlockStop(index=thinking_index)
                thinking_open = False
            if not text_open:
                text_index = next_index
                next_index += 1
                text_open = True
                yield ContentBlockStart(index=text_index, block=TextBlock(text=""))
            yield ContentBlockDelta(
                index=text_index, delta=TextDelta(text=content_piece)
            )

        # Tool call deltas — the meat of Path A.
        for tc in delta.get("tool_calls") or ():
            tc_id = tc.get("id")
            tc_index = tc.get("index")
            fn = tc.get("function") or {}
            name_chunk = fn.get("name")
            args_chunk = fn.get("arguments")

            # Resolve the canonical key. Prefer an index we've already bound;
            # then a known id; then a fresh id (binding its index if present);
            # then a bare index; then continue the last call; finally a synthetic
            # key so two id-less/index-less calls never collapse into one.
            if tc_index is not None and tc_index in index_to_key:
                key = index_to_key[tc_index]
            elif tc_id is not None and tc_id in tool_state:
                key = tc_id
            elif tc_id is not None:
                key = tc_id
                if tc_index is not None:
                    index_to_key[tc_index] = key
            elif tc_index is not None:
                key = tc_index
                index_to_key[tc_index] = key
            elif last_key is not None:
                key = last_key
            else:
                key = ("_syn", synthetic_counter)
                synthetic_counter += 1
            last_key = key

            st = tool_state.get(key)
            if st is None:
                st = {
                    "sdk_index": -1,
                    "id": tc_id or "",
                    "name_buf": name_chunk or "",
                    "opened": False,
                    "args_pending": "",
                }
                tool_state[key] = st
            else:
                # Some OpenAI-compatible streams revise a tool_call's id across
                # deltas (they emit a provisional id, then correct it). Adopt the
                # latest id until the block is committed so the emitted
                # ContentBlockStart — and thus the tool_result key the caller
                # sends back — carries the final id and matches. Once opened the
                # id is on the wire and can't be retracted, so we lock it.
                if tc_id and tc_id != st["id"] and not st["opened"]:
                    st["id"] = tc_id
                if name_chunk:
                    st["name_buf"] += name_chunk

            # Open the block as soon as we have a name. Providers vary in when
            # they send name vs args; we open lazily for max compatibility.
            if not st["opened"] and st["name_buf"]:
                if text_open:
                    yield ContentBlockStop(index=text_index)
                    text_open = False
                if thinking_open:
                    yield ContentBlockStop(index=thinking_index)
                    thinking_open = False
                st["sdk_index"] = next_index
                next_index += 1
                st["opened"] = True
                # Persist the exact id we put on the wire (real or synthetic)
                # so state stays in sync with the emitted block and a later
                # differing id can't silently diverge from what the caller keys
                # its tool_result to.
                st["id"] = st["id"] or f"call_{uuid4().hex[:12]}"
                yield ContentBlockStart(
                    index=st["sdk_index"],
                    block=ToolUseBlock(
                        id=st["id"],
                        name=st["name_buf"],
                        input={},  # filled client-side from streamed args
                    ),
                )
                # Flush any args that arrived before the name landed.
                if st["args_pending"]:
                    yield ContentBlockDelta(
                        index=st["sdk_index"],
                        delta=InputJsonDelta(partial_json=st["args_pending"]),
                    )
                    st["args_pending"] = ""

            if args_chunk:
                if st["opened"]:
                    yield ContentBlockDelta(
                        index=st["sdk_index"],
                        delta=InputJsonDelta(partial_json=args_chunk),
                    )
                else:
                    st["args_pending"] += args_chunk

        fr = choice.get("finish_reason")
        if fr:
            stop_reason = _map_finish_reason(fr)

    # End of stream — close anything still open.
    if text_open:
        yield ContentBlockStop(index=text_index)
    if thinking_open:
        yield ContentBlockStop(index=thinking_index)
    for st in tool_state.values():
        if not st.get("opened"):
            # A call that streamed id/args but never a name would otherwise be
            # silently dropped — a silent agentic dead-end. Surface it with a
            # placeholder name so the executor returns a recoverable is_error
            # ("unknown tool") the model can react to and re-issue, rather than
            # the turn ending with no tool_use at all.
            if not (st["args_pending"] or st["id"]):
                continue
            st["sdk_index"] = next_index
            next_index += 1
            st["opened"] = True
            yield ContentBlockStart(
                index=st["sdk_index"],
                block=ToolUseBlock(
                    id=st["id"] or f"call_{uuid4().hex[:12]}",
                    name=st["name_buf"] or "unknown_tool",
                    input={},
                ),
            )
            if st["args_pending"]:
                yield ContentBlockDelta(
                    index=st["sdk_index"],
                    delta=InputJsonDelta(partial_json=st["args_pending"]),
                )
                st["args_pending"] = ""
        yield ContentBlockStop(index=st["sdk_index"])

    if stop_reason or usage:
        yield MessageDelta(stop_reason=stop_reason, usage=usage)
    yield MessageStop()


# ---------------------------------------------------------------------------
# Prompt-engineered streaming translation (Path B / C)
# ---------------------------------------------------------------------------


async def _translate_prompt_engineered(
    response: httpx.Response,
    requested_model: str,
    *,
    emits_thinking: bool,
) -> AsyncIterator[StreamEvent]:
    """Walk the OpenAI SSE stream for a model that emits ``<tool_call>`` XML
    inline. We pipe every content delta through ToolCallTextParser (and
    ThinkingParser when applicable) and translate parser events into SDK
    events. State for the SDK-side block indexing lives in a single mutable
    cursor dict so the helpers can update it in place."""

    cursor: dict[str, Any] = {
        "next_index": 0,
        "text_open": False,
        "text_index": 0,
        "thinking_open": False,
        "thinking_index": 0,
        "tool_indices": {},  # parser call_id -> SDK block index
    }

    parser = ToolCallTextParser()
    # why: gate the thinking parser strictly on model capability. Plan §9 promises
    # zero cost when emits_inline_thinking is False — so don't construct it.
    think_parser = ThinkingParser() if (emits_thinking and ThinkingParser) else None

    started = False
    stop_reason: str | None = None
    usage: Usage | None = None

    async for data in _iter_openai_sse(response):
        if "error" in data and "choices" not in data:
            raise ProviderError(_err_message(data["error"]), raw=data)

        if not started:
            yield MessageStart(
                message_id=data.get("id") or f"chatcmpl-{uuid4().hex[:12]}",
                model=data.get("model") or requested_model,
            )
            started = True

        u = data.get("usage")
        if u:
            usage = _decode_usage(u)

        choices = data.get("choices") or ()
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}

        # Out-of-band reasoning (some R1 backends use this even when the model
        # also emits inline <think> tags — surface as ThinkingBlock either way).
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            for ev in _open_thinking_and_emit(cursor, reasoning):
                yield ev

        content_piece = delta.get("content")
        if content_piece:
            # First peel off inline <think>...</think> if the model emits it,
            # then feed the rest through the tool-call parser.
            if think_parser is not None:
                segments = list(think_parser.feed(content_piece))  # type: ignore[attr-defined]
            else:
                segments = [("text", content_piece)]

            for kind, chunk in segments:
                if not chunk:
                    continue
                if kind == "thinking":
                    for ev in _open_thinking_and_emit(cursor, chunk):
                        yield ev
                else:
                    for pe in parser.feed(chunk):
                        for ev in _emit_parser_event(pe, cursor):
                            yield ev

        fr = choice.get("finish_reason")
        if fr:
            stop_reason = _map_finish_reason(fr)

    # End-of-stream: flush parser tails.
    if think_parser is not None:
        for kind, chunk in think_parser.finalize():  # type: ignore[attr-defined]
            if not chunk:
                continue
            if kind == "thinking":
                for ev in _open_thinking_and_emit(cursor, chunk):
                    yield ev
            else:
                for pe in parser.feed(chunk):
                    for ev in _emit_parser_event(pe, cursor):
                        yield ev

    for pe in parser.finalize():
        for ev in _emit_parser_event(pe, cursor):
            yield ev

    if cursor["text_open"]:
        yield ContentBlockStop(index=cursor["text_index"])
        cursor["text_open"] = False
    if cursor["thinking_open"]:
        yield ContentBlockStop(index=cursor["thinking_index"])
        cursor["thinking_open"] = False

    if stop_reason or usage:
        yield MessageDelta(stop_reason=stop_reason, usage=usage)
    yield MessageStop()


def _open_thinking_and_emit(cursor: dict[str, Any], chunk: str) -> Iterable[StreamEvent]:
    """Open a thinking block (closing any open text block first) and emit one
    delta. Mutates ``cursor`` in place."""

    if cursor["text_open"]:
        yield ContentBlockStop(index=cursor["text_index"])
        cursor["text_open"] = False
    if not cursor["thinking_open"]:
        idx = cursor["next_index"]
        cursor["next_index"] = idx + 1
        cursor["thinking_index"] = idx
        cursor["thinking_open"] = True
        yield ContentBlockStart(index=idx, block=ThinkingBlock(thinking=""))
    yield ContentBlockDelta(
        index=cursor["thinking_index"], delta=ThinkingDelta(thinking=chunk)
    )


def _emit_parser_event(pe: Any, cursor: dict[str, Any]) -> Iterable[StreamEvent]:
    """Translate one ToolCallTextParser event into zero-or-more SDK events.
    Mutates ``cursor`` in place."""

    if isinstance(pe, TextChunk):
        # why: parser only emits TextChunk for content outside think+tool tags.
        if cursor["thinking_open"]:
            yield ContentBlockStop(index=cursor["thinking_index"])
            cursor["thinking_open"] = False
        if not cursor["text_open"]:
            idx = cursor["next_index"]
            cursor["next_index"] = idx + 1
            cursor["text_index"] = idx
            cursor["text_open"] = True
            yield ContentBlockStart(index=idx, block=TextBlock(text=""))
        yield ContentBlockDelta(index=cursor["text_index"], delta=TextDelta(text=pe.text))
    elif isinstance(pe, ToolCallStart):
        if cursor["text_open"]:
            yield ContentBlockStop(index=cursor["text_index"])
            cursor["text_open"] = False
        if cursor["thinking_open"]:
            yield ContentBlockStop(index=cursor["thinking_index"])
            cursor["thinking_open"] = False
        idx = cursor["next_index"]
        cursor["next_index"] = idx + 1
        cursor["tool_indices"][pe.call_id] = idx
        yield ContentBlockStart(
            index=idx, block=ToolUseBlock(id=pe.call_id, name=pe.name, input={})
        )
    elif isinstance(pe, ToolCallInputDelta):
        sdk_idx = cursor["tool_indices"].get(pe.call_id)
        if sdk_idx is not None:
            yield ContentBlockDelta(index=sdk_idx, delta=InputJsonDelta(partial_json=pe.partial_json))
    elif isinstance(pe, ToolCallStop):
        sdk_idx = cursor["tool_indices"].pop(pe.call_id, None)
        if sdk_idx is not None:
            yield ContentBlockStop(index=sdk_idx)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _decode_usage(raw: dict[str, Any]) -> Usage:
    """OpenAI uses prompt_tokens / completion_tokens; some providers add
    cache counters. Best-effort map to universal Usage."""

    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
        cache_creation_input_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(
            raw.get("cache_read_input_tokens")
            or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        ),
    )


_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


def _map_finish_reason(fr: str) -> str:
    return _FINISH_REASON_MAP.get(fr, fr)


def _lower_max_tokens_for_context_error(payload: dict[str, Any], body: bytes) -> bool:
    text = body.decode("utf-8", "replace")
    if "maximum context length" not in text and "context length" not in text:
        return False
    field = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
    cur = payload.get(field)
    if not isinstance(cur, int) or cur <= 512:
        return False
    max_match = re.search(r"maximum context length is\s+(\d+)", text, re.IGNORECASE)
    input_match = re.search(r"prompt contains at least\s+(\d+)\s+input tokens", text,
                            re.IGNORECASE)
    maximum = int(max_match.group(1)) if max_match else None
    input_tokens = int(input_match.group(1)) if input_match else None
    if maximum and input_tokens:
        next_max = maximum - input_tokens - 512
        next_max = max(256, min(cur - 1, next_max))
    else:
        next_max = max(256, cur // 2)
    if next_max >= cur:
        next_max = cur // 2
    payload[field] = max(256, next_max)
    return payload[field] < cur


def _supports_request_reasoning(
    model: str, cap: ModelCapability | None
) -> bool:
    """Whether ``model`` accepts a request-side reasoning knob (``reasoning_effort``).

    Two signals: the resolved capability's ``supports_reasoning_effort`` bit
    (hosted flagships — o-series / gemini / glm), plus a name check for OpenAI's
    gpt-5.x and o-series, which resolve to the bare "openai" family (shared with
    the reasoning-less gpt-4o) and so can't be told apart at the family level.
    """

    bare = model.lower().rsplit("/", 1)[-1]
    if bare.startswith(("gpt-5", "o1", "o3", "o4")):
        return True
    return bool(cap and cap.supports_reasoning_effort)


def _err_message(err: Any) -> str:
    if isinstance(err, dict):
        return err.get("message") or err.get("type") or "provider error"
    return err if isinstance(err, str) else "provider error"
