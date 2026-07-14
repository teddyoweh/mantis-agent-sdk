from __future__ import annotations

from mantis_agent.tui import MantisTUI


def _tui() -> MantisTUI:
    return MantisTUI(model="mock", backend="mock", api_key=None,
                     system=None, max_tokens=100, temperature=None, max_turns=10)


def test_subagent_progress_tracks_last_tool_and_events() -> None:
    tui = _tui()

    tui._subagent_progress({"id": 7, "phase": "start", "type": "general-purpose", "desc": "Audit CORS"})
    tui._subagent_progress({"id": 7, "phase": "tool", "tool": "grep"})

    rec = tui._live_subagents[7]
    assert rec["tools"] == 1
    assert rec["last_tool"] == "grep"
    assert rec["last_event"] == "tool grep"
    assert rec["events"]


def test_live_agents_command_renders_running_delegates(capsys) -> None:
    tui = _tui()
    tui._subagent_progress({"id": 3, "phase": "start", "type": "explore", "desc": "Probe endpoints"})
    tui._subagent_progress({"id": 3, "phase": "tool", "tool": "read_file"})

    tui._cmd_live_agents("watch")

    out = capsys.readouterr().out
    assert "Live subagents" in out
    assert "#3" in out
    assert "Probe endpoints" in out
    assert "tool read_file" in out


def test_subagent_end_removes_live_record() -> None:
    tui = _tui()
    tui._subagent_progress({"id": 1, "phase": "start", "type": "explore", "desc": "x"})
    tui._subagent_progress({"id": 1, "phase": "end"})

    assert tui._live_subagents == {}
