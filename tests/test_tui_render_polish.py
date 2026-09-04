"""Rendering polish: collapsed reasoning, boxed provider errors, width-safe tool
results, the tool-timed spinner, and the tool-header audit."""

from __future__ import annotations

import re

import pytest
from rich.console import Console

from mantis_agent.errors import AuthError, RateLimitError
from mantis_agent.tool_preview import TOOL_VERBS
from mantis_agent.tui import (
    _RESULT_CAP,
    MantisTUI,
    _Thinking,
    classify_error,
    error_box,
    thinking_summary,
    thinking_tokens,
)
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def _tui(width: int = 80) -> MantisTUI:
    t = MantisTUI(model="gpt-5.6", backend="https://api.openai.com/v1", api_key="sk-x",
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.console = Console(width=width, record=True, force_terminal=False, color_system=None)
    return t


_REASONING = "Let me reason about this carefully.\n" + "\n".join(
    f"Step {i}: consider case {i}." for i in range(40))


# -- thinking: collapsed by default, /thinking show|hide ---------------------------


def test_thinking_collapses_to_one_dim_line_by_default() -> None:
    t = _tui()
    t._render_assistant(AssistantMessage(content=[
        ThinkingBlock(thinking=_REASONING), TextBlock(text="The answer is 42.")]), ToolUseBlock)
    out = t.console.export_text()
    lines = [ln for ln in out.splitlines() if "thinking" in ln]
    assert len(lines) == 1
    assert re.search(r"✻ thinking \(\d+ tokens\)", lines[0])
    assert "Let me reason" in lines[0]            # the opening words
    assert "/thinking show" in lines[0]           # how to see the rest
    assert "Step 30" not in out                   # not dumped
    assert "42" in out
    assert max(len(ln.rstrip()) for ln in out.splitlines()) <= 80


def test_thinking_show_expands_the_capped_block() -> None:
    t = _tui()
    t._cmd_thinking("show")
    t._render_assistant(AssistantMessage(content=[ThinkingBlock(thinking=_REASONING)]), ToolUseBlock)
    out = t.console.export_text()
    assert "Step 5" in out
    assert "more line" in out                     # still capped, with the count


def test_thinking_hide_drops_the_block_entirely() -> None:
    t = _tui()
    t._cmd_thinking("hide")
    assert "hidden" in t.console.export_text(clear=True)
    t._render_assistant(AssistantMessage(content=[
        ThinkingBlock(thinking=_REASONING), TextBlock(text="done")]), ToolUseBlock)
    out = t.console.export_text()
    assert "thinking" not in out and "Step" not in out
    assert "done" in out


def test_thinking_command_reports_and_validates() -> None:
    t = _tui()
    t._cmd_thinking("")
    assert "collapsed" in t.console.export_text()
    t._cmd_thinking("bogus")
    assert "usage: /thinking show|hide|collapse" in t.console.export_text()
    t._cmd_thinking("collapse")
    assert t.show_thinking == "collapsed"


def test_thinking_summary_counts_tokens_and_cuts_to_width() -> None:
    assert thinking_tokens("") == 0
    assert thinking_tokens("x" * 400) == 100
    line = thinking_summary("word " * 200, 60)
    assert line is not None and len(line.plain) <= 60
    assert thinking_summary("   \n ") is None


# -- provider errors: one box, the fix, never a traceback ----------------------------


def _boxed(err: BaseException, backend: str = "https://api.openai.com/v1", width: int = 80) -> str:
    c = Console(width=width, record=True, force_terminal=False, color_system=None)
    c.print(error_box(err, backend, "gpt-5.6", width))
    return c.export_text()


def test_401_is_an_auth_box_with_the_fix() -> None:
    out = _boxed(AuthError("HTTP 401 Unauthorized: invalid api key"))
    assert "✗ auth failed (401)" in out
    assert "mantis setup" in out and "/models" in out
    assert "Traceback" not in out
    assert out.count("╭") == 1 and out.count("╰") == 1


def test_404_429_and_overflow_are_classified() -> None:
    assert classify_error(Exception("HTTP 404: model 'nope' does not exist"))[0] == "not found (404)"
    assert classify_error(RateLimitError("429 too many requests"))[0] == "rate limited (429)"
    assert classify_error(Exception("this model's maximum context length is 8192 tokens"))[0] == "context overflow"
    assert classify_error(ConnectionError("connection refused"))[0] == "backend unreachable"
    assert classify_error(Exception("HTTP 503 upstream"))[0] == "server error (503)"
    assert classify_error(ValueError("plain"))[0] == "ValueError"


def test_rate_limit_box_names_the_wait() -> None:
    err = RateLimitError("429")
    err.retry_after_s = 12
    out = _boxed(err)
    assert "rate limited (429)" in out and "retry in ~12s" in out


def test_error_box_never_wraps_or_leaks_a_traceback() -> None:
    err = Exception("HTTP 401 " + "x" * 500 + "\nTraceback (most recent call last):\n  File ...")
    out = _boxed(err, width=80)
    assert all(len(ln) <= 80 for ln in out.splitlines())
    assert "File ..." not in out
    assert len(out.splitlines()) <= 6


def test_tui_print_error_uses_the_box() -> None:
    t = _tui()
    t._print_error(AuthError("401 bad key"))
    out = t.console.export_text()
    assert "✗ auth failed (401)" in out and "→" in out


# -- tool results: capped and width-safe ----------------------------------------------


def _result(text: str, is_error: bool = False) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id="c1", content=text, is_error=is_error)])


