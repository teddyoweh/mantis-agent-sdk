"""Unit tests for the autopilot (``/goal``) and master-loop (``/agi``) state
machine, exercised through the PURE, module-level helpers in
:mod:`mantis_agent.tui` — no model, no prompt_toolkit, no network.

The live decisions happen inside closures in ``tui_fullscreen.py`` (``_advance_goal``,
``_agi_loop``, the ``/goal`` / ``/agi`` command handlers), but the *decisions
themselves* were factored into importable pure functions that those closures
call, so the exact production logic is what these tests hit:

* ``detect_goal_marker`` / ``GOAL_MARK_RE`` — GOAL COMPLETE/BLOCKED anchoring
  (last block only, line-start, word-boundary — no incidental-mention triggers).
* ``goal_todo_snapshot``               — the stagnation fingerprint.
* ``goal_should_nudge``                — the budget / diminishing-returns decision.
* ``autopilot_conflict``               — mutual exclusion of /goal and /agi.

A regression guard also asserts the fullscreen loop still routes through the
shared helpers (no drifting private copy of the regex).
"""

from __future__ import annotations

import inspect

import pytest

from mantis_agent import tui
from mantis_agent.tui import (
    AGI_MAX_AGENTS_PER_CYCLE,
    GOAL_BLOCKED_MARKER,
    GOAL_COMPLETE_MARKER,
    GOAL_MARK_RE,
    GOAL_MAX_CYCLES,
    agi_cycle_prompt,
    autopilot_conflict,
    detect_goal_marker,
    goal_continue_prompt,
    goal_kickoff_prompt,
    goal_replan_prompt,
    goal_should_nudge,
    goal_todo_snapshot,
    goal_verify_prompt,
)


# ---------------------------------------------------------------------------
# 1. GOAL COMPLETE / BLOCKED marker anchoring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("GOAL COMPLETE", "COMPLETE"),
        ("GOAL BLOCKED", "BLOCKED"),
        ("GOAL COMPLETE — everything passes", "COMPLETE"),
        ("GOAL BLOCKED: cannot access the network", "BLOCKED"),
        # case-insensitive
        ("goal complete", "COMPLETE"),
        ("Goal Blocked", "BLOCKED"),
        # leading whitespace/indent is allowed (^\s*)
        ("   GOAL COMPLETE", "COMPLETE"),
        ("\t GOAL BLOCKED", "BLOCKED"),
    ],
)
def test_marker_detected_at_line_start(text, expected):
    assert detect_goal_marker(text) == expected


def test_marker_matches_only_on_its_own_line_in_a_multiline_block():
    """A real completion reply: prose, then the marker on the final line."""
    reply = (
        "I ran `pytest -q` → exit 0 and the build is green.\n"
        "All todos are verified against real output.\n"
        "GOAL COMPLETE"
    )
    assert detect_goal_marker(reply) == "COMPLETE"


@pytest.mark.parametrize(
    "text",
    [
        # incidental mid-prose mention — NOT at line start → must not trigger
        "do not say GOAL BLOCKED unless you are truly stuck",
        "The verifier looks for the GOAL COMPLETE marker at the end.",
        "remember to end with: GOAL COMPLETE",
        # near-words: the trailing \b must fail on GOAL COMPLETELY / BLOCKEDish
        "GOAL COMPLETELY reworked the module",
        "GOAL BLOCKEDBY a dependency",
        # unrelated
        "still working on the plan",
        "GOALPOST moved",
        "",
    ],
)
def test_no_false_trigger_on_incidental_mentions(text):
    assert detect_goal_marker(text) is None


def test_none_and_empty_input():
    assert detect_goal_marker("") is None
    assert detect_goal_marker(None) is None  # type: ignore[arg-type]


def test_last_block_only_semantics():
    """The closure passes ONLY the last assistant text block to
    ``detect_goal_marker``. So a marker that appears (mid-line) in earlier prose
    of an EARLIER turn is never seen; and within the block we honor line-start.
    Here the block mentions the marker mid-line first, then never on its own
    line → no completion. If the model had put it on its own line, it WOULD."""
    non_triggering = (
        "Earlier I warned you not to write GOAL COMPLETE prematurely.\n"
        "I am still fixing the failing test."
    )
    assert detect_goal_marker(non_triggering) is None
    triggering = non_triggering + "\nGOAL COMPLETE"
    assert detect_goal_marker(triggering) == "COMPLETE"


def test_regex_is_multiline_and_case_insensitive():
    assert GOAL_MARK_RE.flags & __import__("re").MULTILINE
    assert GOAL_MARK_RE.flags & __import__("re").IGNORECASE


