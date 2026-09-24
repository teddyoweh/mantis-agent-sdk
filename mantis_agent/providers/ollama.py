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

import httpx
import msgspec

from ..capabilities import (
    HOSTED_PROFILES,
    BackendCapability,
    ModelCapability,
    lookup_model,
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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)
from .base import (
    HTTPProviderMixin,
    current_turn_start,
    normalize_messages,
    strip_control_keys,
)

DEFAULT_BASE_URL = "http://localhost:11434"

# Context window (``options.num_ctx``). Ollama's own default is 2k-4k depending
# on the version, and a prompt past it is truncated from the FRONT, silently —
# the system prompt and tool definitions are the first casualties. We send the
# window the engine plans against, capped: the KV cache grows linearly with
# num_ctx, and a 128k window OOMs most consumer GPUs. 32k fits a 7-8B model on
# a 16-24 GB card; raise the cap (or pin num_ctx) when you have the memory.
DEFAULT_MAX_NUM_CTX = 32768
_NUM_CTX_ENV = "MANTIS_OLLAMA_NUM_CTX"
_MAX_NUM_CTX_ENV = "MANTIS_OLLAMA_MAX_NUM_CTX"
_KEEP_ALIVE_ENV = "MANTIS_OLLAMA_KEEP_ALIVE"
# Ollama unloads a model after 5 idle minutes; an agent pausing for a human
# (permission prompt, reading a diff) pays a full cold reload on the next turn.
DEFAULT_KEEP_ALIVE = "30m"
_SHOW_TIMEOUT_S = 3.0
# A failed /api/show (daemon still starting, transient 5xx) is retried once.
_PROBE_ATTEMPTS = 2
_OFF = ("", "off", "none", "default", "false")

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
        num_ctx: int | None = None,
        max_num_ctx: int | None = None,
        keep_alive: str | int | None = None,
        probe_model_info: bool | None = None,
    ) -> None:
        """``num_ctx`` pins the context window (``0`` = never send one);
        ``max_num_ctx`` caps the automatic one; ``keep_alive`` is sent as-is
        (``"30m"``, ``-1`` = forever, ``0`` = unload now). Each falls back to
        its ``MANTIS_OLLAMA_*`` variable. ``probe_model_info`` asks
        ``/api/show`` once per model for its trained context length (default
        on, off under ``MANTIS_AGENT_MOCK=1``)."""

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
        self._num_ctx_arg = num_ctx
        self._max_num_ctx_arg = max_num_ctx
        self.keep_alive = _resolve_keep_alive(keep_alive)
        if probe_model_info is None:
            probe_model_info = os.environ.get("MANTIS_AGENT_MOCK") != "1"
        self._probe_model_info = probe_model_info
        # Per model, computed once: a different num_ctx on the next request
        # makes Ollama reload the model, so the value must not drift in-session.
        self._num_ctx: dict[str, int | None] = {}
        self._planned: dict[str, int | None] = {}
        self._model_max_ctx: dict[str, int | None] = {}
        self._probe_attempts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Context window / residency
    # ------------------------------------------------------------------

    def model_context_length(self, model: str) -> int | None:
        """The model's trained context length from ``/api/show``, if probed."""

        return self._model_max_ctx.get(model)

    def num_ctx_for(self, model: str) -> int | None:
        """The ``num_ctx`` this provider sends for ``model`` (``None`` = none,
        the server decides). Only known after the first request."""

        return self._num_ctx.get(model)

    def _is_cloud(self, model: str) -> bool:
        """Ollama Cloud: a ``*-cloud`` / ``*:cloud`` tag (proxied by the local
        daemon) or ollama.com itself. The window is the provider's, not a KV
        cache on the user's GPU — no ``num_ctx``, no VRAM ceiling."""

        m = model.lower()
        if m.endswith("-cloud") or m.endswith(":cloud"):
            return True
        host = (self.client.base_url.host or "").lower()
        return host == "ollama.com" or host.endswith(".ollama.com")

    def _sync_plan(
        self, model: str, model_capability: ModelCapability | None
    ) -> tuple[int | None, int | None]:
        """``(num_ctx to send, window to plan against)`` from settings alone —
        no network. :meth:`_resolve_num_ctx` refines it with the probes."""

        explicit = self._num_ctx_arg
        if explicit is None:
            explicit = _env_int(_NUM_CTX_ENV)
        if explicit is not None:
            # 0 = "respect the server": send nothing, plan against the table.
            if explicit > 0:
                return explicit, explicit
            cap = model_capability or lookup_model(model)
            return None, (getattr(cap, "context_window", 0) or None)
        cap = model_capability or lookup_model(model)
        declared = getattr(cap, "context_window", 0) or 0
        if self._is_cloud(model):
            return None, (declared or None)
        server_len = _env_int("OLLAMA_CONTEXT_LENGTH")
        if server_len:
            # The user configured the daemon; overriding it per request would
            # both ignore their choice and reload the model. Plan against it
            # (it describes the server only when that server is local).
            return None, server_len
        ceiling = self._ceiling()
        value = min(declared, ceiling) if declared > 0 else ceiling
        return value, value

    def _ceiling(self) -> int:
        return self._max_num_ctx_arg or _env_int(_MAX_NUM_CTX_ENV) or DEFAULT_MAX_NUM_CTX

    def planned_context_window(
        self, model: str, model_capability: ModelCapability | None = None
    ) -> int | None:
        """The window the engine should plan against, available BEFORE the
        first request (the agent sizes compaction and ``max_tokens`` first).

        Synchronous: settings only, announced to :mod:`..context_limits`. The
        first :meth:`stream` may then refine it from ``/api/show`` /
        ``/api/ps`` and re-announce the final value."""

        if model in self._planned:
            return self._planned[model]
        _value, planned = self._sync_plan(model, model_capability)
        self._note(model, planned)
        return planned

    def _note(self, model: str, planned: int | None) -> None:
        if not planned:
            return
        try:
            from ..context_limits import note_runtime_limit  # noqa: PLC0415
            note_runtime_limit(model, planned, str(self.client.base_url))
        except Exception:  # noqa: BLE001 — planning hint only
            pass

    async def _probe_context_length(self, model: str) -> int | None:
        """``POST /api/show``; the ``<general.architecture>.context_length``
        entry of ``model_info`` (any ``*.context_length`` as a fallback).

        A definitive answer — found, or a clean body without the key — is
        cached. A failed call (unreachable, 4xx/5xx, bad body) is not: it is
        retried on the next request, up to :data:`_PROBE_ATTEMPTS` in all.
        Failure is a quiet ``None``; this is a clamp, never a reason for a
        turn to fail."""

        if model in self._model_max_ctx:
            return self._model_max_ctx[model]
        self._probe_attempts[model] = self._probe_attempts.get(model, 0) + 1
        try:
            resp = await self.client.post(
                "/api/show",
                content=_JSON_ENCODER.encode({"model": model}),
                timeout=_SHOW_TIMEOUT_S,
            )
            if resp.status_code >= 400:
                return None
            info = (_JSON_DECODER.decode(resp.content) or {}).get("model_info") or {}
        except Exception:  # noqa: BLE001 — unreachable, old server, odd body
            return None
        found = _context_length_from_info(info)
        self._model_max_ctx[model] = found
        return found

    async def _probe_loaded_context(self, model: str) -> int | None:
        """The window the daemon has ``model`` loaded with, from ``GET
        /api/ps`` (newer Ollama reports ``context_length``). ``None`` when not
        loaded, not reported, or unreachable. Not cached: residency changes."""

        try:
            resp = await self.client.get("/api/ps", timeout=_SHOW_TIMEOUT_S)
            if resp.status_code >= 400:
                return None
            body = _JSON_DECODER.decode(resp.content) or {}
        except Exception:  # noqa: BLE001
            return None
        want = _norm_tag(model)
        for entry in body.get("models") or []:
            if not isinstance(entry, dict):
                continue
            names = {_norm_tag(str(entry.get(k) or "")) for k in ("name", "model")}
            if want in names:
                ctx = entry.get("context_length")
                return ctx if isinstance(ctx, int) and ctx > 0 else None
        return None

    async def _resolve_num_ctx(
        self, model: str, model_capability: ModelCapability | None
    ) -> int | None:
        """Decide ``options.num_ctx`` for ``model`` — once, then cached.

        Precedence: constructor ``num_ctx`` > ``MANTIS_OLLAMA_NUM_CTX`` (``0``
        = send none, respect the server) > Ollama Cloud (send none) > a
        server-side ``OLLAMA_CONTEXT_LENGTH`` in this environment (send none)
        > the capability window capped at ``max_num_ctx`` /
        ``MANTIS_OLLAMA_MAX_NUM_CTX`` / 32768.

        The automatic value is then refined: a model the table does not know
        uses its ``/api/show`` trained length (capped) instead of the 8k
        guess; a model already loaded by the daemon with at least that window
        (``/api/ps``) keeps the loaded one, so a larger server-side
        ``OLLAMA_CONTEXT_LENGTH`` is not overridden and no reload happens.
        Everything is clamped to the trained length and announced to
        :mod:`..context_limits` so compaction plans against the real window.
        """

        if model in self._num_ctx:
            return self._num_ctx[model]

        value, planned = self._sync_plan(model, model_capability)
        explicit = self._num_ctx_arg is not None or _env_int(_NUM_CTX_ENV) is not None
        cloud = self._is_cloud(model)
        automatic = value is not None and not explicit

        probe_failed = False
        if self._probe_model_info and planned is not None:
            trained = await self._probe_context_length(model)
            probe_failed = trained is None and model not in self._model_max_ctx
            if trained:
                cap = model_capability or lookup_model(model)
                if cloud and not explicit:
                    # The provider's window, not our GPU's: trust the model.
                    planned = trained
                elif automatic and getattr(cap, "family", "") == "unknown":
                    value = planned = min(trained, self._ceiling())
                if value is not None:
                    value = min(value, trained)
                planned = min(planned, trained)
            if automatic or (value is None and not cloud):
                loaded = await self._probe_loaded_context(model)
                if loaded and trained:
                    loaded = min(loaded, trained)
                if loaded and automatic and loaded >= value:
                    value = planned = loaded
                elif loaded and not automatic:
                    # Respecting the server: plan against what it really runs.
                    planned = loaded

        self._planned[model] = planned
        if probe_failed and self._probe_attempts.get(model, 0) < _PROBE_ATTEMPTS:
            # Retry the probe next request; the value may still be refined
            # once (one reload at worst) rather than pinned to a guess.
            self._note(model, planned)
            return value
        self._num_ctx[model] = value
        self._note(model, planned)
        return value

    # ------------------------------------------------------------------
    # Message serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_messages(
        messages: Iterable[Message],
        system: str | None,
        *,
        native_tools: bool = False,
    ) -> list[dict[str, Any]]:
        """Flatten our typed messages into Ollama's ``messages`` list.

        ``native_tools`` (Path A) sends each tool result as its own
        ``{"role": "tool", "tool_name", "tool_call_id", "content"}`` message
        right after the requesting assistant message — the shape the models'
        chat templates were trained on. Anything else riding in that user
        message (text, reminders, images, images nested in results) follows
        in one user message. The prompt-engineered path keeps the tagged
        ``<tool_result …>`` text the Hermes prompt describes.

        Reasoning goes back in the assistant ``thinking`` field only within
        the current agentic turn (see :func:`current_turn_start`).
        """

        out: list[dict[str, Any]] = []
        if system is not None:
            out.append({"role": "system", "content": system})

        msgs = normalize_messages(messages)
        turn_start = current_turn_start(msgs)
        # tool_use_id -> (name, call position) of the latest assistant calls.
        calls: dict[str, tuple[str, int]] = {}

        for i, m in enumerate(msgs):
            if isinstance(m, SystemMessage):
                content = m.content if isinstance(m.content, str) else _join_text(m.content)
                out.append({"role": "system", "content": content})
                continue
            if isinstance(m, UserMessage):
                if isinstance(m.content, str):
                    out.append({"role": "user", "content": m.content})
                    continue
                if native_tools:
                    out.extend(_encode_user_native(m.content, calls))
                    continue
                # Multi-block user message — may include tool_result + image
                # blocks. Ollama's vision models read images from a per-message
                # ``images: [base64,...]`` array (NOT inline in content), so we
                # split image blocks out there and keep text in ``content``.
                text_buf: list[str] = []
                images: list[str] = []
                for blk in m.content:
                    if isinstance(blk, TextBlock):
                        text_buf.append(blk.text)
                    elif hasattr(blk, "source"):  # ImageBlock
                        data = (blk.source or {}).get("data")
                        if isinstance(data, str) and data:
                            images.append(data)
                    else:
                        # tool_result, etc. — collapse to a tagged string.
                        text_buf.append(_render_block_as_text(blk))
                        # MCP results nest images; use the same native images
                        # array as read_file, retaining the tagged result/id.
                        images.extend(_nested_images(blk))
                umsg: dict[str, Any] = {"role": "user", "content": "\n".join(text_buf)}
                if images:
                    umsg["images"] = images
                out.append(umsg)
                continue
            if isinstance(m, AssistantMessage):
                texts: list[str] = []
                thinking: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                calls = {}
                for blk in m.content:
                    if isinstance(blk, TextBlock):
                        texts.append(blk.text)
                    elif isinstance(blk, ThinkingBlock):
                        thinking.append(blk.thinking)
                    elif isinstance(blk, ToolUseBlock):
                        # Drop null argument values — a cut-off tool call can
                        # carry ``content: null``, which Ollama rejects with
                        # "expected a string, got null" and breaks the whole
                        # next turn. Omitting the key lets the model re-supply it.
                        args = {
                            k: v for k, v in (blk.input or {}).items() if v is not None
                        }
                        call: dict[str, Any] = {
                            "function": {"name": blk.name, "arguments": args}
                        }
                        # ``id`` pairs with the tool message's ``tool_call_id``
                        # on newer servers; older ones ignore unknown fields.
                        if native_tools and blk.id:
                            call["id"] = blk.id
                        tool_calls.append(call)
                        calls[blk.id] = (blk.name, len(calls))
                msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts)}
                # Interleaved reasoning helps across the tool calls of this
                # turn; earlier turns' chain-of-thought is dropped, as the
                # models' own chat templates do.
                if thinking and i >= turn_start and any(t.strip() for t in thinking):
                    msg["thinking"] = "".join(thinking)
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
        thinking: dict[str, Any] | None = None,
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
        # History encoding follows the same split: tool results go back as
        # ``role: tool`` messages unless this model is on the Hermes path
        # (tool-less requests — a wrap-up turn — keep the model's own shape).
        native_history = use_native_tools if tools else bool(
            (model_capability is None or model_capability.supports_native_tools)
            and self.backend_capability.supports_native_tools
        )
        effective_system = system
        if tools and not use_native_tools:
            tools_json = _JSON_ENCODER.encode(tools).decode("utf-8")
            inject = _HERMES_PROMPT % {"tools_json": tools_json}
            effective_system = inject if system is None else f"{system}\n\n{inject}"

        payload: dict[str, Any] = {
            "model": model,
            "messages": self._encode_messages(
                messages, effective_system, native_tools=native_history
            ),
            "stream": True,
            "options": {"num_predict": max_tokens},
        }
        num_ctx = await self._resolve_num_ctx(model, model_capability)
        if num_ctx is not None:
            payload["options"]["num_ctx"] = num_ctx
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
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
            # SDK-level knobs (reasoning aliases, tool-permission lists) are not
            # Ollama wire fields — the ``think`` translation below is how a
            # reasoning request reaches this backend.
            payload.update(strip_control_keys(extra))

        # Universal thinking config -> Ollama's top-level ``think`` flag.
        # Best-effort: most local checkpoints have no reasoning mode, so only
        # send ``think`` when the model actually reasons (per capabilities) —
        # otherwise Ollama rejects the field. When we can't tell, we no-op. An
        # explicit extra["think"] wins (guarded below). ``None`` == no change.
        if (thinking is not None
                and "think" not in payload
                and _model_supports_thinking(model_capability)):
            ttype = thinking.get("type") if isinstance(thinking, dict) else None
            if ttype in ("enabled", "adaptive", "disabled"):
                payload["think"] = ttype != "disabled"

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
                    # Emit the arguments EXACTLY ONCE — via the InputJsonDelta
                    # below — and open the block with empty input, matching the
                    # Anthropic wire contract and every other path in this SDK
                    # (openai_compat native + the prompt-engineered parser path).
                    # Populating ``input`` here AND streaming the same args as a
                    # delta makes any consumer that folds both (per the wire
                    # contract) concatenate the arguments twice into malformed
                    # JSON.
                    yield ContentBlockStart(
                        index=tool_index,
                        block=ToolUseBlock(id=tool_id, name=name, input={}),
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
            # Ollama terminates every healthy stream with a ``done:true`` chunk
            # (which carries the stop reason + usage). Reaching here with
            # ``started`` true means the NDJSON body was cut short mid-message —
            # a dropped connection, a killed/OOM'd backend, or a proxy that
            # closed the stream early. Emitting a clean ``end_turn`` here would
            # hand the agent loop a silently-truncated message as if it were a
            # complete completion. Fail closed instead: raise a transport-level
            # error so the loop treats it as a transient failure (and retries
            # when nothing has streamed yet) rather than accepting the
            # truncation as success.
            raise httpx.RemoteProtocolError(
                "Ollama stream truncated: ended without a final done:true chunk"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_int(name: str) -> int | None:
    """An integer environment variable; unset or unparseable is ``None``."""

    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _context_length_from_info(info: dict[str, Any]) -> int | None:
    """The trained window in an ``/api/show`` ``model_info`` map.

    Exact ``<general.architecture>.context_length`` first — a multimodal model
    also carries e.g. ``clip.context_length`` for its vision tower, and a
    first-match suffix scan could pick that. The scan is only the fallback.
    """

    arch = info.get("general.architecture")
    if isinstance(arch, str) and arch:
        value = info.get(f"{arch}.context_length")
        if isinstance(value, int) and value > 0:
            return value
    for key, value in info.items():
        if str(key).endswith(".context_length") and isinstance(value, int) and value > 0:
            return value
    return None


def _norm_tag(name: str) -> str:
    """``llama3`` and ``llama3:latest`` name the same model."""

    name = name.strip().lower()
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _resolve_keep_alive(value: str | int | None) -> str | int | None:
    """``keep_alive`` for the wire, or ``None`` to leave Ollama's default.

    Ollama parses a string as a Go duration (``"30m"``) and a number as
    seconds — so a bare ``"-1"`` / ``"0"`` must go out as an integer; as a
    string Ollama rejects it for missing a unit.
    """

    if value is None:
        env = os.environ.get(_KEEP_ALIVE_ENV)
        value = DEFAULT_KEEP_ALIVE if env is None else env
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lower() in _OFF:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _model_supports_thinking(cap: ModelCapability | None) -> bool:
    """Whether it's safe to send Ollama a ``think`` flag for this model.

    Ollama only honors ``think`` on models it serves in reasoning mode (R1,
    QwQ, distills, …); sending it to a plain chat model errors. Gate on the
    capability's reasoning signals — request-side knob, or the model emitting
    thinking either out-of-band or inline. Unknown capability -> no-op.
    """

    return bool(
        cap
        and (
            cap.supports_reasoning_effort
            or cap.emits_thinking_blocks
            or cap.emits_inline_thinking
        )
    )


def _join_text(blocks: list[Any]) -> str:
    return "\n".join(b.text for b in blocks if isinstance(b, TextBlock))


def _nested_images(blk: Any) -> list[str]:
    """Base64 payloads of images nested inside a tool result's content."""
    nested = getattr(blk, "content", None)
    found: list[str] = []
    if isinstance(nested, list):
        for part in nested:
            source = getattr(part, "source", None) or {}
            data = source.get("data")
            if isinstance(data, str) and data:
                found.append(data)
    return found


def _tool_result_text(blk: ToolResultBlock) -> str:
    """A tool result's text for a ``tool`` message; errors say so up front
    (the tool role has no error flag). Images travel separately."""
    body = blk.content if isinstance(blk.content, str) else _join_text(blk.content)
    if blk.is_error and not body.lstrip().lower().startswith("error"):
        body = f"Error: {body}" if body else "Error"
    return body


def _encode_user_native(
    blocks: list[Any], calls: dict[str, tuple[str, int]]
) -> list[dict[str, Any]]:
    """Path A: one ``tool`` message per result, in the assistant's call order,
    then one user message for the rest (text, images, result images).

    Tool messages go first because chat templates expect them immediately
    after the assistant's ``tool_calls``. Images never ride on a tool
    message — most templates render only ``content`` for the tool role — so
    they follow in the user message, labelled per call so parallel
    screenshots stay attributable.
    """
    results: list[ToolResultBlock] = []
    text_buf: list[str] = []
    images: list[str] = []
    unsendable_images = 0  # URL-sourced: Ollama takes base64 only
    for blk in blocks:
        if isinstance(blk, ToolResultBlock):
            results.append(blk)
        elif isinstance(blk, TextBlock):
            text_buf.append(blk.text)
        elif hasattr(blk, "source"):  # ImageBlock
            data = (blk.source or {}).get("data")
            if isinstance(data, str) and data:
                images.append(data)
            else:
                unsendable_images += 1

    # Stable: results for unknown ids keep their relative place at the end.
    results.sort(key=lambda r: calls.get(r.tool_use_id, ("", len(calls)))[1])
    out: list[dict[str, Any]] = []
    result_images: list[str] = []
    labels: list[str] = []
    for r in results:
        nested = _nested_images(r)
        content = _tool_result_text(r)
        if not content.strip():
            # An empty tool message reads as "the call did nothing" and small
            # models re-issue it; say what happened instead.
            content = (f"(see images for call {r.tool_use_id} below)" if nested
                       else "(no output)")
        tmsg: dict[str, Any] = {"role": "tool", "content": content}
        name = calls.get(r.tool_use_id, ("", 0))[0]
        if name:
            tmsg["tool_name"] = name
        if r.tool_use_id:
            tmsg["tool_call_id"] = r.tool_use_id
        out.append(tmsg)
        if nested:
            labels.append(f"Images from tool call {r.tool_use_id}: {len(nested)}")
            result_images.extend(nested)

    all_images = result_images + images
    if text_buf or all_images:
        umsg: dict[str, Any] = {"role": "user", "content": "\n".join(labels + text_buf)}
        if all_images:
            umsg["images"] = all_images
        out.append(umsg)
    if not out:
        # Nothing sendable (URL-only images, unknown blocks). Dropping the
        # turn entirely would put two assistant messages back to back, or
        # end the history on the assistant — keep the user's slot.
        out.append({"role": "user",
                    "content": "[image]" if unsendable_images else ""})
    return out


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
