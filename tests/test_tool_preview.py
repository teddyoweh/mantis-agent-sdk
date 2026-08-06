"""The live-agent feed: what a subagent is doing, not just which tool it called.

The inspector used to render five identical `tool grep` lines — no pattern, no
file, no result. These lock in the replacement: verb + the salient argument +
a shape-only summary of what came back.
"""

from __future__ import annotations

from mantis_agent.tool_preview import (
    TOOL_VERBS,
    tool_arg_preview,
    tool_call_preview,
    tool_result_preview,
)


# -- the argument that matters ----------------------------------------------


def test_the_salient_argument_is_picked_per_tool() -> None:
    assert tool_arg_preview("read_file", {"path": "a/b.py"}) == "a/b.py"
    assert tool_arg_preview("grep", {"pattern": "def foo"}) == "def foo"
    assert tool_arg_preview("bash", {"command": "pytest -q"}) == "pytest -q"
    assert tool_arg_preview("web_fetch", {"url": "https://x.dev"}) == "https://x.dev"


def test_alternate_key_spellings_are_accepted() -> None:
    """Tools disagree on path vs file_path; the feed shouldn't go blank for it."""
    assert tool_arg_preview("read_file", {"file_path": "a.py"}) == "a.py"


def test_an_unknown_tool_still_finds_a_target() -> None:
    assert tool_arg_preview("some_mcp_tool", {"query": "widgets"}) == "widgets"
    assert tool_arg_preview("some_mcp_tool", {"nothing_useful": 1}) == ""


def test_arguments_are_flattened_and_clipped() -> None:
    """A multi-line command must not blow the panel open."""
    out = tool_arg_preview("bash", {"command": "line1\n  line2\n\n  line3"}, limit=40)
    assert "\n" not in out
    assert out.startswith("line1 line2")

    long = tool_arg_preview("grep", {"pattern": "x" * 300}, limit=20)
    assert len(long) == 20 and long.endswith("…")


def test_no_arguments_is_empty_not_a_crash() -> None:
    assert tool_arg_preview("todo_write", {}) == ""
    assert tool_arg_preview("todo_write", None) == ""


# -- what came back ----------------------------------------------------------


def test_counting_results_reads_naturally() -> None:
    assert tool_result_preview("grep", ["a", "b", "c"]) == "3 matches"
    assert tool_result_preview("grep", ["a"]) == "1 match"       # not "1 matches"
    assert tool_result_preview("glob", ["a.py", "b.py"]) == "2 files"
    assert tool_result_preview("ls", []) == "0 entries"


def test_sibilant_units_pluralize_correctly() -> None:
    """'3 matchs' is the kind of thing that makes a UI look unfinished."""
    assert tool_result_preview("grep", ["a", "b", "c"]).endswith("matches")


def test_multiline_text_reports_its_size() -> None:
    body = "\n".join(f"line {i}" for i in range(4157))
    assert tool_result_preview("read_file", body) == "4157 lines"
    # a grep's newline-delimited hits count as matches, not lines
    assert tool_result_preview("grep", "a.py:1\nb.py:2") == "2 matches"


def test_short_output_is_shown_verbatim() -> None:
    assert tool_result_preview("bash", "24 passed in 0.08s") == "24 passed in 0.08s"


def test_errors_are_surfaced_not_counted() -> None:
    """An error is the most useful thing in the feed — it must not render as
    an ordinary '1 line' result."""
    assert "404" in tool_result_preview("web_fetch", "Error: 404 not found")
    assert tool_result_preview("read_file", "No such file: x.py").startswith("No such file")


def test_empty_and_missing_results() -> None:
    assert tool_result_preview("bash", "") == "empty"
    assert tool_result_preview("bash", "   ") == "empty"
    assert tool_result_preview("bash", None) == ""      # still in flight


def test_non_string_results_degrade_sanely() -> None:
    assert tool_result_preview("x", True) == "ok"
    assert tool_result_preview("x", False) == "failed"
    assert "k" in tool_result_preview("x", {"k": 1})
    assert tool_result_preview("x", 42) == "42"


# -- the assembled line ------------------------------------------------------


def test_in_flight_has_no_arrow() -> None:
    """A slow grep should read as running, not as having returned nothing."""
    line = tool_call_preview("grep", {"pattern": "needle"}, done=False)
    assert line == "Search needle"
    assert "→" not in line


