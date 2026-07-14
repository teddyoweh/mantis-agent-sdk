"""Streaming-mode rewrite of the TS-SDK-shape ``query()``.

The dict-options branch in ``mantis_agent.query.query`` used to buffer
every Message via ``await agent.run(...)`` and *then* translate them to
SDK shapes. That defeated the whole point of mid-stream tool dispatch —
consumers couldn't see the first ``SDKAssistantMessage`` until the
entire multi-turn run completed.

The "Streaming tool dispatch rewrite" (1.0 prerequisite in README.md)
replaces that buffered loop with ``async for msg in agent.run_iter(...)``
so SDK consumers get each message the instant the agent produces it —
same contract the compat path has shipped since the streaming-mode
work landed.

What this test file proves:
  1. ``SDKAssistantMessage`` lands BEFORE the in-flight tool body
     returns (mid-stream dispatch is visible end-to-end through the
     TS-SDK shape).
  2. The tool-result ``SDKUserMessage`` lands BEFORE the next turn's
     ``SDKAssistantMessage`` (per-batch streaming, not whole-run
     buffering).
  3. Ordering remains:
        SystemMessage(init) → SDKUserMessage(seed)
        → SDKAssistantMessage(turn-1) → SDKUserMessage(tool result)
        → SDKAssistantMessage(turn-2)
        → SDKResultMessage
  4. ``SDKResultMessage.num_turns`` / ``usage`` / ``modelUsage`` are
     accurately accumulated across the streamed turns (no regression
     vs. the buffered baseline).
  5. ``BudgetExceededError`` raised mid-stream still translates to the
     right ``error_subtype`` on the final result message.
"""

from __future__ import annotations

import anyio
import pytest

mock_module = pytest.importorskip("mantis_agent.providers.mock")

