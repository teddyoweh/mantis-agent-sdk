"""Retry UX: with a UI hook installed, retries surface as ONE in-place status
note (no raw log lines tearing through the prompt frame); headless keeps the
WARNING log. Exhaustion still raises so the turn ends with a clean error."""

from __future__ import annotations

import logging

import anyio
import httpx
import pytest

import mantis_agent.retry as retry_mod
from mantis_agent.retry import RetryTransport, _friendly_reason


class _FailingTransport(httpx.AsyncBaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0
    async def handle_async_request(self, request):
        self.calls += 1
        raise self.exc


@pytest.fixture(autouse=True)
def _clean_hook():
    retry_mod.notify = None
    yield
    retry_mod.notify = None


def test_notify_hook_gets_structured_payload_and_silences_warning(caplog) -> None:
    seen: list[dict] = []
    retry_mod.notify = seen.append
    inner = _FailingTransport(httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    t = RetryTransport(inner, attempts=3, base_s=0.001, jitter=False)

    async def go():
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        with pytest.raises(httpx.ConnectError):
            await t.handle_async_request(req)
    with caplog.at_level(logging.WARNING, logger="mantis_agent.retry"):
        anyio.run(go)
    assert inner.calls == 3
    assert len(seen) == 3
    assert seen[0]["host"] == "api.openai.com"
    assert seen[0]["reason"] == "connection failed"
    assert seen[-1]["attempt"] == 3 and seen[-1]["attempts"] == 3
    assert not caplog.records                       # no WARNING spam with a hook


def test_headless_still_logs_warnings(caplog) -> None:
    inner = _FailingTransport(httpx.ConnectError("nope"))
    t = RetryTransport(inner, attempts=2, base_s=0.001, jitter=False)

    async def go():
        with pytest.raises(httpx.ConnectError):
            await t.handle_async_request(httpx.Request("POST", "https://x.test/v1"))
    with caplog.at_level(logging.WARNING, logger="mantis_agent.retry"):
        anyio.run(go)
    assert len(caplog.records) == 2                 # headless behavior unchanged


def test_broken_hook_never_breaks_retries() -> None:
    def boom(_info: dict) -> None:
        raise RuntimeError("ui died")
    retry_mod.notify = boom
    inner = _FailingTransport(httpx.ConnectError("nope"))
    t = RetryTransport(inner, attempts=2, base_s=0.001, jitter=False)

    async def go():
        with pytest.raises(httpx.ConnectError):    # original error, not the hook's
            await t.handle_async_request(httpx.Request("POST", "https://x.test/v1"))
    anyio.run(go)
    assert inner.calls == 2


def test_friendly_reasons() -> None:
    assert _friendly_reason(exc=httpx.ConnectError("x")) == "connection failed"
    assert _friendly_reason(exc=httpx.ReadTimeout("x")) == "timed out"
    assert _friendly_reason(exc=httpx.RemoteProtocolError("x")) == "connection dropped"
    assert _friendly_reason(status=429) == "rate limited (429)"
    assert _friendly_reason(status=503) == "HTTP 503"


def test_offline_dns_error_gets_a_hint() -> None:
    # The macOS offline error must map to the connection hint, not fall through.
    from mantis_agent.tui import error_hint
    e = httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
    hint = error_hint(e, "https://api.openai.com/v1")
    assert hint and "can't reach" in hint


# -- executor param-cache poisoning (id-reuse regression) --------------------------


def test_accepted_params_cache_survives_function_gc_and_id_reuse() -> None:
    """id(fn)-keyed caching poisoned the executor when a GC'd tool closure's
    address was recycled by a NEW function with a different signature — the
    executor then silently dropped the new tool's real arguments. The weak-keyed
    cache must never alias across objects, whatever their addresses."""
    import gc
    from mantis_agent.streaming.executor import _accepted_params

    def make_x():
        async def quick(x: int) -> str:  # noqa: ARG001
            return "x"
        return quick

    def make_value():
        async def quick(value: str) -> str:  # noqa: ARG001
            return "v"
        return quick

    # Force many alloc/free cycles so CPython recycles addresses; correctness
    # must hold regardless of whether an id collision actually occurs.
    for _ in range(200):
        a = make_x()
        assert _accepted_params(a) == frozenset({"x"})
        addr = id(a)
        del a
        gc.collect()
        b = make_value()
        got = _accepted_params(b)
        assert got == frozenset({"value"}), (
            f"cache aliased across objects (id reuse {addr == id(b)}): {got}")
        del b
        gc.collect()
