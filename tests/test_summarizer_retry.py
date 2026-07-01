"""Compaction summarizer retries transient failures too — a throttle during
compaction shouldn't kill the run."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent.agent import Agent
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
from mantis_agent.types import TextBlock, Usage


class _Flaky:
    name = "mock"

    def __init__(self, fails: int, err: Exception) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]
        self.fails = fails
        self.err = err
        self.calls = 0

    async def stream(self, **_kw: Any):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.err
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text="Summary of prior conversation: ok."))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(*_a, **_k):
        return None
    monkeypatch.setattr("anyio.sleep", _fast)


def _summarize(prov, **kw) -> str:
    agent = Agent(model="mock", provider=prov, auto_compact=False, **kw)
    return anyio.run(lambda: agent._summarize("compress this"))


def test_retries_then_succeeds() -> None:
    prov = _Flaky(2, RateLimitError("slow down"))
    out = _summarize(prov, max_retries=2)
    assert out.startswith("Summary")
    assert prov.calls == 3                    # 2 failures + success


def test_5xx_retried() -> None:
    prov = _Flaky(1, ProviderError("gateway", status_code=503))
    assert _summarize(prov, max_retries=2).startswith("Summary")
    assert prov.calls == 2


def test_auth_not_retried() -> None:
    prov = _Flaky(5, AuthError("bad key"))
    with pytest.raises(AuthError):
        _summarize(prov, max_retries=3)
    assert prov.calls == 1


def test_exhausts_and_raises() -> None:
    prov = _Flaky(9, RateLimitError("x"))
    with pytest.raises(RateLimitError):
        _summarize(prov, max_retries=2)
    assert prov.calls == 3                    # initial + 2 retries
