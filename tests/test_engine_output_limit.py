"""A turn that hits ``max_tokens`` with no tool call has run out of room, not
finished. A reasoning model (GLM 5.3 on Fireworks) can spend a whole 4096-token
cap planning before its first tool call; the run used to end right there as a
"success" with nothing built. Now:

* the run continues with a nudge saying the turn was cut off;
* the cap rises for the rest of the run — only when the caller left it at
  the default (an explicit ``max_tokens`` is theirs);
* it is bounded, so a model that always runs long still ends;
* a provider that refuses the raised cap is retried at the limit it states.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent import Agent, tool
from mantis_agent.agent import _assert_message_invariants, _max_tokens_limit_from
from mantis_agent.capabilities import ModelCapability
from mantis_agent.errors import ProviderError
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
    ThinkingDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

GLM = ModelCapability(name="glm-test", family="glm", supports_native_tools=True,
                      context_window=128_000, max_output_tokens=32_768)


@tool
async def noop() -> str:
    """Do nothing."""
    return "ok"


def _cut_off_thinking(i: int):
    yield MessageStart(message_id=f"m{i}", model="mock")
    yield ContentBlockStart(index=0, block=ThinkingBlock(thinking=""))
    yield ContentBlockDelta(index=0, delta=ThinkingDelta(thinking="Plan: sheet one, sheet two, "))
    yield ContentBlockStop(index=0)
    yield MessageDelta(stop_reason="max_tokens", usage=Usage(input_tokens=1, output_tokens=4096))
    yield MessageStop()


def _answer(i: int):
    yield MessageStart(message_id=f"m{i}", model="mock")
    yield ContentBlockStart(index=0, block=TextBlock(text=""))
    yield ContentBlockDelta(index=0, delta=TextDelta(text="built it"))
    yield ContentBlockStop(index=0)
    yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=2))
    yield MessageStop()


class _Scripted(MockProvider):
    name = "mock"

    def __init__(self, cut_offs: int, *, refuse_above: int | None = None) -> None:
        super().__init__()
        self.cut_offs = cut_offs
        self.refuse_above = refuse_above
        self.caps: list[int] = []
        self.n = 0

    async def stream(self, **kw: Any):
        self.caps.append(kw["max_tokens"])
        if self.refuse_above is not None and kw["max_tokens"] > self.refuse_above:
            raise ProviderError(f"`max_tokens` must be less than or equal to `{self.refuse_above}`")
        self.n += 1
        turn = _cut_off_thinking if self.n <= self.cut_offs else _answer
        for ev in turn(self.n):
            yield ev


def _run(prov: MockProvider, **kw: Any) -> tuple[Agent, list]:
    agent = Agent(model="glm-output-limit-test", provider=prov, tools=[noop], max_steps=10, model_capability=GLM,
                  auto_compact=False, include_recall=False, include_env=False, include_memory=False,
                  **kw)
    msgs: list = [UserMessage(content="build the workbook")]
    anyio.run(agent.run, msgs)
    _assert_message_invariants(msgs)
    return agent, msgs


def _nudges(msgs: list) -> list[str]:
    return [str(m.content) for m in msgs
            if isinstance(m, UserMessage) and getattr(m, "isMeta", False) and "output limit" in str(m.content)]


def test_a_cut_off_turn_continues_with_more_room() -> None:
    prov = _Scripted(cut_offs=1)
    agent, msgs = _run(prov)
    assert prov.n == 2                                   # the run went on
    assert prov.caps[0] == 8192 and prov.caps[1] == 32_000   # raised to a quarter of the window
    assert agent.max_tokens == 32_000
    (nudge,) = _nudges(msgs)
    assert "still reasoning" in nudge and "32000" in nudge
    final = msgs[-1]
    assert isinstance(final, AssistantMessage) and final.content[0].text == "built it"


def test_an_explicit_cap_is_kept_but_the_run_still_continues() -> None:
    prov = _Scripted(cut_offs=1)
    agent, msgs = _run(prov, max_tokens=2000)
    assert prov.n == 2 and prov.caps == [2000, 2000]
    assert agent.max_tokens == 2000
    (nudge,) = _nudges(msgs)
    assert "You now have" not in nudge


def test_recovery_is_bounded() -> None:
    prov = _Scripted(cut_offs=99)
    _agent, msgs = _run(prov)
    assert prov.n == 4                                   # the first turn + three recoveries
    assert len(_nudges(msgs)) == 3


def test_a_refused_cap_is_retried_at_the_stated_limit() -> None:
    prov = _Scripted(cut_offs=1, refuse_above=16_384)
    agent, msgs = _run(prov)
    assert prov.caps == [8192, 32_000, 16_384]           # raised, refused, clamped
    assert agent.max_tokens == 16_384
    assert msgs[-1].content[0].text == "built it"


class _CutToolCall(MockProvider):
    """First turn: a write whose arguments stop mid-string at the cap."""
    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.caps: list[int] = []

    async def stream(self, **kw: Any):
        self.caps.append(kw["max_tokens"])
        if len(self.caps) > 1:
            for ev in _answer(len(self.caps)):
                yield ev
            return
        yield MessageStart(message_id="m1", model="mock")
        yield ContentBlockStart(index=0, block=ToolUseBlock(id="w1", name="noop", input={}))
        yield ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json='{"content": "import os\\nfor'))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="max_tokens", usage=Usage(input_tokens=1, output_tokens=8192))
        yield MessageStop()


def test_a_cut_off_tool_call_raises_the_cap_too() -> None:
    prov = _CutToolCall()
    _agent, msgs = _run(prov)
    assert prov.caps == [8192, 32_000]
    (result,) = [b for m in msgs if isinstance(m, UserMessage) and isinstance(m.content, list)
                 for b in m.content if isinstance(b, ToolResultBlock)]
    assert result.is_error and "CUT OFF" in result.content and "now 32000" in result.content


@pytest.mark.parametrize("msg,expected", [
    ("max_tokens is too large: 50000. This model supports at most 16384 completion tokens", 16384),
    ("`max_tokens` must be less than or equal to `8192`", 8192),
    ("max_completion_tokens: must be <= 32768", 32768),
    ("maximum context length is 8192 tokens", None),
    ("connection reset", None),
])
def test_limit_parsing(msg: str, expected: int | None) -> None:
    assert _max_tokens_limit_from(ProviderError(msg)) == expected
