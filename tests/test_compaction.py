"""Auto-compaction wiring tests (ticket T0.1).

Covers both the compaction *primitive* (``SimpleCompactor``) and its *wiring*
into the agent run loop. Everything runs fully OFFLINE via ``MockProvider`` with
scripted SSE-style event lists — no network, no real model.

The summarized span is replaced by a plain ``UserMessage`` (so it serializes
through providers/``query()``/session save-load like any other message), NOT a
bespoke boundary type — the tests assert on that marker message.
"""

from __future__ import annotations

from typing import Any

import pytest

from mantis_agent import (
    Agent,
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    tool,
)
from mantis_agent.capabilities import ModelCapability
from mantis_agent.compact import SimpleCompactor
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
from mantis_agent.providers.mock import MockProvider

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_SUMMARY_MARKER = "[Earlier conversation compacted"


# ---------------------------------------------------------------------------
# Scripted stream-event builders
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
        ContentBlockStart(index=idx, block=ToolUseBlock(id=call_id, name=name, input={})),
        ContentBlockDelta(index=idx, delta=InputJsonDelta(partial_json=input_json)),
        ContentBlockStop(index=idx),
    ]


def _stop(stop_reason: str = "end_turn", usage: Usage | None = None) -> list:
    return [
        MessageDelta(stop_reason=stop_reason, usage=usage or Usage(input_tokens=10, output_tokens=20)),
        MessageStop(),
    ]


# ---------------------------------------------------------------------------
# History builders — each exchange is a clean 4-message tool round.
# ---------------------------------------------------------------------------

_PAD_CHARS = 400  # ~100 estimated tokens per padded message (len // 4)


def _exchange(i: int, n_chars: int = _PAD_CHARS) -> list:
    pad = "x" * n_chars
    return [
        UserMessage(content=f"Q{i}: {pad}"),
        AssistantMessage(
            content=[ToolUseBlock(id=f"call-{i}", name="lookup", input={"q": i})],
            stop_reason="tool_use",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id=f"call-{i}", content=f"R{i}: {pad}")]),
        AssistantMessage(content=[TextBlock(text=f"A{i}: {pad}")], stop_reason="end_turn"),
    ]


def _long_history(n_exchanges: int, n_chars: int = _PAD_CHARS) -> list:
    msgs: list = [SystemMessage(content="You are a helpful agent.")]
    for i in range(n_exchanges):
        msgs.extend(_exchange(i, n_chars))
    return msgs


@tool
async def lookup(q: int) -> str:
    """Look something up by id."""
    return f"value-for-{q}"


# ---------------------------------------------------------------------------
# Tool-pairing / boundary inspection helpers
# ---------------------------------------------------------------------------


def _tool_use_ids(messages: list) -> set[str]:
    ids: set[str] = set()
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list):
            ids.update(b.id for b in content if isinstance(b, ToolUseBlock))
    return ids


def _tool_result_ids(messages: list) -> set[str]:
    ids: set[str] = set()
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list):
            ids.update(b.tool_use_id for b in content if isinstance(b, ToolResultBlock))
    return ids


def _ends_mid_tool_round(messages: list) -> bool:
    if not messages:
        return False
    last = messages[-1]
    content = getattr(last, "content", None)
    if isinstance(last, AssistantMessage) and isinstance(content, list):
        return any(isinstance(b, ToolUseBlock) for b in content)
    return False


def _assert_no_orphans(messages: list) -> None:
    uses, results = _tool_use_ids(messages), _tool_result_ids(messages)
    assert uses == results, (
        f"orphaned tool calls: use-without-result={uses - results}, "
        f"result-without-use={results - uses}"
    )


def _summary_messages(messages: list) -> list:
    return [
        m for m in messages
        if isinstance(m, UserMessage)
        and isinstance(m.content, str)
        and m.content.startswith(_SUMMARY_MARKER)
    ]


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


async def _fake_summarizer(prompt: str) -> str:
    return "Summary of prior conversation: earlier turns set up the task."


def _make_simple_compactor(**kw: Any) -> SimpleCompactor:
    kw.setdefault("threshold", 0.85)
    kw.setdefault("keep_recent_turns", 8)
    return SimpleCompactor(_fake_summarizer, **kw)


class RecordingCompactor:
    """Wraps a Compactor, snapshotting every should_compact call and counting
    compact calls. The ceiling turns a runaway loop into a loud failure."""

    _MAX_COMPACTS = 5

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.should_calls: list[list] = []
        self.compact_calls = 0

    async def should_compact(self, messages, usage, ctx_window) -> bool:
        self.should_calls.append(list(messages))
        return await self._inner.should_compact(messages, usage, ctx_window)

    async def compact(self, messages):
        self.compact_calls += 1
        if self.compact_calls > self._MAX_COMPACTS:
            raise RuntimeError(
                f"compaction did not converge — compact() called {self.compact_calls}x"
            )
        return await self._inner.compact(messages)


