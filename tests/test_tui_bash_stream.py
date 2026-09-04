"""Live output for a running foreground ``bash`` call.

The engine hands every decoded chunk to a sink installed with
``set_bash_output_sink``; the terminal draws the last few rows under the
``⚒ Run …`` call line, repainted in place, and takes the window down before
the ordinary result preview prints — so the finished transcript reads exactly
as it did before streaming existed.

Driven with a fake sink feed and a recording console, no subprocess.
"""

from __future__ import annotations

import asyncio
import io
import re

import pytest
from rich.console import Console

from mantis_agent import term_caps
from mantis_agent.builtin_tools import (
    bash_output_sink,
    reset_bash_output_sink,
    set_bash_output_sink,
)
from mantis_agent.tui import BASH_TAIL_LINES, BashTail, MantisTUI
from mantis_agent.types import ToolResultBlock, UserMessage

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(s: str) -> str:
    return _ANSI.sub("", s)


# -- BashTail: the window itself ---------------------------------------------------


def test_feed_accumulates_partial_lines() -> None:
    t = BashTail(80, paint=False)
    t.feed("hel")
    t.feed("lo\nwor")
    assert t.tail() == ["hello", "wor"]
    t.feed("ld\n")
    assert t.tail() == ["hello", "world"]
    assert t.total == 2 and t.dirty


def test_window_keeps_only_the_last_n_lines_and_counts_the_rest() -> None:
    t = BashTail(80, max_lines=3, paint=False)
    t.feed("".join(f"row {i}\n" for i in range(10)))
    assert t.tail() == ["row 7", "row 8", "row 9"]
    rows = t.rows()
    assert rows[0] == "    … +7 earlier lines"
    assert rows[1:] == ["    row 7", "    row 8", "    row 9"]


def test_rows_are_cut_to_width_and_indented() -> None:
    t = BashTail(40, paint=False)
    t.feed("x" * 100 + "\n")
    row = t.rows()[0]
    assert row.startswith("    ") and row.endswith("…")
    assert len(row) <= 40


def test_default_window_is_eight_rows() -> None:
    assert BASH_TAIL_LINES == 8
    t = BashTail(80, paint=False)
    t.feed("".join(f"{i}\n" for i in range(30)))
    assert len(t.tail()) == 8


def test_repaint_moves_up_over_the_previous_window() -> None:
    t = BashTail(80, paint=False)
    buf = io.StringIO()
    t.feed("a\nb\n")
    t.paint(buf.write)
    first = buf.getvalue()
    assert "\x1b[" not in first                        # nothing to erase yet
    assert first == "    a\n    b\n" and t.painted == 2 and not t.dirty
    t.feed("c\n")
    buf = io.StringIO()
    t.paint(buf.write)
    second = buf.getvalue()
    assert second.startswith(term_caps.repaint_above(2))   # up 2, erase below
    assert _plain(second) == "    a\n    b\n    c\n" and t.painted == 3


def test_erase_removes_exactly_the_painted_rows() -> None:
    t = BashTail(80, paint=False)
    buf = io.StringIO()
    t.feed("a\nb\nc\n")
    t.paint(buf.write)
    buf = io.StringIO()
    t.erase(buf.write)
    assert buf.getvalue() == term_caps.repaint_above(3)
    assert t.painted == 0
    buf = io.StringIO()
    t.erase(buf.write)                                  # idempotent
    assert buf.getvalue() == ""


def test_painted_rows_are_dim_when_colour_is_on() -> None:
    t = BashTail(80, paint=True)
    buf = io.StringIO()
    t.feed("x\n")
    t.paint(buf.write)
    assert "\x1b[38;5;240m" in buf.getvalue()
    assert _plain(buf.getvalue()) == "    x\n"


def test_carriage_returns_become_lines_so_progress_bars_do_not_pile_up() -> None:
    t = BashTail(80, max_lines=2, paint=False)
    t.feed("10%\r20%\r30%\n")
    assert t.tail() == ["20%", "30%"]


# -- the engine hook is a ContextVar with a token ---------------------------------------


def test_sink_install_and_reset() -> None:
    assert bash_output_sink() is None
    def sink(chunk: str) -> None:
        pass
    tok = set_bash_output_sink(sink)
    try:
        assert bash_output_sink() is sink
    finally:
        reset_bash_output_sink(tok)
    assert bash_output_sink() is None


# -- the classic REPL: stream, then the ordinary preview ------------------------------


def _tui() -> MantisTUI:
    t = MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                  max_tokens=1, temperature=None, max_turns=1)
    t.console = Console(width=80, file=io.StringIO(), force_terminal=False,
                        color_system=None)
    return t


def test_classic_paints_a_tail_then_replaces_it_with_the_preview() -> None:
    t = _tui()
    sink = t._bash_stream_sink()
    for i in range(12):
        sink(f"building {i}\n")
    assert t._bash_stream_paint_once() is True
    painted = t.console.file.getvalue()
    assert "… +4 earlier lines" in painted
    assert "    building 11" in painted and "building 3" not in painted
    assert t._bash_stream_paint_once() is False          # nothing new → no repaint

    # The result arrives: window erased, preview printed where it stood.
    t._bash_stream_finish()
    t._render_tool_results(UserMessage(content=[ToolResultBlock(
        tool_use_id="c", content="\n".join(f"building {i}" for i in range(12)))]),
        ToolResultBlock)
    out = t.console.file.getvalue()
    tail_part, _, after = out.partition(term_caps.repaint_above(9))
    assert "building 11" in tail_part                    # the window was there…
    assert after.lstrip().startswith("└ building 0")      # …and the preview took its place
    assert t._bash_tail is None and t._bash_painter is None


def test_classic_sink_is_installed_around_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_run_turn`` installs the sink before the loop and resets it after —
    even when the turn fails."""
    t = _tui()
    seen: dict[str, object] = {}

    class _Agent:
        on_event = None
        tools: list = []

        def run_iter(self, messages):
            async def gen():
                seen["sink"] = bash_output_sink()
                raise RuntimeError("boom")
                yield  # noqa: RET503 — makes this an async generator
            return gen()

    t.agent = _Agent()
    monkeypatch.setattr(t, "_begin_activity_turn", lambda: None)
    monkeypatch.setattr(t, "_end_activity_turn", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(t._run_turn("hi"))
    assert callable(seen["sink"])
    assert bash_output_sink() is None


def test_classic_painter_loop_paints_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _tui()

    async def go() -> None:
        sink = t._bash_stream_sink()
        sink("one\n")
        assert t._bash_painter is not None
        await asyncio.sleep(0.05)
        assert "    one" in t.console.file.getvalue()
        sink("two\n")
        await asyncio.sleep(0.3)
        assert "    two" in t.console.file.getvalue()
        t._bash_stream_finish()
        await asyncio.sleep(0)
        assert t._bash_painter is None

    asyncio.run(go())
