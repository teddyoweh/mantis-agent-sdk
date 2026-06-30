"""Ollama native adapter.

Talks to ``POST /api/chat`` directly. Ollama emits **newline-delimited JSON**
(NDJSON), not SSE — each line is one complete JSON object describing the
next state of the streaming message. We cannot reuse :func:`iter_sse` here.

Wire shape (per line)::

    {
      "model": "qwen2.5",
      "created_at": "2026-...",
      "message": {
        "role": "assistant",
        "content": "Hello",
        "tool_calls": [
          {"function": {"name": "search", "arguments": {"q": "..."}}}
        ]?
      },
      "done": false,
      "done_reason": "stop"?,        // only on the final chunk
      "total_duration": 12345?,
      "prompt_eval_count": 42?,
      "eval_count": 17?
    }

Notes vs OpenAI shape:
* ``tool_calls[].function.arguments`` is a **dict**, not a JSON-encoded string.
* Tool calls usually arrive in a single chunk (Ollama doesn't tokenize them).
* No incremental tool-call streaming — we synthesize Start + InputJsonDelta +
  Stop in one shot the moment we see them.

For models without native tool calling, we inject the Hermes-Pro system
prompt and route content deltas through :class:`ToolCallTextParser`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import uuid4

import msgspec

from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
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
from ..streaming.text_tool_parser import (
    TextChunk,
    ToolCallInputDelta as _ParserToolDelta,
    ToolCallStart as _ParserToolStart,
    ToolCallStop as _ParserToolStop,
    ToolCallTextParser,
)
from ..streaming.thinking_parser import (
    TextChunk as _ThinkTextChunk,
    ThinkingChunk as _ThinkingChunk,
    ThinkingParser,
)
from ..types import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)
from .base import HTTPProviderMixin

DEFAULT_BASE_URL = "http://localhost:11434"

# NOTE: we render with simple ``%`` substitution rather than ``.format()``
# because the literal example below contains JSON braces — ``str.format``
# would treat them as positional placeholders and KeyError on `'"name"'`.
_HERMES_PROMPT = (
    "You have access to the following tools. To call a tool, emit a single\n"
    "<tool_call> block in your response. You can call multiple tools in one\n"
    "response by emitting multiple <tool_call> blocks back-to-back.\n\n"
    "<tool_call>\n"
    '{"name": "<tool_name>", "arguments": {<JSON object>}}\n'
    "</tool_call>\n\n"
    "Available tools:\n%(tools_json)s\n\n"
    "When you receive <tool_result> messages, continue your response using\n"
    "the new information. If you have completed the user's request, respond\n"
    "without any <tool_call> blocks."
)

_JSON_ENCODER = msgspec.json.Encoder()
_JSON_DECODER = msgspec.json.Decoder()


class OllamaProvider(HTTPProviderMixin):
    """Adapter for Ollama's native ``/api/chat`` endpoint."""

    name = "ollama"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        base = base_url or os.environ.get("OLLAMA_HOST") or DEFAULT_BASE_URL
        headers = {"content-type": "application/json", "accept": "application/x-ndjson"}
        # Local Ollama doesn't require auth but remote/proxied deploys may.
        key = api_key or os.environ.get("OLLAMA_API_KEY")
        if key:
            headers["authorization"] = f"Bearer {key}"
        if default_headers:
            headers.update(default_headers)
        self.client = make_client(base_url=base, headers=headers)
        self.backend_capability: BackendCapability = HOSTED_PROFILES["ollama"]

    # ------------------------------------------------------------------
    # Message serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_messages(
        messages: Iterable[Message], system: str | None
    ) -> list[dict[str, Any]]:
        """Flatten our typed messages into Ollama's ``messages`` list.

        Ollama accepts a list of ``{role, content, tool_calls?, tool_call_id?}``.
        Content blocks collapse to a single string by concatenating text blocks
        and rendering tool_results as ``<tool_result>...</tool_result>``.
        """

        out: list[dict[str, Any]] = []
        if system is not None:
            out.append({"role": "system", "content": system})

        for m in messages:
            if isinstance(m, SystemMessage):
                content = m.content if isinstance(m.content, str) else _join_text(m.content)
                out.append({"role": "system", "content": content})
                continue
            if isinstance(m, UserMessage):
                if isinstance(m.content, str):
                    out.append({"role": "user", "content": m.content})
                    continue
                # Multi-block user message — may include tool_result blocks.
                text_buf: list[str] = []
                for blk in m.content:
                    if isinstance(blk, TextBlock):
                        text_buf.append(blk.text)
                    else:
                        # tool_result, image, etc. — collapse to a tagged string.
                        text_buf.append(_render_block_as_text(blk))
                out.append({"role": "user", "content": "\n".join(text_buf)})
                continue
            if isinstance(m, AssistantMessage):
                texts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for blk in m.content:
                    if isinstance(blk, TextBlock):
                        texts.append(blk.text)
                    elif isinstance(blk, ToolUseBlock):
                        tool_calls.append(
                            {
                                "function": {
                                    "name": blk.name,
                                    "arguments": blk.input,
                                }
                            }
                        )
                msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts)}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
                continue
            raise TypeError(f"unsupported message: {type(m).__name__}")
        return out

    # ------------------------------------------------------------------
    # Streaming
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
        # Decide tool-use path. Native if model + backend both support it.
        use_native_tools = bool(
            tools
            and (model_capability is None or model_capability.supports_native_tools)
            and self.backend_capability.supports_native_tools
        )

        # If tools are present but native path isn't usable, fall back to
        # Hermes-Pro prompt injection. The agent loop then parses content
        # deltas via ToolCallTextParser — we just pipe text through.
        effective_system = system
        if tools and not use_native_tools:
            tools_json = _JSON_ENCODER.encode(tools).decode("utf-8")
            inject = _HERMES_PROMPT % {"tools_json": tools_json}
            effective_system = inject if system is None else f"{system}\n\n{inject}"

        payload: dict[str, Any] = {
            "model": model,
            "messages": self._encode_messages(messages, effective_system),
            "stream": True,
            "options": {"num_predict": max_tokens},
        }
        if temperature is not None:
            payload["options"]["temperature"] = temperature
        if use_native_tools:
            # Ollama expects OpenAI-shape function tools, NOT the
            # Anthropic shape returned by Tool.to_wire(). Passing the
            # Anthropic shape silently fails: Ollama still emits a
            # tool_call, but with name="" and mangled arguments — the
            # agent loop drops empty-name calls and the assistant ends
            # up with zero content blocks, which looks like "the model
            # ignored the tools" but is really us sending the wrong
            # schema on the wire.
            payload["tools"] = [_to_openai_tool(t) for t in tools]
        if extra:
            # Merge ``extra.options`` into payload.options instead of clobbering.
            opts = extra.pop("options", None) if isinstance(extra, dict) else None
            if isinstance(opts, dict):
                payload["options"].update(opts)
            payload.update(extra)

        # When we're on the prompt-engineered path, the model emits
        # ``<tool_call>...</tool_call>`` blocks in its text content. We thread
        # a parser through the stream so they become real ToolUseBlock events
        # the agent loop can dispatch. Without this, the model's tool-call
        # syntax would be passed straight through as text — the model would
        # then fabricate its own ``<tool_result>`` in the same response and
        # the SDK would never call a real tool.
        text_parser = None if use_native_tools else ToolCallTextParser()

        # When the model declares inline thinking tags (R1, QwQ,
        # R1-Distill, Marco-o1, Hermes-Pro in reasoning mode, ...), route
        # content through a ThinkingParser too. This handles the case where
        # the model emits <think>...</think> INLINE in the text stream
        # — distinct from Ollama's separate `thinking` field, which only
        # fires when Ollama itself recognizes the model + serves it in
        # reasoning mode. The two paths converge on the same normalized
        # ThinkingBlock events upstream.
        thinking_parser = None
        if model_capability is not None and model_capability.emits_inline_thinking:
            thinking_parser = ThinkingParser(
                tags=model_capability.inline_thinking_tags
            )

        async for ev in self._stream_ndjson(
            payload,
            model=model,
            text_parser=text_parser,
            thinking_parser=thinking_parser,
        ):
            yield ev

    async def _stream_ndjson(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        text_parser: ToolCallTextParser | None = None,
        thinking_parser: ThinkingParser | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Drive the Ollama /api/chat NDJSON stream.

        ``text_parser`` is set when we're on the prompt-engineered Path B/C
        and need to extract ``<tool_call>`` blocks from text content. When
        ``None``, content streams as plain text deltas (Path A native path).
        """

        message_id = f"ollama-{uuid4().hex[:12]}"
        started = False
        # Mutable index cursor — shared with the parser-routing helper.
        cursor: dict[str, Any] = {
            "next_index": 0,
            "text_index": None,
            "text_open": False,
            # Map parser call_id → block index, so InputDelta and Stop emit
            # against the same content block.
            "tool_indices": {},
        }

        async with self.client.stream(
            "POST", "/api/chat", content=_JSON_ENCODER.encode(payload)
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise_for_status(response)

            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = _JSON_DECODER.decode(line)
                except msgspec.DecodeError as e:
                    raise StreamProtocolError(
                        f"bad NDJSON line from Ollama: {line[:200]}"
                    ) from e
                if not isinstance(chunk, dict):
                    continue

                if not started:
                    started = True
                    yield MessageStart(
                        message_id=message_id,
                        model=chunk.get("model", model),
                    )

                msg = chunk.get("message") or {}
                content = msg.get("content") or ""

                if content:
                    # Two parsers can stack: thinking-tag splitter first, then
                    # tool-call splitter on the *text* portion only. Reasoning
                    # is always non-tool content, so we never look for tool
                    # calls inside a <think> block.
                    for piece_kind, piece_text in _pipe_content(
                        content, thinking_parser, finalize=False
                    ):
                        if piece_kind == "thinking":
                            for out in _emit_thinking(piece_text, cursor):
                                yield out
                        else:
                            # piece is plain text — close any open thinking
                            # block first, then route through tool parser or
                            # straight through.
                            if cursor.get("thinking_open"):
                                yield ContentBlockStop(index=cursor["thinking_index"])
                                cursor["thinking_open"] = False
                                cursor["thinking_index"] = None
                            if text_parser is not None:
                                for parser_ev in text_parser.feed(piece_text):
                                    for out in _from_parser_event(parser_ev, cursor):
                                        yield out
                            else:
                                if not cursor["text_open"]:
                                    cursor["text_index"] = cursor["next_index"]
                                    cursor["next_index"] += 1
                                    yield ContentBlockStart(
                                        index=cursor["text_index"],
                                        block=TextBlock(text=""),
                                    )
                                    cursor["text_open"] = True
                                yield ContentBlockDelta(
                                    index=cursor["text_index"],
                                    delta=TextDelta(text=piece_text),
                                )

                # Thinking — DeepSeek-R1 (and other reasoning models served by
                # Ollama) stream thinking as a separate ``thinking`` field
                # on each NDJSON message. We open a dedicated ThinkingBlock
                # content block for these so the agent loop sees them as
                # first-class blocks, not as deltas into a non-existent text
                # block.
                thinking = msg.get("thinking")
                if thinking:
                    if not cursor.get("thinking_open"):
                        cursor["thinking_index"] = cursor["next_index"]
                        cursor["next_index"] += 1
                        cursor["thinking_open"] = True
                        yield ContentBlockStart(
                            index=cursor["thinking_index"],
                            block=ThinkingBlock(thinking=""),
                        )
                    yield ContentBlockDelta(
                        index=cursor["thinking_index"],
                        delta=ThinkingDelta(thinking=thinking),
                    )

                # Native tool calls — Ollama emits them as a complete list in
                # one chunk (usually the final one).
                tool_calls = msg.get("tool_calls") or []
                for tc in tool_calls:
                    fn = (tc or {}).get("function") or {}
                    name = fn.get("name")
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args_dict = _JSON_DECODER.decode(args)
                            if not isinstance(args_dict, dict):
                                args_dict = {}
                        except msgspec.DecodeError:
                            args_dict = {}
                    elif isinstance(args, dict):
                        args_dict = args
                    else:
                        args_dict = {}
                    if not isinstance(name, str) or not name:
                        continue
                    tool_id = tc.get("id") or f"call_{uuid4().hex[:12]}"
                    tool_index = cursor["next_index"]
                    cursor["next_index"] += 1
                    yield ContentBlockStart(
                        index=tool_index,
                        block=ToolUseBlock(id=tool_id, name=name, input=args_dict),
                    )
                    yield ContentBlockDelta(
                        index=tool_index,
                        delta=InputJsonDelta(
                            partial_json=_JSON_ENCODER.encode(args_dict).decode("utf-8")
                        ),
                    )
                    yield ContentBlockStop(index=tool_index)

                # Final chunk.
                if chunk.get("done"):
                    # Flush parsers if present (handles unterminated tags).
                    parser_emitted_tools = False
                    if thinking_parser is not None:
                        for piece_kind, piece_text in _pipe_content(
                            "", thinking_parser, finalize=True
                        ):
                            if piece_kind == "thinking":
                                for out in _emit_thinking(piece_text, cursor):
                                    yield out
                            else:
                                # tail text — route through tool parser too.
                                if cursor.get("thinking_open"):
                                    yield ContentBlockStop(index=cursor["thinking_index"])
                                    cursor["thinking_open"] = False
                                    cursor["thinking_index"] = None
                                if text_parser is not None:
                                    for parser_ev in text_parser.feed(piece_text):
                                        for out in _from_parser_event(parser_ev, cursor):
                                            yield out
                                else:
                                    if not cursor["text_open"]:
                                        cursor["text_index"] = cursor["next_index"]
                                        cursor["next_index"] += 1
                                        yield ContentBlockStart(
                                            index=cursor["text_index"],
                                            block=TextBlock(text=""),
                                        )
                                        cursor["text_open"] = True
                                    yield ContentBlockDelta(
                                        index=cursor["text_index"],
                                        delta=TextDelta(text=piece_text),
                                    )
                    if text_parser is not None:
                        for parser_ev in text_parser.finalize():
                            if isinstance(parser_ev, (_ParserToolStart, _ParserToolStop)):
                                parser_emitted_tools = True
                            for out in _from_parser_event(parser_ev, cursor):
                                yield out
                    if cursor.get("thinking_open"):
                        yield ContentBlockStop(index=cursor["thinking_index"])
                        cursor["thinking_open"] = False
                    if cursor["text_open"] and cursor["text_index"] is not None:
                        yield ContentBlockStop(index=cursor["text_index"])
                        cursor["text_open"] = False
                    usage = _decode_usage(chunk)
                    stop_reason = _map_stop_reason(
                        chunk.get("done_reason"),
                        has_tool_calls=bool(tool_calls)
                        or bool(cursor["tool_indices"])
                        or parser_emitted_tools,
                    )
                    yield MessageDelta(stop_reason=stop_reason, usage=usage)
                    yield MessageStop()
                    return

            # Stream ended without a final ``done:true`` chunk.
            if not started:
                raise ProviderError("Ollama stream ended with no chunks")
            if text_parser is not None:
                for parser_ev in text_parser.finalize():
                    for out in _from_parser_event(parser_ev, cursor):
                        yield out
            if cursor["text_open"] and cursor["text_index"] is not None:
                yield ContentBlockStop(index=cursor["text_index"])
            yield MessageDelta(stop_reason="end_turn", usage=None)
            yield MessageStop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_text(blocks: list[Any]) -> str:
    return "\n".join(b.text for b in blocks if isinstance(b, TextBlock))


def _render_block_as_text(blk: Any) -> str:
    """Collapse a non-text block (tool_result, image) to a tagged string so
    Ollama's plain-text content slot can carry it for prompt-engineered paths."""
    if hasattr(blk, "tool_use_id"):  # ToolResultBlock
        body = blk.content if isinstance(blk.content, str) else _join_text(blk.content)
        err = ' is_error="true"' if getattr(blk, "is_error", False) else ""
        return f'<tool_result tool_call_id="{blk.tool_use_id}"{err}>{body}</tool_result>'
    if hasattr(blk, "source"):  # ImageBlock
        return "[image]"
    return ""


def _pipe_content(
    text: str,
    thinking_parser: ThinkingParser | None,
    *,
    finalize: bool,
) -> list[tuple[str, str]]:
    """Route ``text`` through the (optional) thinking parser.

    Yields ``("thinking", chunk)`` or ``("text", chunk)`` tuples. When
    ``thinking_parser`` is None, the whole input is a single ``("text", ...)``.
    When ``finalize=True``, also drains the parser's tail buffer (called
    once at stream end).
    """

    out: list[tuple[str, str]] = []
    if thinking_parser is None:
        if text:
            out.append(("text", text))
        return out

    for ev in thinking_parser.feed(text):
        if isinstance(ev, _ThinkingChunk):
            out.append(("thinking", ev.text))
        elif isinstance(ev, _ThinkTextChunk):
            out.append(("text", ev.text))
    if finalize:
        for ev in thinking_parser.finalize():
            if isinstance(ev, _ThinkingChunk):
                out.append(("thinking", ev.text))
            elif isinstance(ev, _ThinkTextChunk):
                out.append(("text", ev.text))
    return out


def _emit_thinking(text: str, cursor: dict[str, Any]) -> list[StreamEvent]:
    """Translate a piece of thinking text into ContentBlockStart/Delta events.

    Opens a fresh ThinkingBlock if none is open. Reuses the same block for
    further pieces of thinking from the same reasoning span.
    """

    out: list[StreamEvent] = []
    if not text:
        return out
    if not cursor.get("thinking_open"):
        cursor["thinking_index"] = cursor["next_index"]
        cursor["next_index"] += 1
        cursor["thinking_open"] = True
        out.append(
            ContentBlockStart(
                index=cursor["thinking_index"],
                block=ThinkingBlock(thinking=""),
            )
        )
    out.append(
        ContentBlockDelta(
            index=cursor["thinking_index"],
            delta=ThinkingDelta(thinking=text),
        )
    )
    return out


def _from_parser_event(ev: Any, cursor: dict[str, Any]) -> list[StreamEvent]:
    """Translate a ToolCallTextParser event to normalized StreamEvents.

    ``cursor`` is the mutable state from _stream_ndjson (``next_index``,
    ``text_index``, ``text_open``, ``tool_indices``). We append to it as
    we open/close text and tool-use blocks. Returning a list (not yielding)
    keeps the caller a generator without surprises.
    """

    out: list[StreamEvent] = []
    if isinstance(ev, TextChunk):
        if not cursor["text_open"]:
            cursor["text_index"] = cursor["next_index"]
            cursor["next_index"] += 1
            out.append(
                ContentBlockStart(
                    index=cursor["text_index"], block=TextBlock(text="")
                )
            )
            cursor["text_open"] = True
        out.append(
            ContentBlockDelta(
                index=cursor["text_index"], delta=TextDelta(text=ev.text)
            )
        )
        return out

    if isinstance(ev, _ParserToolStart):
        # Close any open text block before opening a tool_use block — the
        # agent loop expects strict index ordering.
        if cursor["text_open"] and cursor["text_index"] is not None:
            out.append(ContentBlockStop(index=cursor["text_index"]))
            cursor["text_open"] = False
            cursor["text_index"] = None

        tool_index = cursor["next_index"]
        cursor["next_index"] += 1
        cursor["tool_indices"][ev.call_id] = tool_index
        out.append(
            ContentBlockStart(
                index=tool_index,
                block=ToolUseBlock(id=ev.call_id, name=ev.name, input={}),
            )
        )
        return out

    if isinstance(ev, _ParserToolDelta):
        idx = cursor["tool_indices"].get(ev.call_id)
        if idx is None:
            return out  # orphan delta — drop silently
        out.append(
            ContentBlockDelta(
                index=idx, delta=InputJsonDelta(partial_json=ev.partial_json)
            )
        )
        return out

    if isinstance(ev, _ParserToolStop):
        idx = cursor["tool_indices"].pop(ev.call_id, None)
        if idx is None:
            return out
        out.append(ContentBlockStop(index=idx))
        return out

    return out


def _decode_usage(chunk: dict[str, Any]) -> Usage | None:
    prompt = chunk.get("prompt_eval_count")
    eval_count = chunk.get("eval_count")
    if prompt is None and eval_count is None:
        return None
    return Usage(
        input_tokens=int(prompt or 0),
        output_tokens=int(eval_count or 0),
    )


def _to_openai_tool(t: dict[str, Any]) -> dict[str, Any]:
    """Translate Anthropic-shape ``{name, description, input_schema}`` into the
    OpenAI-shape ``{type: "function", function: {name, description, parameters}}``
    that Ollama (and llama.cpp's ``--jinja`` mode, vLLM, TGI, …) require.

    If the input is already OpenAI-shape, it's returned unchanged so callers
    can mix sources without us re-wrapping. Missing fields are tolerated —
    we copy what's present.
    """

    if not isinstance(t, dict):
        return t
    if t.get("type") == "function" and isinstance(t.get("function"), dict):
        return t
    fn: dict[str, Any] = {}
    if "name" in t:
        fn["name"] = t["name"]
    if "description" in t:
        fn["description"] = t["description"]
    # Anthropic calls the schema `input_schema`; OpenAI calls it `parameters`.
    if "input_schema" in t:
        fn["parameters"] = t["input_schema"]
    elif "parameters" in t:
        fn["parameters"] = t["parameters"]
    return {"type": "function", "function": fn}


def _map_stop_reason(done_reason: str | None, *, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_use"
    if not done_reason:
        return "end_turn"
    # Ollama uses "stop", "length", "load" — normalize to upstream vocab.
    if done_reason == "length":
        return "max_tokens"
    if done_reason == "stop":
        return "end_turn"
    return done_reason
