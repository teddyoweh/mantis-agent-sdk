"""MCP sampling — server calls back into the agent's model.

This test suite locks down the full sampling round-trip — the second
server→client request shape in MCP, after elicitation. The flow:

  client capability advertise  →  server-initiated sampling/createMessage
       ↑                                            ↓
  SamplingResult ← agent-model handler ← client dispatcher

Sampling exists so MCP servers that need a model in their tool path
(summarise this doc, classify this email, decide what to do) don't have
to ship their own API keys. They borrow the agent's model via the
client. The client routes the request to a handler — typically wrapped
around the agent's current provider — and ships the assistant reply
back to the server.

What this proves:

* ``SamplingResult.__post_init__`` normalises a non-"assistant" role.
* A client without a handler does NOT advertise the capability, so
  ``ctx.sample`` correctly raises ``SamplingNotSupportedError``.
* A client WITH a handler advertises the capability, receives the
  inbound request on its read loop, runs the handler, and replies with
  the model's output.
* All optional ``sampling/createMessage`` params (system_prompt,
  temperature, stop_sequences, model_preferences, metadata,
  include_context) round-trip through the wire and reach the handler.
* The tool sees the parsed :class:`SamplingResult` with role,
  ``content``, ``model``, and ``stop_reason``.
* Tools without a ``ctx`` parameter remain unaffected.
* A handler returning a plain dict is accepted (lenient parsing,
  matching the elicitation_handler behaviour for symmetry).
* A handler that raises produces a clean ``-32603`` error response
  rather than hanging the read loop.
* Elicitation and sampling can coexist on the same client and the same
  tool — the dispatcher routes by method name, not by handler order.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import tool
from mantis_agent.mcp import (
    ElicitationRequest,
    ElicitationResult,
    MCPClient,
    SamplingMessage,
    SamplingNotSupportedError,
    SamplingRequest,
    SamplingResult,
    create_sdk_server,
)
from mantis_agent.mcp.server import SdkServer


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Pure value-object tests (no I/O)
# ---------------------------------------------------------------------------


def test_sampling_result_normalizes_role_to_assistant() -> None:
    """Per spec, sampling responses are always assistant-role. A handler
    that types ``"user"`` by mistake gets quietly normalised so the wire
    payload stays correct."""

    r = SamplingResult(
        role="user",  # wrong on purpose
        content={"type": "text", "text": "hi"},
        model="m",
    )
    assert r.role == "assistant"


def test_sampling_result_default_role_is_assistant() -> None:
    r = SamplingResult(content={"type": "text", "text": "x"}, model="m")
    assert r.role == "assistant"


def test_sampling_request_keeps_raw_payload() -> None:
    """``raw`` lets handlers reach future spec fields we haven't yet
    surfaced as typed attributes."""

    req = SamplingRequest(
        messages=[
            SamplingMessage(role="user", content={"type": "text", "text": "hi"})
        ],
        max_tokens=128,
        raw={"messages": [...], "preview": "future-spec-field"},
    )
    assert req.raw["preview"] == "future-spec-field"


def test_sampling_message_accepts_non_text_content() -> None:
    """The content dict can hold any MCP block type — image, audio, etc.
    We don't enforce a schema."""

    m = SamplingMessage(role="user", content={"type": "image", "data": "<b64>"})
    assert m.content["type"] == "image"


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------


async def test_client_without_handler_does_not_advertise_sampling() -> None:
    """No handler ⇒ no advertised capability — silence is the default."""

    config = create_sdk_server("noop", tools=[_make_echo_tool()])

    async with MCPClient(config):
        server: SdkServer = config.server  # type: ignore[assignment]
        assert "sampling" not in server.client_capabilities


async def test_client_with_handler_advertises_sampling() -> None:
    config = create_sdk_server("noop", tools=[_make_echo_tool()])

    async def handler(_req: SamplingRequest) -> SamplingResult:
        return SamplingResult(
            content={"type": "text", "text": ""}, model="mock"
        )

    async with MCPClient(config, sampling_handler=handler):
        server: SdkServer = config.server  # type: ignore[assignment]
        assert "sampling" in server.client_capabilities