def _cap(ctx_window: int) -> ModelCapability:
    return ModelCapability(name="mock-7b", family="chatml", context_window=ctx_window)


def _build_agent(*, provider, compactor, ctx_window: int, **kw) -> Agent:
    return Agent(
        model="mock-7b",
        provider=provider,
        tools=kw.pop("tools", [lookup]),
        compactor=compactor,
        model_capability=_cap(ctx_window),
        include_memory=False,
        max_turns=kw.pop("max_turns", 4),
        **kw,
    )


# ===========================================================================
# 1. SimpleCompactor.should_compact — unit
# ===========================================================================


async def test_should_compact_threshold_on_estimated_tokens():
    compactor = _make_simple_compactor()

    short = [SystemMessage(content="hi"), UserMessage(content="hello there")]
    assert await compactor.should_compact(short, Usage(), ctx_window=2048) is False

    long = _long_history(10)  # ~3100 estimated tokens >> 0.85 * 2048
    assert await compactor.should_compact(long, Usage(), ctx_window=2048) is True

    # Relative to the window: a huge window holds the same history fine.
    assert await compactor.should_compact(long, Usage(), ctx_window=200_000) is False


async def test_should_compact_honors_reported_usage():
    compactor = _make_simple_compactor()
    short = [UserMessage(content="tiny")]
    big = Usage(input_tokens=1_900, output_tokens=200)  # 2100 > 0.85 * 2048
    assert await compactor.should_compact(short, big, ctx_window=2048) is True


# ===========================================================================
# 2. Wired into the run loop — compacts, shrinks, marker present, no orphans
# ===========================================================================


async def test_run_loop_compacts_without_orphaning_tool_use():
    history = _long_history(10)
    original_len = len(history)
    assert _tool_use_ids(history) == _tool_result_ids(history)

    provider = MockProvider(scripted_events=_msg() + _text_block(0, "Done.") + _stop())
    compactor = _make_simple_compactor()
    agent = _build_agent(provider=provider, compactor=compactor, ctx_window=2048)

    try:
        msgs = await agent.run(history)
    finally:
        await agent.aclose()

    assert len(msgs) < original_len            # shrunk
    assert len(_summary_messages(msgs)) == 1   # the compaction marker survived
    assert isinstance(msgs[0], SystemMessage)  # head preserved outside the boundary
    _assert_no_orphans(msgs)                   # no dangling tool_use / tool_result

    # Converged: the compacted history no longer wants compaction.
    assert await compactor.should_compact(msgs, Usage(), ctx_window=2048) is False


# ===========================================================================
# 3. Safe-boundary guard — never compact mid-tool-round
# ===========================================================================


async def test_compaction_never_fires_mid_tool_round():
    events_turn1 = _msg() + _tool_use_block(0, "c1", "lookup", '{"q": 1}') + _stop(stop_reason="tool_use")
    events_turn2 = _msg() + _text_block(0, "All set.") + _stop()

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = events_turn1 if self._turn == 0 else events_turn2
            self._turn += 1
            for ev in script:
                yield ev

    recording = RecordingCompactor(_make_simple_compactor())
    agent = _build_agent(provider=TwoTurnMock(), compactor=recording, ctx_window=200_000, max_turns=5)

    try:
        await agent.run([UserMessage(content="please look up 1")])
    finally:
        await agent.aclose()

    assert recording.should_calls, "should_compact was never consulted"
    for snapshot in recording.should_calls:
        assert not _ends_mid_tool_round(snapshot), "consulted mid-tool-round (orphan risk)"
    assert recording.compact_calls == 0  # under threshold the whole run


# ===========================================================================
# 4. Convergence — no infinite-compaction loop
# ===========================================================================


async def test_compaction_converges_no_infinite_loop():
    history = _long_history(10)
    provider = MockProvider(scripted_events=_msg() + _text_block(0, "Done.") + _stop())
    inner = _make_simple_compactor()
    recording = RecordingCompactor(inner)
    agent = _build_agent(provider=provider, compactor=recording, ctx_window=2048)

    try:
        msgs = await agent.run(history)
    finally:
        await agent.aclose()

    assert 1 <= recording.compact_calls <= 2
    assert await inner.should_compact(msgs, Usage(), ctx_window=2048) is False
    _assert_no_orphans(msgs)
