"""``TodoWrite`` — the task-tracking tool, mirroring Claude Code's TodoWrite.

The agent maintains a structured todo list for multi-step work; the harness
renders it so the user can watch progress. Todos are *stateful* — they belong to
a live session, not a stateless function — so the tool is built via
:func:`make_todo_write`, which binds it to a caller-owned ``store`` list. The TUI
keeps that same list and re-renders it whenever the tool is called.

Each todo is ``{"content": str, "status": "pending"|"in_progress"|"completed",
"activeForm": str}``. Exactly one item should be ``in_progress`` at a time —
matching the upstream contract — but we don't hard-enforce it, only summarize.
"""

from __future__ import annotations

from ..tools import Tool, tool

_STATUSES = ("pending", "in_progress", "completed")


def make_todo_write(store: list[dict]) -> Tool:
    """Build a ``todo_write`` tool bound to ``store`` (mutated in place so a UI
    holding the same list sees every update)."""

    @tool(name="todo_write", is_read_only=False, is_concurrency_safe=False)
    async def todo_write(todos: list[dict]) -> str:
        """Record or update the task list for the current multi-step work.

        Call this to plan a task before starting and to mark items done as you
        go. Pass the FULL list every time (it replaces the previous one).

        Args:
            todos: A list of ``{"content": str, "status":
                "pending"|"in_progress"|"completed", "activeForm": str}`` items.
                ``content`` is the imperative form ("Run the tests"); ``activeForm``
                is the present-continuous shown while active ("Running the tests").
                Keep exactly one item ``in_progress``.
        """
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        cleaned: list[dict] = []
        for i, t in enumerate(todos):
            if not isinstance(t, dict) or "content" not in t:
                raise ValueError(f"todo #{i + 1} must be an object with 'content'")
            status = t.get("status", "pending")
            if status not in _STATUSES:
                status = "pending"
            cleaned.append({
                "content": str(t["content"]),
                "status": status,
                "activeForm": str(t.get("activeForm", t["content"])),
            })
        store.clear()
        store.extend(cleaned)

        done = sum(1 for t in cleaned if t["status"] == "completed")
        active = next((t for t in cleaned if t["status"] == "in_progress"), None)
        tail = f"; now: {active['activeForm']}" if active else ""
        return f"todos updated ({done}/{len(cleaned)} done){tail}"

    return todo_write