def test_tool_result_lines_never_wrap_at_80_columns() -> None:
    t = _tui(80)
    body = "\n".join(f"line {i} " + "x" * 150 for i in range(3))
    t._render_tool_results(_result(body), ToolResultBlock)
    out = t.console.export_text().splitlines()
    assert len([ln for ln in out if ln.strip()]) == 3
    assert all(len(ln) <= 80 for ln in out)
    assert all(ln.rstrip().endswith("…") for ln in out if ln.strip())


def test_tool_result_is_capped_with_a_count() -> None:
    t = _tui(80)
    body = "\n".join(f"row {i}" for i in range(_RESULT_CAP + 8))
    t._render_tool_results(_result(body), ToolResultBlock)
    out = t.console.export_text()
    assert f"row {_RESULT_CAP}" in out and f"row {_RESULT_CAP + 1}" not in out
    assert "… +7 more lines" in out and "ctrl+o" in out


def test_tool_result_one_extra_line_is_singular() -> None:
    t = _tui(80)
    body = "\n".join(f"row {i}" for i in range(_RESULT_CAP + 2))
    t._render_tool_results(_result(body), ToolResultBlock)
    assert "… +1 more line " in t.console.export_text()


# -- tool call headers: every builtin has a verb + a tight target ------------------------


@pytest.mark.parametrize(("name", "args", "verb", "target"), [
    ("bash", {"command": "pytest -q"}, "Run", "pytest -q"),
    ("read_file", {"path": "a.py"}, "Read", "a.py"),
    ("write_file", {"path": "b.py", "content": "x"}, "Write", "b.py"),
    ("edit_file", {"path": "c.py"}, "Edit", "c.py"),
    ("multi_edit", {"path": "d.py"}, "Edit", "d.py"),
    ("ls", {"path": "src"}, "List", "src"),
    ("glob", {"pattern": "**/*.py"}, "Find", "**/*.py"),
    ("grep", {"pattern": "def x"}, "Search", "def x"),
    ("web_search", {"query": "rich panel"}, "Search web", "rich panel"),
    ("web_fetch", {"url": "https://x.y"}, "Fetch", "https://x.y"),
])
def test_builtin_headers_are_one_tight_line(name: str, args: dict, verb: str, target: str) -> None:
    t = _tui(80)
    assert t._tool_label(name, args) == (verb, target)
    t._render_assistant(AssistantMessage(content=[ToolUseBlock(id="c", name=name, input=args)]),
                        ToolUseBlock)
    out = [ln for ln in t.console.export_text().splitlines() if ln.strip()]
    assert len(out) == 1
    assert out[0].startswith(f"⚒ {verb} {target}")


def test_long_bash_command_header_stays_on_one_line() -> None:
    t = _tui(80)
    cmd = "for f in $(ls); do echo $f; " * 10
    t._render_assistant(AssistantMessage(content=[ToolUseBlock(id="c", name="bash",
                                                               input={"command": cmd})]), ToolUseBlock)
    out = [ln for ln in t.console.export_text().splitlines() if ln.strip()]
    assert len(out) == 1 and len(out[0]) <= 80 and out[0].endswith("…")


def test_every_builtin_is_in_the_verb_table() -> None:
    for name in ("bash", "read_file", "write_file", "edit_file", "multi_edit", "ls", "glob",
                 "grep", "web_search", "web_fetch"):
        assert name in TOOL_VERBS


# -- the spinner times a running tool ----------------------------------------------


def test_spinner_label_starts_its_own_clock() -> None:
    sp = _Thinking()
    assert sp._label_at is None

    async def go() -> None:
        sp.start(label="⚒ Run pytest -q")
        assert sp._label_at is not None
        await sp.stop()
        sp.start()
        assert sp._label_at is None
        await sp.stop()

    import asyncio
    asyncio.run(go())
