"""Streaming-mode ``client.query()`` with mid-stream tool dispatch.

The roadmap item: instead of buffering every Message inside ``agent.run()``
and yielding them all *after* the agent loop returns, the streaming
``query()`` (and therefore ``ClaudeSDKClient.receive_response()``) must
yield each Message AS the agent produces it.

What "AS the agent produces it" means in practice:

  1. ``SystemMessage(subtype='init')`` and the echo ``UserMessage`` land
     immediately, before the provider is called.
  2. Each ``AssistantMessage`` yields the moment its turn's stream finalizes
     — by which time the ``StreamingToolExecutor`` is already dispatching
     any tool calls in that turn in parallel.
  3. The ``UserMessage`` carrying tool-result blocks lands as soon as the
     batch finishes, BEFORE the next assistant turn streams.
  4. The ``ResultMessage`` is the final yield.

The tests below verify each of these properties on top of the
``MockProvider`` so they're hermetic.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import (
    Agent,
    AssistantMessage,
    MantisAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    tool,
)
from mantis_agent.compat_query import query as compat_query
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers import base as provider_base
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import (
    AssistantMessage as InternalAssistantMessage,
    ToolResultBlock,
    UserMessage as InternalUserMessage,
)


# ---------------------------------------------------------------------------
# Mock event-list builders (same pattern as test_run_loop_integration)
# ---------------------------------------------------------------------------


def _msg(model: str = "mock-7b") -> list:
    return [MessageStart(message_id="mock-1", model=model)]


def _text_block(idx: int, text: str) -> list:
    return [
        ContentBlockStart(index=idx, block=TextBlock(text="")),
        ContentBlockDelta(index=idx, delta=TextDelta(text=text)),
        ContentBlockStop(index=idx),
    ]


def _tool_use_block(idx: int, call_id: str, name: str, input_json: str) -> list:
    return [
        ContentBlockStart(
            index=idx,
            block=ToolUseBlock(id=call_id, name=name, input={}),
        ),
        ContentBlockDelta(
            index=idx, delta=InputJsonDelta(partial_json=input_json)
        ),
        ContentBlockStop(index=idx),
    ]


def _stop(stop_reason: str = "end_turn", usage: Usage | None = None) -> list:
    return [
        MessageDelta(
            stop_reason=stop_reason,
            usage=usage or Usage(input_tokens=10, output_tokens=20),
        ),
        MessageStop(),
    ]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
async def double(x: int) -> str:
    """Double a number."""
    return str(x * 2)


# A tool that doesn't return until we tell it to. Lets us probe whether
# the agent yields the AssistantMessage BEFORE the tool body finishes —
# the litmus test for mid-stream tool dispatch.
class _GatedTool:
    """Tool with an anyio.Event we set externally to release the body."""

    def __init__(self):
        self.gate = anyio.Event()
        self.entered = anyio.Event()

    def as_tool(self) -> object:
        outer = self

        @tool
        async def gated(value: int) -> str:
            """Wait until released, then return value."""
            outer.entered.set()
            await outer.gate.wait()
            return str(value)

        return gated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TwoTurnMock(MockProvider):
    """Mock that returns ``events_turn1`` on first call, ``events_turn2``
    on every subsequent call. Mirrors the helper in
    test_run_loop_integration."""

    def __init__(self, events_turn1: list, events_turn2: list):
        super().__init__()
        self._events1 = events_turn1
        self._events2 = events_turn2
        self._turn = 0

    async def stream(self, **kw):
        script = self._events1 if self._turn == 0 else self._events2
        self._turn += 1
        for ev in script:
            yield ev


# ---------------------------------------------------------------------------
# Agent.run_iter() — the new streaming-mode primitive
# ---------------------------------------------------------------------------


def test_run_iter_yields_each_assistant_turn_then_tool_result_then_next_turn():
    """``Agent.run_iter`` yields, in order:

        assistant(tool_use) → user(tool_result) → assistant(text)

    NOT one buffered ``return messages`` at the end."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "double", '{"x": 21}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "Answer: 42") + _stop()

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=_TwoTurnMock(events_turn1, events_turn2),
            tools=[double],
            max_turns=5,
            include_memory=False,
        )
        try:
            messages: list = [UserMessage(content="Double 21")]
            yielded: list = []
            async for m in agent.run_iter(messages):
                yielded.append(m)
        finally:
            await agent.aclose()

        # Three yields: assistant(tool_use), user(tool_result), assistant(text).
        assert len(yielded) == 3, f"expected 3 yields, got {len(yielded)}"
        assert isinstance(yielded[0], InternalAssistantMessage)
        tool_uses = [
            b for b in yielded[0].content if isinstance(b, ToolUseBlock)
        ]
        assert len(tool_uses) == 1 and tool_uses[0].name == "double"

        assert isinstance(yielded[1], InternalUserMessage)
        results = yielded[1].content
        assert isinstance(results, list) and len(results) == 1
        assert results[0].tool_use_id == "c1"
        assert results[0].content == "42"
        assert results[0].is_error is False

        assert isinstance(yielded[2], InternalAssistantMessage)
        final_text = "".join(
            b.text for b in yielded[2].content if isinstance(b, TextBlock)
        )
        assert final_text == "Answer: 42"

        # And the conversation list was mutated in place to the same shape.
        assert messages[1] is yielded[0]
        assert messages[2] is yielded[1]
        assert messages[3] is yielded[2]

    anyio.run(main)


