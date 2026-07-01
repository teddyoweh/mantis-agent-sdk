"""Diff word-emphasis alignment — a re-indent / try-wrap must not light up every
line (the pairing aligns removed↔added by stripped content, not by offset)."""

from __future__ import annotations

from mantis_agent.tui import _compute_word_emphasis


def _diff(old: list[str], new: list[str]) -> list[str]:
    return ["-" + ln for ln in old] + ["+" + ln for ln in new]


def test_reindent_wrap_produces_no_noise() -> None:
    # The reported bug: wrap a block in try/except (+4-space re-indent). The body
    # lines are unchanged content — they must get NO char emphasis.
    old = [
        "        for conn in conns:",
        "            if not conn.raddr:",
        "                continue",
        "            proc = get(conn)",
    ]
    new = [
        "        try:",
        "            for conn in conns:",
        "                if not conn.raddr:",
        "                    continue",
        "                proc = get(conn)",
        "        except Exception:",
        "            proc = fallback()",
    ]
    emph = _compute_word_emphasis(_diff(old, new))
    assert emph == {}          # nothing lit up — only re-indent + pure inserts


def test_genuine_edit_still_highlights() -> None:
    emph = _compute_word_emphasis(_diff(["    timeout = 30"], ["    timeout = 60"]))
    assert len(emph) == 2                       # both the - and + line
    # exactly the changed digit span, not the whole line
    for spans in emph.values():
        assert spans == [(14, 15)]


def test_pure_add_and_delete_not_emphasized() -> None:
    # pure insertion (no removed counterpart) and pure deletion → no char spans
    assert _compute_word_emphasis(_diff([], ["+new line"])) == {}
    assert _compute_word_emphasis(_diff(["gone line"], [])) == {}


def test_reordered_lines_align_by_content() -> None:
    # a line changed in the middle; surrounding identical lines must stay clean
    old = ["a = 1", "b = 2", "c = 3"]
    new = ["a = 1", "b = 22", "c = 3"]
    emph = _compute_word_emphasis(_diff(old, new))
    # only the 'b' line pair carries emphasis; a/c lines don't
    lit = set(emph.keys())
    assert lit == {1, 4}       # index 1 (the -b) and index 4 (the +b)


def test_unrelated_replacement_stays_quiet() -> None:
    # two wholly unrelated lines shouldn't be word-diffed into noise
    emph = _compute_word_emphasis(_diff(["import os"], ["raise SystemExit(2)"]))
    assert emph == {}          # <30% shared → row colour tells the story
