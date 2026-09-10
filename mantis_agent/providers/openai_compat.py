"""OpenAI-compatible adapter — the workhorse.

One adapter, every provider that speaks ``POST /v1/chat/completions``: OpenAI
itself, Google Gemini's OpenAI-compat endpoint, xAI Grok, vLLM, Together,
Fireworks, Groq, OpenRouter, Cerebras, DeepInfra, Anyscale, DeepSeek, Mistral's
own API.

Responsibilities
----------------
1. Auth + base-URL resolution (host-aware env-key chain, vLLM localhost default).
2. Backend capability detection (hosted profile match or generic vLLM-style).
   The profile also picks the *reasoning style* — how the universal
   ``thinking`` config becomes each vendor's native knob (see
   ``_reasoning_style`` / ``_apply_thinking``):

   * OpenAI reasoning models (gpt-5.x / o-series): ``reasoning_effort``,
     ``max_completion_tokens`` instead of ``max_tokens``, no ``temperature``.
   * Gemini: ``reasoning_effort`` (low/medium/high/none) for effort words; an
     explicit token budget becomes
     ``extra_body.google.thinking_config.thinking_budget``.
   * xAI Grok: ``reasoning_effort`` low/high on grok-3-mini only (grok-4 and
     grok-3 reason at a fixed level and reject the field); ``reasoning_content``
     deltas surface as thinking blocks.
   * everything else: ``reasoning_effort`` only where the model is known to
     accept it.
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
from collections.abc import AsyncIterator, Callable, Iterable
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

__all__ = ["OpenAICompatProvider", "env_key_candidates"]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://localhost:8000/v1"  # vLLM's default OpenAI-compat URL

#: Azure pins the wire contract to a dated API version on its classic
#: deployment surface. This is the current GA version for chat completions;
#: override with ``AZURE_OPENAI_API_VERSION`` when a resource is older or when
#: a preview feature is needed.
DEFAULT_AZURE_API_VERSION = "2024-10-21"


def _is_azure_openai(url: str) -> bool:
    """Is this an Azure OpenAI endpoint? Matched on the host so a caller only
    has to paste the endpoint Azure shows them."""

    lower = (url or "").lower()
    return (
        "openai.azure.com" in lower
        or "cognitiveservices.azure.com" in lower
        or "services.ai.azure.com" in lower
    )


def _azure_base_url(url: str) -> str:
    """Normalize an Azure endpoint to the base the request paths hang off.

    Azure shows people ``https://{resource}.openai.azure.com`` (no path), and
    its docs use both ``/openai/v1`` (the new surface, model in the body) and
    ``/openai/deployments/{name}`` (the classic one). Land on ``…/openai`` or
    ``…/openai/v1`` so both request shapes below resolve.
    """

    base = (url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if "/openai" not in base.lower():
        base = base + "/openai"
    return base

# Order matters: first env var that's set wins. OPENAI_API_KEY first because
# it's the de-facto standard and many users export it once for everything.
_ENV_KEY_CHAIN: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
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

# The vendor's OWN key variables, consulted before the generic chain when the
# base URL names the vendor. Without this a stale OPENAI_API_KEY in the shell
# outranked XAI_API_KEY on a request bound for api.x.ai and produced a 401
# that read like the Grok key was bad. Order within a tuple: canonical name
# first, then the alias people actually export.
_HOST_ENV_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("api.openai.com", ("OPENAI_API_KEY",)),
    ("api.x.ai", ("XAI_API_KEY", "GROK_API_KEY")),
    ("generativelanguage.googleapis", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ("api.together.", ("TOGETHER_API_KEY",)),
    ("fireworks", ("FIREWORKS_API_KEY",)),
    ("groq", ("GROQ_API_KEY",)),
    ("openrouter", ("OPENROUTER_API_KEY",)),
    ("api.deepseek.com", ("DEEPSEEK_API_KEY",)),
    ("deepinfra", ("DEEPINFRA_API_KEY",)),
    ("cerebras", ("CEREBRAS_API_KEY",)),
    ("anyscale", ("ANYSCALE_API_KEY",)),
    ("moonshot", ("MOONSHOT_API_KEY",)),
)


def env_key_candidates(base_url: str | None) -> tuple[str, ...]:
    """Env var names to try for ``base_url``, most specific first.

    The vendor's own variables (``XAI_API_KEY``/``GROK_API_KEY`` for
    ``api.x.ai``, ``GEMINI_API_KEY``/``GOOGLE_API_KEY`` for Google, …) lead;
    the generic chain follows so a self-hosted or unrecognised URL still
    picks up whichever key is exported.
    """

    lower = (base_url or "").lower()
    preferred: tuple[str, ...] = ()
    for needle, names in _HOST_ENV_KEYS:
        if needle in lower:
            preferred = names
            break
    return preferred + tuple(v for v in _ENV_KEY_CHAIN if v not in preferred)

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
        api_key_provider: Callable[[], str] | None = None,
        api_version: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
        model_capability: ModelCapability | None = None,
    ) -> None:
        url = base_url or DEFAULT_BASE_URL
        # Azure OpenAI is the one OpenAI-compatible surface that authenticates
        # differently: the key rides in an ``api-key`` header (not Bearer), the
        # deployment name takes the place of the model, and the classic route
        # requires an ``api-version`` query parameter. Detected from the host so
        # a caller only has to give the endpoint.
        self._azure = _is_azure_openai(url)
        self._api_version = (
            api_version
            or os.environ.get("AZURE_OPENAI_API_VERSION")
            or (DEFAULT_AZURE_API_VERSION if self._azure else "")
        )
        if self._azure:
            url = _azure_base_url(url)
        # The v1 surface reads the model from the body; the classic one routes
        # by deployment path.
        self._azure_v1 = self._azure and url.lower().rstrip("/").endswith("/openai/v1")
        # A token that expires mid-session (Vertex ADC) is re-read per request
        # rather than frozen into the client's headers at construction.
        self._key_provider = api_key_provider
        # api_key semantics: a non-empty string is used verbatim; ``None`` means
        # "discover a key from the env chain"; an empty string ``""`` means
        # "explicitly NO auth — do not read the env" (used by providers like
        # Modal that authenticate with their own headers).
        key = api_key
        if key is None:
            for var in env_key_candidates(url):
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
        if key and self._azure:
            headers["api-key"] = key
        elif key:
            headers["authorization"] = f"Bearer {key}"
        # OpenRouter wants identifying headers for analytics; harmless elsewhere.
        if "openrouter" in url.lower():
            headers.setdefault("http-referer", "https://github.com/teddyoweh/mantis-agent-sdk")
            headers.setdefault("x-title", "mantis-agent-sdk")
        if default_headers:
            # Header names are case-insensitive on the wire but not in a
            # dict: an explicit ``Authorization`` must REPLACE the
            # ``authorization`` set above, not ride beside it (httpx would
            # send both, joined — "Bearer k, Bearer wk-…" — and the endpoint
            # rejects it).
            for name, value in default_headers.items():
                for existing in [k for k in headers if k.lower() == name.lower()]:
                    del headers[existing]
                headers[name] = value

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
        It is translated to the backend's native knob — ``reasoning_effort``
        for OpenAI / generic backends, ``reasoning_effort`` or a
        ``thinking_config`` budget for Gemini, ``reasoning_effort`` low/high
        for Grok's grok-3-mini — only for models that accept a request-side
        knob, and never overrides an explicit reasoning field the caller
        already put in ``extra``. When ``thinking`` is ``None`` the built
        payload is byte-for-byte unchanged.
        """

        cap = model_capability or self._default_model_capability
        bare_model = model.lower().rsplit("/", 1)[-1]
        if tools and bare_model.startswith("gpt-6-astra") and not self._azure:
            async for event in self._stream_responses(
                model=model,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                extra=extra,
                thinking=thinking,
                model_capability=cap,
            ):
                yield event
            return
        # Without tools the path doesn't matter, but we keep Path A semantics
        # so the (text-only) translator runs the simpler hot loop.
        path: ToolUsePath = (
            resolve_tool_use_path(cap, self.backend_capability)
            if (cap is not None and tools)
            else "A"
        )
        # Astra's Chat Completions endpoint rejects function tools whenever
        # reasoning is active, while also rejecting the GPT-5 ``none`` value.
        # Use the prompt-engineered tool protocol until Responses is supported.
        if tools and model.lower().rsplit("/", 1)[-1].startswith("gpt-6-astra"):
            path = "B"
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

        # NB: ``url_path``, not ``path`` — ``path`` is the resolved ToolUsePath
        # above, and shadowing it sent every request down the prompt-engineered
        # branch (silently disabling native tool calls on every backend).
        url_path, params = self._chat_endpoint(model)
        request_headers = self._per_request_headers()

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
                url_path,
                params=params or None,
                headers=request_headers or None,
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
                        if model.lower().rsplit("/", 1)[-1].startswith("gpt-6"):
                            payload.pop("reasoning_effort", None)
                        else:
                            payload["reasoning_effort"] = "none"
                        _disabled_reasoning_effort = True
                        retry = True
                    if retry:
                        continue
                    if _lower_max_tokens_for_context_error(payload, body):
                        continue
                    raise_for_status(response)

                emits_thinking = bool(cap and cap.emits_inline_thinking)
                if path == "A":
                    async for ev in _translate_native(
                        response, model, emits_thinking=emits_thinking,
                        thinking_tags=(cap.inline_thinking_tags if cap else None),
                    ):
                        yield ev
                else:
                    async for ev in _translate_prompt_engineered(
                        response, model, emits_thinking=emits_thinking
                    ):
                        yield ev
                return

    async def _stream_responses(
        self,
        *,
        model: str,
        messages: Iterable[Message],
        system: str | None,
        tools: list[dict[str, Any]],
        max_tokens: int,
        extra: dict[str, Any] | None,
        thinking: dict[str, Any] | None,
        model_capability: ModelCapability | None,
    ) -> AsyncIterator[StreamEvent]:
        """Use OpenAI Responses for Astra's reasoning-compatible native tools."""
        payload = _build_responses_payload(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            extra=extra,
            thinking=thinking,
            model_capability=model_capability,
        )
        response = await self.client.post(
            "/responses",
            headers=self._per_request_headers() or None,
            content=_PAYLOAD_ENCODER.encode(payload),
        )
        raise_for_status(response)
        data = response.json()
        for event in _translate_responses(data, model):
            yield event

    def _chat_endpoint(self, model: str) -> tuple[str, dict[str, str]]:
        """``(path, query)`` for the chat-completions call.

        Everything but classic Azure posts to ``/chat/completions`` relative to
        the base URL. Azure's classic surface routes by *deployment*:
        ``/openai/deployments/{deployment}/chat/completions?api-version=…``,
        where the deployment name is what the caller passes as ``model`` (Azure
        deployments are usually named after the model they serve). Azure's v1
        surface takes the plain path and reads the model from the body, like
        everyone else.
        """

        if not self._azure:
            return "/chat/completions", {}
        params = {"api-version": self._api_version} if self._api_version else {}
        if self._azure_v1:
            return "/chat/completions", ({} if self._api_version in ("", "v1") else params)
        deployment = (model or "").rsplit("/", 1)[-1]
        return f"/deployments/{deployment}/chat/completions", params

    def _per_request_headers(self) -> dict[str, str]:
        """Headers that cannot be frozen at construction — today just a bearer
        token from a provider callback (Vertex ADC tokens expire hourly)."""

        if self._key_provider is None:
            return {}
        token = (self._key_provider() or "").strip()
        return {"authorization": f"Bearer {token}"} if token else {}

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

        # OpenAI's GPT-5/GPT-6 / o-series reject legacy ``max_tokens`` (they want
        # ``max_completion_tokens``) and only accept the default temperature.
        # Other OpenAI-compat backends (vLLM, Groq, Together, …) never serve
        # these ids, so keying off the model name is safe and self-contained.
        _bare = model.lower().rsplit("/", 1)[-1]
        _new_openai = _is_openai_reasoning_model(_bare)
        style = _reasoning_style(model, self.backend_capability)
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
            # NB: a local name, NOT the ``thinking`` parameter. Rebinding it
            # here meant any non-empty ``extra`` without a "thinking" key
            # silently erased the universal thinking config below — so
            # extra={"verbosity": …} quietly turned reasoning off.
            extra_thinking = extra.get("thinking")
            if isinstance(extra_thinking, dict) and extra_thinking.get("effort") is not None:
                effort = extra_thinking["effort"]
            if effort is not None:
                normalized = _normalize_effort(str(effort), style, _bare)
                if normalized is not None:
                    payload["reasoning_effort"] = normalized
            # ``verbosity`` is a real GPT-5 Chat Completions field, but only
            # there — gpt-4o and every OSS server 400 on it.
            if extra.get("verbosity") is not None and _new_openai:
                payload["verbosity"] = extra["verbosity"]

        # Universal thinking config -> the backend's native reasoning knob. Only
        # applied when the caller didn't already set a reasoning field (extra
        # wins), and only for models that accept a request-side knob — sending
        # reasoning_effort to a plain chat model (gpt-4o, most local
        # checkpoints) is a hard 400.
        if thinking and "reasoning_effort" not in payload:
            _apply_thinking(payload, thinking, style=style, bare=_bare,
                            model=model, model_capability=model_capability)

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
            # ``extra_body`` is where Gemini's vendor namespace lives; a caller
            # adding e.g. ``include_thoughts`` must not wipe the thinking budget
            # the translator just wrote (or vice versa). Deep-merge that one key.
            user_extra_body = passthrough.pop("extra_body", None)
            if isinstance(user_extra_body, dict):
                payload["extra_body"] = _deep_merge(
                    payload.get("extra_body") or {}, user_extra_body
                )
            elif user_extra_body is not None:
                payload["extra_body"] = user_extra_body
            payload.update(passthrough)
        return payload


