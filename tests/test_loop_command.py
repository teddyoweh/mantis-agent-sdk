"""/loop — re-fire a prompt on an interval with relative cadence (a fire waits
for the running turn to finish; the next interval starts after the fire)."""

from __future__ import annotations

import anyio

from mantis_agent.tui import (
    LOOP_MIN_INTERVAL_S,
    format_loop_interval,
    parse_loop_command,
    run_prompt_loop,
)


# -- parsing -------------------------------------------------------------------


def test_parse_units() -> None:
    assert parse_loop_command("30s /cost") == (30.0, "/cost")
    assert parse_loop_command("5m check ci") == (300.0, "check ci")
    assert parse_loop_command("1h summarize progress") == (3600.0, "summarize progress")


def test_parse_bare_number_is_minutes() -> None:
    assert parse_loop_command("5 check the deploy") == (300.0, "check the deploy")


def test_parse_clamps_tiny_intervals() -> None:
    secs, _ = parse_loop_command("1s spam")
    assert secs == LOOP_MIN_INTERVAL_S


def test_parse_bad_input_returns_usage() -> None:
    assert "usage" in parse_loop_command("")
    assert "usage" in parse_loop_command("no-interval prompt only")  # no leading number
    assert "usage" in parse_loop_command("5m")  # interval but no prompt


def test_format_loop_interval() -> None:
    assert format_loop_interval(30) == "30s"
    assert format_loop_interval(300) == "5m"
    assert format_loop_interval(3600) == "1h"
    assert format_loop_interval(90) == "90s"


# -- engine --------------------------------------------------------------------


class _Ev:
    def __init__(self) -> None:
        self._set = False
    def set(self) -> None:
        self._set = True
    def is_set(self) -> bool:
        return self._set


def test_loop_fires_and_stops() -> None:
    fired = []
    stop = _Ev()

    async def fake_sleep(_s: float) -> None:
        await anyio.sleep(0)

    async def fire() -> None:
        fired.append(1)
        if len(fired) >= 3:
            stop.set()

    anyio.run(lambda: run_prompt_loop(
        2.0, fire, is_busy=lambda: False, stopped=stop, sleep=fake_sleep))
    assert len(fired) == 3


def test_loop_waits_for_idle_before_firing() -> None:
    # busy for the first 3 polls → the fire happens only after idle.
    fired = []
    stop = _Ev()
    busy_polls = [True, True, True]

    async def fake_sleep(_s: float) -> None:
        await anyio.sleep(0)

    def is_busy() -> bool:
        return busy_polls.pop(0) if busy_polls else False

    async def fire() -> None:
        assert not busy_polls          # every busy poll consumed BEFORE firing
        fired.append(1)
        stop.set()

    anyio.run(lambda: run_prompt_loop(
        1.0, fire, is_busy=is_busy, stopped=stop, sleep=fake_sleep))
    assert fired == [1]


def test_loop_stop_during_wait_never_fires() -> None:
    fired = []
    stop = _Ev()
    ticks = [0]

    async def fake_sleep(_s: float) -> None:
        ticks[0] += 1
        if ticks[0] == 2:
            stop.set()                  # stop mid-interval
        await anyio.sleep(0)

    async def fire() -> None:
        fired.append(1)

    anyio.run(lambda: run_prompt_loop(
        60.0, fire, is_busy=lambda: False, stopped=stop, sleep=fake_sleep))
    assert fired == []


def test_loop_survives_a_failing_fire() -> None:
    fired, errors = [], []
    stop = _Ev()

    async def fake_sleep(_s: float) -> None:
        await anyio.sleep(0)

    async def fire() -> None:
        fired.append(1)
        if len(fired) == 1:
            raise RuntimeError("turn exploded")
        stop.set()

    anyio.run(lambda: run_prompt_loop(
        1.0, fire, is_busy=lambda: False, stopped=stop,
        sleep=fake_sleep, on_error=errors.append))
    assert len(fired) == 2              # kept going after the failure
    assert len(errors) == 1 and "exploded" in str(errors[0])
