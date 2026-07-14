from __future__ import annotations

import anyio

from mantis_agent.compact import SimpleCompactor, _message_token_estimate
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


async def _echoing_summarizer(_prompt: str) -> str:
    # Simulates a weak/local summarizer that echoes far too much transcript.
    return "Summary of prior conversation:\n" + ("echoed transcript\n" * 2_000)


def _tokens(messages: list) -> int:
    return sum(_message_token_estimate(m) for m in messages)


def test_compaction_caps_echoed_summary_to_create_headroom() -> None:
    messages = [UserMessage(content="original request")]
    for i in range(10):
        messages.append(UserMessage(content=f"turn {i} " + ("payload " * 500)))
        messages.append(AssistantMessage(content=[TextBlock(text="assistant " + ("payload " * 500))]))

    compactor = SimpleCompactor(
        _echoing_summarizer,
        keep_recent_turns=2,
        summary_token_budget=256,
    )

    out = anyio.run(lambda: compactor.compact(list(messages)))

    assert len(out) < len(messages)
    assert _tokens(out) < _tokens(messages)
    summary_text = str(getattr(out[1], "content", ""))
    assert "Summary capped at ~256 tokens" in summary_text
    assert "summary truncated to preserve context headroom" in summary_text


def test_tiny_summary_is_not_marked_truncated() -> None:
    async def summarizer(_prompt: str) -> str:
        return "Summary of prior conversation: concise."

    messages = [UserMessage(content="original request")]
    for i in range(3):
        messages.append(UserMessage(content=f"old detail {i}"))
        messages.append(AssistantMessage(content=[TextBlock(text=f"assistant detail {i}")]))
    messages.append(AssistantMessage(content=[TextBlock(text="recent context that must remain")]))
    compactor = SimpleCompactor(
        summarizer,
        keep_recent_turns=1,
        summary_token_budget=512,
    )

    out = anyio.run(lambda: compactor.compact(list(messages)))

    assert len(out) < len(messages)
    summary_text = str(getattr(out[1], "content", ""))
    assert "concise" in summary_text
    assert "summary truncated" not in summary_text
