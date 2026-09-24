"""Fullscreen input fixes (ITEM 18).

* The app-level Esc is not eager, so Alt/Option combos (ESC-prefixed) reach
  prompt_toolkit instead of wiping the line, a lone Esc still fires promptly,
  and in vi mode Esc goes insert→normal instead of being stolen.
* @-file completion keeps the "@" (and quotes paths with spaces) so the
  mention actually attaches; the file listing refreshes on a TTL / per turn.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from mantis_agent.tui import resolve_file_mentions
from mantis_agent.tui_fullscreen import (
    MentionFileIndex,
    _walk_mention_files,
    add_vi_insert_escape,
    app_escape_filter,
    esc_timeout_default,
    mention_completion,
    tune_escape_timing,
)

# -- Esc vs Alt combos ---------------------------------------------------------


def _drive(keys: list[str], *, vi: bool = False, claimed: bool = False,
           pause: float = 0.3, vi_escs: list | None = None,
           ) -> tuple[str, int, list[str], object]:
    """Run a minimal app wired exactly like the fullscreen input (the real
    filter + timing helpers), feed ``keys`` with a pause after each chunk, and
    return (text, cursor, esc_calls, vi_input_mode)."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import BufferControl, Layout, Window
    from prompt_toolkit.output import DummyOutput

    calls: list[str] = []

    async def main() -> tuple[str, int, list[str], object]:
        with create_pipe_input() as inp:
            buf = Buffer()
            kb = KeyBindings()

            @kb.add("escape", filter=app_escape_filter(lambda: claimed))
            def _(event) -> None:
                calls.append(buf.text)
                buf.reset()  # the fullscreen idle action: clear the line

            if vi_escs is not None:
                add_vi_insert_escape(kb, lambda: claimed, lambda: vi_escs.append(1))

            app = Application(
                layout=Layout(Window(BufferControl(buffer=buf))), key_bindings=kb,
                input=inp, output=DummyOutput(),
                editing_mode=EditingMode.VI if vi else EditingMode.EMACS,
            )
            tune_escape_timing(app)
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.1)
            for chunk in keys:
                inp.send_text(chunk)
                await asyncio.sleep(pause)
            mode = app.vi_state.input_mode
            app.exit()
            await task
            return buf.text, buf.cursor_position, calls, mode

    return asyncio.run(main())


def test_option_backspace_deletes_a_word_not_the_line() -> None:
    text, _cur, calls, _ = _drive(["hello world", "\x1b\x7f"])
    assert text == "hello "
    assert calls == []


def test_alt_b_and_alt_f_move_by_word() -> None:
    text, cur, calls, _ = _drive(["hello world", "\x1bb"])
    assert (text, cur, calls) == ("hello world", 6, [])
    text, cur, calls, _ = _drive(["hello world", "\x1bb", "\x1bb", "\x1bf"])
    assert (text, cur, calls) == ("hello world", 5, [])


def test_lone_esc_still_fires_promptly() -> None:
    t0 = time.monotonic()
    text, _cur, calls, _ = _drive(["half typed", "\x1b"], pause=0.35)
    assert calls == ["half typed"] and text == ""
    assert time.monotonic() - t0 < 2.0


def test_escape_timing_is_short_only_while_esc_is_pending() -> None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.output import DummyOutput

    app = Application(output=DummyOutput())
    default = app.timeoutlen
    tune_escape_timing(app)
    assert app.ttimeoutlen <= 0.15
    assert app.timeoutlen == default   # Ctrl-X chords / vi gg keep the slow default


