"""PreCompact hook — fires before summarization; can snapshot or block it."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.agent import Agent
from mantis_agent.capabilities import HOSTED_PROFILES, ModelCapability
from mantis_agent.compact import SimpleCompactor
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.hooks import HookResult, Hooks
from mantis_agent.tools import ToolRegistry, tool
from mantis_agent.types import (
    AssistantMessage, TextBlock, ToolUseBlock, UserMessage, Usage,
)


@tool
async def noop() -> str:
    return "ok"


class _BigUsage:
    """Calls a tool each turn (loop continues) and reports near-full context so
    should_compact() fires on the following turn."""
    name = "mock"

    def __init__(self) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]
        self.n = 0

    async def stream(self, **_kw: Any):
        self.n += 1
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=ToolUseBlock(id=f"c{self.n}", name="noop", input={}))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=999_999, output_tokens=1))
        yield MessageStop()


def _long_history() -> list:
    h: list = []
    for i in range(12):
        h.append(UserMessage(content=f"msg {i}"))
        h.append(AssistantMessage(content=[TextBlock(text=f"a {i}")]))
    return h


async def _summ(_p: str) -> str:
    return "Summary of prior conversation: earlier work."


def _run(hook, *, keep_recent: int = 2) -> tuple[list, dict]:
    calls = {"n": 0}

    async def wrapped(ctx):
        calls["n"] += 1
        return await hook(ctx) if hook else HookResult()

    reg = ToolRegistry()
    reg.add(noop)
    agent = Agent(
        model="mock", provider=_BigUsage(), tools=reg,
        compactor=SimpleCompactor(_summ, keep_recent_turns=keep_recent),
        hooks=Hooks(pre_compact=wrapped), max_steps=3,
        include_recall=False, include_env=False, include_memory=False,
    )
    agent.model_capability = ModelCapability(
        name="mock", family="mock", context_window=1000, max_output_tokens=100)
    msgs = _long_history() + [UserMessage(content="continue")]

    async def go():
        async for _ in agent.run_iter(msgs):
            pass
    anyio.run(go)
    return msgs, calls


def test_precompact_fires_before_compaction() -> None:
    async def hook(_ctx):
        return HookResult()
    _msgs, calls = _run(hook)
    assert calls["n"] >= 1                       # PreCompact ran before summarize


def test_precompact_block_skips_compaction() -> None:
    async def hook(_ctx):
        return HookResult(block=True)
    msgs, calls = _run(hook)
    assert calls["n"] >= 1
    # blocked → no CompactBoundaryMessage / summary was inserted
    assert not any("Summary of prior conversation" in str(getattr(m, "content", "")) or
                   "Summary of prior conversation" in str(getattr(m, "summary", ""))
                   for m in msgs)


def test_no_hook_still_compacts() -> None:
    msgs, _calls = _run(None)
    # with no blocking hook, compaction proceeded (history shrank below the raw 25)
    assert len(msgs) < 25