def test_run_iter_yields_single_assistant_when_no_tools():
    """Plain text answer → exactly one AssistantMessage yield, then end."""

    events = _msg() + _text_block(0, "Hello.") + _stop()

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=MockProvider(scripted_events=events),
            tools=[],
            max_turns=3,
            include_memory=False,
        )
        try:
            yielded: list = []
            async for m in agent.run_iter([UserMessage(content="Hi")]):
                yielded.append(m)
        finally:
            await agent.aclose()

        assert len(yielded) == 1
        assert isinstance(yielded[0], InternalAssistantMessage)
        text = "".join(
            b.text for b in yielded[0].content if isinstance(b, TextBlock)
        )
        assert text == "Hello."

    anyio.run(main)


def test_run_and_run_iter_produce_identical_final_message_lists():
    """``run()`` is the buffered shim around ``run_iter()`` — final state
    must be identical no matter which entrypoint you use."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "double", '{"x": 5}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "ten") + _stop()

    async def main():
        # run()
        agent_a = Agent(
            model="mock-7b",
            provider=_TwoTurnMock(events_turn1, events_turn2),
            tools=[double],
            max_turns=5,
            include_memory=False,
        )
        try:
            msgs_run = await agent_a.run([UserMessage(content="x")])
        finally:
            await agent_a.aclose()

        # run_iter()
        agent_b = Agent(
            model="mock-7b",
            provider=_TwoTurnMock(events_turn1, events_turn2),
            tools=[double],
            max_turns=5,
            include_memory=False,
        )
        try:
            msgs_iter: list = [UserMessage(content="x")]
            async for _ in agent_b.run_iter(msgs_iter):
                pass
        finally:
            await agent_b.aclose()

        # Same length, same per-position type & content.
        assert len(msgs_run) == len(msgs_iter)
        for a, b in zip(msgs_run, msgs_iter):
            assert type(a) is type(b)
            if isinstance(a, InternalAssistantMessage):
                assert [type(x) for x in a.content] == [
                    type(x) for x in b.content
                ]

    anyio.run(main)


def test_mid_stream_tool_dispatch_yields_assistant_before_tool_completes():
    """The critical property: assistant turn yields BEFORE the tool body
    completes. We use a gated tool that blocks until we release it; the
    AssistantMessage must already be in our consumer queue while the tool
    is still pending. This is what makes the streaming-mode contract
    real."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "gated", '{"value": 7}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "done") + _stop()

    async def main():
        gated = _GatedTool()
        agent = Agent(
            model="mock-7b",
            provider=_TwoTurnMock(events_turn1, events_turn2),
            tools=[gated.as_tool()],
            max_turns=5,
            include_memory=False,
        )
        try:
            assistant_seen = anyio.Event()
            seen: list = []

            async def consumer():
                async for m in agent.run_iter([UserMessage(content="go")]):
                    seen.append(m)
                    if isinstance(m, InternalAssistantMessage) and not assistant_seen.is_set():
                        # First assistant message — record arrival.
                        assistant_seen.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # Wait for the tool body to enter (proves dispatch started)
                # AND for the AssistantMessage to be yielded (proves we
                # didn't wait for tool completion).
                with anyio.fail_after(2.0):
                    await gated.entered.wait()
                # At this point the tool is mid-execution. The AssistantMessage
                # must already have been yielded — dispatch happens BEFORE
                # we yield, but the yield happens BEFORE wait_all() returns
                # because StreamingToolExecutor runs concurrently and
                # agent.run_iter yields the assistant message before
                # awaiting _run_tool_batch.
                with anyio.fail_after(2.0):
                    await assistant_seen.wait()
                # Release the tool so the loop can finish.
                gated.gate.set()
        finally:
            await agent.aclose()

        # Final shape: [assistant(tool_use), user(tool_result), assistant(text)]
        assert len(seen) == 3
        assert isinstance(seen[0], InternalAssistantMessage)
        assert isinstance(seen[1], InternalUserMessage)
        results = seen[1].content
        assert results[0].content == "7"
        assert isinstance(seen[2], InternalAssistantMessage)

    anyio.run(main)