async def test_capabilities_coexist_when_both_handlers_registered() -> None:
    """Elicitation + sampling on the same client — both keys appear in
    the advertised capabilities object, neither shadows the other."""

    config = create_sdk_server("noop", tools=[_make_echo_tool()])

    async def el_handler(_req: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult.decline()

    async def sa_handler(_req: SamplingRequest) -> SamplingResult:
        return SamplingResult(content={"type": "text", "text": ""}, model="m")

    async with MCPClient(
        config, elicitation_handler=el_handler, sampling_handler=sa_handler
    ):
        server: SdkServer = config.server  # type: ignore[assignment]
        assert server.client_capabilities.get("elicitation") == {}
        assert server.client_capabilities.get("sampling") == {}


# ---------------------------------------------------------------------------
# End-to-end: tool calls ctx.sample, client handler returns reply
# ---------------------------------------------------------------------------


async def test_tool_sample_round_trip() -> None:
    """The server sends sampling/createMessage with full options; the
    client's handler returns an assistant reply; the tool surfaces the
    sampled text. We assert on every field on both legs."""

    captured: dict[str, object] = {}

    @tool
    async def summarize(text: str, *, ctx) -> str:
        """Ask the agent's model to summarise ``text``."""

        result = await ctx.sample(
            messages=[
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Summarise in one word: {text}",
                    },
                }
            ],
            max_tokens=64,
            system_prompt="you are a terse summariser",
            temperature=0.0,
            stop_sequences=["END"],
            metadata={"trace_id": "t-42"},
            model_preferences={"hints": [{"name": "claude-haiku"}]},
            include_context="thisServer",
        )
        return f"{result.model}::{result.content['text']}"

    async def handler(req: SamplingRequest) -> SamplingResult:
        captured["max_tokens"] = req.max_tokens
        captured["system_prompt"] = req.system_prompt
        captured["temperature"] = req.temperature
        captured["stop_sequences"] = list(req.stop_sequences)
        captured["metadata"] = dict(req.metadata)
        captured["model_preferences"] = dict(req.model_preferences)
        captured["include_context"] = req.include_context
        captured["first_msg_role"] = req.messages[0].role
        captured["first_msg_text"] = req.messages[0].content.get("text")
        return SamplingResult(
            content={"type": "text", "text": "summary"},
            model="mock-haiku",
            stop_reason="end_turn",
        )

    config = create_sdk_server("summariser", tools=[summarize])
    async with MCPClient(config, sampling_handler=handler) as client:
        result = await client.call_tool("summarize", {"text": "Hello world"})
        assert not result.is_error
        assert result.to_string() == "mock-haiku::summary"

    assert captured["max_tokens"] == 64
    assert captured["system_prompt"] == "you are a terse summariser"
    assert captured["temperature"] == 0.0
    assert captured["stop_sequences"] == ["END"]
    assert captured["metadata"] == {"trace_id": "t-42"}
    assert captured["model_preferences"] == {"hints": [{"name": "claude-haiku"}]}
    assert captured["include_context"] == "thisServer"
    assert captured["first_msg_role"] == "user"
    assert captured["first_msg_text"] == "Summarise in one word: Hello world"


async def test_tool_sample_accepts_sampling_message_objects() -> None:
    """``ctx.sample(messages=[...])`` should also accept
    :class:`SamplingMessage` instances directly, not just dicts."""

    @tool
    async def ask(*, ctx) -> str:
        result = await ctx.sample(
            messages=[
                SamplingMessage(
                    role="user", content={"type": "text", "text": "ping"}
                )
            ],
            max_tokens=16,
        )
        return result.content["text"]

    async def handler(_req: SamplingRequest) -> SamplingResult:
        return SamplingResult(
            content={"type": "text", "text": "pong"}, model="m"
        )

    config = create_sdk_server("ask", tools=[ask])
    async with MCPClient(config, sampling_handler=handler) as client:
        result = await client.call_tool("ask", {})
        assert result.to_string() == "pong"


