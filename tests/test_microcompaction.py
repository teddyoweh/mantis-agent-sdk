"""Microcompaction (T2): cheap per-turn clearing of old tool-result bodies."""

from __future__ import annotations

import anyio

from mantis_agent import (
    AssistantMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)
from mantis_agent.compact import SimpleCompactor


async def _fake(_prompt: str) -> str:
    return "SUMMARY"


def _compactor(**kw) -> SimpleCompactor:
    kw.setdefault("micro_keep_tool_results", 2)
    kw.setdefault("micro_min_chars", 10)
    return SimpleCompactor(_fake, **kw)


def _history(n: int, size: int = 100) -> list:
    msgs: list = []
    for i in range(n):
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"c{i}", name="bash", input={})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"c{i}", content="X" * size)]))
    return msgs


def _tool_results(msgs) -> list[ToolResultBlock]:
    return [b for m in msgs if isinstance(m.content, list)
            for b in m.content if isinstance(b, ToolResultBlock)]


def test_clears_old_keeps_recent() -> None:
    c = _compactor()
    msgs = _history(5)
    assert c.microcompact(msgs) is True
    trs = _tool_results(msgs)
    cleared = [b for b in trs if "cleared" in str(b.content)]
    verbatim = [b for b in trs if b.content == "X" * 100]
    assert len(cleared) == 3          # 5 - keep 2
    assert len(verbatim) == 2         # last 2 kept


def test_preserves_tool_use_id() -> None:
    c = _compactor()
    msgs = _history(5)
    c.microcompact(msgs)
    ids = {b.tool_use_id for b in _tool_results(msgs)}
    assert ids == {f"c{i}" for i in range(5)}   # pairing untouched


def test_small_results_untouched() -> None:
    c = _compactor(micro_min_chars=1000)
    msgs = _history(5, size=50)       # each result 50 chars < 1000
    assert c.microcompact(msgs) is False
    assert all(b.content == "X" * 50 for b in _tool_results(msgs))


def test_noop_when_few_results() -> None:
    c = _compactor(micro_keep_tool_results=8)
    msgs = _history(3)                 # 3 <= keep 8
    assert c.microcompact(msgs) is False


def test_idempotent() -> None:
    c = _compactor()
    msgs = _history(5)
    assert c.microcompact(msgs) is True
    assert c.microcompact(msgs) is False


def test_should_microcompact_threshold() -> None:
    c = _compactor(micro_threshold=0.6, threshold=0.85)
    win = 1000
    small = Usage(input_tokens=500, output_tokens=50)   # 55% — below micro
    mid = Usage(input_tokens=650, output_tokens=50)     # 70% — micro yes, full no
    assert c.should_microcompact([], small, win) is False
    assert c.should_microcompact([], mid, win) is True
    assert anyio.run(lambda: c.should_compact([], mid, win)) is False
