"""The shift+tab footer modes must actually gate tool execution (Claude-Code
parity), not just decorate the footer. ``MantisTUI._permit`` is the callback the
agent's permission system consults per tool call; it reads the live mode index.
"""

from __future__ import annotations

import anyio

from mantis_agent.permissions import Allow, Ask, Deny
from mantis_agent.tui import MODES, MantisTUI


def _tui() -> MantisTUI:
    return MantisTUI(
        model="qwen2.5-coder:7b",
        backend="http://localhost:11434",
        api_key=None,
        system=None,
        max_tokens=1,
        temperature=None,
        max_turns=1,
    )


def _mode(name: str) -> int:
    return [m[0] for m in MODES].index(name)


class _FakeTool:
    def __init__(self, name: str, is_read_only: bool) -> None:
        self.name = name
        self.is_read_only = is_read_only


def _decide(tui: MantisTUI, mode_name: str, tool: _FakeTool) -> object:
    tui.mode_idx = _mode(mode_name)
    return anyio.run(tui._permit, tool, {}, None)


def test_plan_mode_blocks_mutating_tools() -> None:
    tui = _tui()
    assert isinstance(_decide(tui, "plan mode on", _FakeTool("bash", False)), Deny)
    assert isinstance(_decide(tui, "plan mode on", _FakeTool("write_file", False)), Deny)
    assert isinstance(_decide(tui, "plan mode on", _FakeTool("edit_file", False)), Deny)


def test_plan_mode_allows_read_only_tools() -> None:
    tui = _tui()
    assert isinstance(_decide(tui, "plan mode on", _FakeTool("ls", True)), Allow)
    assert isinstance(_decide(tui, "plan mode on", _FakeTool("read_file", True)), Allow)
    assert isinstance(_decide(tui, "plan mode on", _FakeTool("grep", True)), Allow)


def test_default_mode_asks_for_mutating_allows_readonly() -> None:
    # T0.2: default mode no longer silently allows mutating tools — it asks.
    tui = _tui()
    assert isinstance(_decide(tui, "default", _FakeTool("bash", False)), Ask)
    assert isinstance(_decide(tui, "default", _FakeTool("write_file", False)), Ask)
    assert isinstance(_decide(tui, "default", _FakeTool("read_file", True)), Allow)


def test_accept_edits_allows_edits_asks_bash() -> None:
    tui = _tui()
    assert isinstance(_decide(tui, "accept edits on", _FakeTool("write_file", False)), Allow)
    assert isinstance(_decide(tui, "accept edits on", _FakeTool("edit_file", False)), Allow)
    assert isinstance(_decide(tui, "accept edits on", _FakeTool("bash", False)), Ask)


def test_bypass_mode_allows_everything() -> None:
    tui = _tui()
    assert isinstance(
        _decide(tui, "bypass permissions on", _FakeTool("write_file", False)), Allow
    )


def test_build_agent_registers_web_tools() -> None:
    names = {t.name for t in _tui()._build_agent().tools}
    assert {"web_search", "web_fetch"} <= names
    assert {"bash", "read_file", "write_file", "edit_file"} <= names


def test_remember_tool_registered() -> None:
    names = {t.name for t in _tui()._build_agent().tools}
    assert "remember" in names


def test_godmode_allows_dangerous_without_prompt() -> None:
    # --godmode / --dangerously-skip-permissions → engine-level bypass: every
    # tool is allowed with no prompt, overriding even the dangerous-command gate.
    from mantis_agent.builtin_tools.fs import CODING_TOOLS
    from mantis_agent.permissions import Allow, check_permission

    bash = next(t for t in CODING_TOOLS if t.name == "bash")

    normal = _tui()._build_agent()
    decision = anyio.run(
        lambda: check_permission(bash, {"command": "rm -rf /tmp/x"}, normal.permissions)
    )
    assert not isinstance(decision, Allow)  # default mode gates a dangerous command

    god = _tui()
    god.force_bypass = True
    agent = god._build_agent()
    assert agent.permissions.mode == "bypass"
    decision = anyio.run(
        lambda: check_permission(bash, {"command": "rm -rf /tmp/x"}, agent.permissions)
    )
    assert isinstance(decision, Allow)  # godmode runs it with no prompt