# ---------------------------------------------------------------------------
# Reasoning knobs — per-vendor translation of the universal thinking config
# ---------------------------------------------------------------------------

#: Reasoning styles the payload builder knows how to speak.
_HOSTED_REASONING_STYLES: frozenset[str] = frozenset({"openai", "gemini", "xai"})


def _is_openai_reasoning_model(bare: str) -> bool:
    """GPT-5/GPT-6 and o-series: ``max_completion_tokens``, no temperature,
    request-side ``reasoning_effort``."""
    return bare.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def _reasoning_style(model: str, backend: BackendCapability | None) -> str:
    """Which vendor's reasoning dialect to speak for this request.

    The backend profile decides when it names a first-party API (``openai``,
    ``gemini``, ``xai``) — a Gemini id sent through an OpenAI-compat proxy
    that reports itself as OpenAI gets OpenAI's knob. Only when the backend is
    the generic (self-hosted / unknown URL) profile does the model name break
    the tie, so ``Agent(model="grok-4", backend="https://my-proxy/v1")``
    still speaks Grok. Everything else is ``"generic"``.
    """

    hint = (backend.provider_hint if backend is not None else "") or ""
    if hint in _HOSTED_REASONING_STYLES:
        return hint
    if hint and hint != "vllm":
        # Together / Groq / OpenRouter / … re-serve open weights (and, on
        # OpenRouter, other vendors' models) with their own parameter
        # validation — the generic path is the safe one there.
        return "generic"
    bare = model.lower().rsplit("/", 1)[-1]
    if bare.startswith(("grok-", "grok/")):
        return "xai"
    if bare.startswith("gemini-"):
        return "gemini"
    if _is_openai_reasoning_model(bare):
        return "openai"
    return "generic"