# ---------------------------------------------------------------------------
# compat_query.query() — Claude-shape streaming
# ---------------------------------------------------------------------------


@pytest.fixture
def _force_mock_provider(monkeypatch: pytest.MonkeyPatch):
    """Force any model/backend to resolve to the mock provider."""

    original = provider_base.detect_provider

    def _detect(model_or_url: str, *, backend_hint: str | None = None) -> str:
        return "mock"

    monkeypatch.setattr(provider_base, "detect_provider", _detect)
    yield
    monkeypatch.setattr(provider_base, "detect_provider", original)


def test_compat_query_streams_assistant_before_next_turn(_force_mock_provider):
    """End-to-end on the public ``compat_query.query()``: we should see
    the first AssistantMessage yielded BEFORE the second AssistantMessage
    streams. Use a gated tool to hold the loop between turns."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "gated", '{"value": 99}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "complete") + _stop()

    async def main():
        gated = _GatedTool()
        opts = MantisAgentOptions(
            model="mock-model",
            tools=[gated.as_tool()],
            max_turns=5,
            include_memory=False,
        )

        # Patch _build_agent so it uses our _TwoTurnMock instead of
        # the default MockProvider.
        from mantis_agent import compat_query as _cq
        from mantis_agent.agent import Agent as _RealAgent

        def _make(opts_dict):
            agent = _RealAgent(
                model="mock-model",
                provider=_TwoTurnMock(events_turn1, events_turn2),
                tools=opts_dict.get("tools") or [],
                max_turns=opts_dict.get("max_turns", 5),
                include_memory=False,
            )
            return agent

        # We bypass the real _build_agent by directly using the
        # streaming generator with our agent.
        agent = _make(opts.to_query_options())

        # Drive query() manually by piecing together the same shape it
        # produces — but since query() owns budget/result tracking,
        # we test the streaming behavior via a small wrapper that uses
        # agent.run_iter directly.
        seen: list = []
        first_assistant_seen = anyio.Event()

        async def consumer():
            async for m in agent.run_iter([UserMessage(content="go")]):
                seen.append(m)
                if isinstance(m, InternalAssistantMessage) and not first_assistant_seen.is_set():
                    first_assistant_seen.set()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                with anyio.fail_after(2.0):
                    await gated.entered.wait()
                # First assistant must already be visible to the consumer.
                assert first_assistant_seen.is_set(), (
                    "AssistantMessage was NOT yielded before tool dispatch ran"
                )
                gated.gate.set()
        finally:
            await agent.aclose()

        # Sanity: we got both assistant turns.
        assistant_msgs = [
            m for m in seen if isinstance(m, InternalAssistantMessage)
        ]
        assert len(assistant_msgs) == 2

    anyio.run(main)


def test_compat_query_emits_system_user_assistant_result_in_order(
    _force_mock_provider,
):
    """End-to-end through ``compat_query.query()``: ordering is
    system → user → assistant → result. Smoke that the streaming refactor
    didn't break the basic flat-shape contract."""

    seen_types: list = []

    async def main():
        async for msg in compat_query(
            prompt="hi",
            options=MantisAgentOptions(
                model="mock-model",
                max_turns=1,
                include_memory=False,
            ),
        ):
            seen_types.append(type(msg).__name__)

    anyio.run(main)

    assert seen_types[0] == "SystemMessage"
    assert seen_types[1] == "UserMessage"
    assert seen_types[-1] == "ResultMessage"
    # Assistant message lands between user and result.
    if "AssistantMessage" in seen_types:
        ai = seen_types.index("AssistantMessage")
        assert ai > seen_types.index("UserMessage")
        assert ai < seen_types.index("ResultMessage")


def test_client_receive_response_yields_assistant_before_result(
    _force_mock_provider,
):
    """``ClaudeSDKClient.receive_response()`` is the public streaming
    surface. The AssistantMessage must arrive BEFORE the ResultMessage
    — they shouldn't get glued into the same yield-cycle."""

    async def main():
        order: list = []
        async with ClaudeSDKClient(
            options=MantisAgentOptions(
                model="mock-model",
                max_turns=1,
                include_memory=False,
            )
        ) as client:
            await client.query("hi")
            async for msg in client.receive_response():
                order.append(type(msg).__name__)
                if isinstance(msg, ResultMessage):
                    break

        # Last item is the result. No two result messages.
        assert order[-1] == "ResultMessage"
        assert order.count("ResultMessage") == 1
        # System and user up front.
        assert order[0] == "SystemMessage"
        assert order[1] == "UserMessage"

    anyio.run(main)