from mantis_agent import (  # noqa: E402
    TextBlock,
    ToolUseBlock,
    Usage,
    tool,
)
from mantis_agent.events import (  # noqa: E402
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers.mock import MockProvider  # noqa: E402
from mantis_agent.query import (  # noqa: E402
    SDKAssistantMessage,
    SDKResultMessage,
    SDKUserMessage,
    query,
)


# ---------------------------------------------------------------------------
# Event builders — match the ones in test_streaming_mode for consistency.
# ---------------------------------------------------------------------------


def _msg(model: str = "mock-model") -> list:
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
# Test tooling: a tool that blocks until released, and a 2-turn provider.
# ---------------------------------------------------------------------------


class _GatedTool:
    """Tool that waits on an external ``anyio.Event`` before returning.

    Lets the test poke at "did the consumer see the first assistant
    message BEFORE the gate opened?". If the dict-options query is
    buffered, the answer is no. If it streams, the answer is yes.
    """

    def __init__(self) -> None:
        self.gate = anyio.Event()
        self.entered = anyio.Event()

    def as_tool(self) -> object:
        outer = self

        @tool
        async def gated(value: int) -> str:
            """Wait until the gate opens."""

            outer.entered.set()
            await outer.gate.wait()
            return str(value)

        return gated


class _TwoTurnMock(MockProvider):
    """Mock that returns ``events_turn1`` on first stream call,
    ``events_turn2`` on every subsequent call. Same pattern as
    ``tests/test_streaming_mode.py::_TwoTurnMock`` but local here so this
    file is self-contained."""

    def __init__(self, events_turn1: list, events_turn2: list) -> None:
        super().__init__()
        self._events1 = events_turn1
        self._events2 = events_turn2
        self._turn = 0

    async def stream(self, **kw):  # type: ignore[override]
        script = self._events1 if self._turn == 0 else self._events2
        self._turn += 1
        for ev in script:
            yield ev
            await anyio.sleep(0)


@pytest.fixture
def anyio_backend() -> str:
    """pytest-anyio fixture — default to asyncio."""

    return "asyncio"


# ---------------------------------------------------------------------------
# 1. Streaming proof — first assistant lands BEFORE the tool body returns.
# ---------------------------------------------------------------------------


def test_query_dict_options_yields_first_assistant_before_tool_completes():
    """If the dict-options ``query()`` truly streams, the
    ``SDKAssistantMessage`` for turn 1 must reach the consumer BEFORE
    the gated tool releases. The pre-rewrite implementation buffered
    via ``await agent.run(...)``, so the first assistant was only
    visible AFTER both turns + every tool body had completed."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "gated", '{"value": 99}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "done") + _stop()

    async def main():
        gated = _GatedTool()
        provider = _TwoTurnMock(events_turn1, events_turn2)

        seen: list = []
        first_assistant_seen = anyio.Event()

        async def consumer():
            async for msg in query(
                prompt="go",
                options={
                    "model": "mock-model",
                    "tools": [gated.as_tool()],
                    "provider": provider,
                    "max_turns": 5,
                    "include_memory": False,
                },
            ):
                seen.append(msg)
                if isinstance(msg, SDKAssistantMessage) and not first_assistant_seen.is_set():
                    first_assistant_seen.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(consumer)
            # Wait until the gated tool body has entered (proving the
            # executor dispatched it — which only happens on
            # ContentBlockStop mid-stream).
            with anyio.fail_after(3.0):
                await gated.entered.wait()
            # At THIS point the consumer task may still be in the
            # ready queue, having not yet been picked by the asyncio
            # scheduler — entered.set() just made main runnable. Give
            # the consumer a bounded window to drain pending yields.
            # The buffered (pre-rewrite) implementation never gets
            # here at all because run() can't return until tools
            # finish — gate is held closed → tool never returns →
            # run() never returns → consumer never sees ANY message.
            # If we got here, run_iter is streaming; the only question
            # is scheduler fairness.
            with anyio.fail_after(2.0):
                while not first_assistant_seen.is_set():
                    await anyio.sleep(0)
            assert first_assistant_seen.is_set(), (
                "SDKAssistantMessage was NOT yielded while the tool "
                "body was held — dict-options query() is still buffering"
            )
            gated.gate.set()

        # Sanity-check the full sequence after both turns drain.
        assistants = [m for m in seen if isinstance(m, SDKAssistantMessage)]
        assert len(assistants) == 2
        results = [m for m in seen if isinstance(m, SDKResultMessage)]
        assert len(results) == 1
        # The first assistant carries the tool_use block; the second
        # carries the final text.
        first_blocks = list(assistants[0].message.content)
        assert any(isinstance(b, ToolUseBlock) for b in first_blocks)
        second_blocks = list(assistants[1].message.content)
        assert any(isinstance(b, TextBlock) for b in second_blocks)

    anyio.run(main)


# ---------------------------------------------------------------------------
# 2. Per-batch streaming — tool-result UserMessage lands BEFORE next turn.
# ---------------------------------------------------------------------------


def test_query_dict_options_tool_result_user_message_lands_before_next_assistant():
    """The agent loop appends a tool-result-bearing UserMessage after
    each batch. In streaming mode, that UserMessage must land BEFORE
    the next turn's AssistantMessage is yielded."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "double", '{"x": 21}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "answer is 42") + _stop()

    @tool
    async def double(x: int) -> str:
        """Double a number."""

        return str(x * 2)

    async def main() -> list:
        provider = _TwoTurnMock(events_turn1, events_turn2)
        seen: list = []
        async for msg in query(
            prompt="double 21",
            options={
                "model": "mock-model",
                "tools": [double],
                "provider": provider,
                "max_turns": 5,
                "include_memory": False,
            },
        ):
            seen.append(msg)
        return seen

    seen = anyio.run(main)

    types = [type(m).__name__ for m in seen]
    # The synthetic UserMessage carrying the ToolResultBlock has
    # isSynthetic=True; the seed user message has isSynthetic=False.
    # We need to find the synthetic one and prove it sits BETWEEN the
    # two assistant messages.
    assistant_indices = [
        i for i, m in enumerate(seen) if isinstance(m, SDKAssistantMessage)
    ]
    synthetic_user_indices = [
        i
        for i, m in enumerate(seen)
        if isinstance(m, SDKUserMessage) and m.isSynthetic
    ]
    assert len(assistant_indices) == 2, types
    assert len(synthetic_user_indices) >= 1, types
    # The synthetic tool-result UserMessage lands AFTER the first
    # assistant and BEFORE the second.
    assert assistant_indices[0] < synthetic_user_indices[0] < assistant_indices[1], (
        f"tool-result UserMessage did not land between turns: types={types}"
    )