def _normalize_effort(effort: str, style: str, bare: str) -> str | None:
    """Map an SDK effort word to what ``style`` accepts, or ``None`` to omit.

    * ``minimal`` → ``low`` everywhere (only gpt-5 knows ``minimal`` and it is
      a near-no-op; ``low`` is the portable floor).
    * ``ultra`` / ``max`` → ``high``: ultra is a multi-agent orchestration
      mode and ``max`` needs the Responses API — neither is a valid Chat
      Completions value (400 with no recovery), so clamp to the ceiling.
    * ``xhigh`` is a real GPT-5/GPT-6 value and is kept for OpenAI; Gemini and
      Grok don't know it, so it clamps to ``high``.
    * GPT-6 Astra rejects ``none``; omit the field and use its default effort.
    * Grok: only grok-3-mini takes the field, and only ``low``/``high``;
      every other Grok model reasons at a fixed level and 400s on it.
    """

    e = effort.strip().lower()
    if e == "minimal":
        e = "low"
    if e in ("ultra", "max"):
        e = "high"
    if bare.startswith("gpt-6-astra") and e in ("none", "off"):
        return None
    if style == "xai":
        if not _grok_accepts_effort(bare):
            return None
        return "low" if e in ("low", "none") else "high"
    if style == "gemini" and e == "xhigh":
        return "high"
    return e


