"""Thinking-block rendering in the TUI — reasoning models' <think> shown dimmed."""

from __future__ import annotations

from mantis_agent.tui import _THINK_CAP, MantisTUI, _thinking_lines
from mantis_agent.types import AssistantMessage, TextBlock, ThinkingBlock, ToolUseBlock


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


def test_thinking_lines_header_and_body() -> None:
    lines = _thinking_lines("first thought\nsecond thought")
    text = [ln.plain for ln in lines]
    assert text[0] == "✻ thinking"
    assert "  first thought" in text and "  second thought" in text


def test_thinking_lines_empty() -> None:
    assert _thinking_lines("") == []
    assert _thinking_lines("   \n  ") == []


def test_thinking_lines_capped() -> None:
    lines = _thinking_lines("\n".join(str(i) for i in range(30)))
    # header + _THINK_CAP body lines + one elision note
    assert len(lines) == _THINK_CAP + 2
    assert "more lines" in lines[-1].plain


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
