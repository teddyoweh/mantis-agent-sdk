"""Tool dispatch tolerates hallucinated extra args (drops what the fn won't take)."""

from __future__ import annotations

import anyio

from mantis_agent.streaming.executor import _filter_tool_input
from mantis_agent.tools import tool


@tool
async def reader(path: str, offset: int = 1, limit: int = 100) -> str:
    return f"{path}:{offset}:{limit}"


@tool(name="q", is_read_only=True,
      input_schema={"type": "object", "properties": {"questions": {"type": "array"}}})
async def q(args: dict) -> str:
    return f"got {len(args.get('questions', []))}"


def test_extra_arg_dropped() -> None:
    assert _filter_tool_input(reader.fn, {"path": "x", "recursive": True}) == {"path": "x"}


def test_clean_input_unchanged() -> None:
    inp = {"path": "x", "offset": 5}
    assert _filter_tool_input(reader.fn, inp) == inp


def test_explicit_schema_passthrough() -> None:
    # tool.fn is the **kwargs wrapper → nothing filtered
    inp = {"questions": [1, 2, 3]}
    assert _filter_tool_input(q.fn, inp) == inp


def test_call_succeeds_despite_extra_arg() -> None:
    # the whole point: a call with a bogus arg still runs instead of TypeError-ing
    filtered = _filter_tool_input(reader.fn, {"path": "app.py", "limit": 50, "junk": "x"})
    out = anyio.run(lambda: reader.fn(**filtered))
    assert out == "app.py:1:50"


def test_empty_input() -> None:
    assert _filter_tool_input(reader.fn, {}) == {}


def test_missing_required_still_errors() -> None:
    # dropping junk doesn't invent a missing required arg — the call still fails clearly
    filtered = _filter_tool_input(reader.fn, {"pathh": "typo"})   # misspelled → dropped
    import pytest
    with pytest.raises(TypeError):
        anyio.run(lambda: reader.fn(**filtered))                  # missing 'path'
