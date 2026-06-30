"""Anti-runaway guards: a stuck model must not burn the whole step budget (and
minutes of wall-clock) re-running the same failing call, and edit misses must be
actionable enough that the model self-corrects instead of looping."""

from __future__ import annotations

import anyio

from mantis_agent.agent import Agent
from mantis_agent.builtin_tools import edit_file, multi_edit
from mantis_agent.providers.mock import MockProvider
from mantis_agent.tools import ToolRegistry, tool
from mantis_agent.types import ToolUseBlock


def _agent(max_repeats: int = 3) -> Agent:
    @tool(name="spin")
    async def spin(x: int) -> str:
        return "ok"

    reg = ToolRegistry()
    reg.add(spin)
    return Agent(
        model="mock",
        provider=MockProvider(),
        tools=reg,
        max_repeated_tool_calls=max_repeats,
        include_memory=False,
    )


def test_identical_calls_short_circuit_after_threshold() -> None:
    ag = _agent(max_repeats=3)
    ag._run_call_sigs = {}

    async def run() -> list[bool]:
        out = []
        for i in range(6):
            call = ToolUseBlock(id=f"c{i}", name="spin", input={"x": 1})
            _, sc = await ag._preflight_call(call, [])
            out.append(sc is not None)
        await ag.aclose()
        return out

    short = anyio.run(run)
    # First 3 allowed, 4th onward short-circuited.
    assert short == [False, False, False, True, True, True]


def test_different_inputs_are_not_throttled() -> None:
    ag = _agent(max_repeats=3)
    ag._run_call_sigs = {}

    async def run() -> list[bool]:
        out = []
        for i in range(6):
            call = ToolUseBlock(id=f"c{i}", name="spin", input={"x": i})
            _, sc = await ag._preflight_call(call, [])
            out.append(sc is not None)
        await ag.aclose()
        return out

    # Every call has distinct input → none throttled.
    assert anyio.run(run) == [False] * 6


def test_guard_disabled_when_zero() -> None:
    ag = _agent(max_repeats=0)
    ag._run_call_sigs = {}

    async def run() -> list[bool]:
        out = []
        for i in range(6):
            call = ToolUseBlock(id=f"c{i}", name="spin", input={"x": 1})
            _, sc = await ag._preflight_call(call, [])
            out.append(sc is not None)
        await ag.aclose()
        return out

    assert anyio.run(run) == [False] * 6


def test_edit_miss_is_actionable(tmp_path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def hello():\n    return 1\n")
    try:
        anyio.run(edit_file.fn, str(f), "def  hello( ):", "x")
        raised = ""
    except ValueError as e:
        raised = str(e)
    assert "Read the file again" in raised
    # Points at the nearest real line so the model can fix it in one step.
    assert "def hello():" in raised


def test_multi_edit_miss_is_actionable(tmp_path) -> None:
    f = tmp_path / "m.py"
    f.write_text("alpha\nbeta\n")
    try:
        anyio.run(multi_edit.fn, str(f), [{"old_string": "GAMMA", "new_string": "x"}])
        raised = ""
    except ValueError as e:
        raised = str(e)
    assert "Read the file again" in raised
