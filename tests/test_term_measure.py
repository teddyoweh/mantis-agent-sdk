"""Terminal text measurement — cells, not characters; clusters, not code points.

``len(s)`` is wrong three ways at once for anything the TUI prints: a CJK glyph
occupies two cells, a combining accent occupies none, and the SGR escapes the
fullscreen UI wraps around every line occupy none *and* must never be cut in
half. A truncation that lands inside ``\\x1b[38;5;113m`` emits the tail of the
sequence as literal text and leaves the terminal in whatever state the partial
escape put it in — every line after it is corrupted, which is why the plan
calls this a correctness fix rather than a feature.

These lock the three primitives in :mod:`mantis_agent.term_measure`:
``display_width`` (what it costs to print), ``truncate`` (cut on a cluster and
escape boundary, never inside one) and ``pad`` (fill by cells).
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from mantis_agent import term_measure
from mantis_agent.term_measure import (
    char_width,
    display_width,
    fit,
    grapheme_clusters,
    pad,
    truncate,
)

# Spelled with escapes so the file stays readable and greppable.
RED = "\x1b[31m"
RESET = "\x1b[0m"
BOLD_256 = "\x1b[1m\x1b[38;5;113m"
LINK_OPEN = "\x1b]8;;https://mantisagent.cc\x1b\\"
LINK_CLOSE = "\x1b]8;;\x1b\\"

FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # man ZWJ woman ZWJ girl
FLAG_US = "\U0001f1fa\U0001f1f8"  # regional indicators U + S
HEART = "❤"  # text presentation by default
HEART_EMOJI = "❤️"  # + VS16 → emoji presentation

# Re-scanned over truncate() output to prove nothing partial escaped. Mirrors
# the sequence grammar the module tokenizes with.
_ESC_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _strip(s: str) -> str:
    return _ESC_RE.sub("", s)


def _no_partial_escape(s: str) -> bool:
    """True when every ESC in ``s`` introduces a complete sequence."""
    return "\x1b" not in _strip(s)


# -- display_width -----------------------------------------------------------


def test_ascii_costs_one_cell_per_character() -> None:
    assert display_width("") == 0
    assert display_width("hello") == 5
    assert display_width("mantis run --resume") == 19


def test_east_asian_wide_and_fullwidth_cost_two() -> None:
    assert display_width("日本語") == 6  # 日本語
    assert display_width("ａｂ") == 4  # fullwidth ａｂ
    assert display_width("안녕") == 4  # precomposed Hangul
    assert display_width("a日b") == 4


def test_combining_marks_are_free() -> None:
    assert display_width("é") == 1  # e + COMBINING ACUTE
    assert display_width("café") == 4
    assert display_width("é") == 1  # precomposed, for comparison
    # Stacked marks still cost nothing beyond the base.
    assert display_width("á̧̈") == 1


def test_ambiguous_width_is_treated_as_narrow() -> None:
    """East Asian Ambiguous renders single-width outside CJK locales, which is
    where Mantis's own glyphs (…, ●, ─) live — counting them as two would make
    every box border overshoot."""
    assert display_width("…") == 1  # …
    assert display_width("●") == 1  # ●
    assert display_width("─" * 10) == 10  # ─


def test_emoji_cost_two_cells() -> None:
    assert display_width("\U0001f600") == 2  # 😀
    assert display_width("\U0001f680 ship") == 7


def test_variation_selector_16_promotes_to_emoji_width() -> None:
    """❤ is a one-cell text glyph until VS16 asks for the emoji rendering."""
    assert display_width(HEART) == 1
    assert display_width(HEART_EMOJI) == 2


def test_zwj_sequence_is_one_two_cell_glyph() -> None:
    assert display_width(FAMILY) == 2


def test_regional_indicator_pair_is_one_flag() -> None:
    assert display_width(FLAG_US) == 2
    assert display_width(FLAG_US * 2) == 4


def test_embedded_escapes_are_weightless() -> None:
    assert display_width(RED + "red" + RESET) == 3
    assert display_width(BOLD_256 + "日本" + RESET) == 4
    assert display_width(LINK_OPEN + "docs" + LINK_CLOSE) == 4
    assert display_width(RESET) == 0


def test_control_characters_and_zero_width_spaces_cost_nothing() -> None:
    assert display_width("\x07") == 0
    assert display_width("a​b") == 2  # ZERO WIDTH SPACE
    assert display_width("a­b") == 2  # SOFT HYPHEN


def test_char_width_exposes_the_per_code_point_rule() -> None:
    assert char_width("a") == 1
    assert char_width("日") == 2
    assert char_width("́") == 0
    assert char_width("‍") == 0


# -- grapheme clustering -----------------------------------------------------


def test_clusters_keep_marks_joiners_and_flags_whole() -> None:
    assert list(grapheme_clusters("abc")) == ["a", "b", "c"]
    assert list(grapheme_clusters("éx")) == ["é", "x"]
    assert list(grapheme_clusters(FAMILY)) == [FAMILY]
    assert list(grapheme_clusters(FLAG_US + "a")) == [FLAG_US, "a"]
    assert list(grapheme_clusters(HEART_EMOJI)) == [HEART_EMOJI]
    assert list(grapheme_clusters("\r\n")) == ["\r\n"]


def test_clusters_do_not_see_escape_sequences() -> None:
    """Clustering is about text; escapes are handled by the tokenizer that
    wraps it, so callers get the printable clusters only."""
    assert list(grapheme_clusters(RED + "ab" + RESET)) == ["a", "b"]


# -- truncate ----------------------------------------------------------------


def test_a_string_that_fits_is_returned_untouched() -> None:
    s = RED + "fits" + RESET
    assert truncate(s, 4) is s
    assert truncate("abc", 99) == "abc"


def test_ascii_is_cut_to_exactly_the_budget() -> None:
    out = truncate("hello world", 8)
    assert out == "hello w…"
    assert display_width(out) == 8


def test_a_wide_glyph_is_never_split_in_half() -> None:
    out = truncate("日本語テスト", 5)
    assert out == "日本…"
    assert display_width(out) == 5
    # An odd budget leaves a cell unused rather than emitting half a glyph.
    assert display_width(truncate("日本語", 3)) <= 3


def test_a_combining_mark_stays_with_its_base() -> None:
    out = truncate("cafébar", 4)
    assert out == "caf…"
    assert truncate("aéxy", 3) == "aé…"


def test_a_zwj_sequence_is_kept_or_dropped_whole() -> None:
    out = truncate(FAMILY + "tail", 3)
    assert out == FAMILY + "…"
    # Not enough room for the glyph plus the ellipsis: drop the whole cluster.
    assert truncate(FAMILY + "tail", 2) == "…"
    assert "‍" not in truncate(FAMILY + "tail", 2)


def test_a_flag_is_kept_or_dropped_whole() -> None:
    assert truncate(FLAG_US + "USA", 3) == FLAG_US + "…"
    assert truncate(FLAG_US + "USA", 2) == "…"


def test_escapes_survive_truncation_and_never_split() -> None:
    out = truncate(RED + "abcdef" + RESET, 4)
    assert out.startswith(RED)
    assert _strip(out) == "abc…"
    assert _no_partial_escape(out)


def test_an_escape_exactly_at_the_boundary_is_not_cut() -> None:
    """The budget runs out precisely where the sequence begins — the classic
    off-by-one that leaks ``[38;5;113m`` into the transcript."""
    s = "abc" + BOLD_256 + "def"
    out = truncate(s, 3)
    assert _no_partial_escape(out)
    assert display_width(out) <= 3
    # The escape belongs to text we dropped, so it must not be emitted.
    assert "38;5;113" not in out


def test_styling_left_open_by_a_cut_is_closed() -> None:
    """Cutting inside a coloured run would otherwise bleed the colour onto
    everything the terminal prints afterwards."""
    out = truncate(RED + "abcdef" + RESET, 4)
    assert out.endswith(RESET)
    plain = truncate("abcdef", 4)
    assert "\x1b" not in plain  # nothing to close, nothing appended


def test_an_open_hyperlink_is_closed_by_a_cut() -> None:
    out = truncate(LINK_OPEN + "documentation" + LINK_CLOSE, 6)
    assert out.endswith(LINK_CLOSE)
    assert _no_partial_escape(out)
    assert _strip(out) == "docum…"


def test_a_dangling_escape_is_never_emitted() -> None:
    """Input can already be malformed (a stream cut mid-sequence); an
    incomplete escape is never mistaken for a complete one and kept."""
    out = truncate("abcd\x1b[3", 3)
    assert "\x1b" not in out
    assert out == "ab…"


def test_degenerate_budgets_are_safe() -> None:
    assert truncate("hello", 0) == ""
    assert truncate("hello", -5) == ""
    assert truncate("hello", 1) == "…"


def test_an_ellipsis_wider_than_the_budget_falls_back_to_a_hard_cut() -> None:
    assert truncate("hello", 2, ellipsis="...") == "he"
    assert display_width(truncate("hello", 2, ellipsis="...")) == 2


def test_the_ellipsis_is_configurable_and_may_be_empty() -> None:
    assert truncate("hello world", 5, ellipsis="") == "hello"
    assert truncate("hello world", 8, ellipsis="...") == "hello..."
    assert display_width(truncate("hello world", 8, ellipsis="...")) == 8


# -- pad / fit ---------------------------------------------------------------


def test_pad_fills_to_the_display_width() -> None:
    assert pad("ab", 5) == "ab   "
    assert pad("日本", 6) == "日本  "
    assert pad("é", 3) == "é  "


def test_pad_measures_past_escapes() -> None:
    out = pad(RED + "ab" + RESET, 5)
    assert out == RED + "ab" + RESET + "   "
    assert display_width(out) == 5


def test_pad_never_shrinks() -> None:
    assert pad("hello", 2) == "hello"
    assert pad("", 0) == ""


def test_pad_aligns() -> None:
    assert pad("ab", 6, align="right") == "    ab"
    assert pad("ab", 6, align="center") == "  ab  "
    assert pad("ab", 7, align="center") == "  ab   "  # extra cell goes right
    assert pad("ab", 6, fill=".") == "ab...."


def test_pad_rejects_a_fill_that_is_not_one_cell() -> None:
    with pytest.raises(ValueError):
        pad("ab", 6, fill="日")
    with pytest.raises(ValueError):
        pad("ab", 6, fill="")


def test_fit_is_truncate_then_pad() -> None:
    assert fit("hello world", 8) == "hello w…"
    assert fit("hi", 6) == "hi    "
    assert display_width(fit("日本語", 5)) == 5
    assert fit("hi", 6, align="right") == "    hi"


# -- properties over a corpus ------------------------------------------------

CORPUS = (
    "",
    "a",
    "hello world",
    "mantis run --resume 3",
    "日本語テスト",
    "a日b本c",
    "café latté",
    "á̧̈z",
    "\U0001f600\U0001f680\U0001f9e0",
    FAMILY + FAMILY,
    FLAG_US + FLAG_US + "x",
    HEART + HEART_EMOJI + "!",
    RED + "coloured" + RESET,
    BOLD_256 + "日本" + RESET + " tail",
    LINK_OPEN + "a link" + LINK_CLOSE,
    "\x1b[2m" + FAMILY + "\x1b[0m done",
    "a​b­c",
    "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",  # tag flag
)


@pytest.mark.parametrize("s", CORPUS)
@pytest.mark.parametrize("n", list(range(0, 14)))
def test_truncate_output_never_exceeds_the_budget(s: str, n: int) -> None:
    out = truncate(s, n)
    assert display_width(out) <= n


@pytest.mark.parametrize("s", CORPUS)
@pytest.mark.parametrize("n", list(range(0, 14)))
def test_truncate_output_never_contains_a_partial_escape(s: str, n: int) -> None:
    assert _no_partial_escape(truncate(s, n))


@pytest.mark.parametrize("s", CORPUS)
@pytest.mark.parametrize("n", list(range(0, 14)))
def test_truncate_output_is_a_prefix_of_the_visible_text(s: str, n: int) -> None:
    """Whatever survives is the front of the string — truncation never
    reorders, re-encodes, or re-wraps."""
    visible = _strip(truncate(s, n)).rstrip("…")
    assert _strip(s).startswith(visible)


@pytest.mark.parametrize("s", CORPUS)
@pytest.mark.parametrize("n", list(range(0, 14)))
def test_fit_lands_exactly_on_the_budget(s: str, n: int) -> None:
    out = fit(s, n)
    assert display_width(out) == n


@pytest.mark.parametrize("s", CORPUS)
def test_clusters_reassemble_into_the_original_text(s: str) -> None:
    assert "".join(grapheme_clusters(s)) == _strip(s)


@pytest.mark.parametrize("s", CORPUS)
def test_width_is_the_sum_of_its_clusters(s: str) -> None:
    assert display_width(s) == sum(display_width(c) for c in grapheme_clusters(s))


# -- compatibility -----------------------------------------------------------


def test_module_parses_under_python_39() -> None:
    """The package supports 3.9–3.14 and CI runs one interpreter; parse the
    source with the older grammar rather than trusting review."""
    src = inspect.getsource(term_measure)
    ast.parse(src, feature_version=(3, 9))