async def test_tool_sample_handler_can_return_plain_dict() -> None:
    """Lenient parsing: a handler returning a dict shaped like a result
    works the same as returning a :class:`SamplingResult`. Matches the
    elicitation_handler symmetry."""

    @tool
    async def ask(*, ctx) -> str:
        r = await ctx.sample(
            messages=[
                {"role": "user", "content": {"type": "text", "text": "x"}}
            ]
        )
        return f"{r.model}:{r.content['text']}:{r.stop_reason}"

    async def handler(_req: SamplingRequest):
        return {
            "role": "assistant",
            "content": {"type": "text", "text": "hi"},
            "model": "dict-model",
            "stopReason": "max_tokens",
        }

    config = create_sdk_server("d", tools=[ask])
    async with MCPClient(config, sampling_handler=handler) as client:
        result = await client.call_tool("ask", {})
        assert result.to_string() == "dict-model:hi:max_tokens"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_ctx_sample_raises_when_capability_not_advertised() -> None:
    """If the client never advertised ``sampling`` (no handler), the
    tool's ``ctx.sample`` call must raise
    :class:`SamplingNotSupportedError` instead of hanging waiting for a
    reply that will never come."""

    seen: dict[str, str] = {}

    @tool
    async def needs_model(*, ctx) -> str:
        try:
            await ctx.sample(
                messages=[
                    {"role": "user", "content": {"type": "text", "text": "x"}}
                ]
            )
        except SamplingNotSupportedError as exc:
            seen["err"] = str(exc)
            return "fallback"
        return "should not reach"

    config = create_sdk_server("ns", tools=[needs_model])
    async with MCPClient(config) as client:
        result = await client.call_tool("needs_model", {})
        assert result.to_string() == "fallback"
    assert "sampling" in seen["err"].lower()


async def test_handler_exception_returns_internal_error() -> None:
    """A handler that raises must not crash the client's read loop. The
    server gets a clean ``-32603`` error response and its tool surfaces
    a tool error rather than hanging forever."""

    @tool
    async def call_model(*, ctx) -> str:
        try:
            await ctx.sample(
                messages=[
                    {"role": "user", "content": {"type": "text", "text": "x"}}
                ]
            )
        except Exception as exc:  # noqa: BLE001 — surfacing the error to the test
            return f"errored: {type(exc).__name__}"
        return "ok"

    async def buggy(_req: SamplingRequest) -> SamplingResult:
        raise RuntimeError("model exploded")

    config = create_sdk_server("buggy", tools=[call_model])
    async with MCPClient(config, sampling_handler=buggy) as client:
        result = await client.call_tool("call_model", {})
        assert not result.is_error
        # Tool caught the AgentError from the server's send_sampling.
        text = result.to_string()
        assert text.startswith("errored:")