# ---------------------------------------------------------------------------
# 3. Full ordering preserved.
# ---------------------------------------------------------------------------


def test_query_dict_options_ordering_full_sequence():
    """system(init) → user(seed) → assistant(tool_use) →
    user(tool_result) → assistant(text) → result. No reorderings."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "double", '{"x": 3}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "six") + _stop()

    @tool
    async def double(x: int) -> str:
        """Double a number."""

        return str(x * 2)

    async def main() -> list:
        provider = _TwoTurnMock(events_turn1, events_turn2)
        out: list = []
        async for msg in query(
            prompt="double",
            options={
                "model": "mock-model",
                "tools": [double],
                "provider": provider,
                "max_turns": 5,
                "include_memory": False,
            },
        ):
            out.append(msg)
        return out

    seen = anyio.run(main)
    types = [type(m).__name__ for m in seen]

    assert types[0] == "SDKSystemMessage", types
    assert types[1] == "SDKUserMessage", types  # seed
    # First assistant (with tool_use) comes before the synthetic
    # tool-result user message, which comes before the second
    # assistant (with text).
    a1 = types.index("SDKAssistantMessage")
    a2 = types.index("SDKAssistantMessage", a1 + 1)
    # Locate the synthetic UserMessage between a1 and a2.
    syn_between = [
        i
        for i, m in enumerate(seen)
        if isinstance(m, SDKUserMessage) and m.isSynthetic and a1 < i < a2
    ]
    assert syn_between, f"no synthetic user message between turns: {types}"
    assert types[-1] == "SDKResultMessage", types


# ---------------------------------------------------------------------------
# 4. SDKResultMessage accuracy across streamed turns.
# ---------------------------------------------------------------------------


def test_query_dict_options_result_message_accumulates_usage_across_turns():
    """num_turns, aggregated usage, and modelUsage must all reflect
    every turn the streaming iteration produced — not just the last
    one. Regression guard against accidentally clobbering ``agg_usage``
    in the per-message loop."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "double", '{"x": 7}')
        + _stop(
            stop_reason="tool_use",
            usage=Usage(input_tokens=5, output_tokens=11),
        )
    )
    events_turn2 = (
        _msg()
        + _text_block(0, "fourteen")
        + _stop(
            stop_reason="end_turn",
            usage=Usage(input_tokens=4, output_tokens=3),
        )
    )

    @tool
    async def double(x: int) -> str:
        """Double a number."""

        return str(x * 2)

    async def main() -> list:
        provider = _TwoTurnMock(events_turn1, events_turn2)
        out: list = []
        async for msg in query(
            prompt="double 7",
            options={
                "model": "mock-model",
                "tools": [double],
                "provider": provider,
                "max_turns": 5,
                "include_memory": False,
            },
        ):
            out.append(msg)
        return out

    seen = anyio.run(main)
    result = next(m for m in reversed(seen) if isinstance(m, SDKResultMessage))

    # Two turns streamed; both counted.
    assert result.num_turns == 2
    # Tokens are summed across both turns.
    assert result.usage.input_tokens == 5 + 4
    assert result.usage.output_tokens == 11 + 3
    # modelUsage carries the same aggregated counts.
    assert "mock-model" in result.modelUsage
    mu = result.modelUsage["mock-model"]
    assert mu.inputTokens == 5 + 4
    assert mu.outputTokens == 11 + 3
    assert result.is_error is False
    assert result.subtype == "success"
    assert result.stop_reason == "end_turn"
    assert "fourteen" in result.result


# ---------------------------------------------------------------------------
# 5. Mid-stream BudgetExceededError still maps to the right subtype.
# ---------------------------------------------------------------------------