def test_parser_wait_is_long_enough_for_split_arrow_keys() -> None:
    # 0.05 split ESC [ B on laggy links: an arrow key interrupted the turn
    assert esc_timeout_default({}) == 0.1
    assert esc_timeout_default({"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}) == 0.15
    assert esc_timeout_default({"SSH_TTY": "/dev/ttys001"}) == 0.15
    assert esc_timeout_default({"MANTIS_ESC_TIMEOUT": "0.3", "SSH_TTY": "x"}) == 0.3
    assert esc_timeout_default({"MANTIS_ESC_TIMEOUT": "junk"}) == 0.1
    assert esc_timeout_default({"MANTIS_ESC_TIMEOUT": "-1"}) == 0.1


def test_vi_insert_esc_enters_normal_mode_and_keeps_the_line() -> None:
    from prompt_toolkit.key_binding.vi_state import InputMode

    text, _cur, calls, mode = _drive(["abc", "\x1b"], vi=True)
    assert text == "abc" and calls == []
    assert mode == InputMode.NAVIGATION


def test_vi_insert_esc_is_seen_by_the_app_for_esc_esc() -> None:
    # the first Esc (insert -> normal) must count toward Esc-Esc rewind,
    # otherwise it takes three presses
    from prompt_toolkit.key_binding.vi_state import InputMode

    seen: list = []
    text, cur, calls, mode = _drive(["abc", "\x1b"], vi=True, vi_escs=seen)
    assert (text, cur, calls, mode) == ("abc", 2, [], InputMode.NAVIGATION)
    assert seen == [1]
    # Esc #2 (normal mode) reaches the app handler
    _t, _c, calls, _ = _drive(["abc", "\x1b", "\x1b"], vi=True, vi_escs=seen)
    assert calls == ["abc"]


def test_vi_insert_esc_still_reaches_the_app_when_it_is_claimed() -> None:
    # a running turn / open overlay: Esc must still interrupt / close it
    _text, _cur, calls, _ = _drive(["abc", "\x1b"], vi=True, claimed=True)
    assert calls == ["abc"]


# -- @-file completion ---------------------------------------------------------


def test_completion_keeps_the_at_sign() -> None:
    assert mention_completion("look at @sr", "src/foo.py") == "look at @src/foo.py "
    assert mention_completion("@", "a.py") == "@a.py "


def test_completion_quotes_paths_with_spaces() -> None:
    assert mention_completion("see @my", "my notes.md") == 'see @"my notes.md" '


@pytest.mark.parametrize("name", [
    "a+b.py", "icon@2x.png", "report(1).txt", "foo,bar.md", "x#y", "my notes.md"])
def test_any_path_the_bare_form_cannot_carry_is_quoted_and_resolves(
        tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("ok")
    line = mention_completion("read @", name)
    assert line == f'read @"{name}" '
    assert [p for p, _ in resolve_file_mentions(line, tmp_path)] == [name]


def test_paths_with_a_double_quote_are_never_offered(tmp_path: Path) -> None:
    (tmp_path / 'say"hi".txt').write_text("x")
    (tmp_path / 'we"ird').mkdir()
    (tmp_path / 'we"ird' / "inner.py").write_text("x")
    (tmp_path / "ok.txt").write_text("x")
    assert _walk_mention_files(str(tmp_path)) == ["ok.txt"]


def test_walk_skips_any_egg_info_and_is_breadth_first(tmp_path: Path, monkeypatch) -> None:
    import mantis_agent.tui_fullscreen as tf

    (tmp_path / "mantis_agent.egg-info").mkdir()
    (tmp_path / "mantis_agent.egg-info" / "PKG-INFO").write_text("x")
    deep = tmp_path / "vendor" / "a" / "b"
    deep.mkdir(parents=True)
    for i in range(20):
        (deep / f"v{i}.js").write_text("x")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "README.md").write_text("x")
    monkeypatch.setattr(tf, "_MENTION_SCAN_CAP", 5)
    rels = [r.replace("\\", "/") for r in _walk_mention_files(str(tmp_path))]
    assert "README.md" in rels and "src/main.py" in rels   # not starved by vendor/
    assert not any("egg-info" in r for r in rels)


def test_completed_mentions_resolve(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("print(1)")
    (tmp_path / "my notes.md").write_text("hello")
    line = mention_completion("explain @fo", "src/foo.py")
    line = mention_completion(line + "and @my", "my notes.md") + "please"
    got = dict(resolve_file_mentions(line, tmp_path))
    assert got == {"src/foo.py": "print(1)", "my notes.md": "hello"}


# -- file listing cache --------------------------------------------------------


class _Walker:
    def __init__(self) -> None:
        self.files = ["a.py"]
        self.calls = 0
        self.done = threading.Event()

    def __call__(self, cwd: str) -> list[str]:
        self.calls += 1
        out = list(self.files)
        self.done.set()
        return out


def _settle(idx: MentionFileIndex, walker: _Walker) -> None:
    assert walker.done.wait(2.0)
    for _ in range(100):
        if not idx._refreshing:
            return
        time.sleep(0.01)


def test_index_walks_once_then_serves_the_cache() -> None:
    w = _Walker()
    idx = MentionFileIndex(walk=w, ttl=10.0)
    assert idx.files("/r") == []        # first walk is off-thread too
    _settle(idx, w)
    assert idx.files("/r") == ["a.py"]
    assert idx.files("/r") == ["a.py"]
    assert w.calls == 1


def test_first_walk_never_runs_on_the_calling_thread() -> None:
    main = threading.get_ident()
    where: list[int] = []
    done = threading.Event()

    def walk(cwd: str) -> list[str]:
        where.append(threading.get_ident())
        done.set()
        return ["a.py"]

    repainted: list[int] = []
    idx = MentionFileIndex(walk=walk, on_refresh=lambda: repainted.append(1))
    assert idx.files("/r") == []
    assert done.wait(2.0)
    assert where and where[0] != main
    for _ in range(100):
        if repainted:
            break
        time.sleep(0.01)
    assert repainted == [1] and idx.files("/r") == ["a.py"]


def test_invalidate_during_an_in_flight_rewalk_keeps_the_index_stale() -> None:
    # A TTL re-walk starts, the turn writes a file and invalidates, then the
    # older walk lands with the pre-write listing: it must not clear _stale.
    gate, started = threading.Event(), threading.Event()
    listings = [["a.py"], ["a.py"], ["a.py", "new.py"]]

    def walk(cwd: str) -> list[str]:
        out = listings.pop(0)
        if len(listings) == 1:          # the in-flight TTL re-walk
            started.set()
            assert gate.wait(2.0)
        return out

    now = [0.0]
    idx = MentionFileIndex(walk=walk, ttl=10.0, clock=lambda: now[0])
    idx.files("/r")
    for _ in range(200):
        if idx.files("/r") and not idx._refreshing:
            break
        time.sleep(0.01)
    now[0] = 11.0
    idx.files("/r")                     # TTL re-walk starts, blocks in walk()
    assert started.wait(2.0)
    idx.invalidate()                    # the turn wrote new.py meanwhile
    gate.set()
    for _ in range(200):
        if not idx._refreshing:
            break
        time.sleep(0.01)
    assert idx._stale                   # the pre-write listing didn't clear it
    for _ in range(200):
        if idx.files("/r") == ["a.py", "new.py"]:
            break
        time.sleep(0.01)
    assert idx.files("/r") == ["a.py", "new.py"]


def test_invalidate_picks_up_new_files_in_the_background() -> None:
    w = _Walker()
    repainted: list[int] = []
    idx = MentionFileIndex(walk=w, ttl=10.0, on_refresh=lambda: repainted.append(1))
    idx.files("/r")
    _settle(idx, w)
    repainted.clear()
    w.files = ["a.py", "new.py"]        # the agent created a file this turn
    w.done.clear()
    idx.invalidate()
    assert idx.files("/r") == ["a.py"]  # stale served now, never blocks
    _settle(idx, w)
    assert idx.files("/r") == ["a.py", "new.py"]
    assert repainted == [1]


def test_ttl_expiry_rewalks() -> None:
    now = [0.0]
    w = _Walker()
    idx = MentionFileIndex(walk=w, ttl=10.0, clock=lambda: now[0])
    idx.files("/r")
    _settle(idx, w)
    w.files = ["b.py"]
    w.done.clear()
    now[0] = 5.0
    idx.files("/r")
    assert w.calls == 1                 # still fresh
    now[0] = 11.0
    idx.files("/r")
    _settle(idx, w)
    assert idx.files("/r") == ["b.py"]


def test_cwd_change_rewalks_off_thread() -> None:
    w = _Walker()
    idx = MentionFileIndex(walk=w)
    idx.files("/r")
    _settle(idx, w)
    w.files = ["other.py"]
    w.done.clear()
    assert idx.files("/elsewhere") == []   # never the old dir's listing
    _settle(idx, w)
    assert idx.files("/elsewhere") == ["other.py"]


@pytest.mark.parametrize("text", ["email a@b.com", 'odd @"unterminated'])
def test_quoted_regex_does_not_overmatch(tmp_path: Path, text: str) -> None:
    assert resolve_file_mentions(text, tmp_path) == []
