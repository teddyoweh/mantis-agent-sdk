"""Word-level diff highlighting (T2): _word_diff_spans + a render smoke test."""

from __future__ import annotations

from mantis_agent.tui import MantisTUI, _word_diff_spans


def test_localized_change_highlights_only_the_word() -> None:
    old, new = "    return foo(x)", "    return bar(x)"
    o, n = _word_diff_spans(old, new)
    assert o == [(11, 14)] and n == [(11, 14)]
    assert old[o[0][0]:o[0][1]] == "foo"
    assert new[n[0][0]:n[0][1]] == "bar"


def test_appended_text() -> None:
    o, n = _word_diff_spans("x = 1", "x = 1  # note")
    assert o == []                       # nothing removed
    assert n and "".join("x = 1  # note"[s:e] for s, e in n).strip() == "# note"


def test_wholesale_rewrite_no_emphasis() -> None:
    # Too little in common → don't word-highlight (row colour already says it).
    assert _word_diff_spans("abcdef", "zyxwvuq") == ([], [])


def test_identical_and_empty() -> None:
    assert _word_diff_spans("same", "same") == ([], [])
    assert _word_diff_spans("", "x") == ([], [])
    assert _word_diff_spans("x", "") == ([], [])


def test_render_diff_smoke_with_modified_line() -> None:
    # The renderer must run without error on a modification (a '-' then '+' pair)
    # and produce output. We can't assert colours easily, but it must not raise.
    tui = MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                    max_tokens=1, temperature=None, max_turns=1)
    diff = [
        "@@ -1,3 +1,3 @@",
        " def f():",
        "-    return foo(x)",
        "+    return bar(x)",
        " # end",
    ]
    with tui.console.capture() as cap:
        tui._render_diff(diff, path="a.py")
    out = cap.get()
    assert "foo" in out and "bar" in out