def test_completed_line_is_the_in_flight_line_plus_the_result() -> None:
    args = {"pattern": "needle"}
    head = tool_call_preview("grep", args, done=False)
    done = tool_call_preview("grep", args, result=["a", "b"], done=True)
    assert done == f"{head} → 2 matches"


def test_every_verb_is_titlecased_and_short() -> None:
    """These render in a narrow panel; a long verb eats the argument."""
    for name, (verb, _keys) in TOOL_VERBS.items():
        assert verb and verb[0].isupper(), name
        assert len(verb) <= 14, name


# -- the progress sink that feeds the panel ---------------------------------


def _sink():
    from mantis_agent.tui import MantisTUI

    tui = MantisTUI.__new__(MantisTUI)
    tui._live_subagents = {}
    return tui


def _tool(tui, rid, name, args, **done):
    from mantis_agent.tui import MantisTUI

    arg = tool_arg_preview(name, args)
    MantisTUI._subagent_progress(
        tui, {"id": rid, "phase": "tool", "tool": name, "arg": arg, "args": args})
    MantisTUI._subagent_progress(
        tui, {"id": rid, "phase": "tool_done", "tool": name, "arg": arg, **done})


def test_a_call_is_one_feed_line_updated_in_place() -> None:
    """Start and finish are two events; showing them as two lines would halve
    the useful history in a five-row panel."""
    tui = _sink()
    from mantis_agent.tui import MantisTUI

    MantisTUI._subagent_progress(tui, {"id": 1, "phase": "start", "type": "explore"})
    _tool(tui, 1, "grep", {"pattern": "needle"}, result=["a", "b"])

    rec = tui._live_subagents[1]
    assert rec["tools"] == 1                      # not double-counted
    assert len(rec["events"]) == 1                # not two lines
    assert rec["events"][0][1] == "Search needle → 2 matches"
    assert rec["last_event"] == "Search needle → 2 matches"


def test_the_feed_says_what_each_call_did() -> None:
    tui = _sink()
    from mantis_agent.tui import MantisTUI

    MantisTUI._subagent_progress(tui, {"id": 1, "phase": "start", "type": "explore"})
    _tool(tui, 1, "read_file", {"path": "mantis_agent/tui.py"},
          result="\n".join(["x"] * 4157))
    _tool(tui, 1, "bash", {"command": "pytest -q"}, result="24 passed")

    lines = [text for _ts, text in tui._live_subagents[1]["events"]]
    assert lines == [
        "Read mantis_agent/tui.py → 4157 lines",
        "Run pytest -q → 24 passed",
    ]


def test_a_raising_tool_shows_its_error() -> None:
    """The failure is the whole reason you opened the inspector."""
    tui = _sink()
    from mantis_agent.tui import MantisTUI

    MantisTUI._subagent_progress(tui, {"id": 1, "phase": "start", "type": "explore"})
    _tool(tui, 1, "read_file", {"path": "gone.py"},
          error="FileNotFoundError: gone.py")

    assert tui._live_subagents[1]["events"][0][1] == (
        "Read gone.py → FileNotFoundError: gone.py")


def test_an_in_flight_call_appears_before_it_returns() -> None:
    tui = _sink()
    from mantis_agent.tui import MantisTUI

    MantisTUI._subagent_progress(tui, {"id": 1, "phase": "start", "type": "explore"})
    MantisTUI._subagent_progress(tui, {
        "id": 1, "phase": "tool", "tool": "grep", "arg": "slow pattern",
        "args": {"pattern": "slow pattern"}})
    rec = tui._live_subagents[1]
    assert rec["events"][0][1] == "Search slow pattern"
    assert "→" not in rec["last_event"]


def test_progress_for_an_unknown_agent_is_ignored() -> None:
    """Events can arrive after the agent finished; that must not raise."""
    tui = _sink()
    from mantis_agent.tui import MantisTUI

    MantisTUI._subagent_progress(tui, {"id": 99, "phase": "tool", "tool": "grep"})
    MantisTUI._subagent_progress(tui, {"id": 99, "phase": "tool_done", "tool": "grep"})
    assert tui._live_subagents == {}