# ---------------------------------------------------------------------------
# 2. Stagnation snapshot / guard fingerprint
# ---------------------------------------------------------------------------


def _todos(*pairs):
    return [{"content": c, "status": s} for c, s in pairs]


def test_snapshot_counts_total_and_completed():
    todos = _todos(("a", "completed"), ("b", "in_progress"), ("c", "completed"))
    n, done, _ = goal_todo_snapshot(todos)
    assert n == 3
    assert done == 2


def test_snapshot_stable_for_identical_plans():
    a = _todos(("write tests", "in_progress"), ("ship", "pending"))
    b = _todos(("write tests", "in_progress"), ("ship", "pending"))
    assert goal_todo_snapshot(a) == goal_todo_snapshot(b)


def test_snapshot_changes_when_a_status_advances():
    before = _todos(("write tests", "in_progress"), ("ship", "pending"))
    after = _todos(("write tests", "completed"), ("ship", "pending"))
    assert goal_todo_snapshot(before) != goal_todo_snapshot(after)


def test_snapshot_changes_on_content_edit_add_and_reorder():
    base = _todos(("a", "pending"), ("b", "pending"))
    assert goal_todo_snapshot(base) != goal_todo_snapshot(_todos(("a2", "pending"), ("b", "pending")))
    assert goal_todo_snapshot(base) != goal_todo_snapshot(_todos(("a", "pending"), ("b", "pending"), ("c", "pending")))
    # order-sensitive: swapping rows is a different plan state
    assert goal_todo_snapshot(base) != goal_todo_snapshot(_todos(("b", "pending"), ("a", "pending")))


def test_snapshot_empty_plan():
    n, done, _ = goal_todo_snapshot([])
    assert (n, done) == (0, 0)


def test_snapshot_tolerates_missing_keys():
    # todo tool may emit rows without content/status yet — must not raise.
    snap = goal_todo_snapshot([{}, {"status": "completed"}])
    assert snap[0] == 2 and snap[1] == 1


def test_stagnation_guard_progression_via_snapshot_equality():
    """Model the guard's inputs: equal snapshots across cycles are what drives
    the stall counter in ``_advance_goal`` (unchanged 3 cycles → replan)."""
    plan = _todos(("a", "in_progress"))
    stall = 0
    prev = None
    for _ in range(3):  # three idle cycles with NO plan movement
        snap = goal_todo_snapshot(plan)
        stall = stall + 1 if snap == prev else 0
        prev = snap
    assert stall == 2  # first cycle sets baseline, next two match → stalled
    # now the plan moves → the counter resets
    plan[0]["status"] = "completed"
    snap = goal_todo_snapshot(plan)
    stall = stall + 1 if snap == prev else 0
    assert stall == 0


# ---------------------------------------------------------------------------
# 3. Budget-aware / diminishing-returns continuation decision
# ---------------------------------------------------------------------------


def test_nudge_when_uncapped_window():
    # window falsy → "no cap" → always under budget
    assert goal_should_nudge(tokens=10 ** 9, window=0, cont=1, delta=9999, prev_delta=9999) is True


def test_nudge_while_under_ninety_percent():
    assert goal_should_nudge(tokens=8000, window=10000, cont=1, delta=1000, prev_delta=1000) is True


def test_no_nudge_at_or_over_budget():
    # 9000 / 10000 == 0.9 → NOT < 0.9*win → over budget
    assert goal_should_nudge(tokens=9000, window=10000, cont=1, delta=9999, prev_delta=9999) is False
    assert goal_should_nudge(tokens=9500, window=10000, cont=1, delta=9999, prev_delta=9999) is False


def test_diminishing_returns_stops_nudging():
    # cont>=3 AND two consecutive sub-500 deltas → diminishing → no nudge
    assert goal_should_nudge(tokens=100, window=10000, cont=3, delta=400, prev_delta=400) is False


def test_not_diminishing_before_three_continuations():
    # small deltas but only 2 continuations → still nudges
    assert goal_should_nudge(tokens=100, window=10000, cont=2, delta=10, prev_delta=10) is True


def test_not_diminishing_if_either_delta_is_large():
    assert goal_should_nudge(tokens=100, window=10000, cont=5, delta=800, prev_delta=100) is True
    assert goal_should_nudge(tokens=100, window=10000, cont=5, delta=100, prev_delta=800) is True


def test_delta_boundary_is_strict_less_than_500():
    # delta==500 is NOT < 500 → not diminishing → nudge
    assert goal_should_nudge(tokens=100, window=10000, cont=9, delta=500, prev_delta=499) is True


