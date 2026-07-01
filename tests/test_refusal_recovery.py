"""Refusal recovery — a bare no-tool-call refusal is nudged once and retried
instead of dead-ending the task."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.agent import Agent, _looks_like_refusal
from mantis_agent.capabilities import HOSTED_PROFILES
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


class _ScriptedTexts:
    """Provider that returns a different text turn on each call."""

    name = "mock"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.backend_capability = HOSTED_PROFILES["mock"]
        self.calls = 0

    async def stream(self, *, model: str, messages: Any, **_kw: Any):
        self.calls += 1
        text = self._texts.pop(0) if self._texts else "(done)"
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text=text))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _run(provider, **agent_kw) -> list:
    async def go():
        agent = Agent(model="mock", provider=provider, **agent_kw)
        msgs: list = [UserMessage(content="list my listening ports")]
        async for _ in agent.run_iter(msgs):
            pass
        return msgs
    return anyio.run(go)


def _texts(msgs) -> list[str]:
    return ["".join(b.text for b in m.content if isinstance(b, TextBlock))
            for m in msgs if isinstance(m, AssistantMessage)]


def test_refusal_is_nudged_and_retried() -> None:
    prov = _ScriptedTexts([
        "I'm sorry, but I can't complete that request.",
        "Here are your listening ports: 8000, 8888, 5433.",
    ])
    msgs = _run(prov)
    assert prov.calls == 2                                   # it retried
    # a one-shot authorized-context nudge was injected
    assert any(getattr(m, "isMeta", False) and "authorized" in str(m.content).lower()
               for m in msgs)
    assert "8000, 8888, 5433" in _texts(msgs)[-1]            # real answer produced


def test_opt_out_stops_on_refusal() -> None:
    prov = _ScriptedTexts([
        "I'm sorry, but I can't complete that request.",
        "should never be reached",
    ])
    msgs = _run(prov, recover_refusals=False)
    assert prov.calls == 1                                   # no retry
    assert not any(getattr(m, "isMeta", False) for m in msgs)


def test_only_retries_once() -> None:
    prov = _ScriptedTexts([
        "I'm sorry, but I can't help with that.",
        "I cannot help with that.",          # refuses again after the nudge
        "should never be reached",
    ])
    msgs = _run(prov)
    assert prov.calls == 2                                   # nudged once, then gave up
    assert _texts(msgs)[-1] == "I cannot help with that."


def test_normal_answer_not_retried() -> None:
    prov = _ScriptedTexts(["Sure — your ports are 8000 and 8888."])
    msgs = _run(prov)
    assert prov.calls == 1                                   # no spurious retry
    assert not any(getattr(m, "isMeta", False) for m in msgs)


def test_detector_precision() -> None:
    assert _looks_like_refusal("I'm sorry, but I cannot assist with that.")
    assert not _looks_like_refusal("I can't find that file — did you mean app.py?")
    assert not _looks_like_refusal("Done. " * 200)          # long answer, not a refusal
