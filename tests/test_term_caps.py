"""Terminal capability detection — NO_COLOR, TERM, and one layout truth.

Mantis painted hardcoded ANSI at every call site and asked ``isatty()`` and
nothing else, so ``NO_COLOR=1`` (https://no-color.org) did nothing and a
``TERM=dumb`` pipe still got escape codes. These lock the precedence ladder in
:mod:`mantis_agent.term_caps` so a fix elsewhere can't quietly re-order it.

Every test drives ``detect(force_refresh=True)`` because the real one is
cached; the ``_clean_env`` fixture strips the ambient terminal env so the
result depends only on what the test sets.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import time
from unittest import mock
from typing import Any

import pytest

from mantis_agent import term_caps
from mantis_agent.term_caps import (
    TerminalCaps,
    detect,
    strip_ansi,
    supports_color,
    terminal_size,
)

_ENV_VARS = (
    "NO_COLOR", "FORCE_COLOR", "COLORTERM", "TERM", "TERM_PROGRAM",
    "LANG", "LC_ALL", "LC_CTYPE", "COLUMNS", "LINES",
    "MANTIS_TERM_COLOR", "MANTIS_TERM_UNICODE", "MANTIS_TERM_BOX",
    "MANTIS_TERM_WIDTH", "MANTIS_TERM_HEIGHT", "MANTIS_TERM_TTY",
)


class _FakeStream(io.StringIO):
    """A stream whose tty-ness we control — detection reads ``isatty()``."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient terminal state, no stale cache, not a tty by default."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(term_caps, "_CACHE", None)
    _set_tty(monkeypatch, False)


def _set_tty(monkeypatch: pytest.MonkeyPatch, tty: bool) -> None:
    monkeypatch.setattr("sys.stdin", _FakeStream(tty))
    monkeypatch.setattr("sys.stdout", _FakeStream(tty))


def _caps(monkeypatch: pytest.MonkeyPatch, tty: bool = True, **env: str) -> Any:
    _set_tty(monkeypatch, tty)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return detect(force_refresh=True)


# -- NO_COLOR: the whole point ----------------------------------------------


def test_no_color_disables_color_on_a_color_capable_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """https://no-color.org — presence of a non-empty value is the signal."""
    caps = _caps(monkeypatch, NO_COLOR="1", TERM="xterm-256color",
                 COLORTERM="truecolor")
    assert caps.color == "none"
    assert caps.is_tty is True  # only color is suppressed, not the tty


def test_no_color_value_is_irrelevant_as_long_as_it_is_non_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("1", "0", "false", "no", "yes", " "):
        caps = _caps(monkeypatch, NO_COLOR=value, COLORTERM="truecolor")
        assert caps.color == "none", value


def test_empty_no_color_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spec's own carve-out: NO_COLOR="" must NOT disable color."""
    caps = _caps(monkeypatch, NO_COLOR="", TERM="xterm-256color")
    assert caps.color == "256"


def test_no_color_beats_force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, tty=False, NO_COLOR="1", FORCE_COLOR="3",
                 COLORTERM="truecolor")
    assert caps.color == "none"


def test_supports_color_follows_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    _caps(monkeypatch, TERM="xterm-256color")
    assert supports_color() is True
    _caps(monkeypatch, TERM="xterm-256color", NO_COLOR="1")
    assert supports_color() is False


# -- MANTIS_TERM_* overrides sit above everything ---------------------------


