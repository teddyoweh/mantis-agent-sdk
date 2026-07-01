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
from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import uuid4

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
from .base import HTTPProviderMixin

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
    protocol. Accepts OpenAI function-tool format OR flattened Anthropic-shaped."""

    flattened = [t["function"] if isinstance(t.get("function"), dict) else t for t in tools]
    return (
        _TOOL_PROTOCOL_PREAMBLE
        + json.dumps(flattened, separators=(",", ":"))
        + _TOOL_PROTOCOL_TRAILER
    )


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
                if v:
                    key = v
                    break

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
    ) -> AsyncIterator[StreamEvent]:
        """Stream a chat completion as normalized ``StreamEvent``s."""

        cap = model_capability or self._default_model_capability
        # Without tools the path doesn't matter, but we keep Path A semantics
        # so the (text-only) translator runs the simpler hot loop.
        path: ToolUsePath = (
            resolve_tool_use_path(cap, self.backend_capability)
            if (cap is not None and tools)
            else "A"
        )

        payload = self._build_payload(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=extra,
            path=path,
        )

        # why: ``client.stream(...)`` is the only httpx call that doesn't drain
        # the body up front; we need that to keep the SSE channel open.
        # One retry: some recent OpenAI models require ``max_completion_tokens``
        # instead of ``max_tokens`` (and reject a custom temperature) but aren't
        # matched by name — e.g. the bare ``chat-latest`` alias. Rather than fail
        # a valid model, swap the field on that specific 400 and retry once.
        for _attempt in range(2):
            async with self.client.stream(
                "POST",
                "/chat/completions",
                content=_PAYLOAD_ENCODER.encode(payload),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    # Handle the two "recent OpenAI model" param rejections
                    # independently (OpenAI may report either first): swap
                    # max_tokens→max_completion_tokens, and/or drop a temperature
                    # the model won't accept. Retry once if we changed anything.
                    body = response.content
                    if _attempt == 0:
                        retry = False
                        if "max_tokens" in payload and b"max_completion_tokens" in body:
                            payload["max_completion_tokens"] = payload.pop("max_tokens")
                            retry = True
                        if ("temperature" in payload and b"temperature" in body
                                and (b"nsupported" in body or b"does not support" in body
                                     or b"only the default" in body or b"only supports" in body)):
                            payload.pop("temperature", None)
                            retry = True
                        if retry:
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

        if path == "A" and tools:
            payload["tools"] = _normalize_tool_defs(tools)
            payload["tool_choice"] = "auto"
        # why: Path C grammar (GBNF / guided_json / response_format) is built
        # elsewhere — caller injects it via ``extra``. Without it, Path C
        # degrades to Path B at the wire; the Hermes-Pro prompt is strict
        # enough that this is safe for most models.

        if extra:
            # Shallow-merge last so callers can override anything above (e.g.
            # guided_grammar, response_format, vendor knobs).
            payload.update(extra)
        return payload


# ---------------------------------------------------------------------------
# Message encoding (universal -> OpenAI chat shape)
# ---------------------------------------------------------------------------


def _split_system(messages: list[Message]) -> tuple[str | None, list[Message]]:
    """Pull SystemMessages out. Multiple system messages concatenate with
    blank-line separators — last-wins would silently drop user intent."""

    sys_parts: list[str] = []
    body: list[Message] = []
    for m in messages:
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
            image_parts.append(msgspec.to_builtins(b))

    out: list[dict[str, Any]] = []
    if path == "A":
        for tr in tool_results:
            out.append({
                "role": "tool",
                "tool_call_id": tr.tool_use_id,
                "content": _tool_result_content_to_string(tr.content),
            })
    else:
        for tr in tool_results:
            text_pieces.insert(
                0,
                f'<tool_result tool_call_id="{tr.tool_use_id}">'
                f"{_tool_result_content_to_string(tr.content)}</tool_result>",
            )

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
    # Tool calls keyed by OpenAI's per-stream ``index`` (position in the
    # ``tool_calls`` array). Value:
    #   {sdk_index, id, name_buf, opened, args_pending}
    tool_state: dict[int, dict[str, Any]] = {}
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
            oai_idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            name_chunk = fn.get("name")
            args_chunk = fn.get("arguments")

            st = tool_state.get(oai_idx)
            if st is None:
                st = {
                    "sdk_index": -1,
                    "id": tc.get("id") or "",
                    "name_buf": name_chunk or "",
                    "opened": False,
                    "args_pending": "",
                }
                tool_state[oai_idx] = st
            else:
                if tc.get("id") and not st["id"]:
                    st["id"] = tc["id"]
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
                yield ContentBlockStart(
                    index=st["sdk_index"],
                    block=ToolUseBlock(
                        id=st["id"] or f"call_{uuid4().hex[:12]}",
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
        if st.get("opened"):
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


def _err_message(err: Any) -> str:
    if isinstance(err, dict):
        return err.get("message") or err.get("type") or "provider error"
    return err if isinstance(err, str) else "provider error"
