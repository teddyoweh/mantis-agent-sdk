"""Plan-mode approval handoff (T1.6): exit_plan_mode presents a plan, and on
approval the harness lifts plan mode so the agent can edit."""

from __future__ import annotations

import anyio

from mantis_agent.builtin_tools.plan import make_exit_plan_mode
from mantis_agent.tui import MODES, MantisTUI


def _plan_idx() -> int:
    return [m[0] for m in MODES].index("plan mode on")


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


# -- the tool --------------------------------------------------------------


def test_tool_delegates_to_presenter() -> None:
    async def presenter(plan):
        return f"got plan of {len(plan)} chars"

    tool = make_exit_plan_mode(presenter)
    assert tool.name == "exit_plan_mode"
    assert tool.is_read_only is True  # allowed through the plan-mode gate
    out = anyio.run(lambda: tool.fn(plan="do X then Y"))
    assert "got plan of 11 chars" in out


def test_tool_empty_plan() -> None:
    tool = make_exit_plan_mode(lambda p: None)
    out = anyio.run(lambda: tool.fn(plan="  "))
    assert "No plan" in out


def test_tool_headless_no_presenter() -> None:
    tool = make_exit_plan_mode(None)
    out = anyio.run(lambda: tool.fn(plan="a real plan"))
    assert "proceed" in out.lower()


# -- the TUI handoff -------------------------------------------------------


def test_registered_in_terminal() -> None:
    names = {t.name for t in _tui()._build_agent().tools}
    assert "exit_plan_mode" in names


def test_not_in_plan_mode_just_proceeds() -> None:
    tui = _tui()
    tui.mode_idx = 0  # default, not plan mode
    out = anyio.run(lambda: tui._exit_plan_mode("some plan"))
    assert "not in plan mode" in out.lower()


def test_approval_lifts_plan_mode() -> None:
    tui = _tui()
    tui.mode_idx = _plan_idx()

    async def fake_fs_plan(plan):
        # simulate the fullscreen presenter approving + flipping the mode
        tui.mode_idx = 0
        return "Plan approved. Plan mode is now OFF — proceed."

    tui._fs_plan = fake_fs_plan
    out = anyio.run(lambda: tui._exit_plan_mode("do the thing"))
    assert "approved" in out.lower()
    assert tui.mode_idx == 0  # plan mode lifted


def test_reject_stays_in_plan_mode() -> None:
    tui = _tui()
    tui.mode_idx = _plan_idx()

    async def fake_fs_plan(plan):
        return "The user did not approve the plan yet. Stay in plan mode."

    tui._fs_plan = fake_fs_plan
    out = anyio.run(lambda: tui._exit_plan_mode("do the thing"))
    assert "did not approve" in out.lower()
    assert tui.mode_idx == _plan_idx()  # still in plan mode
