"""Context-overflow recovery is bounded and shape-preserving.

* A provider that keeps refusing the prompt as too long must not put the
  turn into an unbounded compact-and-retry loop: one emergency compaction per
  turn, then the error surfaces.
* The emergency compaction never orphans a ``tool_use`` and never drops the
  latest user turn — the retry must be a request the provider can accept.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent import Agent, tool
from mantis_agent.agent import _assert_message_invariants
from mantis_agent.capabilities import ModelCapability
from mantis_agent.compact import SimpleCompactor
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

OVERFLOW = "This model's maximum context length is 8192 tokens. Please reduce the length of the messages."


@tool
async def lookup(q: str) -> str:
    """Look something up."""
    return "ok"


def _ok() -> list:
    return [
        MessageStart(message_id="m", model="mock"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="fine")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=2)),
        MessageStop(),
    ]


class _Overflowing(MockProvider):
    """Raises the overflow error on the first ``fail_n`` calls."""

    name = "mock"

    def __init__(self, fail_n: int) -> None:
        super().__init__()
        self._fail_n = fail_n
        self.n = 0
        self.seen: list[list] = []

    async def stream(self, **kw: Any):
        self.n += 1
        self.seen.append(list(kw["messages"]))
        if self.n <= self._fail_n:
            raise RuntimeError(OVERFLOW)
        for ev in _ok():
            yield ev


async def _summarize(prompt: str) -> str:
    return "Summary of prior conversation: the user asked for many lookups."


def _history(rounds: int = 10) -> list:
    msgs: list = [UserMessage(content="Please look up a lot of things for me.")]
    for i in range(rounds):
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"c{i}", name="lookup", input={"q": str(i)})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"c{i}", content="x" * 4000)]))
    msgs.append(UserMessage(content="LAST QUESTION: and one more lookup, please."))
    return msgs


def _agent(prov: Any) -> Agent:
    cap = ModelCapability(name="mock", family="mock", context_window=8192, max_output_tokens=1024)
    return Agent(model="mock", provider=prov, tools=[lookup], model_capability=cap,
                 compactor=SimpleCompactor(_summarize), include_recall=False,
                 include_env=False, include_memory=False, max_steps=3, max_retries=0)


def test_persistent_overflow_is_bounded_to_one_retry() -> None:
    prov = _Overflowing(fail_n=100)
    agent = _agent(prov)
    msgs = _history()

    async def go() -> None:
        with anyio.fail_after(10), pytest.raises(RuntimeError, match="context length"):
            await agent.run(msgs)

    anyio.run(go)
    assert prov.n == 2, f"expected initial call + exactly one post-compaction retry, got {prov.n}"
    _assert_message_invariants(msgs)


def test_emergency_compaction_keeps_last_user_turn_and_tool_pairing() -> None:
    prov = _Overflowing(fail_n=1)
    agent = _agent(prov)
    msgs = _history()

    anyio.run(agent.run, msgs)

    assert prov.n == 2
    retry = prov.seen[1]
    _assert_message_invariants(retry)

    def _size(ms: list) -> int:
        return sum(len(str(getattr(m, "content", ""))) for m in ms)

    assert _size(retry) < _size(prov.seen[0]), "the retry should be smaller than the refused prompt"
    texts = [m.content for m in retry if isinstance(m, UserMessage) and isinstance(m.content, str)]
    assert any("LAST QUESTION" in t for t in texts), "the latest user turn was lost in compaction"
    # The run then completed normally on the retry.
    assert isinstance(msgs[-1], AssistantMessage)
    assert msgs[-1].content[0].text == "fine"
