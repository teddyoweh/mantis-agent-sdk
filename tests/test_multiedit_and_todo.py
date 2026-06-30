"""Iteration-2 Claude Code parity tools: MultiEdit (atomic batch edits) and
TodoWrite (stateful task tracking bound to a session store)."""

from __future__ import annotations

import anyio

from mantis_agent.builtin_tools import multi_edit
from mantis_agent.builtin_tools.todo import make_todo_write


def test_multi_edit_applies_in_order(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("alpha beta gamma")
    out = anyio.run(
        multi_edit.fn,
        str(f),
        [
            {"old_string": "alpha", "new_string": "A"},
            {"old_string": "gamma", "new_string": "G"},
        ],
    )
    assert "Updated" in out and "+" in out  # Claude-style "Updated … · +N -M"
    assert f.read_text() == "A beta G"


def test_multi_edit_is_atomic_on_failure(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("one two")
    try:
        anyio.run(
            multi_edit.fn,
            str(f),
            [
                {"old_string": "one", "new_string": "1"},
                {"old_string": "MISSING", "new_string": "x"},
            ],
        )
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert f.read_text() == "one two"  # nothing written


def test_multi_edit_rejects_ambiguous_without_replace_all(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x x x")
    try:
        anyio.run(multi_edit.fn, str(f), [{"old_string": "x", "new_string": "y"}])
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert f.read_text() == "x x x"


def test_todo_write_mutates_bound_store() -> None:
    store: list[dict] = []
    tool = make_todo_write(store)
    out = anyio.run(
        tool.fn,
        [
            {"content": "Build", "status": "completed", "activeForm": "Building"},
            {"content": "Test", "status": "in_progress", "activeForm": "Testing"},
            {"content": "Ship", "status": "pending", "activeForm": "Shipping"},
        ],
    )
    assert "1/3 done" in out
    assert "Testing" in out  # active item surfaced
    assert [t["status"] for t in store] == ["completed", "in_progress", "pending"]


def test_todo_write_replaces_previous_list() -> None:
    store: list[dict] = [{"content": "old", "status": "pending", "activeForm": "old"}]
    tool = make_todo_write(store)
    anyio.run(tool.fn, [{"content": "new", "status": "pending"}])
    assert [t["content"] for t in store] == ["new"]


def test_todo_write_normalizes_bad_status() -> None:
    store: list[dict] = []
    tool = make_todo_write(store)
    anyio.run(tool.fn, [{"content": "X", "status": "bogus"}])
    assert store[0]["status"] == "pending"
