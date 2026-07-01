"""Compaction preserves the original task (first user message) verbatim."""

from __future__ import annotations

import anyio

from mantis_agent.compact import SimpleCompactor
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


async def _summ(_p: str) -> str:
    return "Summary: the middle turns."


def _long(task: str) -> list:
    msgs: list = [UserMessage(content=task)]
    for i in range(10):
        msgs.append(AssistantMessage(content=[TextBlock(text=f"step {i}")]))
        msgs.append(UserMessage(content=f"followup {i}"))
    return msgs


def _contents(msgs: list) -> list[str]:
    return [m.content for m in msgs if isinstance(getattr(m, "content", None), str)]


def test_original_task_kept_verbatim() -> None:
    task = "ORIGINAL TASK: build the auth system"
    out = anyio.run(lambda: SimpleCompactor(_summ, keep_recent_turns=2).compact(_long(task)))
    assert task in _contents(out)                              # verbatim, not summarized
    assert any("Summary:" in c for c in _contents(out))        # summary also present


def test_anchor_comes_before_summary() -> None:
    task = "TASK ONE"
    out = anyio.run(lambda: SimpleCompactor(_summ, keep_recent_turns=2).compact(_long(task)))
    strs = _contents(out)
    task_i = next(i for i, c in enumerate(strs) if c == task)
    sum_i = next(i for i, c in enumerate(strs) if "Summary:" in c)
    assert task_i < sum_i                                      # original request first


def test_anchor_after_meta_head() -> None:
    # a synthetic isMeta context head stays at index 0; the task anchor follows it
    msgs = [UserMessage(content="<ctx>", isMeta=True), *_long("THE GOAL")]
    out = anyio.run(lambda: SimpleCompactor(_summ, keep_recent_turns=2).compact(msgs))
    assert getattr(out[0], "isMeta", False) is True            # head preserved
    assert out[1].content == "THE GOAL"                        # task anchor next


def test_short_conversation_untouched() -> None:
    msgs = [UserMessage(content="hi"), AssistantMessage(content=[TextBlock(text="hello")])]
    out = anyio.run(lambda: SimpleCompactor(_summ, keep_recent_turns=2).compact(msgs))
    assert out == msgs                                         # nothing to compact