def _grok_accepts_effort(bare: str) -> bool:
    """xAI documents ``reasoning_effort`` for grok-3-mini only; grok-4 /
    grok-3 / grok-code-fast reject the parameter."""
    return bare.startswith("grok-3-mini")


def _apply_thinking(
    payload: dict[str, Any],
    thinking: dict[str, Any],
    *,
    style: str,
    bare: str,
    model: str,
    model_capability: ModelCapability | None,
) -> None:
    """Write the universal ``thinking`` config onto ``payload`` in the
    vendor's dialect. Mutates ``payload``; never sets a field the vendor
    would reject."""

    ttype = thinking.get("type")
    budget = thinking.get("budget_tokens")

    if style == "gemini":
        # Google's OpenAI-compat endpoint accepts BOTH ``reasoning_effort``
        # (low=1024 / medium=8192 / high=24576 thinking tokens, ``none`` to
        # switch off on Flash-class models) and a precise budget under its
        # vendor namespace, ``extra_body.google.thinking_config``. An explicit
        # token budget from the SDK is honoured exactly through the latter;
        # effort words go through the former. ``adaptive`` with no budget is
        # Gemini's own default (dynamic thinking) so nothing is sent. Thought
        # summaries are opt-in — pass
        # ``extra={"extra_body": {"google": {"thinking_config":
        # {"include_thoughts": True}}}}`` and they stream as thinking blocks.
        if ttype == "disabled":
            payload["reasoning_effort"] = "none"
        elif budget is not None:
            google = payload.setdefault("extra_body", {}).setdefault("google", {})
            cfg = google.setdefault("thinking_config", {})
            cfg["thinking_budget"] = int(budget)
        elif ttype == "enabled":
            payload["reasoning_effort"] = "high"
        return

    if style == "xai":
        # grok-3-mini: ``low`` / ``high`` only. Every other Grok model reasons
        # at a fixed level, can't be switched off, and 400s on the field.
        if not _grok_accepts_effort(bare) or ttype == "disabled":
            return
        if ttype == "enabled":
            payload["reasoning_effort"] = (
                "low" if (budget is not None and int(budget) <= 4096) else "high"
            )
        # ``adaptive``: leave the model's default alone.
        return

    # OpenAI proper and the generic path share ``reasoning_effort``. Only for
    # models that take a request-side knob — a plain chat model 400s.
    if not _supports_request_reasoning(model, model_capability):
        return
    if ttype == "disabled":
        # ``none`` is a GPT-5 value; GPT-6 Astra and the o-series reject it,
        # so leave the field out and let those models use their default effort.
        if not bare.startswith(("gpt-6", "o1", "o3", "o4")):
            payload["reasoning_effort"] = "none"
        return
    # ``budget_tokens`` is deliberately dropped: Chat Completions has no
    # per-request thinking budget. "enabled" is an explicit ask for deeper
    # reasoning; "adaptive" lets the model self-pace — map to a middle
    # setting, since there's no reasoning_effort value for "adaptive".
    payload["reasoning_effort"] = "high" if ttype == "enabled" else "medium"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out



