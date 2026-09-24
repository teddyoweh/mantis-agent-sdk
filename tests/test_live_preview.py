"""Live reply streaming in the terminals.

The full-screen UI buffers token deltas into a :class:`LivePreview` and shows
its tail above the spinner until the message finalizes; the classic REPL
prints tokens inline and must start a restreamed reply on a fresh line.
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from rich.console import Console

from mantis_agent.events import ContentBlockDelta, MessageStart, TextDelta
from mantis_agent.live_preview import LivePreview
from mantis_agent.tui import MantisTUI
from mantis_agent.types import AssistantMessage, TextBlock
from tests.test_term_caps import _stand_up_app, tui_fullscreen  # noqa: F401

# -- the buffer ------------------------------------------------------------------


def test_tail_shows_only_the_last_rows() -> None:
    p = LivePreview()
    for i in range(50):
        p.feed_text(f"line {i}\n")
    rows = p.tail(width=80, max_rows=3)
    assert [r for _, r in rows] == ["line 47", "line 48", "line 49"]


def test_deltas_join_across_chunks_and_wrap_at_width() -> None:
    p = LivePreview()
    for ch in "abcdefghij":
        p.feed_text(ch)
    assert [r for _, r in p.tail(width=8, max_rows=5)] == ["abcdefgh", "ij"]
    # A second render (after compaction) is identical.
    assert [r for _, r in p.tail(width=8, max_rows=5)] == ["abcdefgh", "ij"]


def test_thinking_and_text_are_separate_rows_with_their_own_kind() -> None:
    p = LivePreview()
    p.feed_thinking("let me ")
    p.feed_thinking("think")
    p.feed_text("The answer")
    p.feed_text(" is 4.")
    assert p.tail(width=80, max_rows=5) == [
        ("thinking", "let me think"), ("text", "The answer is 4.")]


def test_reset_clears_and_empty_buffer_is_falsy() -> None:
    p = LivePreview()
    assert not p
    p.feed_text("\n\n")
    assert not p                     # whitespace alone never opens the window
    p.feed_text("hello")
    assert p
    p.reset()
    assert not p and p.tail(80, 5) == []


def test_control_characters_are_stripped() -> None:
    p = LivePreview()
    p.feed_text("a\x1b[31mred\x07\tb")
    [(_, row)] = p.tail(80, 3)
    assert "\x1b" not in row and "\x07" not in row
    assert row.startswith("a[31mred")


def test_due_throttles_repaints() -> None:
    p = LivePreview(max_hz=20.0)     # one repaint per 50 ms
    assert p.due(now=100.0) is True
    assert p.due(now=100.01) is False
    assert p.due(now=100.049) is False
    assert p.due(now=100.06) is True


def test_control_filter_drops_c1_controls_and_expands_tabs() -> None:
    p = LivePreview()
    p.feed_text("a\x9b31mb\x7fc\x85d\tz")   # C1 CSI, DEL, NEL
    [(_, row)] = p.tail(80, 3)
    assert row.replace(" ", "") == "a31mbcdz" and "\t" not in row


def test_a_huge_single_line_renders_its_tail_fast() -> None:
    body = "".join(chr(ord("a") + i % 26) for i in range(200_000))
    p = LivePreview()
    for i in range(0, len(body), 1000):    # streamed in chunks, one long line
        p.feed_text(body[i:i + 1000])
    width, max_rows = 78, 8
    full = [body[i:i + width] for i in range(0, len(body), width)]
    t0 = time.perf_counter()
    rows = p.tail(width, max_rows)
    first = time.perf_counter() - t0
    assert [r for _, r in rows] == full[-max_rows:]   # same wrap as the whole line
    assert first < 0.005, f"tail took {first * 1000:.2f} ms"
    # The height probe and the content render share one computation.
    t0 = time.perf_counter()
    assert p.tail(width, max_rows) == rows
    assert time.perf_counter() - t0 < 0.001
    p.feed_text("END")
    assert p.tail(width, max_rows)[-1][1].endswith("END")   # cache invalidated


def test_tail_cache_is_dropped_by_reset() -> None:
    p = LivePreview()
    p.feed_text("hello")
    assert p.tail(80, 3) == [("text", "hello")]
    p.reset()
    assert p.tail(80, 3) == []


# -- the full-screen layout: the preview yields to everything else ---------------


def _closure(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def test_preview_never_overflows_a_short_terminal(
        tui_fullscreen, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """24 rows, a live checklist and live subagents: every window is
    Dimension.exact and the app isn't full-screen, so the heights must sum to
    at most the terminal height or prompt_toolkit paints "Window too small".
    (The old preview took max(3, rows // 3) + 1 = 9 rows regardless.)"""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.dimension import to_dimension

    app, tui, controls = _stand_up_app(monkeypatch, tui_fullscreen)
    preview_ft = next(c.text for c in controls if c.text.__name__ == "live_preview_ft")
    rows_fn = _closure(preview_ft, "_live_preview_rows")
    state = _closure(rows_fn, "state")
    preview = _closure(rows_fn, "live_preview")

    tui.todos = [{"content": f"task {i}", "status": "pending"} for i in range(6)]
    tui._live_subagents = {i: {"type": "explore", "started": 0.0, "tools": 1,
                               "desc": "d", "last_event": "Grep"} for i in range(6)}
    state.update(working=True, started=time.monotonic(), word="Mulling", frame=0)
    for i in range(200):
        preview.feed_text(f"streamed line {i}\n")

    def total(rows: int) -> tuple[int, int]:
        monkeypatch.setattr(app.output, "get_size", lambda: Size(rows=rows, columns=80))
        with set_app(app):
            windows = app.layout.container.get_children()
            heights = {id(w): to_dimension(w.height).preferred for w in windows}
            preview_w = next(w for w in windows if isinstance(w, Window)
                             and getattr(w.content, "text", None) is preview_ft)
            shown = [r for _, r in rows_fn()]
            return sum(heights.values()), heights[id(preview_w)], shown

    used, preview_h, shown = total(24)
    assert used <= 24, f"layout needs {used} rows on a 24-row terminal"
    assert used - preview_h + 9 > 24    # the old fixed-size preview overflowed
    # Shrunk (possibly to nothing), never the old min-3 floor forced in.
    assert preview_h == 0 or shown[-1] == "streamed line 199"

    # So tight the preview has no room at all: hidden, not overflowing.
    used, preview_h, shown = total(used - preview_h + 1)
    assert preview_h == 0 and shown == []

    # With room to spare the preview comes back, capped at a third, newest last.
    used, preview_h, shown = total(60)
    assert used <= 60
    assert 0 < preview_h <= 60 // 3 + 1
    assert shown[-1] == "streamed line 199"

    # Drop the subagents: a 24-row terminal now has room for a preview.
    tui._live_subagents = {}
    used, preview_h, shown = total(24)
    assert used <= 24 and preview_h > 0 and shown[-1] == "streamed line 199"


# -- the classic REPL: a restream starts on a fresh line --------------------------


def _tui() -> MantisTUI:
    t = MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                  max_tokens=1, temperature=None, max_turns=1)
    t.console = Console(width=80, file=io.StringIO(), force_terminal=False,
                        color_system=None)
    return t


def _delta(s: str) -> ContentBlockDelta:
    return ContentBlockDelta(index=0, delta=TextDelta(text=s))


def test_classic_restream_does_not_share_a_line_with_the_partial(
        monkeypatch: pytest.MonkeyPatch) -> None:
    t = _tui()

    class _Agent:
        on_event = None
        tools: list = []

        def run_iter(self, messages):
            agent = self

            async def gen():
                agent.on_event(MessageStart(message_id="1", model="m"))
                agent.on_event(_delta("partial ans"))
                # truncation retry: the stream restarts from scratch
                agent.on_event(MessageStart(message_id="2", model="m"))
                agent.on_event(_delta("full answer"))
                msg = AssistantMessage(content=[TextBlock(text="full answer")])
                messages.append(msg)
                yield msg
            return gen()

    t.agent = _Agent()
    monkeypatch.setattr(t, "_begin_activity_turn", lambda: None)
    monkeypatch.setattr(t, "_end_activity_turn", lambda: None)
    monkeypatch.setattr(t, "_persist_messages", lambda base: None)

    async def _no_title() -> None:
        return None
    monkeypatch.setattr(t, "_maybe_autotitle", _no_title)
    asyncio.run(t._run_turn("hi"))
    out = t.console.file.getvalue()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert any(ln.strip() == "● partial ans" for ln in lines), out
    assert any(ln.strip() == "● full answer" for ln in lines), out
    assert "full answer" not in out.replace("● full answer", "", 1)  # printed once
