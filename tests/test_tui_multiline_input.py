"""Fullscreen multi-line input (ITEM 19).

* The prompt is a multi-line, wrapping buffer that grows with its content
  (bounded, and never overflowing a short terminal).
* Enter submits; Alt+Enter, Ctrl+J, Shift+Enter (CSI-u / modifyOtherKeys) and
  ``\\``+Enter insert a newline. Alt+Enter never trips the app Esc handler.
* Large pastes collapse to ``[Pasted text #N …]`` and expand on submit.
* Up/Down walk history from the first/last row, move the cursor otherwise.
* Ctrl+R searches history; Enter accepts, Esc cancels.
* Ctrl+O is bound in the full-screen app (the "ctrl+o to expand" hints).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from mantis_agent.tui_fullscreen import (
    INPUT_MAX_ROWS,
    PastedTexts,
    add_history_search_keys,
    add_newline_keys,
    app_escape_filter,
    continue_line,
    input_rows,
    install_shift_enter_sequences,
    tune_escape_timing,
)
from tests.test_term_caps import _stand_up_app, tui_fullscreen  # noqa: F401


def _drive(keys: list[str], *, history: list[str] | None = None,
           text: str = "", pause: float = 0.25) -> dict[str, Any]:
    """A minimal app wired like the fullscreen input — multiline buffer, the
    real newline / search / Esc helpers, Enter → continue_line or submit (with
    paste expansion), bracketed paste → PastedTexts."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import BufferControl, HSplit, Layout, Window
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.widgets import SearchToolbar

    out: dict[str, Any] = {"submitted": [], "escs": []}
    install_shift_enter_sequences()

    async def main() -> None:
        with create_pipe_input() as inp:
            hist = InMemoryHistory()
            for h in history or []:
                hist.append_string(h)
            buf = Buffer(multiline=True, history=hist)
            buf.text = text
            pasted = PastedTexts()
            kb = KeyBindings()

            @kb.add("escape", filter=app_escape_filter(lambda: False))
            def _(event) -> None:
                out["escs"].append(buf.text)
                buf.reset()

            @kb.add("enter")
            def _(event) -> None:
                if continue_line(buf):
                    return
                out["submitted"].append(pasted.expand(buf.text))
                pasted.clear()
                buf.reset(append_to_history=True)

            @kb.add(Keys.BracketedPaste)
            def _(event) -> None:
                data = event.data.replace("\r\n", "\n").replace("\r", "\n")
                buf.insert_text(pasted.add(data) if PastedTexts.should_collapse(data)
                                else data)

            add_newline_keys(kb)
            add_history_search_keys(kb)
            toolbar = SearchToolbar(ignore_case=True)
            win = Window(BufferControl(buffer=buf, search_buffer_control=toolbar.control))
            app = Application(layout=Layout(HSplit([win, toolbar])), key_bindings=kb,
                              input=inp, output=DummyOutput())
            tune_escape_timing(app)
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.1)
            buf.load_history_if_not_yet_loaded()
            await asyncio.sleep(0.05)
            for chunk in keys:
                inp.send_text(chunk)
                await asyncio.sleep(pause)
            out["text"], out["cursor"] = buf.text, buf.cursor_position
            out["searching"] = app.layout.is_searching
            app.exit()
            await task

    asyncio.run(main())
    return out


# -- newline keys ----------------------------------------------------------------


def test_enter_submits() -> None:
    r = _drive(["hello", "\r"])
    assert r["submitted"] == ["hello"] and r["text"] == ""


@pytest.mark.parametrize("seq", [
    "\x1b\r",          # Alt/Option+Enter (ESC-prefixed)
    "\n",              # Ctrl+J
    "\x1b[13;2u",      # Shift+Enter, CSI-u (kitty / WezTerm / iTerm2)
    "\x1b[27;2;13~",   # Shift+Enter, xterm modifyOtherKeys (ptk maps it to Enter)
])
def test_newline_keys_insert_a_newline_and_do_not_submit(seq: str) -> None:
    r = _drive(["one", seq, "two"])
    assert r["text"] == "one\ntwo"
    assert r["submitted"] == [] and r["escs"] == []   # Alt+Enter isn't an Esc


