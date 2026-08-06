"""What this terminal can actually do — colour depth, unicode, and its size.

Mantis used to answer "can I paint?" with ``sys.stdout.isatty()`` and then emit
hardcoded escapes (``\\033[38;5;113m`` and friends) regardless. Two things fell
out of that:

* ``NO_COLOR=1`` did nothing. The convention (https://no-color.org) is honoured
  by ripgrep, fd, gh, cargo — every serious CLI — and Mantis ignored it, so a
  user who has it exported globally still got escape codes in their pager.
* ``shutil.get_terminal_size((80, 24))`` was spelled out at ~10 call sites
  across ``tui.py`` and ``tui_fullscreen.py``, so there was no single source of
  layout truth and no way to override the size for a test or a recording.

This module is the answer to both, and is deliberately self-contained: pure
stdlib, no imports from the rest of the package, so anything (including the two
very large TUI files) can import it without a cycle.

Detection is **passive**. We never write a query escape (DA1, DECRQSS, OSC 11)
and wait for the terminal to answer: terminals that don't implement the query
never reply and the read blocks forever, and terminals that do reply inject the
answer into stdin where — if nobody consumes it — it lands in the user's next
prompt as garbage. Environment variables and ``isatty()`` only.

Precedence, most authoritative first:

1. ``MANTIS_TERM_*`` — an explicit in-app override, so it wins outright.
2. ``NO_COLOR`` set to any **non-empty** value → no colour. Per the spec the
   *presence* of a value is the signal, so ``NO_COLOR=0`` still disables; an
   empty string is explicitly not a signal.
3. ``FORCE_COLOR`` → colour even when stdout isn't a tty (CI logs, ``| less
   -R``). ``FORCE_COLOR=0`` is the opt-out, ``1``/``2``/``3`` pick a level
   as a floor, anything else just means "keep detecting, floor at 16".
4. not a tty → no colour.
5. ``COLORTERM`` in ``truecolor``/``24bit`` → 24-bit.
6. ``TERM``: ``dumb`` disables colour *and* unicode; ``*-256color`` → 256.
7. ``TERM_PROGRAM`` for the terminals that under-report through ``TERM``
   (Apple Terminal ships ``TERM=xterm-256color`` while iTerm and VS Code do
   truecolor). This can only *upgrade* a level, never downgrade one.
8. ``LC_ALL``/``LC_CTYPE``/``LANG`` containing UTF-8 → unicode is safe.

Judgement call worth knowing about: rule 2 sits above rule 3, so ``NO_COLOR``
beats ``FORCE_COLOR``. Some libraries (chalk) do the reverse. Accessibility
wins over convenience here — a user who exported ``NO_COLOR`` did so for a
reason, and ``MANTIS_TERM_COLOR`` remains available to override it per-app.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass

__all__ = [
    "COLOR_LEVELS",
    "TerminalCaps",
    "detect",
    "strip_ansi",
    "supports_color",
    "terminal_size",
]

# Ordered weakest → strongest so `max(..., key=COLOR_LEVELS.index)` is an
# upgrade and comparisons read like the thing they mean.
COLOR_LEVELS: tuple[str, ...] = ("none", "16", "256", "truecolor")

# Terminals whose TERM understates what they can do. Values are floors: the
# entry only applies if detection hasn't already found something better.
_PROGRAM_COLOR: dict[str, str] = {
    "iTerm.app": "truecolor",
    "Apple_Terminal": "256",
    "vscode": "truecolor",
    "WezTerm": "truecolor",
    "Hyper": "truecolor",
    "ghostty": "truecolor",
    "alacritty": "truecolor",
    "kitty": "truecolor",
    "warpterminal": "truecolor",
    "rio": "truecolor",
    "tabby": "truecolor",
}

# ESC-introduced sequences: CSI (``ESC [ … final``), OSC (``ESC ] … BEL`` or
# ``ESC ] … ST``) and the single-character Fe escapes. Written as one
# alternation so a sequence at either end of the string is stripped the same
# way one in the middle is.
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"          # CSI: params, intermediates, final byte
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: terminated by BEL or ST
    r"|[@-Z\\-_]"                   # Fe: ESC 7, ESC M, ESC =, …
    r")"
)

_TRUECOLOR_TERMS = {"truecolor", "24bit"}
_FALLBACK_SIZE = (80, 24)


@dataclass(frozen=True)
class TerminalCaps:
    """A frozen snapshot of what the attached terminal supports.

    ``width``/``height`` are the size *at detection time*. Anything that
    repaints on SIGWINCH must call :func:`terminal_size` per frame instead of
    reading these — that's why the size helper is public and separate.
    """

    is_tty: bool
    color: str  # one of COLOR_LEVELS
    unicode: bool
    box_drawing: bool
    width: int
    height: int
    term: str
    program: str

    @property
    def color_depth(self) -> int:
        """Number of colours implied by ``color`` — handy for prompt_toolkit
        style tables that want a count rather than a name."""
        return {"none": 0, "16": 16, "256": 256, "truecolor": 1 << 24}[self.color]


_CACHE: TerminalCaps | None = None


def _env(name: str) -> str:
    """``os.environ`` lookup that treats unset and empty the same, except where
    the caller deliberately checks for presence (``NO_COLOR``)."""
    return (os.environ.get(name) or "").strip()


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _falsey(value: str) -> bool:
    return value.lower() in {"0", "false", "no", "off"}


def _stream_is_tty() -> bool:
    """Both ends must be a terminal. If stdout is redirected to a file the
    escapes end up *in the file*; if stdin isn't a tty there's no interactive
    session to paint for. Streams can be replaced with objects that lack
    ``isatty`` (pytest capture, some notebook shims) — treat that as "not"."""
    for stream in (sys.stdin, sys.stdout):
        try:
            if not stream.isatty():
                return False
        except (AttributeError, ValueError):  # detached/closed stream
            return False
    return True


def _upgrade(current: str, floor: str) -> str:
    """Return whichever of the two levels is stronger. Used so a TERM_PROGRAM
    hint can raise a level but never lower one detection already earned."""
    return max(current, floor, key=COLOR_LEVELS.index)


def _positive_int(raw: str) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def terminal_size() -> tuple[int, int]:
    """``(columns, lines)`` — the ONE place ``shutil.get_terminal_size`` is
    called.

    ``tui.py`` and ``tui_fullscreen.py`` between them spell
    ``shutil.get_terminal_size((80, 24))`` at ~10 sites, which means the
    fallback and any future clamping have to be kept in sync by hand and no
    test or screen recording can pin the layout. Those call sites should
    migrate here; new code should never call ``shutil`` directly.

    ``MANTIS_TERM_WIDTH``/``MANTIS_TERM_HEIGHT`` override both dimensions
    independently (deterministic snapshots, ``asciinema`` recordings, CI diffs).
    Garbage or non-positive values are ignored rather than fatal — a bad env
    var must not be able to crash the renderer.
    """
    size = shutil.get_terminal_size(_FALLBACK_SIZE)
    width = _positive_int(_env("MANTIS_TERM_WIDTH")) or size.columns
    height = _positive_int(_env("MANTIS_TERM_HEIGHT")) or size.lines
    # A zero/negative size is reported by some CI runners and by Windows
    # consoles mid-resize; fall back rather than hand out a divide-by-zero.
    return (width if width > 0 else _FALLBACK_SIZE[0],
            height if height > 0 else _FALLBACK_SIZE[1])


def _detect_color(is_tty: bool, term: str, program: str) -> str:
    """The precedence ladder from the module docstring, colour dimension."""
    # 1. Explicit app override — above everything, including NO_COLOR.
    override = _env("MANTIS_TERM_COLOR").lower()
    if override in COLOR_LEVELS:
        return override
    if override and _falsey(override):
        return "none"

    # 2. NO_COLOR: presence of a non-empty value is the signal, whatever the
    #    value is — `NO_COLOR=0` disables colour, because the spec is about
    #    presence, not truthiness. Read raw rather than through `_env`: the
    #    empty string is the one documented non-signal, but a lone space is a
    #    value and so still counts.
    if os.environ.get("NO_COLOR", "") != "":
        return "none"

    # 3. FORCE_COLOR: colour even without a tty (CI logs, `| less -R`). A
    #    numeric value is a *floor*, not a cap — `FORCE_COLOR=1` in a
    #    truecolor terminal shouldn't cost you 24-bit output, it should only
    #    guarantee that at least the 16 basic colours get emitted.
    forced = False
    forced_floor = "none"
    if "FORCE_COLOR" in os.environ:
        raw = os.environ["FORCE_COLOR"].strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return "none"
        forced = True
        forced_floor = {
            "2": "256", "256": "256",
            "3": "truecolor", "truecolor": "truecolor", "24bit": "truecolor",
        }.get(raw, "16")

    # 4. Redirected output gets plain text.
    if not is_tty and not forced:
        return "none"

    level = "none"

    # 5. COLORTERM is the only reliable truecolor advertisement.
    if _env("COLORTERM").lower() in _TRUECOLOR_TERMS:
        level = "truecolor"

    # 6. TERM. "dumb" is a hard stop — Emacs' shell buffer and friends render
    #    escapes literally — unless the user explicitly forced colour on.
    lower_term = term.lower()
    if lower_term == "dumb":
        return forced_floor if forced else "none"
    if "256color" in lower_term or lower_term.endswith("-256"):
        level = _upgrade(level, "256")
    elif lower_term:
        level = _upgrade(level, "16")
    elif os.name == "nt":
        # Windows rarely sets TERM at all, but Win10+ consoles speak VT and
        # CPython enables it for us. Absent TERM elsewhere we stay silent —
        # here it's the norm, so treat a tty as capable.
        level = _upgrade(level, "256")

    # 7. Terminals that lie downward through TERM.
    if program:
        floor = _PROGRAM_COLOR.get(program) or _PROGRAM_COLOR.get(program.lower())
        if floor:
            level = _upgrade(level, floor)

    return _upgrade(level, forced_floor) if forced else level


def _detect_unicode(term: str) -> bool:
    """UTF-8 per the locale, with ``TERM=dumb`` vetoing it outright."""
    override = _env("MANTIS_TERM_UNICODE")
    if override:
        return _truthy(override)
    if term.lower() == "dumb":
        return False
    # LC_ALL outranks LC_CTYPE outranks LANG (POSIX). First one set decides;
    # falling through to the next would let a stale LANG override LC_ALL=C.
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = _env(name)
        if value:
            normalized = value.lower().replace("-", "")
            return "utf8" in normalized
    # No locale at all: trust the stream's own encoding (Windows Terminal and
    # modern Pythons report utf-8 there even with no LANG set).
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    return "utf8" in encoding


def detect(force_refresh: bool = False) -> TerminalCaps:
    """Capabilities of the attached terminal, cached after the first call.

    Cached because every frame of the TUI would otherwise re-read a dozen
    environment variables, and because the answer is stable for a session:
    nothing short of the user re-exec'ing changes ``NO_COLOR`` or ``TERM``
    mid-run. Pass ``force_refresh=True`` after deliberately mutating the
    environment (tests, ``/theme``-style commands) to recompute.
    """
    global _CACHE
    if _CACHE is not None and not force_refresh:
        return _CACHE

    term = _env("TERM")
    program = _env("TERM_PROGRAM")

    tty_override = _env("MANTIS_TERM_TTY")
    is_tty = _truthy(tty_override) if tty_override else _stream_is_tty()

    color = _detect_color(is_tty, term, program)
    unicode_ok = _detect_unicode(term)

    # Box drawing is a strict subset of unicode: a terminal can be UTF-8 clean
    # and still render ── as a broken glyph in the wrong font, so it gets its
    # own escape hatch.
    box_override = _env("MANTIS_TERM_BOX")
    box = _truthy(box_override) if box_override else unicode_ok

    width, height = terminal_size()
    _CACHE = TerminalCaps(
        is_tty=is_tty,
        color=color,
        unicode=unicode_ok,
        box_drawing=box and unicode_ok,
        width=width,
        height=height,
        term=term,
        program=program,
    )
    return _CACHE


def supports_color() -> bool:
    """True when it is safe to emit SGR escapes. The one-liner every call site
    that currently open-codes ``isatty()`` should be asking instead."""
    return detect().color != "none"


def strip_ansi(s: str) -> str:
    """Text with every escape sequence removed.

    Two jobs: measuring the printable width of a painted string (``len`` over
    raw escapes over-counts wildly, breaking every truncation and box border),
    and producing plain output when :func:`supports_color` is False but the
    string was already built with colour in it.
    """
    if not s or "\x1b" not in s:
        return s
    return _ANSI_RE.sub("", s)