def test_mantis_override_beats_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit in-app override is more authoritative than the convention."""
    caps = _caps(monkeypatch, MANTIS_TERM_COLOR="truecolor", NO_COLOR="1")
    assert caps.color == "truecolor"


def test_mantis_color_override_works_without_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = _caps(monkeypatch, tty=False, MANTIS_TERM_COLOR="256")
    assert caps.color == "256"


def test_unparseable_mantis_color_override_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = _caps(monkeypatch, MANTIS_TERM_COLOR="chartreuse",
                 TERM="xterm-256color")
    assert caps.color == "256"


def test_mantis_unicode_and_box_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, MANTIS_TERM_UNICODE="1", LANG="C")
    assert caps.unicode is True and caps.box_drawing is True
    caps = _caps(monkeypatch, MANTIS_TERM_BOX="0", LANG="en_US.UTF-8")
    assert caps.unicode is True and caps.box_drawing is False
    caps = _caps(monkeypatch, MANTIS_TERM_UNICODE="0", LANG="en_US.UTF-8")
    assert caps.unicode is False and caps.box_drawing is False


def test_mantis_tty_override_forces_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, tty=False, MANTIS_TERM_TTY="1", TERM="xterm")
    assert caps.is_tty is True
    assert caps.color == "16"


# -- FORCE_COLOR ------------------------------------------------------------


def test_force_color_turns_color_on_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    piped = _caps(monkeypatch, tty=False, TERM="xterm-256color")
    assert piped.color == "none"
    forced = _caps(monkeypatch, tty=False, TERM="xterm-256color", FORCE_COLOR="1")
    assert forced.color == "256"  # still not a tty, but detection continues
    assert forced.is_tty is False


def test_force_color_numeric_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _caps(monkeypatch, tty=False, FORCE_COLOR="1").color == "16"
    assert _caps(monkeypatch, tty=False, FORCE_COLOR="2").color == "256"
    assert _caps(monkeypatch, tty=False, FORCE_COLOR="3").color == "truecolor"


def test_force_color_level_is_a_floor_not_a_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORCE_COLOR=1 in a truecolor terminal must not cost you 24-bit."""
    caps = _caps(monkeypatch, FORCE_COLOR="1", COLORTERM="truecolor")
    assert caps.color == "truecolor"
    # ...but it does raise a terminal that detects lower.
    assert _caps(monkeypatch, FORCE_COLOR="3", TERM="xterm").color == "truecolor"


def test_force_color_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    """chalk's semantics: FORCE_COLOR=0 is an opt-out, not an opt-in."""
    caps = _caps(monkeypatch, FORCE_COLOR="0", TERM="xterm-256color")
    assert caps.color == "none"


def test_bare_force_color_floors_at_16_even_for_dumb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = _caps(monkeypatch, tty=False, FORCE_COLOR="", TERM="dumb")
    assert caps.color == "16"


# -- isatty -----------------------------------------------------------------


def test_not_a_tty_means_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, tty=False, TERM="xterm-256color",
                 COLORTERM="truecolor")
    assert caps.is_tty is False
    assert caps.color == "none"


def test_one_half_of_the_pipe_being_redirected_is_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdout redirected to a file while stdin stays a tty: don't paint."""
    monkeypatch.setattr("sys.stdin", _FakeStream(True))
    monkeypatch.setattr("sys.stdout", _FakeStream(False))
    monkeypatch.setenv("TERM", "xterm-256color")
    caps = detect(force_refresh=True)
    assert caps.is_tty is False
    assert caps.color == "none"


# -- COLORTERM / TERM / TERM_PROGRAM ----------------------------------------


@pytest.mark.parametrize("value", ["truecolor", "24bit", "TrueColor"])
def test_colorterm_means_truecolor(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    caps = _caps(monkeypatch, TERM="xterm", COLORTERM=value)
    assert caps.color == "truecolor"


def test_unknown_colorterm_does_not_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = _caps(monkeypatch, TERM="xterm", COLORTERM="yes")
    assert caps.color == "16"


def test_term_dumb_disables_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, TERM="dumb", COLORTERM="truecolor",
                 LANG="en_US.UTF-8")
    assert caps.color == "none"
    assert caps.unicode is False
    assert caps.box_drawing is False


def test_term_256color_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    for term in ("xterm-256color", "screen-256color", "tmux-256color"):
        assert _caps(monkeypatch, TERM=term).color == "256"


