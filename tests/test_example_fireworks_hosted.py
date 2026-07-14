"""Tests for the ``fireworks_hosted`` example.

The example has two run modes:

  * **Real Fireworks** — guarded by ``$FIREWORKS_API_KEY``.
  * **Offline mock** — guarded by ``MANTIS_AGENT_MOCK=1``; uses a scripted
    :class:`MockProvider` to exercise the tool-call → tool-result →
    final-answer path with zero network.

These tests cover the mock path (which is also what CI / contributors run)
and the live-mode guard (so a missing key fails fast with a clear hint
instead of hitting Fireworks anonymously and 401-ing).

We intentionally do **not** hit the real Fireworks endpoint here — that's
what the example's ``FIREWORKS_API_KEY`` path is for. Tests must stay
hermetic.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from mantis_agent.examples import fireworks_hosted as example


# ---------------------------------------------------------------------------
# Tool unit-tests — keep the canned lookup deterministic
# ---------------------------------------------------------------------------


def test_lookup_company_known_value():
    """Known company names hit the canned dict and stay stable."""

    async def go() -> str:
        return await example.lookup_company.fn(name="Spawn Labs")

    out = anyio.run(go)
    assert out == "Spawn Labs builds AI agents."


def test_lookup_company_case_insensitive():
    """Lookup must be case + whitespace insensitive — otherwise model
    quirks ("Spawn labs" vs "spawn labs") break the demo."""

    async def go() -> str:
        return await example.lookup_company.fn(name="  FIREWORKS  ")

    out = anyio.run(go)
    assert out == "Fireworks AI runs hosted open-weight inference."


def test_lookup_company_unknown_falls_through():
    """Unknown names still return a one-line string (no exception)."""

    async def go() -> str:
        return await example.lookup_company.fn(name="Acme Co")

    out = anyio.run(go)
    assert out.startswith("Acme Co")
    assert "no record" in out


# ---------------------------------------------------------------------------
# Mock provider — script structure is the contract with Agent loop
# ---------------------------------------------------------------------------


def test_mock_provider_yields_two_turns_in_order():
    """First stream() must emit a tool_use → stop; second must emit a
    text answer → stop. If the order ever flips, the example deadlocks
    (parent expects a tool result before any text). Lock the contract."""

    from mantis_agent.events import (
        ContentBlockStart,
        MessageStart,
        MessageStop,
    )
    from mantis_agent.types import TextBlock, ToolUseBlock

    provider = example._build_mock_provider()

    async def collect_turn() -> list:
        out = []
        async for ev in provider.stream():
            out.append(ev)
        return out

    turn_1 = anyio.run(collect_turn)
    turn_2 = anyio.run(collect_turn)

    # Turn 1 — tool call.
    assert isinstance(turn_1[0], MessageStart)
    starts_1 = [e for e in turn_1 if isinstance(e, ContentBlockStart)]
    assert len(starts_1) == 1
    assert isinstance(starts_1[0].block, ToolUseBlock)
    assert starts_1[0].block.name == "lookup_company"
    assert isinstance(turn_1[-1], MessageStop)

    # Turn 2 — final text.
    assert isinstance(turn_2[0], MessageStart)
    starts_2 = [e for e in turn_2 if isinstance(e, ContentBlockStart)]
    assert len(starts_2) == 1
    assert isinstance(starts_2[0].block, TextBlock)
    assert isinstance(turn_2[-1], MessageStop)


def test_mock_provider_replays_last_script_on_overflow():
    """Defensive: a stray extra ``stream()`` call (e.g. agent loops one
    more turn under a retry) must not IndexError — it should replay the
    last script. Keeps the example resilient under model-shape changes."""

    provider = example._build_mock_provider()

    async def consume(n: int) -> list[list]:
        rounds: list[list] = []
        for _ in range(n):
            evs = []
            async for ev in provider.stream():
                evs.append(ev)
            rounds.append(evs)
        return rounds

    rounds = anyio.run(consume, 4)
    assert len(rounds) == 4
    # The 3rd and 4th calls should equal the 2nd (final text replay).
    assert [type(e).__name__ for e in rounds[2]] == [
        type(e).__name__ for e in rounds[1]
    ]
    assert [type(e).__name__ for e in rounds[3]] == [
        type(e).__name__ for e in rounds[1]
    ]


# ---------------------------------------------------------------------------
# main() — end-to-end through query() + Agent loop in mock mode
# ---------------------------------------------------------------------------


def test_main_mock_mode_exercises_tool_path(capsys, monkeypatch, tmp_path):
    """main() in mock mode must:

      * dispatch ``lookup_company`` (Path A tool call);
      * thread the tool result back into the parent;
      * print a final assistant line that quotes the result;
      * end with the "[ok] mock-mode smoke test passed." marker.

    The example's own assertions also enforce the first two — but we
    re-check externally so a regression in the print path also fails.
    """

    monkeypatch.setenv("MANTIS_AGENT_MOCK", "1")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "mantis_agent_home"))
    # Defensive: make sure a stray real key doesn't shift behavior.
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)

    anyio.run(example.main)

    out = capsys.readouterr().out
    assert "[assistant → lookup_company] dispatching" in out
    assert "Spawn Labs builds AI agents." in out
    assert "[ok] mock-mode smoke test passed." in out


def test_main_live_mode_without_api_key_raises_systemexit(monkeypatch, tmp_path):
    """Live mode must fail fast with a clear hint when the key is unset
    — not silently call the API and 401, and not crash with a KeyError.
    """

    monkeypatch.delenv("MANTIS_AGENT_MOCK", raising=False)
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "mantis_agent_home"))

    with pytest.raises(SystemExit) as excinfo:
        anyio.run(example.main)

    msg = str(excinfo.value)
    assert "FIREWORKS_API_KEY" in msg
    assert "MANTIS_AGENT_MOCK=1" in msg


# ---------------------------------------------------------------------------
# Module exports / shape — keep the example self-contained
# ---------------------------------------------------------------------------


def test_example_module_exposes_expected_top_level_names():
    """The example must expose ``main``, ``lookup_company``, and
    ``_build_mock_provider`` — they're the seams tests and downstream
    code can rely on."""

    importlib.reload(example)
    for name in ("main", "lookup_company", "_build_mock_provider"):
        assert hasattr(example, name), f"missing top-level name: {name}"


# ---------------------------------------------------------------------------
# Subprocess smoke — keep the example runnable as a CLI
# ---------------------------------------------------------------------------


def test_example_runs_in_mock_mode_via_subprocess(tmp_path):
    """Run ``python -m mantis_agent.examples.fireworks_hosted`` in mock
    mode as a subprocess. Catches regressions where in-process tests
    pass but the example's ``if __name__ == '__main__'`` path is broken
    (e.g. asyncio.run + nested loop interactions, sys.exit codes, etc.).
    """

    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["MANTIS_AGENT_MOCK"] = "1"
    env["MANTIS_AGENT_HOME"] = str(tmp_path / "mantis_agent_home")
    env.pop("FIREWORKS_API_KEY", None)
    Path(env["MANTIS_AGENT_HOME"]).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mantis_agent.examples.fireworks_hosted",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        # Surface logs on failure so debugging is one scroll away.
        print("---- stdout ----")
        print(result.stdout)
        print("---- stderr ----")
        print(result.stderr)

    assert result.returncode == 0, "example exited non-zero in mock mode"
    assert "mock-mode smoke test passed" in result.stdout, (
        "example didn't reach its end-of-main success line"
    )
    assert "Spawn Labs builds AI agents" in result.stdout, (
        "tool result didn't flow through to the parent's final text"
    )


def test_example_subprocess_without_api_key_exits_nonzero(tmp_path):
    """Real Fireworks mode without ``FIREWORKS_API_KEY`` must exit
    non-zero with a hint on stderr — protect users from accidentally
    burning quota on a hung run."""

    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.pop("MANTIS_AGENT_MOCK", None)
    env.pop("FIREWORKS_API_KEY", None)
    env["MANTIS_AGENT_HOME"] = str(tmp_path / "mantis_agent_home")
    Path(env["MANTIS_AGENT_HOME"]).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mantis_agent.examples.fireworks_hosted",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0, (
        "example exited 0 with no API key — should have raised SystemExit"
    )
    combined = result.stdout + result.stderr
    assert "FIREWORKS_API_KEY" in combined
    assert "MANTIS_AGENT_MOCK=1" in combined
