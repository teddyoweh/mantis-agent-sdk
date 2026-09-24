"""One retry authority per error class, and bounded timeouts.

A single model call used to be retried at BOTH layers — the transport (4
attempts) and the engine (1 + 2) — so a 429 or a dead server meant up to 12
HTTP requests, each honouring ``Retry-After``, and ``read=600`` on a wedged
llama.cpp could stall a turn for ~2 hours. Now:

* the transport retries connect / 429 / 5xx before the body and tags what it
  gave up on, so the engine doesn't retry it again;
* nobody retries a first-byte timeout;
* the engine re-streams only stream-level failures (idle / drop mid-body);
* ``Retry-After`` beyond the cap surfaces an error instead of a silent sleep.

Everything runs through the real ``OpenAICompatProvider`` + ``RetryTransport``
over ``httpx.MockTransport`` and counts HTTP requests end to end.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest

from mantis_agent import Agent
from mantis_agent.errors import ProviderError, RateLimitError
from mantis_agent.events import ContentBlockDelta, MessageStop, TextDelta
from mantis_agent.http import make_client
from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.retry import (
    FirstByteTimeout,
    RetryTransport,
    StreamIdleTimeout,
    retried_by_transport,
)
from mantis_agent.types import UserMessage

BASE = "https://api.example.com/v1"
ATTEMPTS = 3
OK_SSE = (
    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


@pytest.fixture(autouse=True)
def _fast_engine_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mantis_agent.agent._retry_delay", lambda *a, **k: 0.0)


def _agent(handler: Any, *, max_retries: int = 2, fallback_model: str | None = None,
           first_byte_s: float | None = 5.0, idle_s: float | None = 5.0,
           retry_after_max_s: float = 60.0) -> Agent:
    p = OpenAICompatProvider(base_url=BASE, api_key="x")
    p.client = httpx.AsyncClient(
        base_url=BASE,
        transport=RetryTransport(
            httpx.MockTransport(handler), attempts=ATTEMPTS, base_s=0.001,
            jitter=False, retry_after_max_s=retry_after_max_s,
            first_byte_s=first_byte_s, idle_s=idle_s,
        ),
    )
    return Agent(model="some-model", provider=p, max_retries=max_retries,
                 fallback_model=fallback_model, auto_compact=False)


def _drain(agent: Agent) -> list:
    async def go() -> list:
        with anyio.fail_after(5):  # a hang here is the bug under test
            return [ev async for ev in agent._stream_with_fallback([UserMessage(content="hi")])]
    return anyio.run(go)


def _text(events: list) -> str:
    return "".join(e.delta.text for e in events
                   if isinstance(e, ContentBlockDelta) and isinstance(e.delta, TextDelta))


@pytest.mark.parametrize("status", [429, 503])
def test_status_errors_are_retried_by_the_transport_only(status: int) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"error": {"message": "busy"}})

    with pytest.raises(ProviderError) as ei:
        _drain(_agent(handler))
    assert calls["n"] == ATTEMPTS            # not ATTEMPTS * (1 + max_retries)
    assert retried_by_transport(ei.value)
    if status == 429:
        assert isinstance(ei.value, RateLimitError)


def test_connect_errors_are_retried_by_the_transport_only() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(httpx.ConnectError) as ei:
        _drain(_agent(handler))
    assert calls["n"] == ATTEMPTS
    assert retried_by_transport(ei.value)


def test_transport_recovery_is_invisible_to_the_engine() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"message": "warming"}})
        return httpx.Response(200, text=OK_SSE, headers={"content-type": "text/event-stream"})

    events = _drain(_agent(handler))
    assert calls["n"] == 2 and _text(events) == "ok"


def test_exhausted_transport_goes_straight_to_the_fallback_model() -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "some-model":
            return httpx.Response(503, json={"error": {"message": "down"}})
        return httpx.Response(200, text=OK_SSE, headers={"content-type": "text/event-stream"})

    events = _drain(_agent(handler, fallback_model="backup-model"))
    assert models == ["some-model"] * ATTEMPTS + ["backup-model"]
    assert _text(events) == "ok"


def test_retry_after_within_cap_is_honoured_once_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("mantis_agent.retry.anyio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "7"},
                              json={"error": {"message": "slow down"}})

    with pytest.raises(RateLimitError):
        _drain(_agent(handler))
    # Transport sleeps between its attempts; the engine adds none on top.
    assert calls["n"] == ATTEMPTS
    assert sleeps == [7.0] * (ATTEMPTS - 1)


def test_retry_after_beyond_cap_surfaces_an_error_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("mantis_agent.retry.anyio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "3600"},
                              json={"error": {"message": "quota"}})

    with pytest.raises(RateLimitError) as ei:
        _drain(_agent(handler))
    assert calls["n"] == 1 and sleeps == []
    assert "3600" in str(ei.value) and "MANTIS_AGENT_RETRY_AFTER_MAX_S" in str(ei.value)
    assert ei.value.retry_after_s == 3600.0


def test_first_byte_timeout_is_never_retried() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await anyio.sleep(30)  # a wedged server that never answers
        raise AssertionError("unreachable")

    with pytest.raises(FirstByteTimeout) as ei:
        _drain(_agent(handler, first_byte_s=0.1))
    assert calls["n"] == 1
    assert "MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S" in str(ei.value)


def test_first_byte_bound_covers_a_slow_first_body_chunk() -> None:
    """vLLM-style: headers at once, prefill happens before the first body byte."""

    async def body():
        await anyio.sleep(30)
        yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncBody(body()),
                              headers={"content-type": "text/event-stream"})

    with pytest.raises(FirstByteTimeout):
        _drain(_agent(handler, first_byte_s=0.1, idle_s=10))


def test_slow_prefill_within_first_byte_budget_is_not_cut_by_idle_bound() -> None:
    async def body():
        await anyio.sleep(0.3)          # longer than idle_s, inside first_byte_s
        for line in OK_SSE.split("\n\n"):
            if line:
                yield (line + "\n\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncBody(body()),
                              headers={"content-type": "text/event-stream"})

    events = _drain(_agent(handler, first_byte_s=5, idle_s=0.1))
    assert _text(events) == "ok"


def test_idle_mid_stream_is_retried_by_the_engine_not_the_transport() -> None:
    calls = {"n": 0}

    async def stalled():
        yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        await anyio.sleep(30)           # server goes silent mid-body
        yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, stream=_AsyncBody(stalled()),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, text=OK_SSE, headers={"content-type": "text/event-stream"})

    events = _drain(_agent(handler, idle_s=0.1))
    assert calls["n"] == 2              # one engine re-stream, no transport replay
    assert _text(events) == "ok"
    assert any(isinstance(e, MessageStop) for e in events)


def test_idle_mid_stream_retries_are_bounded_by_max_retries() -> None:
    calls = {"n": 0}

    async def stalled():
        yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        await anyio.sleep(30)
        yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, stream=_AsyncBody(stalled()),
                              headers={"content-type": "text/event-stream"})

    with pytest.raises(StreamIdleTimeout):
        _drain(_agent(handler, idle_s=0.05, max_retries=2))
    assert calls["n"] == 3              # 1 + max_retries, each a single request


def test_timeout_env_plumbs_into_client_and_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S", "42")
    monkeypatch.setenv("MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S", "7")
    monkeypatch.setenv("MANTIS_AGENT_RETRY_AFTER_MAX_S", "15")
    monkeypatch.delenv("MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S", raising=False)
    client = make_client(base_url=BASE)
    try:
        # httpx's read is the non-streaming floor, never below a first-byte budget.
        assert client.timeout.read == 600.0
        assert client.timeout.connect == 10.0
        t = client._transport
        assert isinstance(t, RetryTransport)
        assert t._first_byte_s == 42.0 and t._idle_s == 7.0
        # An explicit first-byte budget applies to loopback servers too.
        assert t._local_first_byte_s == 42.0
        assert t._default_read_s == 600.0
        assert t._retry_after_max_s == 15.0
    finally:
        anyio.run(client.aclose)


def test_timeout_defaults_and_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S", raising=False)
    client = make_client(base_url=BASE)
    try:
        assert client.timeout.read == 900.0      # >= the local cold-load budget
        assert client._transport._first_byte_s == 300.0
        assert client._transport._local_first_byte_s == 900.0
        assert client._transport._idle_s == 180.0
    finally:
        anyio.run(client.aclose)
    monkeypatch.setenv("MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S", "0")
    monkeypatch.setenv("MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S", "0")
    client = make_client(base_url=BASE)
    try:
        assert client.timeout.read is None
        assert client._transport._first_byte_s is None
        assert client._transport._local_first_byte_s is None
        assert client._transport._idle_s is None
    finally:
        anyio.run(client.aclose)


def test_untagged_errors_are_still_retried_by_the_engine() -> None:
    """A provider that doesn't use the retrying transport keeps engine retries."""
    from mantis_agent.agent import _engine_should_retry

    assert _engine_should_retry(ProviderError("busy", status_code=503))
    assert _engine_should_retry(httpx.ReadTimeout("idle"))
    tagged = ProviderError("busy", status_code=503)
    tagged.retried_by_transport = True  # type: ignore[attr-defined]
    assert not _engine_should_retry(tagged)


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, gen: Any) -> None:
        self._gen = gen

    async def __aiter__(self):
        async for chunk in self._gen:
            yield chunk

    async def aclose(self) -> None:
        await self._gen.aclose()


