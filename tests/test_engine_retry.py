"""Retry policy at the engine and transport layers.

* The model-call retry honours a provider ``Retry-After`` (capped) and
  otherwise backs off exponentially (capped).
* The transport retry honours ``Retry-After`` on every retryable status —
  503/overloaded, not just 429 — and never on a success.
* Retries only ever replay the MODEL call. A transient failure on turn N+1
  must not re-execute turn N's tools (tool execution is not idempotent).
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx

from mantis_agent import Agent, tool
from mantis_agent.agent import _retry_delay
from mantis_agent.errors import ProviderError, RateLimitError
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.retry import RetryTransport
from mantis_agent.types import TextBlock, ToolUseBlock, Usage, UserMessage


def test_retry_delay_honours_retry_after_and_caps_it() -> None:
    assert _retry_delay(RateLimitError("slow down", retry_after_s=3.0), attempt=0) == 3.0
    assert _retry_delay(RateLimitError("slow down", retry_after_s=9999.0), attempt=0) == 60.0
    # No hint: exponential, capped at 8s.
    assert _retry_delay(ProviderError("busy", status_code=503), attempt=0) == 0.5
    assert _retry_delay(ProviderError("busy", status_code=503), attempt=1) == 1.0
    assert _retry_delay(ProviderError("busy", status_code=503), attempt=10) == 8.0


def test_transport_honours_retry_after_on_503_and_not_on_success(monkeypatch: Any) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("mantis_agent.retry.anyio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "2"}, text="overloaded")
        return httpx.Response(200, text="ok")

    transport = RetryTransport(httpx.MockTransport(handler), attempts=3, jitter=False)

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.post("https://api.example.com/v1/chat", json={})

    resp = anyio.run(go)
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert sleeps == [2.0]


RAN: list[int] = []


@tool
async def bump(n: int) -> str:
    """Record that the tool ran."""
    RAN.append(n)
    return "ok"


class _FlakyTurn2(MockProvider):
    """Turn 1 calls a tool; the first attempt at turn 2 fails transiently."""

    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    async def stream(self, **kw: Any):
        self.n += 1
        if self.n == 2:
            raise ProviderError("upstream hiccup", status_code=503)
        yield MessageStart(message_id=f"m{self.n}", model="mock")
        if self.n == 1:
            yield ContentBlockStart(index=0, block=ToolUseBlock(id="c1", name="bump", input={}))
            yield ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json='{"n": 1}'))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=1, output_tokens=1))
        else:
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="done"))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def test_model_retry_never_re_executes_a_finished_tool(monkeypatch: Any) -> None:
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("anyio.sleep", no_sleep)
    RAN.clear()
    prov = _FlakyTurn2()
    agent = Agent(model="mock", provider=prov, tools=[bump], max_retries=2, max_steps=4,
                  auto_compact=False, include_recall=False, include_env=False, include_memory=False)
    msgs: list = [UserMessage(content="go")]
    anyio.run(agent.run, msgs)
    assert prov.n == 3            # turn 1, failed turn 2, retried turn 2
    assert RAN == [1]             # the tool ran exactly once
    assert msgs[-1].content[0].text == "done"
