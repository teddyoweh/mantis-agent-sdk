"""The sleep tool — bounded, interruptible wait (patched so tests don't wait)."""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.builtin_tools.fs import CODING_TOOLS, sleep


@pytest.fixture(autouse=True)
def _capture_sleep(monkeypatch):
    """Record the duration passed to anyio.sleep and return instantly."""
    seen = {"secs": None}

    async def _fake(s):
        seen["secs"] = s
    monkeypatch.setattr("anyio.sleep", _fake)
    return seen


def _run(**kw) -> str:
    return anyio.run(lambda: sleep.fn(**kw))


def test_returns_slept_message(_capture_sleep) -> None:
    assert _run(seconds=5) == "slept 5s"
    assert _capture_sleep["secs"] == 5.0


def test_clamps_high(_capture_sleep) -> None:
    assert _run(seconds=99999) == "slept 600s"
    assert _capture_sleep["secs"] == 600.0


def test_clamps_negative_to_zero(_capture_sleep) -> None:
    assert _run(seconds=-5) == "slept 0s"
    assert _capture_sleep["secs"] == 0.0


def test_string_seconds_coerced(_capture_sleep) -> None:
    assert _run(seconds="0.1") == "slept 0.1s"


def test_bad_seconds_defaults(_capture_sleep) -> None:
    assert _run(seconds="abc") == "slept 5s"            # falls back to default


def test_read_only_and_registered() -> None:
    assert sleep.is_read_only is True
    assert any(t.name == "sleep" for t in CODING_TOOLS)
