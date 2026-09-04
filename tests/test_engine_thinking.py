"""Thinking-block handling at the engine, independent of provider.

Providers peel inline ``<think>`` tags only when the capability table says the
model emits them. A model that is not in the table (or a hosted endpoint that
emits BOTH out-of-band ``reasoning_content`` AND inline tags in one reply)
leaks its reasoning into the answer text. The engine is the last line: any
complete or unterminated reasoning span still inside a text block is split
into a ``ThinkingBlock`` before the message is used.
"""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent import Agent, tool
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
    ThinkingDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    Usage,
    UserMessage,
)

RAN: list[str] = []


@tool
async def bash(command: str) -> str:
    """Run a shell command (stub)."""
    RAN.append(command)
    return "ran"


def _events(text: str, *, oob_thinking: str | None = None) -> list:
    idx = 0
    out: list = [MessageStart(message_id="m1", model="mock")]
    if oob_thinking is not None:
        out += [
            ContentBlockStart(index=idx, block=ThinkingBlock(thinking="")),
            ContentBlockDelta(index=idx, delta=ThinkingDelta(thinking=oob_thinking)),
            ContentBlockStop(index=idx),
        ]
        idx += 1
    out += [
        ContentBlockStart(index=idx, block=TextBlock(text="")),
        ContentBlockDelta(index=idx, delta=TextDelta(text=text)),
        ContentBlockStop(index=idx),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1)),
        MessageStop(),
    ]
    return out


def _run(events: list, **kw: Any) -> list:
    agent = Agent(model="mock", provider=MockProvider(scripted_events=events),
                  auto_compact=False, include_recall=False, include_env=False,
                  include_memory=False, max_steps=2, **kw)
    msgs: list = [UserMessage(content="hi")]
    anyio.run(agent.run, msgs)
    return msgs


def _assistant(msgs: list) -> AssistantMessage:
    return next(m for m in msgs if isinstance(m, AssistantMessage))


def test_inline_think_plus_out_of_band_reasoning_yields_clean_text() -> None:
    msgs = _run(_events("<think>hidden plan</think>The answer is 4.", oob_thinking="oob"))
    a = _assistant(msgs)
    texts = [b.text for b in a.content if isinstance(b, TextBlock)]
    thinks = [b.thinking for b in a.content if isinstance(b, ThinkingBlock)]
    assert texts == ["The answer is 4."]
    assert "oob" in thinks and "hidden plan" in thinks
    # Order preserved: reasoning precedes the answer.
    kinds = [type(b).__name__ for b in a.content]
    assert kinds.index("TextBlock") > max(i for i, k in enumerate(kinds) if k == "ThinkingBlock")


def test_unterminated_inline_think_is_thinking_not_answer() -> None:
    msgs = _run(_events("<think>cut off mid-reason"))
    a = _assistant(msgs)
    assert not any("<think>" in b.text for b in a.content if isinstance(b, TextBlock))
    assert any("cut off" in b.thinking for b in a.content if isinstance(b, ThinkingBlock))


def test_plain_text_is_untouched() -> None:
    msgs = _run(_events("Just an answer, no tags."))
    a = _assistant(msgs)
    assert [type(b).__name__ for b in a.content] == ["TextBlock"]
    assert a.content[0].text == "Just an answer, no tags."


def test_reasoning_inside_think_never_reaches_text_tool_salvage() -> None:
    """A reasoning span that muses about a shell command must not be executed
    by the text-channel tool-call salvage."""
    RAN.clear()
    msgs = _run(
        _events('<think>{"name": "bash", "arguments": {"command": "rm -rf /"}}</think>No.'),
        tools=[bash],
    )
    assert RAN == []
    a = _assistant(msgs)
    assert [b.text for b in a.content if isinstance(b, TextBlock)] == ["No."]