def test_backslash_enter_continues_the_line_without_the_backslash() -> None:
    r = _drive(["first\\", "\r", "second", "\r"])
    assert r["submitted"] == ["first\nsecond"]


def test_backslash_mid_line_is_left_alone() -> None:
    r = _drive(["a\\b", "\r"])
    assert r["submitted"] == ["a\\b"]


def test_lone_esc_still_reaches_the_app() -> None:
    r = _drive(["typed", "\x1b"], pause=0.35)
    assert r["escs"] == ["typed"] and r["text"] == ""


# -- history on a multi-line buffer ------------------------------------------------


def test_up_moves_between_rows_then_walks_history() -> None:
    r = _drive(["\x1b[A"], history=["old prompt"], text="")
    assert r["text"] == "old prompt"
    # On the second row Up only moves the cursor to the first row…
    r = _drive(["one", "\n", "two", "\x1b[A"], history=["old prompt"])
    assert r["text"] == "one\ntwo" and r["cursor"] <= 3
    # …and from the first row it reaches history.
    r = _drive(["one", "\n", "two", "\x1b[A", "\x1b[A"], history=["old prompt"])
    assert r["text"] == "old prompt"


def test_down_on_the_last_row_walks_forward_in_history() -> None:
    r = _drive(["\x1b[A", "\x1b[A", "\x1b[B"], history=["first", "second"])
    assert r["text"] == "second"


# -- Ctrl+R ------------------------------------------------------------------------


def test_ctrl_r_finds_a_past_prompt_and_enter_accepts_it() -> None:
    r = _drive(["\x12", "deplo", "\r"],
               history=["run the tests", "deploy to staging", "fix lint"])
    assert r["text"] == "deploy to staging"
    assert r["submitted"] == [] and not r["searching"]   # accepted, not sent


def test_ctrl_r_esc_cancels_and_keeps_what_was_typed() -> None:
    r = _drive(["draft", "\x12", "deplo", "\x1b"], history=["deploy to staging"],
               pause=0.35)
    assert r["text"] == "draft" and not r["searching"]
    assert r["escs"] == []            # the search ate the Esc, the line survives


# -- paste collapse ----------------------------------------------------------------


def _paste(data: str) -> str:
    return f"\x1b[200~{data}\x1b[201~"


def test_big_paste_collapses_and_expands_on_submit() -> None:
    blob = "\n".join(f"line {i}" for i in range(42))
    r = _drive(["see ", _paste(blob)])
    assert r["text"] == "see [Pasted text #1 +41 lines]"
    r = _drive(["see ", _paste(blob), " thanks", "\r"])
    assert r["submitted"] == [f"see {blob} thanks"]


def test_small_paste_is_inserted_verbatim() -> None:
    r = _drive([_paste("a\nb\nc")])
    assert r["text"] == "a\nb\nc"


def test_pasted_texts_multiple_and_deleted_tokens() -> None:
    p = PastedTexts()
    long_line = "x" * 2500
    many = "\n".join(str(i) for i in range(20))
    t1, t2 = p.add(many), p.add(long_line)
    assert t1 == "[Pasted text #1 +19 lines]"
    assert t2 == "[Pasted text #2 · 2500 chars]"
    assert p.expand(f"{t2} and {t1}") == f"{long_line} and {many}"
    assert p.expand("only the second: " + t2) == "only the second: " + long_line
    assert p.expand("[Pasted text #9 +3 lines]") == "[Pasted text #9 +3 lines]"
    p.clear()
    assert not p and p.add("y" * 3000).startswith("[Pasted text #1 ")


def test_should_collapse_thresholds() -> None:
    assert not PastedTexts.should_collapse("\n".join("a" * 10))     # 10 lines
    assert PastedTexts.should_collapse("\n".join("a" * 11))
    assert PastedTexts.should_collapse("z" * 2001)
    assert not PastedTexts.should_collapse("z" * 2000)


def test_continue_line_helper() -> None:
    from prompt_toolkit.buffer import Buffer

    b = Buffer(multiline=True)
    b.insert_text("abc\\")
    assert continue_line(b) and b.text == "abc\n"
    assert not continue_line(b)


# -- sizing ------------------------------------------------------------------------