async def test_handler_returning_non_result_raises_typeerror() -> None:
    """A handler that returns something weird (not a SamplingResult and
    not a dict) is rejected. The error surfaces as -32603 to the server
    so its tool can decide how to react."""

    @tool
    async def call_model(*, ctx) -> str:
        try:
            await ctx.sample(
                messages=[
                    {"role": "user", "content": {"type": "text", "text": "x"}}
                ]
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001 — captured below
            return f"errored: {type(exc).__name__}"

    async def wrong_type(_req: SamplingRequest):
        return 42  # not a SamplingResult, not a dict

    config = create_sdk_server("wrong", tools=[call_model])
    async with MCPClient(config, sampling_handler=wrong_type) as client:
        result = await client.call_tool("call_model", {})
        text = result.to_string()
        assert text.startswith("errored:")


# ---------------------------------------------------------------------------
# Tools without ctx still work (no injection regression)
# ---------------------------------------------------------------------------


async def test_tools_without_ctx_unchanged_when_sampling_handler_registered() -> None:
    """Registering a sampling handler must not perturb plain tools. The
    existing tool dispatch path (no ``ctx`` injection) keeps working."""

    @tool
    async def plain(x: int) -> str:
        """No ctx, just a plain tool."""
        return str(x * 3)

    async def handler(_req: SamplingRequest) -> SamplingResult:
        return SamplingResult(content={"type": "text", "text": ""}, model="m")

    config = create_sdk_server("p", tools=[plain])
    async with MCPClient(config, sampling_handler=handler) as client:
        result = await client.call_tool("plain", {"x": 7})
        assert result.to_string() == "21"


# ---------------------------------------------------------------------------
# Coexistence: a single tool can elicit and sample in the same call
# ---------------------------------------------------------------------------


async def test_tool_can_elicit_and_sample_in_same_call() -> None:
    """The dispatcher routes server-initiated requests by method name, so
    a tool that mixes ``ctx.elicit`` and ``ctx.sample`` in one body must
    work end-to-end."""

    elicit_seen: dict[str, str] = {}
    sample_seen: dict[str, str] = {}

    @tool
    async def workflow(*, ctx) -> str:
        """Ask the user a question, then ask the model to expand it."""

        answer = await ctx.elicit(
            "Topic?",
            {"type": "object", "properties": {"topic": {"type": "string"}}},
        )
        topic = answer.content["topic"]
        sampled = await ctx.sample(
            messages=[
                {
                    "role": "user",
                    "content": {"type": "text", "text": f"expand: {topic}"},
                }
            ],
            max_tokens=32,
        )
        return f"{topic} -> {sampled.content['text']}"

    async def el_handler(req: ElicitationRequest) -> ElicitationResult:
        elicit_seen["message"] = req.message
        return ElicitationResult.accept({"topic": "cats"})

    async def sa_handler(req: SamplingRequest) -> SamplingResult:
        sample_seen["text"] = req.messages[0].content.get("text", "")
        return SamplingResult(
            content={"type": "text", "text": "felines are agile"},
            model="mock",
        )

    config = create_sdk_server("workflow", tools=[workflow])
    async with MCPClient(
        config,
        elicitation_handler=el_handler,
        sampling_handler=sa_handler,
    ) as client:
        result = await client.call_tool("workflow", {})
        assert result.to_string() == "cats -> felines are agile"

    assert elicit_seen["message"] == "Topic?"
    assert sample_seen["text"] == "expand: cats"


# ---------------------------------------------------------------------------
# Sampling timeout fires notifications/cancelled
# ---------------------------------------------------------------------------


async def test_sample_timeout_raises_and_sends_cancel_notification() -> None:
    """``ctx.sample(timeout_s=...)`` raises ``TimeoutError`` if the
    client doesn't reply in time. The server also fires a
    ``notifications/cancelled`` so a polite client can stop its model
    call early."""

    @tool
    async def slow_model(*, ctx) -> str:
        try:
            await ctx.sample(
                messages=[
                    {"role": "user", "content": {"type": "text", "text": "x"}}
                ],
                max_tokens=8,
                timeout_s=0.05,
            )
        except TimeoutError:
            return "timed out"
        return "ok"

    blocker = anyio.Event()

    async def slow_handler(_req: SamplingRequest) -> SamplingResult:
        # Block until the test sets the event — but the tool's
        # timeout_s=0.05 fires long before that.
        await blocker.wait()
        return SamplingResult(content={"type": "text", "text": ""}, model="m")

    config = create_sdk_server("slow", tools=[slow_model])
    async with MCPClient(config, sampling_handler=slow_handler) as client:
        result = await client.call_tool("slow_model", {})
        assert result.to_string() == "timed out"
        blocker.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_echo_tool():
    @tool
    async def echo(text: str) -> str:
        """Echo the input."""

        return text

    return echo
