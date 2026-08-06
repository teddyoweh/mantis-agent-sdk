"""The fullscreen UI measures and cuts in terminal *cells*, not code points.

``tui_fullscreen`` draws framed panels — the model picker, the /mcp card, the
/workflows overlay — by assembling coloured fragments and then trimming or
padding the result to a fixed column width. Both halves of that were wrong:

* the trim was a code-point slice, so it could land inside ``\\x1b[38;5;113m``
  and put ``8;5;113m`` on screen as literal text while leaving the terminal in
  whatever state the fragment set — corrupting every line printed *after* the
  panel, not just the panel;
* the measurement was ``len()``, so a CJK session title or an emoji counted one
  column per character and pushed the box border two columns past the edge.

These tests pin both, and pin the wiring: the risky call sites must go through
:mod:`mantis_agent.term_measure` rather than ``str.ljust``/``tui.ellipsize``.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from mantis_agent import term_measure, tui_fullscreen as tf
from mantis_agent.tui import ellipsize as _plain_ellipsize

# Complete escape sequences: CSI, OSC, and the single-character Fe escapes.
# Whatever ESC survives this has to be a fragment.
_COMPLETE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)

# A painted row shaped exactly like the ones the overlays build: SGR opener,
# text, reset, another opener, more text, reset.
PAINTED = "\x1b[38;5;113mhello\x1b[0m \x1b[90mworld wide\x1b[0m"


def _stripped(s: str) -> str:
    """``s`` with every *complete* escape removed."""
    return _COMPLETE.sub("", s)


def _leaks_escape(s: str) -> bool:
    """True if the terminal would be shown escape machinery as text."""
    rest = _stripped(s)
    if "\x1b" in rest:
        return True
    # The tail of a CSI that lost its ESC[ — "38;5;113m", "0m", "90m".
    return bool(re.search(r"\d+(?:;\d+)*m", rest))


# -- the cut is safe at every width -----------------------------------------


@pytest.mark.parametrize("width", list(range(0, 30)))
def test_cut_never_emits_a_partial_escape(width: int) -> None:
    out = tf._cut(PAINTED, width)
    assert not _leaks_escape(out), (width, out)
    assert term_measure.display_width(out) <= width


def test_cut_closes_the_colour_it_cut_into() -> None:
    """A cut inside a coloured run drops the run's own reset. Without a closer,
    every later line the app prints comes out green."""
    out = tf._cut(PAINTED, 4)
    assert out.endswith("\x1b[0m")
    assert _stripped(out) == "hel…"


def test_cut_leaves_a_string_that_already_fits_byte_for_byte() -> None:
    assert tf._cut(PAINTED, 40) is PAINTED
    assert tf._cut("plain", 5) == "plain"


def test_cut_does_not_split_a_grapheme_cluster() -> None:
    """Whatever survives the cut is a whole number of user-perceived characters:
    an accent keeps its base, a ZWJ family is kept or dropped entire, and no cut
    leaves a dangling joiner for the terminal to hang the next glyph off."""
    zwj = "\u200d"
    family = "a \U0001F468" + zwj + "\U0001F469" + zwj + "\U0001F467 b"
    for text in ("cafe\u0301 au lait", family):
        whole = list(term_measure.grapheme_clusters(text))
        for width in range(1, 12):
            out = tf._cut(text, width)
            kept = list(term_measure.grapheme_clusters(out))
            if kept and kept[-1] == "\u2026":
                kept.pop()
            assert kept == whole[: len(kept)], (width, out)
            assert not out.endswith(zwj)


# -- measurement is in cells -------------------------------------------------


def test_cells_ignores_escapes_and_counts_wide_glyphs() -> None:
    assert tf._cells(PAINTED) == len("hello world wide")
    assert tf._cells("日本語") == 6            # three characters, six columns
    assert tf._cells("🔒 needs key") == 12     # the padlock is two columns
    assert tf._cells("café") == 4        # combining mark is free


def test_pad_fills_to_a_cell_count_not_a_code_point_count() -> None:
    """``"日本語".ljust(10)`` is 4 spaces too many — the row it is in overshoots
    its frame by four columns and the border lands in the middle of the text."""
    padded = tf._pad("日本語", 10)
    assert term_measure.display_width(padded) == 10
    assert padded != "日本語".ljust(10)
    assert term_measure.display_width(tf._pad("x", 6, "right")) == 6
    assert tf._pad("x", 6, "right").endswith("x")


def test_a_rendered_row_of_cjk_stays_inside_its_frame() -> None:
    """The picker's row shape: gutter, padded name, right-aligned meta."""
    cw, rw = 40, 12
    name = tf._ellipsize("日本語のセッションタイトルはとても長い", cw - 4 - rw)
    row = f"   {tf._pad(name, cw - 4 - rw)}{tf._pad('🔒 needs key', rw + 1, 'right')} "
    assert term_measure.display_width(row) == cw + 1  # rw+1: padlock's extra cell
    assert not _leaks_escape(row)


# -- _ellipsize is a drop-in for tui.ellipsize -------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "short", "a much longer line of plain ascii text", "  collapse   me  now  ",
     "multi\nline\ttext", "exactly ten"],
)
@pytest.mark.parametrize("limit", [1, 2, 5, 10, 11, 40])
def test_ellipsize_matches_the_plain_one_on_ascii(text: str, limit: int) -> None:
    """The surfaces this replaces are PTY-tested with ASCII content, so the
    answer there has to be unchanged byte for byte."""
    assert tf._ellipsize(text, limit) == _plain_ellipsize(text, limit)


def test_ellipsize_measures_wide_text_in_cells() -> None:
    # The old one keeps 7 characters = 14 columns for a limit of 8.
    out = tf._ellipsize("日本語テキストです", 8)
    assert term_measure.display_width(out) <= 8
    assert term_measure.display_width(_plain_ellipsize("日本語テキストです", 8)) > 8


def test_ellipsize_is_escape_safe() -> None:
    out = tf._ellipsize(PAINTED, 6)
    assert not _leaks_escape(out)
    assert term_measure.display_width(out) <= 6


# -- the wiring --------------------------------------------------------------

_SRC = pathlib.Path(tf.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _body_of(name: str) -> str:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError(f"{name} is gone from tui_fullscreen")


def test_no_code_point_truncation_helper_is_imported_any_more() -> None:
    """``workflow_view._truncate_visible`` counts code points and only knows
    ``ESC…m``; the overlays here must not measure with it."""
    assert "_truncate_visible" not in _SRC
    assert "_visible_len" not in _SRC


@pytest.mark.parametrize(
    "func",
    ["picker_ft", "_mcp_list_body", "_mcp_lines", "_workflows_lines",
     "_live_subagent_rows", "_build_session_items", "_build_rewind_items"],
)
def test_truncating_surfaces_do_not_use_the_code_point_ellipsize(func: str) -> None:
    body = _body_of(func)
    assert "from .tui import ellipsize" not in body
    assert not re.search(r"(?<![_\w])ellipsize\(", body), func


@pytest.mark.parametrize(
    "func", ["picker_ft", "_mcp_list_body", "_workflows_lines"]
)
def test_frame_rows_pad_in_cells(func: str) -> None:
    """``ljust``/``rjust`` pad by code point, which is the same bug as the slice
    with the sign flipped — the row comes out too wide instead of too narrow."""
    body = _body_of(func)
    assert ".ljust(" not in body, func
    assert ".rjust(" not in body, func