# ---------------------------------------------------------------------------
# Anthropic / Vertex / Bedrock keep the transport's signals (status, 429 type,
# Retry-After, the "not waiting" note, retried_by_transport)
# ---------------------------------------------------------------------------


def _retrying_client(handler: Any, *, base_url: str = "", retry_after_max_s: float = 60.0,
                     **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        transport=RetryTransport(
            httpx.MockTransport(handler), attempts=ATTEMPTS, base_s=0.001,
            jitter=False, retry_after_max_s=retry_after_max_s, **kw,
        ),
    )


def _anthropic(handler: Any, **kw: Any):
    from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider

    p = AnthropicPassthroughProvider(api_key="sk-ant-test")
    headers = dict(p.client.headers)
    anyio.run(p.client.aclose)
    p.client = _retrying_client(handler, base_url=p.base_url, **kw)
    p.client.headers.update(headers)
    return p


def _bedrock(handler: Any, **kw: Any):
    from mantis_agent.providers.anthropic_bedrock import AnthropicBedrockProvider

    p = AnthropicBedrockProvider(region="us-east-1", access_key_id="AKIA",
                                 secret_access_key="secret")
    anyio.run(p.client.aclose)
    p.client = _retrying_client(handler, **kw)
    return p


def _run_provider(p: Any, model: str) -> list:
    async def go() -> list:
        with anyio.fail_after(5):
            return [ev async for ev in p.stream(
                model=model, messages=[UserMessage(content="hi")], max_tokens=16)]
    return anyio.run(go)


