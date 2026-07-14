"""Persistence loop — the completion contract, diminishing-returns guard,
hard continuation cap, and adaptive-thinking escalation.

Mirrors Claude Code's gated-stop design (checkTokenBudget / queryLoop) adapted
for open models: a no-tool-use turn is not automatically the end of the run.
Persist mode re-drives it ONLY when there's a real unfinished-work signal (open
todos or an unmet spend target) and progress isn't diminishing, under a hard
cap. A plain query() with no todos and no target must behave EXACTLY as before
persistence existed.

All hermetic: a scripted mock provider stands in for the model. No network.
The mock can mutate the shared todo list as a side effect of a turn — that's
how "the model reported progress" is simulated without a live tool round-trip.
"""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.agent import Agent, _MAX_CONTINUATIONS
from mantis_agent.budget import Budget, BudgetTracker
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
from mantis_agent.types import TextBlock, UserMessage, Usage


# ---------------------------------------------------------------------------
# Scripted mock provider
# ---------------------------------------------------------------------------


def _text_events() -> list:
    """A plain no-tool-use turn — a 'final answer'."""
    return [
        MessageStart(message_id="m", model="mock"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="here is my answer")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=3, output_tokens=2)),
        MessageStop(),
    ]


class _ScriptedMock:
    """Replay-based provider whose turns can be callables with side effects.

    ``turns`` is a list of either event lists or zero-arg callables returning an
    event list. Each ``stream()`` call advances one turn (clamped to the last so
    a run longer than the script keeps replaying the final turn). Records the
    ``thinking`` kwarg the agent forwarded on every call for assertion.
    """

    name = "mock"

    def __init__(self, turns: list) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]
        self._turns = list(turns)
        self.calls = 0
        self.thinking_calls: list[Any] = []

    async def stream(self, **kw: Any):
        self.thinking_calls.append(kw.get("thinking"))
        idx = min(self.calls, len(self._turns) - 1)
        turn = self._turns[idx]
        self.calls += 1
        events = turn() if callable(turn) else turn
        for ev in events:
            yield ev

    async def aclose(self) -> None:
        return None


def _make_agent(provider: _ScriptedMock, **kw: Any) -> Agent:
    return Agent(
        model="mock",
        provider=provider,
        auto_compact=False,
        include_recall=False,
        include_env=False,
        include_memory=False,
        **kw,
    )


def _run(agent: Agent, prompt: str = "do a task") -> list:
    msgs: list = [UserMessage(content=prompt)]

    async def _drain() -> None:
        async for _ in agent.run_iter(msgs):
            pass

    anyio.run(_drain)
    return msgs


# ---------------------------------------------------------------------------
# (a) no todos + final answer => stops after ONE turn (today's semantics)
# ---------------------------------------------------------------------------


def test_no_todos_final_answer_stops_after_one_turn() -> None:
    mock = _ScriptedMock([_text_events()])
    agent = _make_agent(mock)  # persist=True by default
    _run(agent)
    assert mock.calls == 1  # exactly one turn — unchanged from pre-persistence
    assert agent.persist is True


# ---------------------------------------------------------------------------
# (b) open todos + progress => continues, then stops when todos clear
# ---------------------------------------------------------------------------


def test_open_todos_with_progress_continues_then_stops() -> None:
    todos = [
        {"content": "a", "status": "pending"},
        {"content": "b", "status": "pending"},
    ]

    def complete_one_then_answer() -> list:
        for t in todos:
            if t["status"] != "completed":
                t["status"] = "completed"
                break
        return _text_events()

    mock = _ScriptedMock([complete_one_then_answer])
    agent = _make_agent(mock, todos=todos, max_steps=20)
    _run(agent)
    # Turn 1 clears "a" (b still open) -> continue; turn 2 clears "b" -> all
    # done -> natural stop. No third turn.
    assert mock.calls == 2
    assert all(t["status"] == "completed" for t in todos)


# ---------------------------------------------------------------------------
# (c) diminishing returns (2 near-zero-progress turns) => stops
# ---------------------------------------------------------------------------


