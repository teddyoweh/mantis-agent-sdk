"""Budget wrap-up — an agent approaching its budget is nudged to summarize before
the hard cap raises BudgetExceededError."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.agent import Agent, _final_turn_reminder
from mantis_agent.budget import Budget
from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.events import (
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
)
from mantis_agent.events import ContentBlockStart
from mantis_agent.tools import ToolRegistry, tool
from mantis_agent.types import ToolUseBlock, UserMessage, Usage


@tool
async def noop() -> str:
    return "ok"


class _AlwaysTool:
    name = "mock"

    def __init__(self) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]
        self.n = 0

    async def stream(self, **_kw: Any):
        self.n += 1
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=ToolUseBlock(id=f"c{self.n}", name="noop", input={}))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _run(**kw) -> list:
    reg = ToolRegistry()
    reg.add(noop)
    kw.setdefault("max_steps", 20)
    agent = Agent(model="mock", provider=_AlwaysTool(), tools=reg,
                  auto_compact=False, include_recall=False, include_env=False,
                  include_memory=False, **kw)
    msgs: list = [UserMessage(content="a long task")]

    async def go():
        try:
            async for _ in agent.run_iter(msgs):
                pass
        except Exception:  # BudgetExceededError expected
            pass
    anyio.run(go)
    return msgs


def _count(msgs: list, needle: str) -> int:
    return sum(1 for m in msgs if getattr(m, "isMeta", False) and needle in str(m.content))


def test_budget_wrapup_fires_once() -> None:
    msgs = _run(budget=Budget(max_turns=8))
    assert _count(msgs, "budget limit") == 1        # injected once, before the cap


def test_no_budget_no_wrapup() -> None:
    msgs = _run(max_steps=4)
    assert _count(msgs, "budget limit") == 0


def test_turn_limit_reminder_distinct() -> None:
    # the max_steps wrap-up still uses the 'turn limit' wording
    msgs = _run(max_steps=4)
    assert _count(msgs, "turn limit") == 1


def test_reminder_reason_wording() -> None:
    assert "budget limit" in str(_final_turn_reminder("budget limit").content)
    assert "turn limit" in str(_final_turn_reminder().content)   # default
