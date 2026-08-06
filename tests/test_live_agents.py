from __future__ import annotations

from mantis_agent.tui import MantisTUI


def _tui() -> MantisTUI:
    return MantisTUI(model="mock", backend="mock", api_key=None,
                     system=None, max_tokens=100, temperature=None, max_turns=10)


def test_subagent_progress_tracks_last_tool_and_events() -> None:
    tui = _tui()

    tui._subagent_progress({"id": 7, "phase": "start", "type": "general-purpose", "desc": "Audit CORS"})
    tui._subagent_progress({"id": 7, "phase": "tool", "tool": "grep",
                            "arg": "Access-Control-Allow"})

    rec = tui._live_subagents[7]
    assert rec["tools"] == 1
    assert rec["last_tool"] == "grep"
    # The feed says WHAT is being searched for, not just that grep ran.
    assert rec["last_event"] == "Search Access-Control-Allow"
    assert rec["events"]


def test_an_argless_progress_event_still_names_the_tool() -> None:
    """Not every emitter supplies an argument (older jobs, MCP tools with
    nothing salient) — the line must degrade to the verb, not to blank."""
    tui = _tui()
    tui._subagent_progress({"id": 8, "phase": "start", "type": "explore", "desc": "x"})
    tui._subagent_progress({"id": 8, "phase": "tool", "tool": "grep"})

    assert tui._live_subagents[8]["last_event"] == "Search"


def test_live_agents_command_renders_running_delegates(capsys) -> None:
    tui = _tui()
    tui._subagent_progress({"id": 3, "phase": "start", "type": "explore", "desc": "Probe endpoints"})
    tui._subagent_progress({"id": 3, "phase": "tool", "tool": "read_file",
                            "arg": "routes.py"})
    tui._subagent_progress({"id": 3, "phase": "tool_done", "tool": "read_file",
                            "arg": "routes.py", "result": "a\nb\nc"})

    tui._cmd_live_agents("watch")

    out = capsys.readouterr().out
    assert "Live subagents" in out
    assert "#3" in out
    assert "Probe endpoints" in out
    assert "Read routes.py" in out
    assert "3 lines" in out


def test_subagent_end_removes_live_record() -> None:
    tui = _tui()
    tui._subagent_progress({"id": 1, "phase": "start", "type": "explore", "desc": "x"})
    tui._subagent_progress({"id": 1, "phase": "end"})

    assert tui._live_subagents == {}