def test_plain_term_is_16_and_missing_term_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _caps(monkeypatch, TERM="xterm").color == "16"
    assert _caps(monkeypatch, TERM="vt100").color == "16"
    # No TERM at all on a POSIX "tty" — nothing says escapes are safe.
    monkeypatch.setattr(term_caps.os, "name", "posix")
    monkeypatch.delenv("TERM", raising=False)
    assert _caps(monkeypatch).color == "none"


def test_windows_without_term_is_still_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows consoles rarely set TERM but Win10+ speaks VT, so the bare
    "no TERM means no colour" rule would blank the whole UI there."""
    monkeypatch.setattr(term_caps.os, "name", "nt")
    assert _caps(monkeypatch).color == "256"
    assert _caps(monkeypatch, NO_COLOR="1").color == "none"


def test_term_program_upgrades_a_lying_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apple Terminal/iTerm/VS Code under-report through TERM."""
    caps = _caps(monkeypatch, TERM="xterm-256color", TERM_PROGRAM="iTerm.app")
    assert caps.color == "truecolor"
    assert caps.program == "iTerm.app"


def test_term_program_never_downgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, TERM="xterm-256color", COLORTERM="truecolor",
                 TERM_PROGRAM="Apple_Terminal")
    assert caps.color == "truecolor"  # Apple_Terminal's table entry is 256


def test_term_program_cannot_resurrect_dumb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = _caps(monkeypatch, TERM="dumb", TERM_PROGRAM="iTerm.app")
    assert caps.color == "none"


# -- unicode ----------------------------------------------------------------


@pytest.mark.parametrize("var", ["LANG", "LC_ALL", "LC_CTYPE"])
def test_utf8_locale_enables_unicode(
    monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    caps = _caps(monkeypatch, TERM="xterm", **{var: "en_US.UTF-8"})
    assert caps.unicode is True
    assert caps.box_drawing is True


def test_utf8_spelled_without_a_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, TERM="xterm", LANG="C.utf8")
    assert caps.unicode is True


def test_non_utf8_locale_disables_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = _caps(monkeypatch, TERM="xterm", LANG="C", LC_ALL="POSIX")
    assert caps.unicode is False
    assert caps.box_drawing is False


def test_lc_all_outranks_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, TERM="xterm", LANG="en_US.UTF-8", LC_ALL="C")
    assert caps.unicode is False


# -- caching ----------------------------------------------------------------


def test_detect_is_cached_until_force_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _caps(monkeypatch, TERM="xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")
    assert detect() is first  # cached: the env change is not seen
    assert detect(force_refresh=True).color == "none"


def test_caps_are_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, TERM="xterm")
    with pytest.raises(Exception):
        caps.color = "truecolor"  # type: ignore[misc]
    assert isinstance(caps, TerminalCaps)


# -- no interactive probing -------------------------------------------------


