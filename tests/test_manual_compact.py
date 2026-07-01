"""/compact — manual on-demand compaction with an optional focus hint."""

from __future__ import annotations

import anyio

from mantis_agent.compact import run_manual_compaction
from mantis_agent.tui import SLASH_COMMANDS
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


def _convo(n: int) -> list:
    msgs: list = []
    for i in range(n):
        msgs.append(UserMessage(content=f"user {i}"))
        msgs.append(AssistantMessage(content=[TextBlock(text=f"assistant {i}")]))
    return msgs


async def _summ(_prompt: str) -> str:
    return "Summary of prior conversation: earlier turns handled setup."


def test_compacts_long_conversation() -> None:
    msgs = _convo(8)
    new, note = anyio.run(lambda: run_manual_compaction(msgs, _summ, keep_recent=4))
    assert len(new) < len(msgs)
    assert "compacted" in note
    assert msgs == _convo(8)        # input list not mutated


def test_short_conversation_is_noop() -> None:
    msgs = _convo(1)
    new, note = anyio.run(lambda: run_manual_compaction(msgs, _summ, keep_recent=4))
    assert new == msgs
    assert note.startswith("nothing")


def test_focus_is_injected_into_prompt() -> None:
    seen = {}

    async def summ(prompt: str) -> str:
        seen["prompt"] = prompt
        return "Summary of prior conversation: x."

    anyio.run(lambda: run_manual_compaction(_convo(8), summ, focus="the retry logic", keep_recent=4))
    assert "the retry logic" in seen["prompt"]


def test_no_focus_leaves_prompt_plain() -> None:
    seen = {}

    async def summ(prompt: str) -> str:
        seen["prompt"] = prompt
        return "Summary of prior conversation: x."

    anyio.run(lambda: run_manual_compaction(_convo(8), summ, keep_recent=4))
    assert "Focus your summary" not in seen["prompt"]


def test_compact_in_slash_menu() -> None:
    assert "/compact" in SLASH_COMMANDS