@pytest.mark.parametrize("make,model", [
    (_anthropic, "claude-sonnet-4-5"),
    (_bedrock, "claude-sonnet-4-5"),
])
def test_anthropic_family_429_is_a_tagged_rate_limit_error(make: Any, model: str) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"},
                              json={"type": "error", "error": {"message": "slow down"},
                                    "message": "slow down"})

    with pytest.raises(RateLimitError) as ei:
        _run_provider(make(handler), model)
    assert calls["n"] == ATTEMPTS
    assert ei.value.status_code == 429 and ei.value.retry_after_s == 0.0
    assert retried_by_transport(ei.value)
    from mantis_agent.agent import _engine_should_retry
    assert not _engine_should_retry(ei.value)   # no second retry layer


@pytest.mark.parametrize("make", [_anthropic, _bedrock])
def test_anthropic_family_529_keeps_its_status_and_tag(make: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": {"message": "overloaded"},
                                         "message": "overloaded"})

    with pytest.raises(ProviderError) as ei:
        _run_provider(make(handler), "claude-sonnet-4-5")
    assert ei.value.status_code == 529 and retried_by_transport(ei.value)


@pytest.mark.parametrize("make", [_anthropic, _bedrock])
@pytest.mark.parametrize("status", [429, 503])
def test_anthropic_family_long_retry_after_names_the_wait(make: Any, status: int) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, headers={"Retry-After": "3600"},
                              json={"error": {"message": "quota"}, "message": "quota"})

    with pytest.raises(ProviderError) as ei:
        _run_provider(make(handler), "claude-sonnet-4-5")
    assert calls["n"] == 1
    assert ei.value.status_code == status
    assert "3600" in str(ei.value) and "not waiting" in str(ei.value)
    assert retried_by_transport(ei.value)
    if status == 429:
        assert isinstance(ei.value, RateLimitError) and ei.value.retry_after_s == 3600.0