def test_query_dict_options_budget_exceeded_mid_stream_maps_to_error_subtype():
    """When ``BudgetExceededError`` raises *inside* the streaming
    iteration (e.g. between turn 1 and turn 2), the final result message
    must surface ``error_max_budget_usd`` (or ``error_max_turns``) — not
    a generic ``error_during_execution``. This regression-guards the
    exception-translation block that lives after the ``async for``."""

    # A high-cost first turn that immediately blows the $0.0001 cap.
    # Pricing for unknown models is 0, so we set a max_turns=1 ceiling
    # instead — the second turn never starts because the loop hits
    # the turn cap, raising ``error_max_turns``.
    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "double", '{"x": 1}')
        + _stop(
            stop_reason="tool_use",
            usage=Usage(input_tokens=1, output_tokens=1),
        )
    )
    events_turn2 = _msg() + _text_block(0, "ok") + _stop()

    @tool
    async def double(x: int) -> str:
        """Double a number."""

        return str(x * 2)

    async def main() -> list:
        provider = _TwoTurnMock(events_turn1, events_turn2)
        out: list = []
        async for msg in query(
            prompt="double",
            options={
                "model": "mock-model",
                "tools": [double],
                "provider": provider,
                "max_turns": 1,  # turn cap → BudgetExceededError after turn 1
                "max_usd": 100.0,
                "include_memory": False,
            },
        ):
            out.append(msg)
        return out

    seen = anyio.run(main)
    result = next(m for m in reversed(seen) if isinstance(m, SDKResultMessage))
    assert result.is_error is True
    assert result.subtype == "error_max_turns", result.subtype
    assert any("BudgetExceededError" in e for e in result.errors)


# ---------------------------------------------------------------------------
# 6. Seeds aren't re-yielded by the streaming loop.
# ---------------------------------------------------------------------------


def test_query_dict_options_seeds_emitted_once_not_twice():
    """``run_iter`` mutates the list passed in and re-yields seed
    UserMessages; the streaming translator must skip them so the
    consumer doesn't see a duplicate of the seed user message
    immediately before the first assistant turn."""

    events = _msg() + _text_block(0, "hi") + _stop()
    provider = MockProvider(scripted_events=events)

    async def main() -> list:
        out: list = []
        async for msg in query(
            prompt="hello",
            options={
                "model": "mock-model",
                "provider": provider,
                "max_turns": 1,
                "include_memory": False,
            },
        ):
            out.append(msg)
        return out

    seen = anyio.run(main)

    # The non-synthetic UserMessage (the seed) must appear exactly once.
    seed_users = [
        m
        for m in seen
        if isinstance(m, SDKUserMessage) and not m.isSynthetic
    ]
    assert len(seed_users) == 1, [type(m).__name__ for m in seen]


# ---------------------------------------------------------------------------
# 7. SDKUserMessage for the synthetic meta context (if any) is marked
#    isSynthetic=True so consumers can filter it from a typed-message view.
# ---------------------------------------------------------------------------


def test_query_dict_options_meta_user_context_marked_synthetic():
    """If memory injection produces a ``UserMessage`` with
    ``isMeta=True``, it should arrive on the SDK side with
    ``isSynthetic=True`` (not as a fake "the user said this" message).
    With ``include_memory=False`` no meta injection happens, but the
    tool-result branch follows the same code path — so we assert no
    SDKUserMessage with ``isSynthetic=False`` shows up AFTER the first
    assistant turn (only the seed can be non-synthetic)."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "noop", '{}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "done") + _stop()

    @tool
    async def noop() -> str:
        """No-op."""

        return "OK"

    async def main() -> list:
        provider = _TwoTurnMock(events_turn1, events_turn2)
        out: list = []
        async for msg in query(
            prompt="hi",
            options={
                "model": "mock-model",
                "tools": [noop],
                "provider": provider,
                "max_turns": 5,
                "include_memory": False,
            },
        ):
            out.append(msg)
        return out

    seen = anyio.run(main)

    # Find the first assistant; every UserMessage AFTER it must have
    # isSynthetic=True.
    a1 = next(
        i for i, m in enumerate(seen) if isinstance(m, SDKAssistantMessage)
    )
    for m in seen[a1 + 1 :]:
        if isinstance(m, SDKUserMessage):
            assert m.isSynthetic is True, (
                "non-synthetic UserMessage appeared after the first "
                "assistant turn — tool-result branch isn't flagging "
                "isSynthetic correctly"
            )