def test_detection_never_writes_to_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DA1/DECRQSS query hangs on terminals that never answer and leaks the
    reply into stdin when they do. Detection must be pure env + isatty."""
    out = _FakeStream(True)
    monkeypatch.setattr("sys.stdin", _FakeStream(True))
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setenv("TERM", "xterm-256color")
    detect(force_refresh=True)
    assert out.getvalue() == ""


# -- strip_ansi -------------------------------------------------------------


def test_strip_ansi_removes_sgr_codes() -> None:
    assert strip_ansi("\033[38;5;113mgreen\033[0m") == "green"
    assert strip_ansi("\033[90mdim\033[0m and \033[31mred\033[0m") == "dim and red"


def test_strip_ansi_handles_escapes_at_both_boundaries() -> None:
    assert strip_ansi("\033[0m") == ""
    assert strip_ansi("\033[1mbold") == "bold"
    assert strip_ansi("bold\033[0m") == "bold"
    assert strip_ansi("\033[1m\033[31mx\033[0m\033[0m") == "x"


def test_strip_ansi_is_a_noop_on_plain_text() -> None:
    assert strip_ansi("") == ""
    assert strip_ansi("plain ascii") == "plain ascii"
    assert strip_ansi("unicode ── ✓ box") == "unicode ── ✓ box"


def test_strip_ansi_removes_cursor_and_erase_sequences() -> None:
    assert strip_ansi("\033[2J\033[H\033[Kclear") == "clear"
    assert strip_ansi("a\033[3Db") == "ab"


def test_strip_ansi_removes_osc_title_sequences() -> None:
    assert strip_ansi("\033]0;a title\007hi") == "hi"
    assert strip_ansi("\033]8;;https://x.dev\033\\link\033]8;;\033\\") == "link"


def test_strip_ansi_length_is_the_printable_width_basis() -> None:
    painted = "\033[38;5;113m*\033[0m mantis \033[90m(v2)\033[0m"
    assert strip_ansi(painted) == "* mantis (v2)"
    assert len(strip_ansi(painted)) == len("* mantis (v2)")


# -- terminal_size: one source of layout truth ------------------------------


def test_terminal_size_falls_back_to_80x24(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless/CI has no window; the fallback is the contract every call site
    used to spell out by hand."""
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size(fallback),
    )
    assert terminal_size() == (80, 24)


def test_terminal_size_honors_mantis_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANTIS_TERM_WIDTH", "120")
    monkeypatch.setenv("MANTIS_TERM_HEIGHT", "40")
    assert terminal_size() == (120, 40)


def test_terminal_size_ignores_garbage_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANTIS_TERM_WIDTH", "wide")
    monkeypatch.setenv("MANTIS_TERM_HEIGHT", "-3")
    width, height = terminal_size()
    assert width > 0 and height > 0


