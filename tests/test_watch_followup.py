"""A monitor event has to WAKE a turn, not just queue itself as context.

The defect this pins: ``_on_job_stream`` appended the event as passive meta
context and printed a notification line, so the model only ever reacted the next
time the user typed. For a monitor that is the whole point — you arm it so
something happens *without* you sitting there — and the observed behaviour was
that it "just said done".

Waking a turn is the agent acting on its own, so the gate is the part worth
testing: every ``False`` below is a reason it must NOT act.
"""

from __future__ import annotations

from mantis_agent.tui_fullscreen import (
    WATCH_FOLLOWUP_COOLDOWN_S,
    WATCH_FOLLOWUP_MAX_PER_JOB,
    watch_followup_due,
    watch_followup_prompt,
)


def _fresh() -> dict:
    return {"fires": 0, "last": 0.0}


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_first_event_on_an_idle_session_wakes_a_turn() -> None:
    assert watch_followup_due(_fresh(), 100.0, busy=False) is True


def test_a_never_fired_monitor_is_not_held_back_by_the_cooldown() -> None:
    # last=0.0 means "never" — a monitor armed at t=5 that fires at t=6 must not
    # be blocked because 6 - 0 happens to be small on a fresh clock.
    assert watch_followup_due(_fresh(), WATCH_FOLLOWUP_COOLDOWN_S + 1, busy=False) is True


# --------------------------------------------------------------------------
# every reason not to act
# --------------------------------------------------------------------------


def test_never_fires_while_a_turn_is_running() -> None:
    assert watch_followup_due(_fresh(), 100.0, busy=True) is False


def test_never_fires_when_disabled() -> None:
    assert watch_followup_due(_fresh(), 100.0, busy=False, enabled=False) is False


def test_cooldown_blocks_a_flapping_monitor() -> None:
    rec = {"fires": 1, "last": 100.0}
    assert watch_followup_due(rec, 100.0 + WATCH_FOLLOWUP_COOLDOWN_S - 0.1,
                              busy=False) is False
    assert watch_followup_due(rec, 100.0 + WATCH_FOLLOWUP_COOLDOWN_S,
                              busy=False) is True


def test_per_monitor_ceiling_stops_an_unbounded_spend_loop() -> None:
    rec = {"fires": WATCH_FOLLOWUP_MAX_PER_JOB, "last": 0.0}
    assert watch_followup_due(rec, 10_000.0, busy=False) is False


def test_ceiling_is_reached_not_exceeded() -> None:
    rec = {"fires": WATCH_FOLLOWUP_MAX_PER_JOB - 1, "last": 0.0}
    assert watch_followup_due(rec, 10_000.0, busy=False) is True


def test_busy_wins_over_everything_else() -> None:
    # Even a monitor that is otherwise perfectly eligible must not interrupt a
    # running turn or a pending permission prompt.
    assert watch_followup_due(_fresh(), 10_000.0, busy=True, enabled=True) is False


def test_tolerates_a_missing_record_shape() -> None:
    # The record is created lazily; a partially-populated dict must not raise on
    # the render path.
    assert watch_followup_due({}, 10_000.0, busy=False) is True


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------


def test_prompt_names_the_monitor_and_carries_the_event() -> None:
    p = watch_followup_prompt("Monitor google.com status",
                              "  DOWN: https://www.google.com 000 at 12:00  ")
    assert p.startswith("[monitor] Monitor google.com status reported:")
    assert "DOWN: https://www.google.com 000 at 12:00" in p
    # Whitespace from the emitting script must not leak into the turn.
    assert "reported:   DOWN" not in p


def test_prompt_invites_a_one_line_answer_when_routine() -> None:
    # Without this a chatty monitor turns into an essay every cooldown window.
    assert "one line" in watch_followup_prompt("m", "tick")


def test_prompt_survives_an_empty_description() -> None:
    assert watch_followup_prompt("", "evt").startswith("[monitor] monitor reported:")