def _build_responses_payload(
    *,
    model: str,
    messages: Iterable[Message],
    system: str | None,
    tools: list[dict[str, Any]],
    max_tokens: int,
    extra: dict[str, Any] | None,
    thinking: dict[str, Any] | None,
    model_capability: ModelCapability | None,
) -> dict[str, Any]:
    """Translate universal history into the OpenAI Responses input format."""
    msg_list = list(messages)
    pulled_system, body = _split_system(msg_list)
    instructions = "\n\n".join(p for p in (system, pulled_system) if p)
    items: list[dict[str, Any]] = []
    for message in body:
        if isinstance(message, UserMessage):
            if isinstance(message.content, str):
                items.append({"role": "user", "content": message.content})
                continue
            text: list[str] = []
            content: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    items.append({
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": _tool_result_content_to_string(block.content),
                    })
                elif isinstance(block, TextBlock):
                    text.append(block.text)
                elif hasattr(block, "source"):
                    image = _image_block_to_openai_part(block)["image_url"]["url"]
                    content.append({"type": "input_image", "image_url": image})
            if text:
                content.insert(0, {"type": "input_text", "text": "\n".join(text)})
            if content:
                items.append({"role": "user", "content": content})
        elif isinstance(message, AssistantMessage):
            text = "\n".join(b.text for b in message.content if isinstance(b, TextBlock))
            if text:
                items.append({"role": "assistant", "content": text})
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    items.append({
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input, separators=(",", ":")),
                    })

    response_tools: list[dict[str, Any]] = []
    for tool in _normalize_tool_defs(tools):
        function = tool["function"]
        response_tools.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {}),
        })
    payload: dict[str, Any] = {
        "model": model,
        "input": items,
        "tools": response_tools,
        "max_output_tokens": max_tokens,
    }
    if instructions:
        payload["instructions"] = instructions
    raw_effort = (extra or {}).get("reasoning_effort") or (extra or {}).get("effort")
    explicit = _normalize_effort(raw_effort, "openai", model.lower().rsplit("/", 1)[-1]) if raw_effort else None
    if explicit:
        payload["reasoning"] = {"effort": explicit}
    elif thinking and thinking.get("type") != "disabled":
        payload["reasoning"] = {"effort": "medium"}
    elif model_capability and model_capability.reasoning_mode == "always_on":
        payload["reasoning"] = {"effort": "low"}
    return payload


