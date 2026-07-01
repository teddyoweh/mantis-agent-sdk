"""todo_write maps status synonyms to the canonical value (done → completed)."""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.builtin_tools.todo import _normalize_status, make_todo_write


@pytest.mark.parametrize("s,expected", [
    ("completed", "completed"), ("complete", "completed"), ("done", "completed"),
    ("finished", "completed"), ("Done", "completed"), ("FIXED", "completed"),
    ("in_progress", "in_progress"), ("in-progress", "in_progress"), ("doing", "in_progress"),
    ("active", "in_progress"), ("WIP", "in_progress"), ("working", "in_progress"),
    ("pending", "pending"), ("todo", "pending"), ("open", "pending"), ("blocked", "pending"),
    ("nonsense", "pending"), ("", "pending"),
])
def test_normalize_status(s: str, expected: str) -> None:
    assert _normalize_status(s) == expected


def test_done_todo_counts_as_completed() -> None:
    store: list = []
    tw = make_todo_write(store)
    out = anyio.run(lambda: tw.fn(todos=[
        {"content": "build", "status": "done"},
        {"content": "ship", "status": "doing"},
    ]))
    assert store[0]["status"] == "completed"      # not pending!
    assert store[1]["status"] == "in_progress"
    assert "1/2 done" in out


def test_unknown_status_defaults_pending() -> None:
    store: list = []
    tw = make_todo_write(store)
    anyio.run(lambda: tw.fn(todos=[{"content": "x", "status": "weird"}]))
    assert store[0]["status"] == "pending"
