"""AskUserQuestion tool — the agent asks the user structured multiple-choice
questions mid-task. The interactive picker is the terminal's job; here we test
the tool + schema + asker delegation OFFLINE with a fake asker.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.builtin_tools.ask import _normalize, make_ask_user_question

_Q = {
    "question": "Which database?",
    "header": "Database",
    "options": [
        {"label": "Postgres (Recommended)", "description": "relational, robust"},
        {"label": "SQLite", "description": "embedded, zero-config"},
    ],
}


def test_schema_shape() -> None:
    tool = make_ask_user_question(None)
    props = tool.input_schema["properties"]
    assert "questions" in props
    items = props["questions"]["items"]["properties"]
    assert {"question", "header", "options", "multiSelect"} <= set(items)
    assert props["questions"]["maxItems"] == 4


def test_tool_returns_user_choice() -> None:
    async def asker(questions):
        return [{"question": q["question"], "header": q["header"],
                 "answers": [q["options"][0]["label"]]} for q in questions]

    tool = make_ask_user_question(asker)
    out = anyio.run(lambda: tool.fn(questions=[_Q]))
    assert "Database: Postgres (Recommended)" in out


def test_multiselect_answers_joined() -> None:
    async def asker(questions):
        return [{"question": _Q["question"], "header": "Database",
                 "answers": ["Postgres (Recommended)", "SQLite"]}]

    tool = make_ask_user_question(asker)
    out = anyio.run(lambda: tool.fn(questions=[{**_Q, "multiSelect": True}]))
    assert "Postgres (Recommended), SQLite" in out


def test_headless_no_asker() -> None:
    tool = make_ask_user_question(None)
    out = anyio.run(lambda: tool.fn(questions=[_Q]))
    assert "No interactive user" in out


def test_normalize_validates() -> None:
    with pytest.raises(ValueError):
        _normalize([])
    with pytest.raises(ValueError):
        _normalize([{"question": "x", "options": [{"label": "only one", "description": "d"}]}])
    # header truncated to 12 chars; options capped at 4
    norm = _normalize([{
        "question": "q?", "header": "a-very-long-header-label",
        "options": [{"label": f"o{i}", "description": "d"} for i in range(6)],
    }])
    assert len(norm[0]["header"]) <= 12
    assert len(norm[0]["options"]) == 4


def test_wired_into_terminal_and_classic_asker_skips_without_tty(monkeypatch) -> None:
    # The tool is registered in the TUI belt, and the classic asker returns no
    # answer when there's no TTY (so an unattended run doesn't hang on input()).
    from mantis_agent.tui import MantisTUI

    tui = MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                    max_tokens=1, temperature=None, max_turns=1)
    names = {t.name for t in tui._build_agent().tools}
    assert "ask_user_question" in names

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    res = anyio.run(lambda: tui._ask_user_question([_Q]))
    assert res == [{"question": _Q["question"], "header": _Q["header"], "answers": []}]
