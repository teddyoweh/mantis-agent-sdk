"""AskUserQuestion overlay rendering — width-safe rows (the old renderer let
long descriptions wrap and get cut off by the exact-height window), progress
chips, multi-select feedback, and the ask tool's normalize/answer plumbing."""

from __future__ import annotations

import re

import anyio

from mantis_agent.tui import format_question_rows

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(row: str) -> str:
    return _ANSI.sub("", row)


def _q(multi: bool = False) -> dict:
    return {
        "question": "Which HTTP library should we use?",
        "header": "Library",
        "multiSelect": multi,
        "options": [
            {"label": "httpx (Recommended)", "description": "async-first, modern API, HTTP/2"},
            {"label": "requests", "description": "sync, battle-tested, everywhere"},
            {"label": "aiohttp", "description": "async, mature, heavier API surface"},
        ],
    }


def test_rows_shape_and_count() -> None:
    rows = format_question_rows(_q(), 0, set(), False, 100)
    # head + 3 options + Other + hint == len(opts) + 3  (the window height contract)
    assert len(rows) == 3 + 3
    assert "Which HTTP library" in _plain(rows[0]) and "[Library]" in _plain(rows[0])
    assert _plain(rows[1]).strip().startswith("1 httpx")
    assert "Other…" in _plain(rows[4])
    assert "esc skips" in _plain(rows[5])


def test_no_row_ever_exceeds_width() -> None:
    q = _q()
    q["options"][0]["description"] = "x" * 500   # the old overflow bug
    q["question"] = "y" * 300
    for width in (40, 60, 100):
        for sel in range(5):
            rows = format_question_rows(q, sel, set(), False, width)
            for r in rows:
                assert len(_plain(r)) <= width, (width, sel, _plain(r))


def test_progress_chip_for_multiple_questions() -> None:
    rows = format_question_rows(_q(), 0, set(), False, 100, index=2, total=3)
    assert "Library · 2/3" in _plain(rows[0])
    rows1 = format_question_rows(_q(), 0, set(), False, 100)
    assert "2/3" not in _plain(rows1[0])          # single question → no counter


def test_selected_row_is_inverted() -> None:
    rows = format_question_rows(_q(), 1, set(), False, 100)
    assert "\x1b[30;48;5;113m" in rows[2]         # row 2 (option index 1) highlighted
    assert "\x1b[30;48;5;113m" not in rows[1]


def test_multiselect_boxes_and_count() -> None:
    rows = format_question_rows(_q(multi=True), 0, {0, 2}, False, 100)
    assert "● httpx" in _plain(rows[1])
    assert "○ requests" in _plain(rows[2])
    assert "2 selected" in _plain(rows[-1])
    assert "space toggles" in _plain(rows[-1])


def test_typing_hint_replaces_footer() -> None:
    rows = format_question_rows(_q(), 3, set(), True, 100)
    assert "type your answer" in _plain(rows[-1])


# -- tool plumbing ---------------------------------------------------------------


def test_ask_tool_round_trip() -> None:
    from mantis_agent.builtin_tools.ask import make_ask_user_question

    async def asker(qs):
        assert qs[0]["header"] == "Library"
        return [{"question": qs[0]["question"], "header": "Library",
                 "answers": ["httpx (Recommended)"]}]

    t = make_ask_user_question(asker)
    out = anyio.run(lambda: t.fn(questions=[_q()]))
    assert out == "Library: httpx (Recommended)"


def test_ask_tool_headless_note() -> None:
    from mantis_agent.builtin_tools.ask import make_ask_user_question
    t = make_ask_user_question(None)
    out = anyio.run(lambda: t.fn(questions=[_q()]))
    assert "headless" in out and "best judgment" in out


def test_ask_tool_skipped_answer() -> None:
    from mantis_agent.builtin_tools.ask import make_ask_user_question

    async def asker(qs):
        return [{"question": qs[0]["question"], "header": "Library", "answers": []}]
    t = make_ask_user_question(asker)
    out = anyio.run(lambda: t.fn(questions=[_q()]))
    assert "(skipped)" in out