def test_diminishing_returns_stops() -> None:
    todos = [{"content": "never done", "status": "pending"}]
    mock = _ScriptedMock([_text_events()])  # never completes the todo
    agent = _make_agent(mock, todos=todos, max_steps=20)
    _run(agent)
    # Turn 1: open todo, zero progress -> streak 1, continue.
    # Turn 2: still zero progress -> streak 2 -> diminishing returns -> stop.
    assert mock.calls == 2
    assert todos[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# (d) hard continuation cap respected
# ---------------------------------------------------------------------------


def test_hard_continuation_cap_respected() -> None:
    # Progress every turn (so diminishing returns never trips) but never fully
    # clear the list, so only the hard cap can stop the run.
    todos = [{"content": str(i), "status": "pending"} for i in range(100)]

    def complete_one_then_answer() -> list:
        for t in todos:
            if t["status"] != "completed":
                t["status"] = "completed"
                break
        return _text_events()

    mock = _ScriptedMock([complete_one_then_answer])
    agent = _make_agent(mock, todos=todos, max_steps=50)
    _run(agent)
    # _MAX_CONTINUATIONS continuations + the final turn that hits the cap.
    assert mock.calls == _MAX_CONTINUATIONS + 1
    # The cap bound the run well under max_steps and the todo list.
    assert mock.calls < 50


# ---------------------------------------------------------------------------
# (e) persist=False => stops immediately at a no-tool turn even with open todos
# ---------------------------------------------------------------------------


def test_persist_false_stops_immediately() -> None:
    todos = [{"content": "still open", "status": "pending"}]
    mock = _ScriptedMock([_text_events()])
    agent = _make_agent(mock, todos=todos, persist=False, max_steps=20)
    _run(agent)
    assert mock.calls == 1  # one turn, no continuation despite the open todo
    assert todos[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# (f) ultrathink keyword escalates the thinking cfg passed to the provider
# ---------------------------------------------------------------------------


def test_default_effort_passes_no_thinking() -> None:
    mock = _ScriptedMock([_text_events()])
    agent = _make_agent(mock)  # effort defaults to "medium"
    _run(agent, prompt="just solve this normally")
    # Medium effort maps to the provider default => no thinking block forwarded.
    assert mock.thinking_calls[0] is None


def test_ultrathink_keyword_escalates_thinking() -> None:
    mock = _ScriptedMock([_text_events()])
    agent = _make_agent(mock)
    _run(agent, prompt="ultrathink about this and give the answer")
    cfg = mock.thinking_calls[0]
    assert isinstance(cfg, dict)
    assert cfg.get("type") == "enabled"
    assert isinstance(cfg.get("budget_tokens"), int) and cfg["budget_tokens"] > 0


def test_think_harder_keyword_escalates_thinking() -> None:
    mock = _ScriptedMock([_text_events()])
    agent = _make_agent(mock, effort="low")
    _run(agent, prompt="think harder and then answer")
    cfg = mock.thinking_calls[0]
    # "think harder" lifts a low-effort turn to at least the high-effort budget.
    assert isinstance(cfg, dict) and cfg.get("type") == "enabled"


def test_explicit_high_effort_passes_thinking() -> None:
    mock = _ScriptedMock([_text_events()])
    agent = _make_agent(mock, effort="high")
    _run(agent, prompt="normal request")
    cfg = mock.thinking_calls[0]
    assert isinstance(cfg, dict) and cfg.get("type") == "enabled"


# ---------------------------------------------------------------------------
# Budget helpers — runway (two-sided brake/motivate) + target floor
# ---------------------------------------------------------------------------


def test_runway_none_when_no_ceiling() -> None:
    tracker = BudgetTracker(budget=Budget())
    assert tracker.runway() is None


def test_runway_shrinks_and_floors_at_zero() -> None:
    tracker = BudgetTracker(budget=Budget(max_turns=4))
    assert tracker.runway() == 1.0
    tracker.add_turn()
    tracker.add_turn()
    assert abs(tracker.runway() - 0.5) < 1e-9  # 2 of 4 used
    tracker.add_turn()
    tracker.add_turn()
    tracker.add_turn()  # overshoot
    assert tracker.runway() == 0.0  # clamped, never negative


def test_target_unmet_then_met() -> None:
    tracker = BudgetTracker(budget=Budget(target_total_tokens=100))
    assert tracker.target_unmet() is True
    tracker.add_usage(Usage(input_tokens=60, output_tokens=50), "mock")
    assert tracker.target_unmet() is False  # 110 >= 100


def test_target_keeps_persist_going_until_met() -> None:
    # An unmet token target is an unfinished-work signal even with no todos:
    # the run continues until the target is met (or diminishing returns / cap).
    # Each turn reports usage, so the target is reached and the run stops.
    budget = Budget(target_total_tokens=8)
    mock = _ScriptedMock([_text_events()])
    agent = _make_agent(mock, budget=budget, max_steps=20)
    _run(agent)
    # Turn 1: 5 tokens (< 8, unmet) -> continue. Turn 2: 10 total (>= 8) -> the
    # target is met, so the no-tool turn is a real stop.
    assert mock.calls == 2
    assert agent._budget_tracker is not None
    assert agent._budget_tracker.target_unmet() is False
