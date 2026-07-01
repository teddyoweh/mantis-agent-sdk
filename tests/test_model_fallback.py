"""Model fallback (T2): a pre-output model failure retries on fallback_model."""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import Agent, AssistantMessage, UserMessage
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
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import TextBlock, Usage


def _text_turn(text: str = "hi from fallback") -> list:
    return [
        MessageStart(message_id="m", model="fb"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=2)),
        MessageStop(),
    ]


class _FailFirst(MockProvider):
    """Raises on the first stream() call, streams a text turn afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def stream(self, **kw):
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("model overloaded")
        for ev in _text_turn():
            yield ev


class _AlwaysFail(MockProvider):
    async def stream(self, **kw):
        raise ProviderError("down")
        yield  # pragma: no cover — makes this an async generator


def _assistant_text(msgs) -> str:
    for m in msgs:
        if isinstance(m, AssistantMessage):
            return "".join(b.text for b in m.content if isinstance(b, TextBlock))
    return ""


def test_falls_back_on_pre_output_failure() -> None:
    provider = _FailFirst()

    async def main():
        agent = Agent(model="primary-32b", fallback_model="fb-7b", provider=provider,
                      include_env=False, include_memory=False, include_recall=False)
        try:
            msgs = await agent.run([UserMessage(content="go")])
        finally:
            await agent.aclose()
        return msgs, agent

    msgs, agent = anyio.run(main)
    assert "hi from fallback" in _assistant_text(msgs)  # the retry succeeded
    assert agent.model == "fb-7b"                        # switched to fallback
    assert agent._fallback_used is True
    assert provider.calls == 2                            # failed once, retried once


def test_no_fallback_propagates_error() -> None:
    async def main():
        agent = Agent(model="primary", provider=_AlwaysFail(),
                      include_env=False, include_memory=False, include_recall=False)
        try:
            with pytest.raises(Exception):
                await agent.run([UserMessage(content="go")])
        finally:
            await agent.aclose()

    anyio.run(main)


def test_fallback_only_used_once() -> None:
    # Fallback also fails → the error propagates (not an infinite retry loop).
    async def main():
        agent = Agent(model="primary", fallback_model="fb", provider=_AlwaysFail(),
                      include_env=False, include_memory=False, include_recall=False)
        try:
            with pytest.raises(Exception):
                await agent.run([UserMessage(content="go")])
        finally:
            await agent.aclose()
        assert agent._fallback_used is True   # it tried the fallback once

    anyio.run(main)
