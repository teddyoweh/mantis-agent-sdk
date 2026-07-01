"""Final-turn wrap-up reminder — a turn-limited run gets nudged to summarize."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.agent import Agent, _final_turn_reminder
from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.tools import ToolRegistry, tool
from mantis_agent.types import TextBlock, ToolUseBlock, UserMessage, Usage


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


class _JustText:
    name = "mock"

    def __init__(self) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]

    async def stream(self, **_kw: Any):
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text="done"))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _run(provider, **kw) -> list:
    reg = ToolRegistry()
    reg.add(noop)
    agent = Agent(model="mock", provider=provider, tools=reg, auto_compact=False,
                  include_recall=False, include_env=False, include_memory=False, **kw)
    msgs: list = [UserMessage(content="do a task")]
    anyio.run(lambda: _drain(agent, msgs))
    return msgs


async def _drain(agent, msgs):
    async for _ in agent.run_iter(msgs):
        pass


def _has_final_reminder(msgs: list) -> int:
    return sum(1 for m in msgs if getattr(m, "isMeta", False) and "turn limit" in str(m.content))


def test_reminder_on_turn_limit() -> None:
    msgs = _run(_AlwaysTool(), max_steps=3)
    assert _has_final_reminder(msgs) == 1        # injected once, on the last step


def test_no_reminder_on_natural_stop() -> None:
    msgs = _run(_JustText(), max_steps=5)         # stops turn 1
    assert _has_final_reminder(msgs) == 0


def test_reminder_content() -> None:
    r = _final_turn_reminder()
    body = str(r.content).lower()
    assert "turn limit" in body and "summary" in body
    assert getattr(r, "isMeta", False) is True


def test_single_step_run_no_reminder() -> None:
    # max_steps=1 (degenerate single-turn) → no wrap-up reminder
    msgs = _run(_AlwaysTool(), max_steps=1)
    assert _has_final_reminder(msgs) == 0
