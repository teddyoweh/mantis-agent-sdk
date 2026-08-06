"""Thinking-block rendering in the TUI — reasoning models' <think> shown dimmed."""

from __future__ import annotations

from mantis_agent.tui import _THINK_CAP, MantisTUI, _thinking_lines
from mantis_agent.types import AssistantMessage, TextBlock, ThinkingBlock, ToolUseBlock


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


def _plain(renderable) -> str:
    """Body lines are Padding-wrapped so wrapped prose keeps its indent; the
    header and the elision note stay bare Text."""
    inner = getattr(renderable, "renderable", renderable)
    return inner.plain


def test_thinking_lines_header_and_body() -> None:
    lines = _thinking_lines("first thought\nsecond thought")
    text = [_plain(ln) for ln in lines]
    assert text[0] == "✻ thinking"
    assert "first thought" in text and "second thought" in text


def test_thinking_lines_empty() -> None:
    assert _thinking_lines("") == []
    assert _thinking_lines("   \n  ") == []


def test_thinking_lines_capped() -> None:
    lines = _thinking_lines("\n".join(str(i) for i in range(30)))
    # header + _THINK_CAP body lines + one elision note
    assert len(lines) == _THINK_CAP + 2
    assert "more lines" in _plain(lines[-1])


def test_render_shows_thinking_and_answer() -> None:
    tui = _tui()
    msg = AssistantMessage(content=[
        ThinkingBlock(thinking="Let me reason.\nStep 1."),
        TextBlock(text="The answer is 42."),
    ])
    with tui.console.capture() as cap:
        had_tool = tui._render_assistant(msg, ToolUseBlock)
    out = cap.get()
    assert "thinking" in out and "Step 1" in out   # reasoning shown
    assert "42" in out                              # answer shown
    assert had_tool is False


def test_render_thinking_then_tool_call() -> None:
    tui = _tui()
    msg = AssistantMessage(content=[
        ThinkingBlock(thinking="I should read the file."),
        ToolUseBlock(id="c1", name="read_file", input={"path": "x.py"}),
    ])
    with tui.console.capture() as cap:
        had_tool = tui._render_assistant(msg, ToolUseBlock)
    out = cap.get()
    assert "thinking" in out and "read the file" in out
    assert had_tool is True                         # tool call still detected


def test_capped_reasoning_points_at_the_expand_view() -> None:
    """Capped tool output says "(ctrl+o to expand)"; reasoning made the same
    promise ("… N more lines") without saying where the rest lived."""
    lines = _thinking_lines("\n".join(f"line {i}" for i in range(30)))
    assert "(ctrl+o to expand)" in _plain(lines[-1])
    # …and the header says how much reasoning there was in total.
    assert f"({30} lines)" in _plain(lines[0])


def test_short_reasoning_gets_no_count_and_no_hint() -> None:
    lines = _thinking_lines("one\ntwo")
    assert _plain(lines[0]) == "✻ thinking"
    assert not any("ctrl+o" in _plain(ln) for ln in lines)


def test_one_hidden_line_reads_as_singular() -> None:
    lines = _thinking_lines("\n".join(f"l{i}" for i in range(_THINK_CAP + 1)))
    assert "+1 more line " in _plain(lines[-1])


def test_blank_runs_are_collapsed_so_the_preview_shows_content() -> None:
    """Raw reasoning arrives with ragged blank runs; spending the 12-line
    budget on gaps is what made the block look like spill."""
    from mantis_agent.tui import _thinking_body

    assert _thinking_body("a\n\n\n\nb\n\n") == ["a", "", "b"]
    assert _thinking_body("\n\n  \na") == ["a"]


def test_body_lines_keep_their_indent_when_wrapped() -> None:
    """A literal two-space prefix only indents the FIRST screen line, so every
    continuation of a long paragraph fell back to column 0."""
    from rich.console import Console

    long_line = "word " * 60
    console = Console(width=40, no_color=True)
    with console.capture() as cap:
        for ln in _thinking_lines(long_line):
            console.print(ln)
    body = [l for l in cap.get().splitlines() if l.strip() and "thinking" not in l]
    assert len(body) > 1, "expected the paragraph to wrap"
    assert all(l.startswith("  ") for l in body), body


def test_expand_view_actually_contains_the_reasoning(monkeypatch) -> None:
    """The inline cap points at ctrl+o. Before this, the expand view skipped
    ThinkingBlock entirely — so following that pointer showed nothing more and
    the hint was a lie."""
    from mantis_agent.types import AssistantMessage, TextBlock, ThinkingBlock

    tui = _tui()
    tui.messages = [AssistantMessage(content=[
        ThinkingBlock(thinking="\n".join(f"secret reasoning {i}" for i in range(30))),
        TextBlock(text="the answer"),
    ])]
    # The pager would swallow the output; render straight to the capture buffer.
    monkeypatch.setattr(tui.console, "pager", lambda **kw: _null_ctx())
    with tui.console.capture() as cap:
        tui._show_transcript()
    out = cap.get()
    assert "secret reasoning 0" in out
    assert "secret reasoning 29" in out, "expand must show what the cap hid"
    assert "the answer" in out


class _null_ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False
