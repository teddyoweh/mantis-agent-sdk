"""The shift+tab footer modes must actually gate tool execution (Claude-Code
parity), not just decorate the footer. ``MantisTUI._permit`` is the callback the
agent's permission system consults per tool call; it reads the live mode index.
"""

from __future__ import annotations

import anyio

from mantis_agent.permissions import Allow, Ask, Deny
from mantis_agent.tui import MODES, MantisTUI, permission_mode_label, permission_mode_summary


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


def test_god_mode_allows_everything() -> None:
    tui = _tui()
    assert isinstance(
        _decide(tui, "god mode on", _FakeTool("write_file", False)), Allow
    )


def test_build_agent_registers_web_tools() -> None:
    names = {t.name for t in _tui()._build_agent().tools}
    assert {"web_search", "web_fetch"} <= names
    assert {"bash", "read_file", "write_file", "edit_file"} <= names


def test_remember_tool_registered() -> None:
    # remember is a big-model tool — small local models get the slim belt
    # (7B-class models were saving junk memories), so build with a big model.
    big = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                    system=None, max_tokens=64, temperature=0.2, max_turns=4)
    names = {t.name for t in big._build_agent().tools}
    assert "remember" in names
    slim_names = {t.name for t in _tui()._build_agent().tools}
    assert "remember" not in slim_names


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


def test_permission_mode_label_accepts_cli_and_settings_spellings() -> None:
    assert permission_mode_label("acceptEdits") == "accept edits on"
    assert permission_mode_label("accept-edits") == "accept edits on"
    assert permission_mode_label("plan") == "plan mode on"
    assert permission_mode_label("bypass permissions on") == "god mode on"
    assert permission_mode_label("godmode") == "god mode on"
    assert permission_mode_label("future-mode") is None


def test_permission_mode_summary_explains_god_mode() -> None:
    assert "including dangerous shell commands" in permission_mode_summary(
        "god mode on"
    )
    assert "including dangerous shell commands" in permission_mode_summary(
        "god mode on", force_bypass=True
    )


def test_settings_permission_mode_sets_initial_footer(monkeypatch, tmp_path) -> None:
    import json

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"permission_mode": "acceptEdits"}))
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))

    tui = _tui()
    assert MODES[tui.mode_idx][0] == "accept edits on"


def test_cli_permission_mode_helper_overrides_settings(monkeypatch, tmp_path) -> None:
    import json

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"permission_mode": "acceptEdits"}))
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))

    tui = _tui()
    assert tui._apply_initial_permission_mode("plan") is True
    assert MODES[tui.mode_idx][0] == "plan mode on"


def test_tui_model_knobs_flow_to_agent_extra() -> None:
    tui = MantisTUI(
        model="gpt-5.6-sol",
        backend="https://api.openai.com/v1",
        api_key="k",
        system=None,
        max_tokens=1,
        temperature=None,
        max_turns=1,
        effort="xhigh",
        verbosity="high",
        reasoning_mode="pro",
    )
    assert tui._model_extra() == {
        "effort": "xhigh",
        "verbosity": "high",
        "reasoning_mode": "pro",
    }
    agent = tui._build_agent()
    assert agent.extra == tui._model_extra()


def test_tui_knobs_command_updates_agent_extra() -> None:
    tui = _tui()
    tui._cmd_knobs("effort=xhigh verbosity=high reasoning=pro")
    assert tui.effort == "xhigh"
    assert tui.verbosity == "high"
    assert tui.reasoning_mode == "pro"
    assert tui.agent.extra == {
        "effort": "xhigh",
        "verbosity": "high",
        "reasoning_mode": "pro",
    }
    tui._cmd_knobs("effort=off verbosity=off reasoning=off")
    assert tui.agent.extra == {}