def test_over_budget_beats_not_diminishing():
    # even actively-progressing turns stop once the context is nearly full
    assert goal_should_nudge(tokens=9999, window=10000, cont=1, delta=9999, prev_delta=9999) is False


# ---------------------------------------------------------------------------
# 4. Mutual exclusion of /goal and /agi
# ---------------------------------------------------------------------------


def test_starting_agi_while_goal_active_is_refused():
    msg = autopilot_conflict(goal_active=True, agi_active=False, starting="agi")
    assert msg is not None
    assert "mutually exclusive" in msg
    assert "/goal stop" in msg


def test_starting_goal_while_agi_active_is_refused():
    msg = autopilot_conflict(goal_active=False, agi_active=True, starting="goal")
    assert msg is not None
    assert "mutually exclusive" in msg
    assert "/agi stop" in msg


def test_no_conflict_when_nothing_running():
    assert autopilot_conflict(goal_active=False, agi_active=False, starting="agi") is None
    assert autopilot_conflict(goal_active=False, agi_active=False, starting="goal") is None


def test_restart_same_kind_is_not_a_conflict():
    # restarting /agi while an /agi runs (no goal) is allowed here — the handler
    # tears down the old loop itself; only the CROSS pair is mutually exclusive.
    assert autopilot_conflict(goal_active=False, agi_active=True, starting="agi") is None
    assert autopilot_conflict(goal_active=True, agi_active=False, starting="goal") is None


# ---------------------------------------------------------------------------
# 5. Prompt / marker parity + constants
# ---------------------------------------------------------------------------


def test_verify_prompt_embeds_the_completion_marker_but_does_not_self_trigger():
    p = goal_verify_prompt("ship the feature")
    assert GOAL_COMPLETE_MARKER in p
    # the marker is embedded mid-line ("end your reply with: GOAL COMPLETE"),
    # so the INSTRUCTION itself must not read as a completion.
    assert detect_goal_marker(p) is None
    # but a bare marker line (what the model actually emits) does.
    assert detect_goal_marker(GOAL_COMPLETE_MARKER) == "COMPLETE"


@pytest.mark.parametrize("fn", [goal_kickoff_prompt, goal_continue_prompt, goal_replan_prompt])
def test_blocked_marker_available_to_the_model(fn):
    # kickoff/continue tell the model how to declare BLOCKED; replan does not,
    # but all three should mention the goal text they receive.
    sig = inspect.signature(fn)
    args = ["the goal"] + [1] * (len(sig.parameters) - 1)
    p = fn(*args)
    assert "the goal" in p


def test_kickoff_and_continue_reference_blocked_marker():
    assert GOAL_BLOCKED_MARKER in goal_kickoff_prompt("x")
    assert GOAL_BLOCKED_MARKER in goal_continue_prompt("x", 1, GOAL_MAX_CYCLES)


def test_agi_cycle_prompt_carries_cycle_seed_and_wave_cap():
    p = agi_cycle_prompt("build a search engine", 7)
    assert "cycle 7" in p
    assert "build a search engine" in p
    assert str(AGI_MAX_AGENTS_PER_CYCLE) in p


def test_constants_are_sane():
    assert GOAL_MAX_CYCLES == 30
    assert GOAL_COMPLETE_MARKER == "GOAL COMPLETE"
    assert GOAL_BLOCKED_MARKER == "GOAL BLOCKED"
    assert AGI_MAX_AGENTS_PER_CYCLE >= 1
    # the shared markers actually satisfy the detector (round-trip)
    assert detect_goal_marker(GOAL_COMPLETE_MARKER) == "COMPLETE"
    assert detect_goal_marker(GOAL_BLOCKED_MARKER) == "BLOCKED"


# ---------------------------------------------------------------------------
# 6. Regression guard: the fullscreen loop routes through the shared helpers
# ---------------------------------------------------------------------------


def test_fullscreen_uses_shared_helpers_no_private_regex_copy():
    """Guards against drift: ``tui_fullscreen`` must not resurrect its own
    ``_GOAL_MARK_RE`` — it now imports ``detect_goal_marker`` from ``tui``."""
    src = inspect.getsource(__import__("mantis_agent.tui_fullscreen", fromlist=["x"]))
    assert "_GOAL_MARK_RE" not in src
    assert "detect_goal_marker" in src
    assert "goal_todo_snapshot" in src
    assert "goal_should_nudge" in src
    assert "autopilot_conflict" in src


def test_helpers_live_at_module_level_on_tui():
    for name in ("detect_goal_marker", "goal_todo_snapshot", "goal_should_nudge",
                 "autopilot_conflict", "GOAL_MARK_RE"):
        assert hasattr(tui, name), name
