"""``max_turns`` / ``max_budget_usd`` / persistence must not fight each other.

Two ways the stop controls used to contradict one another:

1. ``Agent(max_usd=...)`` built its shortcut ``Budget`` with
   ``max_turns=max_steps``. The loop schedules the final step as a wrap-up
   turn (it injects "wrap up NOW" and lets the model answer), but the budget
   tracker's ``>=`` turn check then raised ``BudgetExceededError`` on that very
   turn — BEFORE the summary was yielded. Adding a dollar ceiling silently
   turned a clean turn-limited stop into an error.

2. After a wrap-up reminder ("wrap up NOW") the model summarizes and stops;
   persist mode then re-drove the natural stop with "keep working — do not
   summarize". The two nudges are contradictory; the reminder must win.
"""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent import Agent, tool
from mantis_agent.agent import _TODO_SENTINEL
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import AssistantMessage, TextBlock, ToolUseBlock, Usage, UserMessage


class _Provider(MockProvider):
    """``tool_turns`` calls emit a tool call; every later call is text."""

    name = "mock"

    def __init__(self, tool_name: str, tool_turns: int) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._tool_turns = tool_turns
        self.n = 0

    async def stream(self, **kw: Any):
        self.n += 1
        self.calls.append(kw)
        yield MessageStart(message_id=f"m{self.n}", model="mock")
        if self.n <= self._tool_turns:
            yield ContentBlockStart(index=0, block=ToolUseBlock(id=f"c{self.n}", name=self._tool_name, input={}))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=1, output_tokens=1))
        else:
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="Summary: done what I could."))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _meta(msgs: list, needle: str) -> list:
    return [m for m in msgs if isinstance(m, UserMessage) and getattr(m, "isMeta", False)
            and needle in str(m.content)]


def _make(provider: Any, **kw: Any) -> Agent:
    return Agent(model="mock", provider=provider, auto_compact=False,
                 include_recall=False, include_env=False, include_memory=False, **kw)


def test_max_usd_shortcut_keeps_the_turn_limit_a_clean_stop() -> None:
    @tool
    async def noop() -> str:
        """Do nothing."""
        return "ok"

    prov = _Provider("noop", tool_turns=100)
    agent = _make(prov, tools=[noop], max_steps=2, max_usd=100.0)
    msgs: list = [UserMessage(content="task")]

    anyio.run(agent.run, msgs)  # must NOT raise BudgetExceededError("turns")

    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    assert len(assistants) == 2
    assert prov.n == 2
    assert len(_meta(msgs, "turn limit")) == 1
    # Every tool_use answered — the final turn was fully processed.
    assert isinstance(msgs[-1], UserMessage) and isinstance(msgs[-1].content, list)


def test_wrapup_reminder_is_not_followed_by_a_persist_nudge() -> None:
    todos = [{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}]

    @tool
    async def mark_done() -> str:
        """Complete the first todo (so progress is visible to the persist gate)."""
        todos[0]["status"] = "completed"
        return "ok"

    prov = _Provider("mark_done", tool_turns=1)
    agent = _make(prov, tools=[mark_done], todos=todos, max_steps=3, persist=True)
    msgs: list = [UserMessage(content="task")]

    anyio.run(agent.run, msgs)

    reminders = [i for i, m in enumerate(msgs) if m in _meta(msgs, "wrap up NOW")]
    assert reminders, "the final-step wrap-up reminder should have fired"
    last_reminder = reminders[-1]
    nudges_after = [i for i, m in enumerate(msgs) if i > last_reminder and m in _meta(msgs, "Keep working")]
    assert nudges_after == [], "persist re-drove a natural stop AFTER telling the model to wrap up"
    # The run honoured its own reminder: it ended at the step cap without an extension.
    assert prov.n == 3
    assert any(_TODO_SENTINEL in str(m.content) for m in msgs if getattr(m, "isMeta", False))


def _query_result(provider: Any, **opts: Any) -> Any:
    from mantis_agent import query
    from mantis_agent.query import SDKResultMessage

    @tool
    async def noop() -> str:
        """Do nothing."""
        return "ok"

    async def go() -> Any:
        out = None
        async for m in query(prompt="task", options={
            "model": "mock", "provider": provider, "tools": [noop],
            "include_memory": False, **opts,
        }):
            if isinstance(m, SDKResultMessage):
                out = m
        return out

    return anyio.run(go)


def test_step_cap_cutoff_reports_error_max_turns_with_or_without_usd_budget() -> None:
    """The subtype for a turn-cap cutoff is the SDK's ``error_max_turns`` and
    must not depend on whether a USD ceiling is also configured."""
    plain = _query_result(_Provider("noop", tool_turns=100), max_turns=2)
    with_usd = _query_result(_Provider("noop", tool_turns=100), max_turns=2, max_usd=100.0)
    for res in (plain, with_usd):
        assert res.is_error is True
        assert res.subtype == "error_max_turns", res.subtype
        assert res.num_turns == 2


def test_wrapping_up_on_the_final_turn_is_a_success() -> None:
    """The final-step reminder lets the model summarize; a model that does so
    ended naturally — not a cutoff — so the result is ``success``."""
    res = _query_result(_Provider("noop", tool_turns=1), max_turns=2, max_usd=100.0)
    assert res.is_error is False
    assert res.subtype == "success"
    assert "Summary" in res.result


def test_persist_still_continues_when_no_wrapup_was_requested() -> None:
    """Regression guard for the gate: with plenty of steps left, an open todo
    that just made progress still gets the persist nudge."""
    todos = [{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}]

    @tool
    async def mark_done() -> str:
        """Complete the first todo."""
        todos[0]["status"] = "completed"
        return "ok"

    prov = _Provider("mark_done", tool_turns=1)
    agent = _make(prov, tools=[mark_done], todos=todos, max_steps=20, persist=True)
    msgs: list = [UserMessage(content="task")]
    anyio.run(agent.run, msgs)
    assert _meta(msgs, "Keep working"), "persist should nudge when work is open and progress was made"
