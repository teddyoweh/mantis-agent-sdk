"""A model that takes no images (Fireworks GLM: "This model does not support
image inputs") must not fail a turn because a tool read a PNG. The engine
retries once with every image described in a line, keeps doing so for the
rest of the run, and leaves the persisted history's images alone."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent.agent import Agent, _is_image_rejection, _without_images
from mantis_agent.capabilities import HOSTED_PROFILES
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
from mantis_agent.types import (
    AssistantMessage,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)

_PNG = ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="})


@pytest.mark.parametrize("msg,expected", [
    ("This model does not support image inputs (400 from https://api.fireworks.ai/...)", True),
    ("image_url is not supported for this model", True),
    ("Model doesn't support images", True),
    ("maximum context length is 8192 tokens", False),
    ("invalid api key", False),
])
def test_image_rejection_detection(msg: str, expected: bool) -> None:
    assert _is_image_rejection(ProviderError(msg)) is expected


class _NoVision:
    name = "mock"

    def __init__(self) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]
        self.seen: list[list[Any]] = []

    async def stream(self, *, messages: list[Any], **_kw: Any):
        self.seen.append(messages)
        if _has_image(messages):
            raise ProviderError("This model does not support image inputs (400)")
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text="ok"))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _has_image(messages: list[Any]) -> bool:
    for m in messages:
        for b in m.content if isinstance(m.content, list) else ():
            if isinstance(b, ImageBlock):
                return True
            if isinstance(b, ToolResultBlock) and isinstance(b.content, list):
                if any(isinstance(x, ImageBlock) for x in b.content):
                    return True
    return False


def _history() -> list:
    return [
        UserMessage(content="make the chart and check it"),
        AssistantMessage(content=[ToolUseBlock(id="t1", name="read_file", input={"path": "chart.png"})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=[_PNG])]),
    ]


def _drain(agent: Agent, msgs: list) -> list:
    async def go():
        return [ev async for ev in agent._stream_with_fallback(msgs)]
    return anyio.run(go)


def _agent(prov: _NoVision) -> Agent:
    return Agent(model="mock", provider=prov, include_recall=False, include_env=False, include_memory=False)


def test_retries_with_images_described_and_keeps_history() -> None:
    prov = _NoVision()
    agent = _agent(prov)
    msgs = _history()
    events = _drain(agent, msgs)
    assert any(isinstance(e, MessageStop) for e in events)    # the turn finished
    assert len(prov.seen) == 2                                # refused once, retried once
    assert not _has_image(prov.seen[1])
    assert "image omitted" in str(prov.seen[1])
    assert msgs[-1].content[0].content == [_PNG]               # history keeps the image


def test_later_requests_skip_images_without_another_refusal() -> None:
    prov = _NoVision()
    agent = _agent(prov)
    _drain(agent, _history())
    prov.seen.clear()
    _drain(agent, [*_history(), UserMessage(content=[_PNG, TextBlock(text="and this one?")])])
    assert len(prov.seen) == 1                                 # no second refusal round-trip


def test_other_errors_are_not_mistaken_for_image_refusals() -> None:
    class _Down(_NoVision):
        async def stream(self, *, messages: list[Any], **_kw: Any):
            self.seen.append(messages)
            raise ProviderError("invalid api key")
            yield  # pragma: no cover

    prov = _Down()
    agent = Agent(model="mock", provider=prov, max_retries=0,
                  include_recall=False, include_env=False, include_memory=False)
    with pytest.raises(ProviderError):
        _drain(agent, _history())
    assert agent._no_images is False


def test_without_images_handles_dict_images_and_leaves_text() -> None:
    msgs = [UserMessage(content=[{"type": "image", "source": {}}, TextBlock(text="hi")]), UserMessage(content="plain")]
    out = _without_images(msgs)
    assert out[0].content[0] == TextBlock(text="[image omitted — this model can't view images]")
    assert out[0].content[1] == TextBlock(text="hi")
    assert out[1] is msgs[1]
