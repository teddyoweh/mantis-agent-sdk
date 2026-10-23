"""Whether a model can see images comes from evidence before name guesses.
A name list said GLM 5.3 Flash couldn't (the terminal warned on every pasted
screenshot) though Fireworks publishes ``supportsImageInput: true`` for it
and it reads images fine; GLM 5.3 really can't."""

from __future__ import annotations

import time
from typing import Any

import anyio

from mantis_agent import vision
from mantis_agent.tui import model_supports_vision

FLASH = "accounts/fireworks/models/glm-5p3-flash"
GLM = "accounts/fireworks/models/glm-5p3"


def test_what_the_endpoint_did_wins_over_the_name() -> None:
    vision.record("gpt-4o", False)                     # the name says yes; the endpoint said no
    assert model_supports_vision("gpt-4o") is False
    vision.record("some-text-model", True)
    assert model_supports_vision("some-text-model") is True


def test_a_fireworks_model_is_looked_up_not_guessed(monkeypatch) -> None:
    published = {FLASH: True, GLM: False}
    monkeypatch.setattr(vision, "published", lambda m: published.get(m))
    # while the lookup may still answer, no warning
    assert model_supports_vision(FLASH) is True
    assert model_supports_vision(GLM) is True
    for _ in range(100):
        if vision.known(GLM) is not None and vision.known(FLASH) is not None:
            break
        time.sleep(0.01)
    assert model_supports_vision(FLASH) is True
    assert model_supports_vision(GLM) is False


def test_an_unknown_non_fireworks_name_still_warns() -> None:
    assert model_supports_vision("mystery-7b") is False


def test_verdicts_persist(tmp_path, monkeypatch) -> None:
    store = tmp_path / "vision.json"
    monkeypatch.setattr(vision, "_path", lambda: store)
    vision._reset_for_tests()
    vision.record(FLASH, True)
    vision._reset_for_tests()                          # a new process
    assert vision.known(FLASH) is True


def test_the_engine_records_both_outcomes() -> None:
    from mantis_agent.agent import Agent
    from mantis_agent.capabilities import HOSTED_PROFILES
    from mantis_agent.errors import ProviderError
    from mantis_agent.events import (ContentBlockDelta, ContentBlockStart, ContentBlockStop,
                                     MessageDelta, MessageStart, MessageStop, TextDelta)
    from mantis_agent.types import ImageBlock, TextBlock, Usage, UserMessage

    png = ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="})

    class _P:
        name = "mock"

        def __init__(self, refuse: bool) -> None:
            self.backend_capability = HOSTED_PROFILES["mock"]
            self.refuse = refuse

        async def stream(self, *, messages: list[Any], **_kw: Any):
            if self.refuse and any(isinstance(b, ImageBlock) for m in messages
                                   for b in (m.content if isinstance(m.content, list) else ())):
                raise ProviderError("This model does not support image inputs (400)")
            yield MessageStart(message_id="m", model="mock")
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="ok"))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
            yield MessageStop()

    def drain(model: str, refuse: bool) -> None:
        agent = Agent(model=model, provider=_P(refuse), include_recall=False, include_env=False,
                      include_memory=False)
        msgs = [UserMessage(content=[png, TextBlock(text="what is this?")])]

        async def go():
            return [ev async for ev in agent._stream_with_fallback(msgs)]
        anyio.run(go)

    drain("sees-model", refuse=False)
    drain("blind-model", refuse=True)
    assert vision.known("sees-model") is True
    assert vision.known("blind-model") is False