def test_openai_compat_503_with_long_retry_after_names_the_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "3600"},
                              json={"error": {"message": "maintenance"}})

    with pytest.raises(ProviderError) as ei:
        _drain(_agent(handler))
    assert ei.value.status_code == 503 and "not waiting" in str(ei.value)


# ---------------------------------------------------------------------------
# The first-byte bound is for streams only; caller timeouts win
# ---------------------------------------------------------------------------


def _send(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
    async def go() -> httpx.Response:
        async with client:
            with anyio.fail_after(5):
                r = await client.request(method, url, **kw)
                await r.aread()
                return r
    return anyio.run(go)


def test_non_streaming_call_is_not_cut_by_the_first_byte_bound() -> None:
    """A /responses POST for a high-effort reasoning model: the first byte IS
    the whole answer, so it may take longer than the streaming first-byte bound."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(0.3)
        return httpx.Response(200, json={"output": []})

    client = _retrying_client(handler, first_byte_s=0.05, idle_s=0.05)
    r = _send(client, "POST", f"{BASE}/responses", json={"model": "m", "input": "hi"})
    assert r.status_code == 200


def test_streaming_call_still_gets_the_first_byte_bound() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(30)
        raise AssertionError("unreachable")

    client = _retrying_client(handler, first_byte_s=0.05)
    with pytest.raises(FirstByteTimeout):
        _send(client, "POST", f"{BASE}/chat/completions", json={"model": "m", "stream": True})


def test_is_streaming_request_detection() -> None:
    from mantis_agent.retry import STREAMING_EXTENSION, is_streaming_request

    def req(url: str = f"{BASE}/chat/completions", **kw: Any) -> httpx.Request:
        return httpx.Request("POST", url, **kw)

    assert is_streaming_request(req(json={"stream": True}))
    assert not is_streaming_request(req(json={"stream": False}))
    assert not is_streaming_request(req(json={"model": "m"}))
    # A user message quoting the flag is an escaped string, not the key.
    assert not is_streaming_request(req(json={"input": '{"stream": true}'}))
    assert is_streaming_request(req(headers={"accept": "text/event-stream"}))
    assert is_streaming_request(req(
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke-with-response-stream"))
    assert is_streaming_request(req(
        "https://us-east5-aiplatform.googleapis.com/v1/p/models/claude:streamRawPredict"))
    assert not is_streaming_request(req(json={"stream": True},
                                        extensions={STREAMING_EXTENSION: False}))
    assert is_streaming_request(req(json={}, extensions={STREAMING_EXTENSION: True}))


def _slow_stream_handler(delay: float) -> Any:
    async def handler(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(delay)
        return httpx.Response(200, text=OK_SSE, headers={"content-type": "text/event-stream"})
    return handler


def test_caller_set_larger_read_timeout_stretches_the_first_byte_bound() -> None:
    client = _retrying_client(_slow_stream_handler(0.3), first_byte_s=0.05,
                              default_read_s=5.0)
    r = _send(client, "POST", f"{BASE}/chat/completions", json={"stream": True},
              timeout=httpx.Timeout(5.0, read=2.0))
    assert r.status_code == 200


def test_caller_timeout_none_lifts_the_first_byte_bound() -> None:
    client = _retrying_client(_slow_stream_handler(0.3), first_byte_s=0.05,
                              default_read_s=5.0)
    r = _send(client, "POST", f"{BASE}/chat/completions", json={"stream": True},
              timeout=None)
    assert r.status_code == 200


def test_the_client_default_read_does_not_count_as_a_caller_timeout() -> None:
    client = _retrying_client(_slow_stream_handler(30), first_byte_s=0.05,
                              default_read_s=5.0)   # httpx.AsyncClient's default read
    with pytest.raises(FirstByteTimeout):
        _send(client, "POST", f"{BASE}/chat/completions", json={"stream": True})


def test_short_probe_timeout_surfaces_as_httpx_read_timeout() -> None:
    """ollama's 3s ``/api/show`` probe timing out is the caller's bound — it is
    not a 'no response within 300s' FirstByteTimeout."""

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _retrying_client(handler, first_byte_s=300.0, default_read_s=900.0)
    with pytest.raises(httpx.ReadTimeout) as ei:
        _send(client, "POST", "http://localhost:11434/api/show",
              json={"model": "m"}, timeout=3.0)
    assert not isinstance(ei.value, FirstByteTimeout)
    assert calls["n"] == 1

    # Same for a streaming request whose short caller read fired early.
    client = _retrying_client(handler, first_byte_s=300.0, default_read_s=900.0)
    with pytest.raises(httpx.ReadTimeout) as ei:
        _send(client, "POST", f"{BASE}/chat/completions", json={"stream": True})
    assert not isinstance(ei.value, FirstByteTimeout)


class _ConnectingForever(httpx.AsyncBaseTransport):
    """Emits httpcore's connect trace, then hangs — a SYN into the void."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        trace = request.extensions.get("trace")
        if trace is not None:
            await trace("connection.connect_tcp.started", {})
        await anyio.sleep(30)
        raise AssertionError("unreachable")


def test_deadline_while_connecting_is_a_retried_connect_error() -> None:
    inner = _ConnectingForever()
    seen: list[str] = []

    async def caller_trace(name: str, info: dict) -> None:
        seen.append(name)

    client = httpx.AsyncClient(transport=RetryTransport(
        inner, attempts=ATTEMPTS, base_s=0.001, jitter=False, first_byte_s=0.05))
    with pytest.raises(httpx.ConnectTimeout) as ei:
        _send(client, "POST", f"{BASE}/chat/completions", json={"stream": True},
              extensions={"trace": caller_trace})
    assert not isinstance(ei.value, FirstByteTimeout)
    assert inner.calls == ATTEMPTS and retried_by_transport(ei.value)
    assert seen == ["connection.connect_tcp.started"] * ATTEMPTS   # caller trace chained


def test_headers_on_the_deadline_get_a_grace_for_the_first_read() -> None:
    from mantis_agent.retry import _WatchdogStream

    async def body():
        await anyio.sleep(0.01)
        yield b"data: x\n\n"

    async def go() -> list[bytes]:
        req = httpx.Request("POST", f"{BASE}/chat/completions")
        # Deadline already reached as the headers land.
        w = _WatchdogStream(_AsyncBody(body()), req, anyio.current_time(), 1.0, 2.0)
        return [c async for c in w]

    assert anyio.run(go) == [b"data: x\n\n"]


# ---------------------------------------------------------------------------
# Loopback servers get a cold-load budget
# ---------------------------------------------------------------------------


def test_loopback_hosts_get_the_local_first_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S", "MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    t = RetryTransport(httpx.MockTransport(lambda r: httpx.Response(200)))

    def budget(url: str) -> float | None:
        return t._first_byte_budget(httpx.Request("POST", url, json={"stream": True}))

    assert budget("http://localhost:11434/api/chat") == 900.0
    assert budget("http://127.0.0.1:8080/v1/chat/completions") == 900.0
    assert budget(f"{BASE}/chat/completions") == 300.0
    assert t._first_byte_budget(httpx.Request("POST", "http://localhost:11434/api/show",
                                              json={"model": "m"})) is None

    monkeypatch.setenv("MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S", "1200")
    t = RetryTransport(httpx.MockTransport(lambda r: httpx.Response(200)))
    assert budget("http://localhost:11434/api/chat") == 1200.0
    monkeypatch.setenv("MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S", "0")
    t = RetryTransport(httpx.MockTransport(lambda r: httpx.Response(200)))
    assert budget("http://localhost:11434/api/chat") is None


def test_local_first_byte_message_mentions_cold_loads_and_the_env_var() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(30)
        raise AssertionError("unreachable")

    client = _retrying_client(handler, first_byte_s=0.05)
    with pytest.raises(FirstByteTimeout) as ei:
        _send(client, "POST", "http://localhost:11434/api/chat",
              json={"model": "m", "stream": True})
    msg = str(ei.value)
    assert "loading the model" in msg and "MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S" in msg

    client = _retrying_client(handler, first_byte_s=0.05)
    with pytest.raises(FirstByteTimeout) as ei:
        _send(client, "POST", f"{BASE}/chat/completions", json={"stream": True})
    assert "cold-starting" in str(ei.value)
    assert "MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S" in str(ei.value)