def _translate_responses(data: dict[str, Any], requested_model: str) -> Iterable[StreamEvent]:
    """Translate one completed Responses object to normalized stream events."""
    yield MessageStart(
        message_id=data.get("id") or f"resp-{uuid4().hex[:12]}",
        model=data.get("model") or requested_model,
    )
    index = 0
    has_tool = False
    for item in data.get("output") or []:
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if part.get("type") not in {"output_text", "text"}:
                    continue
                text = part.get("text") or ""
                yield ContentBlockStart(index=index, block=TextBlock(text=""))
                if text:
                    yield ContentBlockDelta(index=index, delta=TextDelta(text=text))
                yield ContentBlockStop(index=index)
                index += 1
        elif item_type == "function_call":
            has_tool = True
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid4().hex[:12]}"
            name = item.get("name") or "unknown_tool"
            arguments = item.get("arguments") or "{}"
            yield ContentBlockStart(
                index=index, block=ToolUseBlock(id=call_id, name=name, input={})
            )
            yield ContentBlockDelta(index=index, delta=InputJsonDelta(partial_json=arguments))
            yield ContentBlockStop(index=index)
            index += 1
    usage_data = data.get("usage") or {}
    usage = Usage(
        input_tokens=int(usage_data.get("input_tokens") or 0),
        output_tokens=int(usage_data.get("output_tokens") or 0),
    )
    yield MessageDelta(stop_reason="tool_use" if has_tool else "end_turn", usage=usage)
    yield MessageStop()

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
    *,
    emits_thinking: bool = False,
    thinking_tags: tuple[tuple[str, str], ...] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Walk the OpenAI SSE stream and emit normalized events for the native
    tool-call path. Events are emitted as soon as they materialize so the
    streaming tool executor (plan §5) can dispatch tools mid-stream.

    When the model is known to emit inline ``<think>…</think>`` (Qwen3,
    DeepSeek-R1 on a server that doesn't split it out, …) the text deltas run
    through ``ThinkingParser`` so consumers get thinking events *as they
    stream* instead of raw tags mid-answer. The engine's post-hoc split stays
    as a net; text that reaches it here is already tag-free, so it is a no-op.
    """

    started = False
    # Text / thinking block bookkeeping shared with the helpers below.
    cursor: dict[str, Any] = {
        "next_index": 0,
        "text_open": False,
        "text_index": 0,
        "thinking_open": False,
        "thinking_index": 0,
        "tool_indices": {},
    }
    think_parser = (
        ThinkingParser(tags=thinking_tags) if (emits_thinking and ThinkingParser and thinking_tags)
        else ThinkingParser() if (emits_thinking and ThinkingParser)
        else None
    )
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

    def _text_segments(piece: str) -> list[tuple[str, str]]:
        if think_parser is None:
            return [("text", piece)]
        return _tag_segments(think_parser.feed(piece))  # type: ignore[attr-defined]

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

        # Reasoning arrives out-of-band as ``reasoning_content`` (DeepSeek,
        # Fireworks, xAI Grok) or ``reasoning`` (OpenAI-style gateways), or as
        # a Gemini content part flagged ``extra_content.google.thought``.
        # Surface all of them as a ThinkingBlock — same channel as Anthropic's
        # thinking_delta so the agent UI doesn't care.
        reasoning, content_piece = _split_reasoning(delta)
        if reasoning:
            for ev in _open_thinking_and_emit(cursor, reasoning):
                yield ev

        # Plain text content — split inline <think> tags out when the model
        # is known to emit them.
        if content_piece:
            for kind, chunk in _text_segments(content_piece):
                if not chunk:
                    continue
                if kind == "thinking":
                    for ev in _open_thinking_and_emit(cursor, chunk):
                        yield ev
                else:
                    for ev in _open_text_and_emit(cursor, chunk):
                        yield ev

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
                for ev in _close_text_and_thinking(cursor):
                    yield ev
                st["sdk_index"] = cursor["next_index"]
                cursor["next_index"] += 1
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

    # End of stream — flush the tag parser's tail, then close anything open.
    if think_parser is not None:
        for kind, chunk in _tag_segments(think_parser.finalize()):  # type: ignore[attr-defined]
            if not chunk:
                continue
            emit = _open_thinking_and_emit if kind == "thinking" else _open_text_and_emit
            for ev in emit(cursor, chunk):
                yield ev
    for ev in _close_text_and_thinking(cursor):
        yield ev
    for st in tool_state.values():
        if not st.get("opened"):
            # A call that streamed id/args but never a name would otherwise be
            # silently dropped — a silent agentic dead-end. Surface it with a
            # placeholder name so the executor returns a recoverable is_error
            # ("unknown tool") the model can react to and re-issue, rather than
            # the turn ending with no tool_use at all.
            if not (st["args_pending"] or st["id"]):
                continue
            st["sdk_index"] = cursor["next_index"]
            cursor["next_index"] += 1
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
        reasoning, content_piece = _split_reasoning(delta)
        if reasoning:
            for ev in _open_thinking_and_emit(cursor, reasoning):
                yield ev

        if content_piece:
            # First peel off inline <think>...</think> if the model emits it,
            # then feed the rest through the tool-call parser.
            if think_parser is not None:
                segments = _tag_segments(think_parser.feed(content_piece))  # type: ignore[attr-defined]
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
        for kind, chunk in _tag_segments(think_parser.finalize()):  # type: ignore[attr-defined]
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


def _tag_segments(events: Iterable[Any]) -> list[tuple[str, str]]:
    """Normalize ``ThinkingParser`` output (``ThinkingChunk`` / ``TextChunk``
    structs) to ``("thinking"|"text", text)`` pairs."""

    out: list[tuple[str, str]] = []
    for ev in events:
        if isinstance(ev, tuple) and len(ev) == 2:
            out.append((str(ev[0]), str(ev[1])))
            continue
        kind = "thinking" if type(ev).__name__ == "ThinkingChunk" else "text"
        out.append((kind, getattr(ev, "text", "") or ""))
    return out


def _open_text_and_emit(cursor: dict[str, Any], chunk: str) -> Iterable[StreamEvent]:
    """Open a text block (closing any open thinking block first) and emit one
    delta. Mutates ``cursor`` in place."""

    if cursor["thinking_open"]:
        yield ContentBlockStop(index=cursor["thinking_index"])
        cursor["thinking_open"] = False
    if not cursor["text_open"]:
        idx = cursor["next_index"]
        cursor["next_index"] = idx + 1
        cursor["text_index"] = idx
        cursor["text_open"] = True
        yield ContentBlockStart(index=idx, block=TextBlock(text=""))
    yield ContentBlockDelta(index=cursor["text_index"], delta=TextDelta(text=chunk))


def _close_text_and_thinking(cursor: dict[str, Any]) -> Iterable[StreamEvent]:
    """Close whichever of the text / thinking blocks is open."""

    if cursor["text_open"]:
        yield ContentBlockStop(index=cursor["text_index"])
        cursor["text_open"] = False
    if cursor["thinking_open"]:
        yield ContentBlockStop(index=cursor["thinking_index"])
        cursor["thinking_open"] = False


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


def _reasoning_to_text(value: Any) -> str:
    """Flatten the shapes vendors use for streamed reasoning to plain text.

    ``reasoning_content`` / ``reasoning`` are strings almost everywhere, but a
    few gateways wrap them (``{"text": …}``) or ship a list of parts; a
    non-string must never reach ``ThinkingDelta`` (msgspec would reject it)
    nor be dropped on the floor.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("text") or value.get("content") or value.get("summary")
        return _reasoning_to_text(inner) if inner is not None else ""
    if isinstance(value, list):
        return "".join(_reasoning_to_text(v) for v in value)
    return ""


def _split_reasoning(delta: dict[str, Any]) -> tuple[str, Any]:
    """``(reasoning_text, content)`` for one streamed delta.

    Gemini's OpenAI-compat endpoint (with ``include_thoughts``) streams thought
    summaries as ordinary ``content`` deltas carrying
    ``extra_content.google.thought: true`` — those move to the reasoning side
    so they render as thinking rather than as the answer.
    """

    reasoning = _reasoning_to_text(
        delta.get("reasoning_content") or delta.get("reasoning") or ""
    )
    if not reasoning:
        # OpenRouter-style ``reasoning_details: [{"type": "reasoning.text",
        # "text": …}]``.
        details = delta.get("reasoning_details")
        if isinstance(details, list):
            reasoning = "".join(
                _reasoning_to_text(d.get("text") or d.get("summary") or "")
                for d in details if isinstance(d, dict)
            )
    content = delta.get("content")
    if isinstance(content, str) and content and _is_thought_part(delta):
        reasoning = reasoning + content
        content = None
    return reasoning, content


def _is_thought_part(delta: dict[str, Any]) -> bool:
    extra = delta.get("extra_content")
    if not isinstance(extra, dict):
        return False
    google = extra.get("google")
    return isinstance(google, dict) and bool(google.get("thought"))


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
    if bare.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        return True
    return bool(cap and cap.supports_reasoning_effort)


def _err_message(err: Any) -> str:
    if isinstance(err, dict):
        return err.get("message") or err.get("type") or "provider error"
    return err if isinstance(err, str) else "provider error"
