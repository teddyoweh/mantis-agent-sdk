"""A reasoning model can spend a whole summary cap thinking about a long
transcript and write nothing (GLM 5.3 on Fireworks: 2560 tokens of reasoning,
``stop_reason="max_tokens"``, no text). Compaction then no-ops on every try and
the history only grows. The summarize call retries once with room to think,
and keeps that room for the agent's later summaries."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent import Agent
from mantis_agent.capabilities import ModelCapability
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
from mantis_agent.types import TextBlock, ThinkingBlock, Usage

CAP = ModelCapability(name="thinker", family="glm", supports_native_tools=True,
                      context_window=128_000, max_output_tokens=32_768)


class _Thinker(MockProvider):
    """Reasons for 6000 tokens before it writes; anything less is all thinking."""
    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.caps: list[int] = []

    async def stream(self, **kw: Any):
        self.caps.append(kw["max_tokens"])
        yield MessageStart(message_id=f"s{len(self.caps)}", model="mock")
        yield ContentBlockStart(index=0, block=ThinkingBlock(thinking=""))
        yield ContentBlockDelta(index=0, delta=ThinkingDelta(thinking="Let me think about every turn…"))
        yield ContentBlockStop(index=0)
        if kw["max_tokens"] < 6000:
            yield MessageDelta(stop_reason="max_tokens", usage=Usage(input_tokens=9, output_tokens=kw["max_tokens"]))
        else:
            yield ContentBlockStart(index=1, block=TextBlock(text=""))
            yield ContentBlockDelta(index=1, delta=TextDelta(text="Goal: build the report. Done: charts."))
            yield ContentBlockStop(index=1)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=9, output_tokens=6100))
        yield MessageStop()


def test_a_summary_eaten_by_reasoning_is_retried_with_room() -> None:
    prov = _Thinker()
    agent = Agent(model="summary-thinker-test", provider=prov, model_capability=CAP,
                  include_env=False, include_memory=False, include_recall=False)
    first = anyio.run(agent._summarize, "transcript " * 2000)
    assert first.startswith("Goal: build the report")
    assert prov.caps[0] == agent._summary_max_tokens() and prov.caps[1] > 6000
    # the next summary asks for the room up front — one call, not two
    anyio.run(agent._summarize, "transcript " * 2000)
    assert len(prov.caps) == 3 and prov.caps[2] == prov.caps[1]


def test_the_room_fits_what_the_window_leaves() -> None:
    agent = Agent(model="summary-thinker-test", provider=_Thinker(), model_capability=CAP,
                  include_env=False, include_memory=False, include_recall=False)
    huge = "x" * 380_000                      # ~127k tokens of a 128k window: no room left
    assert agent._summary_thinking_room(huge) == agent._summary_max_tokens()