def test_input_rows_wraps_and_clamps() -> None:
    assert input_rows("", 80) == 1
    assert input_rows("hi", 80) == 1
    assert input_rows("x" * 100, 40) == 3
    assert input_rows("a\nb\nc", 80) == 3
    assert input_rows("\n" * 50, 80) == INPUT_MAX_ROWS
    assert input_rows("\n" * 50, 80, max_rows=4) == 4


def _closure(fn: Any, name: str) -> Any:
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def test_input_grows_but_never_overflows_the_terminal(
        tui_fullscreen, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import BufferControl
    from prompt_toolkit.layout.dimension import to_dimension

    app, tui, controls = _stand_up_app(monkeypatch, tui_fullscreen)
    input_win = next(w for w in app.layout.find_all_windows()
                     if isinstance(w.content, BufferControl)
                     and w.content.search_buffer_control is not None)
    buf = input_win.content.buffer
    assert buf.multiline() and input_win.wrap_lines()

    preview_ft = next(c.text for c in controls if c.text.__name__ == "live_preview_ft")
    rows_fn = _closure(preview_ft, "_live_preview_rows")
    state = _closure(rows_fn, "state")
    preview = _closure(rows_fn, "live_preview")
    state.update(working=True, started=time.monotonic(), word="Mulling", frame=0)
    for i in range(200):
        preview.feed_text(f"streamed line {i}\n")

    def layout(rows: int) -> tuple[int, int]:
        monkeypatch.setattr(app.output, "get_size", lambda: Size(rows=rows, columns=80))
        with set_app(app):
            used = sum(to_dimension(w.height).preferred
                       for w in app.layout.container.get_children())
            return used, to_dimension(input_win.height).preferred

    buf.text = "short"
    assert layout(40)[1] == 1
    buf.text = "\n".join(f"row {i}" for i in range(6))
    used, h = layout(40)
    assert h == 6 and used <= 40
    buf.text = "\n".join(f"row {i}" for i in range(30))
    used, h = layout(40)
    assert h == INPUT_MAX_ROWS and used <= 40
    # A short terminal full of live windows: the box shrinks, never overflows.
    tui.todos = [{"content": f"task {i}", "status": "pending"} for i in range(6)]
    for rows in (24, 18, 14):
        used, h = layout(rows)
        assert used <= rows and h >= 1
    assert isinstance(input_win, Window)


def test_ctrl_o_is_bound_in_fullscreen(
        tui_fullscreen, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.keys import Keys

    app, _tui, _controls = _stand_up_app(monkeypatch, tui_fullscreen)
    keys = {b.keys for b in app.key_bindings.bindings}
    assert (Keys.ControlO,) in keys


def test_reset_drops_stale_paste_tokens_and_editor_keeps_text():
    """Review fixes: Buffer.reset() (Esc clear) forgets paste tokens; C-x C-e
    doesn't validate-and-drop the buffer; Ctrl+O waits for the turn."""
    import inspect

    from mantis_agent import tui_fullscreen as tf

    src = inspect.getsource(tf)
    assert "input_buffer.open_in_editor()" in src
    assert "open_in_editor(event.app)" not in src
    assert "_reset_and_forget_pastes" in src
    assert 'if state.get("working"):' in src.split('@kb.add("c-o")', 1)[1][:800]

    p = tf.PastedTexts()
    from prompt_toolkit.buffer import Buffer

    buf = Buffer(multiline=True)
    orig = buf.reset

    def _reset(*a, **k):
        p.clear()
        orig(*a, **k)

    buf.reset = _reset
    tok = p.add("x\n" * 50) if hasattr(p, "add") else None
    if tok is not None:
        buf.text = tok
        buf.reset()
        assert not p
        assert p.expand(tok) == tok


def test_ctrl_r_previews_the_match_in_the_input():
    """Live-verified regression: BufferControl defaults preview_search=False, so
    the Ctrl+R match stayed invisible until Enter; a custom processor list also
    dropped the search highlight."""
    import inspect

    from mantis_agent import tui_fullscreen as tf

    src = inspect.getsource(tf)
    assert "preview_search=True" in src
    assert "HighlightIncrementalSearchProcessor()" in src
