"""The general-purpose `task` tool — delegate a read-only investigation to a
fresh subagent that returns just its findings."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.builtin_tools import CODING_TOOLS
from mantis_agent.providers.mock import MockProvider
from mantis_agent.subagent import make_task_tool


def _explore() -> list:
    return [t for t in CODING_TOOLS if getattr(t, "is_read_only", False)]


def _task(default_text: str = "done") -> Any:
    prov = MockProvider(default_text=default_text)
    return make_task_tool(model="mock", provider=prov, tools=_explore())


def test_tool_shape() -> None:
    t = _task()
    assert t.name == "task"
    assert t.is_read_only is True                 # safe: read-only exploration
    props = t.input_schema["properties"]
    assert "prompt" in props and "description" in props
    assert t.input_schema["required"] == ["prompt"]


def test_returns_child_final_text() -> None:
    t = _task("The auth check lives in security/gate.py:88.")
    out = anyio.run(lambda: t.fn(prompt="where is auth enforced?", description="find auth"))
    assert out == "The auth check lives in security/gate.py:88."


def test_empty_prompt_guarded() -> None:
    t = _task()
    assert "required" in anyio.run(lambda: t.fn(prompt="   "))
    assert "required" in anyio.run(lambda: t.fn(description="no prompt"))


def test_subagent_has_no_write_or_recursion() -> None:
    # The kit handed to the subagent is read-only and never includes `task`
    # itself (no infinite recursion) or edit/write/bash.
    names = {t.name for t in _explore()}
    assert "task" not in names
    assert "write_file" not in names and "edit_file" not in names and "bash" not in names
    assert "read_file" in names and "grep" in names


def test_subagent_runs_isolated_context() -> None:
    # The child gets a fresh single-turn message list — the parent's prompt only.
    prov = MockProvider(default_text="ok")
    t = make_task_tool(model="mock", provider=prov, tools=_explore())
    anyio.run(lambda: t.fn(prompt="investigate X"))
    # the child's very first call carried exactly the delegated prompt as the
    # sole user message (no parent history leaked in)
    first = prov.calls[0]["messages"]
    user_texts = [m.content for m in first if getattr(m, "role", "") == "user"
                  and isinstance(m.content, str)]
    assert any("investigate X" in tx for tx in user_texts)
