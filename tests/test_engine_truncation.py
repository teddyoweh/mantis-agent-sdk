"""A stream cut off mid-turn must not throw away work that already happened.

Tool calls dispatch eagerly the moment their block closes, so by the time a
connection drops (or the body ends with a block never closed) an edit / bash
may already have run. The engine keeps the closed blocks as a partial
``stop_reason="truncated"`` turn, answers the dispatched calls with their real
results, and keeps going:

* closed tool_use then connection reset → tool ran once, result in history,
  next provider call happens, run completes.
* closed tool_use then body ends without closing the next block → same.
* truncated before ANY block closed → the turn is re-streamed (transient).
* text-only truncation → one continuation nudge, not a silent mid-sentence stop.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest

from mantis_agent import Agent, tool
from mantis_agent.agent import _assert_message_invariants
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
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

RAN: list[int] = []


@tool
async def bump(n: int) -> str:
    """Record that the tool ran."""
    RAN.append(n)
    return f"bumped {n}"


def _done_turn(i: int):
    yield MessageStart(message_id=f"m{i}", model="mock")
    yield ContentBlockStart(index=0, block=TextBlock(text=""))
    yield ContentBlockDelta(index=0, delta=TextDelta(text="done"))
    yield ContentBlockStop(index=0)
    yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
    yield MessageStop()


class _Scripted(MockProvider):
    name = "mock"

    def __init__(self, first_turn_mode: str) -> None:
        super().__init__()
        self.n = 0
        self.mode = first_turn_mode

    async def stream(self, **kw: Any):
        self.n += 1
        if self.n > 1:
            for ev in _done_turn(self.n):
                yield ev
            return
        yield MessageStart(message_id="m1", model="mock")
        if self.mode in ("tool_then_reset", "tool_then_eof"):
            yield ContentBlockStart(index=0, block=ToolUseBlock(id="c1", name="bump", input={}))
            yield ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json='{"n": 1}'))
            yield ContentBlockStop(index=0)
            # A second call starts but never closes — must NOT run.
            yield ContentBlockStart(index=1, block=ToolUseBlock(id="c2", name="bump", input={}))
            yield ContentBlockDelta(index=1, delta=InputJsonDelta(partial_json='{"n": 2'))
            if self.mode == "tool_then_reset":
                raise httpx.ReadError("connection reset by peer")
            return  # body ends mid-block, no MessageStop
        if self.mode == "nothing_closed":
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="half an ans"))
            raise httpx.ReadError("connection reset by peer")
        if self.mode == "text_only":
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="Part one."))
            yield ContentBlockStop(index=0)
            yield ContentBlockStart(index=1, block=TextBlock(text=""))
            yield ContentBlockDelta(index=1, delta=TextDelta(text="Part tw"))
            raise httpx.RemoteProtocolError("peer closed connection")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: Any) -> None:
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("anyio.sleep", no_sleep)
    RAN.clear()


def _run(prov: MockProvider) -> list:
    agent = Agent(model="mock", provider=prov, tools=[bump], max_retries=2, max_steps=6,
                  auto_compact=False, include_recall=False, include_env=False,
                  include_memory=False)
    msgs: list = [UserMessage(content="go")]
    anyio.run(agent.run, msgs)
    _assert_message_invariants(msgs)
    return msgs


@pytest.mark.parametrize("mode", ["tool_then_reset", "tool_then_eof"])
def test_closed_tool_call_survives_truncation(mode: str) -> None:
    prov = _Scripted(mode)
    msgs = _run(prov)
    assert RAN == [1]                    # closed call ran exactly once; unclosed never
    assert prov.n == 2                   # loop continued to a second provider call
    partial = msgs[1]
    assert isinstance(partial, AssistantMessage)
    assert partial.stop_reason == "truncated"
    assert [b.id for b in partial.content if isinstance(b, ToolUseBlock)] == ["c1"]
    results = msgs[2]
    assert isinstance(results, UserMessage)
    assert [(r.tool_use_id, r.content) for r in results.content
            if isinstance(r, ToolResultBlock)] == [("c1", "bumped 1")]
    final = msgs[-1]
    assert isinstance(final, AssistantMessage) and final.stop_reason == "end_turn"


def test_truncation_before_any_block_closed_retries_the_turn() -> None:
    prov = _Scripted("nothing_closed")
    msgs = _run(prov)
    assert prov.n == 2
    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    assert len(assistants) == 1          # the half-answer was discarded, not kept
    assert assistants[0].content[0].text == "done"


def test_text_only_truncation_continues_once() -> None:
    prov = _Scripted("text_only")
    msgs = _run(prov)
    assert prov.n == 2
    partial = msgs[1]
    assert isinstance(partial, AssistantMessage) and partial.stop_reason == "truncated"
    # The unclosed-but-seen text is kept so the continuation doesn't repeat it.
    assert "".join(b.text for b in partial.content) == "Part one.Part tw"
    nudge = msgs[2]
    assert isinstance(nudge, UserMessage) and nudge.isMeta
    assert "cut off" in nudge.content
    assert msgs[-1].stop_reason == "end_turn"


class _AlwaysTruncatesText(MockProvider):
    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    async def stream(self, **kw: Any):
        self.n += 1
        yield MessageStart(message_id=f"m{self.n}", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text=f"chunk {self.n}"))
        yield ContentBlockStop(index=0)
        raise httpx.ReadError("connection reset by peer")


def test_text_truncation_continuation_is_bounded() -> None:
    prov = _AlwaysTruncatesText()
    _run(prov)
    assert prov.n == 2                   # one continuation, then a normal stop


# --- reviewer regressions ---------------------------------------------------


class _EmptyTextThenCutTool(MockProvider):
    """OpenAI-compat shape: an empty text block opened+closed before the tool
    call, then the connection drops mid tool-call JSON."""

    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    async def stream(self, **kw: Any):
        self.n += 1
        if self.n > 1:
            for ev in _done_turn(self.n):
                yield ev
            return
        yield MessageStart(message_id="m1", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockStop(index=0)
        yield ContentBlockStart(index=1, block=ToolUseBlock(id="c1", name="bump", input={}))
        yield ContentBlockDelta(index=1, delta=InputJsonDelta(partial_json='{"n": 1'))
        raise httpx.ReadError("connection reset by peer")


def test_closed_empty_text_is_not_content_so_the_turn_is_restreamed() -> None:
    prov = _EmptyTextThenCutTool()
    msgs = _run(prov)
    assert prov.n == 2 and RAN == []
    for m in msgs:
        if isinstance(m, AssistantMessage):
            assert m.stop_reason != "truncated"
            assert all(not (isinstance(b, TextBlock) and not b.text) for b in m.content)


def test_restream_notifies_the_retry_hook(monkeypatch: Any) -> None:
    from mantis_agent import retry as retry_mod

    seen: list[dict] = []
    monkeypatch.setattr(retry_mod, "notify", seen.append)
    _run(_Scripted("nothing_closed"))
    assert [s["reason"] for s in seen] == ["stream truncated"]


def test_no_restream_after_cancel() -> None:
    class _CancelThenCut(_Scripted):
        agent: Any = None

        async def stream(self, **kw: Any):
            self.n += 1
            yield MessageStart(message_id="m1", model="mock")
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="half"))
            self.agent.cancel()
            raise httpx.ReadError("connection reset by peer")

    prov = _CancelThenCut("nothing_closed")
    agent = Agent(model="mock", provider=prov, tools=[bump], max_retries=2, max_steps=6,
                  auto_compact=False, include_recall=False, include_env=False,
                  include_memory=False)
    prov.agent = agent
    with pytest.raises(httpx.ReadError):
        anyio.run(agent.run, [UserMessage(content="go")])
    assert prov.n == 1                   # the model was not asked again


def test_text_truncation_on_last_step_is_a_natural_stop_not_a_step_cutoff() -> None:
    prov = _AlwaysTruncatesText()
    agent = Agent(model="mock", provider=prov, tools=[bump], max_retries=2, max_steps=1,
                  auto_compact=False, include_recall=False, include_env=False,
                  include_memory=False)
    msgs: list = [UserMessage(content="go")]
    anyio.run(agent.run, msgs)
    assert prov.n == 1
    assert agent._stop_cause == "natural"
    assert not any(isinstance(m, UserMessage) and m.isMeta for m in msgs)
