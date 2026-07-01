"""Transient-error retry with backoff — a rate-limit/5xx blip before any output
is retried, not fatal. Non-transient errors are not retried."""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest

from mantis_agent.agent import Agent, _is_transient
from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.errors import AuthError, ProviderError, RateLimitError
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.types import TextBlock, UserMessage, Usage


class _Flaky:
    name = "mock"

    def __init__(self, fail_times: int, err: Exception) -> None:
        self.fail_times = fail_times
        self.err = err
        self.calls = 0
        self.backend_capability = HOSTED_PROFILES["mock"]

    async def stream(self, **_kw: Any):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.err
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text="ok"))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(*_a, **_k):
        return None
    monkeypatch.setattr("anyio.sleep", _fast)


def _drain(agent: Agent) -> list:
    async def go():
        return [ev async for ev in agent._stream_with_fallback([UserMessage(content="hi")])]
    return anyio.run(go)


def test_retries_then_succeeds() -> None:
    prov = _Flaky(fail_times=2, err=RateLimitError("slow down"))
    agent = Agent(model="mock", provider=prov, max_retries=2, auto_compact=False)
    events = _drain(agent)
    assert prov.calls == 3            # 2 failures + 1 success
    assert any(isinstance(e, MessageStop) for e in events)


def test_exhausts_and_raises() -> None:
    prov = _Flaky(fail_times=5, err=ProviderError("gateway", status_code=503))
    agent = Agent(model="mock", provider=prov, max_retries=2, auto_compact=False)
    with pytest.raises(ProviderError):
        _drain(agent)
    assert prov.calls == 3            # initial + 2 retries, then give up


def test_non_transient_not_retried() -> None:
    prov = _Flaky(fail_times=5, err=AuthError("bad key"))
    agent = Agent(model="mock", provider=prov, max_retries=3, auto_compact=False)
    with pytest.raises(AuthError):
        _drain(agent)
    assert prov.calls == 1            # auth failures are not retried


def test_max_retries_zero_disables() -> None:
    prov = _Flaky(fail_times=5, err=RateLimitError("x"))
    agent = Agent(model="mock", provider=prov, max_retries=0, auto_compact=False)
    with pytest.raises(RateLimitError):
        _drain(agent)
    assert prov.calls == 1


def test_transient_classifier() -> None:
    assert _is_transient(RateLimitError("x"))
    assert _is_transient(ProviderError("x", status_code=502))
    assert _is_transient(httpx.ConnectError("refused"))
    assert not _is_transient(AuthError("x"))
    assert not _is_transient(ProviderError("x", status_code=400))
    assert not _is_transient(ValueError("x"))