def test_caps_carry_the_size(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = _caps(monkeypatch, TERM="xterm", MANTIS_TERM_WIDTH="100",
                 MANTIS_TERM_HEIGHT="30")
    assert (caps.width, caps.height) == (100, 30)


# -- the wiring: NO_COLOR has to reach the actual TUI ------------------------
#
# Detection is only half the fix. `tui_fullscreen.py` defines its palette as
# module-level constants and interpolates them into every row it renders, so if
# those constants stay raw escapes then `NO_COLOR=1` changes nothing a user can
# see. These lock the constants to `term_caps` and prove it end to end on a
# real rendered line.

_PALETTE_NAMES = (
    "_GREEN", "_YELLOW", "_DIM", "_GREY", "_RESET",
    "_BOLD", "_CYAN", "_RED", "_HL", "_AMBER", "_AMBER_FG",
)


@pytest.fixture()
def tui_fullscreen(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The module with its palette restored afterwards — ``_refresh_palette``
    rebinds module globals, and a leaked blank palette would silently strip
    colour out of every other test in the session."""
    mod = pytest.importorskip("mantis_agent.tui_fullscreen")
    saved = {name: getattr(mod, name) for name in (*_PALETTE_NAMES, "_PAINT")}
    yield mod
    for name, value in saved.items():
        setattr(mod, name, value)


def _repaint(mod: Any, monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mod._refresh_palette()


def test_tui_palette_goes_blank_under_no_color(
    tui_fullscreen: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every colour constant in the fullscreen TUI must be the empty string
    when detection says "none" — that is what makes the rendered rows clean."""
    _repaint(tui_fullscreen, monkeypatch, NO_COLOR="1", TERM="xterm-256color",
             COLORTERM="truecolor")
    blank = {name: getattr(tui_fullscreen, name) for name in _PALETTE_NAMES}
    assert blank == dict.fromkeys(_PALETTE_NAMES, "")


def test_tui_palette_paints_when_the_terminal_can(
    tui_fullscreen: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repaint(tui_fullscreen, monkeypatch, MANTIS_TERM_COLOR="256")
    for name in _PALETTE_NAMES:
        assert getattr(tui_fullscreen, name).startswith("\x1b["), name


def test_footer_line_is_escape_free_under_no_color(
    tui_fullscreen: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end on the real renderer: the footer is the status line under the
    prompt (mode · model · knobs · context), and it is the one place a mode
    colour is built from ``_MODE_ANSI`` rather than a constant."""
    _repaint(tui_fullscreen, monkeypatch, NO_COLOR="1", TERM="xterm-256color")
    for mode_idx in range(4):
        line = tui_fullscreen._footer_line(
            mode_idx, "claude-opus-5", knobs=["effort=high"], ctx="12k/32k 38% · $0.03")
        assert "\x1b" not in line, (mode_idx, repr(line))
        assert "claude-opus-5" in line  # the text itself survives


def test_footer_line_paints_the_mode_when_color_is_allowed(
    tui_fullscreen: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repaint(tui_fullscreen, monkeypatch, MANTIS_TERM_COLOR="256")
    plan = tui_fullscreen._footer_line(2, "claude-opus-5")
    assert plan.startswith("\x1b[36m")          # ansicyan — plan mode
    assert "\x1b[90mclaude-opus-5\x1b[0m" in plan
    assert strip_ansi(plan) == "⏸ plan mode on (shift+tab to cycle)   claude-opus-5"


def test_no_sgr_escape_literals_survive_outside_the_palette(
    tui_fullscreen: Any,
) -> None:
    """Guards the wiring against drift. Any colour escape written straight into
    a render string bypasses ``_refresh_palette`` and would still paint under
    ``NO_COLOR`` — so the only SGR literals allowed in the module are the ones
    inside ``_refresh_palette``/``_mode_color``. Non-SGR sequences (the
    ``\\033[2J\\033[3J\\033[H`` screen clear) are not colour and are exempt."""
    import inspect  # noqa: PLC0415
    import re as _re  # noqa: PLC0415

    src, start = inspect.getsourcelines(tui_fullscreen)
    sgr = _re.compile(r"\\033\[[0-9;{]")  # colour-ish: SGR params or an f-slot
    clear = _re.compile(r"\\033\[[23]J")  # erase-screen/scrollback — not colour
    allowed: set[int] = set()
    for fn in (tui_fullscreen._refresh_palette, tui_fullscreen._mode_color):
        body, first = inspect.getsourcelines(fn)
        allowed.update(range(first, first + len(body)))
    strays = [
        (start + i, line.rstrip())
        for i, line in enumerate(src)
        if sgr.search(line) and not clear.search(line) and (start + i) not in allowed
    ]
    assert not strays, "raw colour escapes outside the palette:\n" + "\n".join(
        f"  line {n}: {text}" for n, text in strays)


# -- the whole live app, rendered ------------------------------------------


def _stand_up_app(monkeypatch: pytest.MonkeyPatch, mod: Any) -> tuple[Any, Any, list]:
    """Build the REAL fullscreen application and hand back its controls.

    ``run_fullscreen`` constructs the layout and then awaits ``app.run_async()``
    forever, so we stub that one await out: everything before it — the buffers,
    the key bindings, every ``FormattedTextControl`` — is the production code
    path, and its ``finally`` block still runs the normal teardown.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout.controls import FormattedTextControl

    from mantis_agent.tui import MODES

    captured: dict = {}

    async def _capture(self: Any, *a: Any, **kw: Any) -> int:
        captured["app"] = self
        return 0

    monkeypatch.setattr(Application, "run_async", _capture)
    monkeypatch.setenv("MANTIS_NO_CLIPBOARD_HINT", "1")

    agent = mock.AsyncMock()
    agent.model_capability.context_window = 200_000
    tui = mock.MagicMock()
    tui.messages, tui.transcript, tui.pending_attachments = [], None, []
    tui.mode_idx = len(MODES) - 2          # plan mode — a coloured left segment
    tui.model, tui.backend = "claude-opus-5", "anthropic"
    tui.effort, tui.verbosity, tui.reasoning_mode = "high", None, None
    tui.vim_mode = False
    tui._check_unclean_exit.return_value = None
    tui._available_models.return_value = (["claude-opus-5", "gpt-5.6-sol"], {})
    tui._build_agent.return_value = agent
    tui.agent = agent
    tui._live_subagents = {1: {"type": "explore", "started": 0.0, "tools": 3,
                               "desc": "scan tests", "last_event": "Grep",
                               "events": [(0.0, "Grep tests/")]}}

    asyncio.run(mod.run_fullscreen(tui))
    app = captured["app"]
    controls = [w.content for w in app.layout.find_all_windows()
                if isinstance(w.content, FormattedTextControl)
                and callable(w.content.text)]
    return app, tui, controls


def _open_the_overlays(app: Any, tui: Any, controls: list) -> None:
    """Poke the closed-over ``state`` so the overlays render: those rows carry
    the highlight/amber backgrounds and the cyan headings, which is exactly
    where a missed escape would hide."""
    from prompt_toolkit.application.current import set_app

    perm = next(c.text for c in controls if c.text.__name__ == "perm_ft")
    state = perm.__closure__[perm.__code__.co_freevars.index("state")].cell_contents
    state.update(
        pending_perm={"sel": 0, "prompt": "Bash(rm -rf build)"},
        picking_effort={"items": ["low", "medium", "high"], "sel": 2},
        retry_note=("connection reset — retrying", time.monotonic() + 3600),
        working=True, started=time.monotonic(), word="Mulling", frame=0,
        agent_inspector={"sel": 0, "detail": False},
    )
    # The model picker is opened by a slash command, not by a control — walk the
    # closure graph to the real opener rather than faking its state by hand.
    seen: set[int] = set()
    queue = [c.text for c in controls] + [b.handler for b in app.key_bindings.bindings]
    while queue:
        fn = queue.pop()
        if id(fn) in seen or not hasattr(fn, "__code__"):
            continue
        seen.add(id(fn))
        for name, cell in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if name == "_open_model_picker":
                with set_app(app):
                    value("")
                return
            if hasattr(value, "__code__"):
                queue.append(value)


def _render(controls: list) -> list[tuple[str, str]]:
    out = []
    for ctl in controls:
        ft = ctl.text()
        out.append((ctl.text.__name__, getattr(ft, "value", "") or ""))
    return out


def test_every_live_control_renders_escape_free_under_no_color(
    tui_fullscreen: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The end-to-end contract: stand up the real app, open its overlays, and
    render every control. Under ``NO_COLOR`` not one byte of ESC may come out;
    with colour allowed the exact same controls paint again."""
    pytest.importorskip("prompt_toolkit")
    app, tui, controls = _stand_up_app(monkeypatch, tui_fullscreen)
    _open_the_overlays(app, tui, controls)

    _repaint(tui_fullscreen, monkeypatch, NO_COLOR="1", TERM="xterm-256color",
             COLORTERM="truecolor")
    plain = _render(controls)
    painted_rows = [(name, body) for name, body in plain if "\x1b" in body]
    assert not painted_rows, painted_rows
    # ...and it really did render something, rather than 16 empty strings.
    names = {name for name, body in plain if body}
    assert {"footer_ft", "picker_ft", "perm_ft"} <= names, sorted(names)

    monkeypatch.delenv("NO_COLOR")
    _repaint(tui_fullscreen, monkeypatch, MANTIS_TERM_COLOR="256")
    colored = dict(_render(controls))
    assert "\x1b[30;48;5;113m" in colored["perm_ft"]   # selected-row highlight
    assert "\x1b[36m" in colored["footer_ft"]          # plan-mode cyan
    assert strip_ansi(colored["footer_ft"]) == dict(plain)["footer_ft"]
