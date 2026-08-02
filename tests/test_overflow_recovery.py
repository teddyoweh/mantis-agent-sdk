"""Context-overflow auto-recovery: emergency-compact and retry once."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent.agent import Agent, _is_context_overflow
from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.compact import SimpleCompactor
from mantis_agent.errors import ProviderError
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage, Usage


@pytest.mark.parametrize("msg,expected", [
    ("This model's maximum context length is 8192 tokens", True),
    ("context_length_exceeded", True),
    ("prompt is too long", True),
    ("too many tokens in the request", True),
    ("Input tokens exceed the configured limit of 922000 tokens", True),
    ("connection refused", False),
    ("invalid api key", False),
])
def test_overflow_detection(msg: str, expected: bool) -> None:
    assert _is_context_overflow(ProviderError(msg)) is expected


class _Overflow:
    name = "mock"

    def __init__(self, fail_times: int = 1) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]
        self.calls = 0
        self.fail_times = fail_times

    async def stream(self, **_kw: Any):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError("maximum context length is 8192 tokens")
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text="ok"))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


async def _summ(_p: str) -> str:
    return "Summary of prior conversation: earlier turns."


def _long_msgs() -> list:
    m: list = []
    for i in range(8):
        m.append(UserMessage(content=f"turn {i} " * 20))
        m.append(AssistantMessage(content=[TextBlock(text=f"reply {i}")]))
    return m


def _drain(agent: Agent, msgs: list) -> list:
    async def go():
        return [ev async for ev in agent._stream_with_fallback(msgs)]
    return anyio.run(go)


def test_recovers_after_overflow() -> None:
    prov = _Overflow(fail_times=1)
    agent = Agent(model="mock", provider=prov,
                  compactor=SimpleCompactor(_summ, keep_recent_turns=1),
                  include_recall=False, include_env=False, include_memory=False)
    msgs = _long_msgs()
    events = _drain(agent, msgs)
    assert any(isinstance(e, MessageStop) for e in events)   # recovered
    assert prov.calls == 2                                    # failed once, retried
    assert len(msgs) < 16                                     # history compacted


def test_only_retries_overflow_once() -> None:
    prov = _Overflow(fail_times=9)                            # always overflows
    agent = Agent(model="mock", provider=prov,
                  compactor=SimpleCompactor(_summ, keep_recent_turns=1),
                  include_recall=False, include_env=False, include_memory=False)
    with pytest.raises(ProviderError):
        _drain(agent, _long_msgs())
    assert prov.calls == 2                                    # one emergency retry, then give up


def test_recovery_clears_a_single_recent_oversized_tool_result() -> None:
    from mantis_agent.types import ToolResultBlock

    prov = _Overflow(fail_times=1)
    agent = Agent(model="mock", provider=prov,
                  compactor=SimpleCompactor(_summ, keep_recent_turns=8),
                  include_recall=False, include_env=False, include_memory=False)
    msgs = [
        UserMessage(content="record a demo"),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="read-progress", content="x" * 2_000_000,
        )]),
    ]
    events = _drain(agent, msgs)
    assert any(isinstance(e, MessageStop) for e in events)
    assert prov.calls == 2
    result = msgs[-1].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.content == "[old tool result cleared to save context]"


def test_no_compactor_no_recovery() -> None:
    prov = _Overflow(fail_times=9)
    agent = Agent(model="mock", provider=prov, auto_compact=False,
                  include_recall=False, include_env=False, include_memory=False)
    with pytest.raises(ProviderError):
        _drain(agent, _long_msgs())
    assert prov.calls == 1                                    # no compactor → no retry
