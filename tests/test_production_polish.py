"""Production polish: memory injection, HTTP retries, structured logging."""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
import httpx
import pytest

from mantis_agent import (
    Agent,
    MemoryEntry,
    save_memory_entry,
    update_memory_index,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.retry import RetryTransport, _parse_retry_after


# ---------------------------------------------------------------------------
# Memory injection
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_mantis_agent_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    return tmp_path


def test_agent_injects_memory_as_system_reminder_user_message(
    tmp_mantis_agent_home: Path,
) -> None:
    """When include_memory=True (default), MEMORY.md is wrapped in a
    ``<system-reminder>`` and injected as a synthetic isMeta user message
    at the head of the conversation when ``run()`` is invoked. The system
    prompt itself is left untouched — Claude SDK parity."""

    from mantis_agent import UserMessage
    from mantis_agent.events import (
        ContentBlockDelta,
        ContentBlockStart,
        ContentBlockStop,
        MessageDelta,
        MessageStart,
        MessageStop,
        TextDelta,
    )
    from mantis_agent import TextBlock, Usage

    save_memory_entry(
        MemoryEntry(
            slug="sf_housing",
            name="SF housing search",
            description="Apartment search in SF, $3500 max",
            type="project",
            body="Looking in Mission, Hayes Valley.",
        )
    )
    update_memory_index()

    # Scripted MockProvider — emit a trivial assistant turn.
    events = [
        MessageStart(message_id="m1", model="qwen2.5-7b-instruct"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="ok")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=2)),
        MessageStop(),
    ]
    agent = Agent(
        model="qwen2.5-7b-instruct",
        provider=MockProvider(scripted_events=events),
        system="You are a helpful agent.",
    )
    try:
        # System prompt is UNTOUCHED.
        assert agent.system == "You are a helpful agent."

        messages: list = [UserMessage(content="hi")]
        anyio.run(agent.run, messages)

        # First message is now the meta context-injection user message.
        first = messages[0]
        assert isinstance(first, UserMessage)
        assert first.isMeta is True
        content = first.content if isinstance(first.content, str) else ""
        assert "<system-reminder>" in content
        assert "SF housing search" in content
        # User's original turn is still there.
        assert any(
            isinstance(m, UserMessage) and m.content == "hi" and not m.isMeta
            for m in messages
        )
    finally:
        anyio.run(agent.aclose)


def test_agent_include_memory_false_skips_injection(
    tmp_mantis_agent_home: Path,
) -> None:
    """Opt out via include_memory=False; no synthetic message is injected."""

    from mantis_agent import UserMessage
    from mantis_agent.events import (
        ContentBlockDelta,
        ContentBlockStart,
        ContentBlockStop,
        MessageDelta,
        MessageStart,
        MessageStop,
        TextDelta,
    )
    from mantis_agent import TextBlock, Usage

    save_memory_entry(
        MemoryEntry(slug="x", name="X", description="x", type="project", body="x")
    )
    update_memory_index()

    events = [
        MessageStart(message_id="m1", model="m"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="ok")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn"),
        MessageStop(),
    ]
    agent = Agent(
        model="qwen2.5-7b-instruct",
        provider=MockProvider(scripted_events=events),
        system="Pristine system.",
        include_memory=False,
    )
    try:
        messages: list = [UserMessage(content="hi")]
        anyio.run(agent.run, messages)
        # No meta user message inserted at head.
        assert not (
            isinstance(messages[0], UserMessage) and messages[0].isMeta
        )
        assert agent.system == "Pristine system."
    finally:
        anyio.run(agent.aclose)


def test_memory_missing_is_silent_noop(tmp_mantis_agent_home: Path) -> None:
    """No MEMORY.md → no injection; system prompt unchanged; no crash."""

    from mantis_agent import UserMessage
    from mantis_agent.events import (
        ContentBlockDelta,
        ContentBlockStart,
        ContentBlockStop,
        MessageDelta,
        MessageStart,
        MessageStop,
        TextDelta,
    )
    from mantis_agent import TextBlock

    events = [
        MessageStart(message_id="m1", model="m"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="ok")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn"),
        MessageStop(),
    ]
    agent = Agent(
        model="qwen2.5-7b-instruct",
        provider=MockProvider(scripted_events=events),
        system="Original system.",
    )
    try:
        messages: list = [UserMessage(content="hi")]
        anyio.run(agent.run, messages)
        assert agent.system == "Original system."
        # No meta message — empty memory means no injection.
        assert not (
            isinstance(messages[0], UserMessage) and messages[0].isMeta
        )
    finally:
        anyio.run(agent.aclose)


# ---------------------------------------------------------------------------
# Retry middleware
# ---------------------------------------------------------------------------


def test_retry_after_parsing() -> None:
    assert _parse_retry_after("3") == 3.0
    assert _parse_retry_after("0.5") == 0.5
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("not-a-number") is None
    # Negative values are invalid per spec — ignore.
    assert _parse_retry_after("-1") is None


def test_retry_replays_on_503_then_succeeds() -> None:
    """Inject a flaky transport: first 2 calls 503, then 200. The retry
    middleware should swallow the 503s and surface the 200 to the caller."""

    calls = {"n": 0}

    class FlakyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(503, text="busy")
            return httpx.Response(200, text="ok")

        async def aclose(self) -> None:
            pass

    rt = RetryTransport(FlakyTransport(), attempts=5, base_s=0.01, max_s=0.05, jitter=False)

    async def main():
        async with httpx.AsyncClient(transport=rt) as client:
            r = await client.get("http://example/x")
            return r

    r = anyio.run(main)
    assert r.status_code == 200
    assert calls["n"] == 3  # 2 failures + 1 success


def test_retry_exhausts_attempts_and_returns_last_failure() -> None:
    """Endless 500s with attempts=2 → return the second 500 (no retries
    left) rather than raising."""

    class AlwaysFails(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(500, text="boom")

        async def aclose(self) -> None:
            pass

    rt = RetryTransport(AlwaysFails(), attempts=2, base_s=0.01, max_s=0.05, jitter=False)

    async def main():
        async with httpx.AsyncClient(transport=rt) as client:
            return await client.get("http://example/x")

    r = anyio.run(main)
    assert r.status_code == 500


def test_retry_on_connect_error() -> None:
    """Two ConnectErrors then a 200 → success."""

    calls = {"n": 0}

    class FlakyNet(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise httpx.ConnectError("kaboom")
            return httpx.Response(200, text="ok")

        async def aclose(self) -> None:
            pass

    rt = RetryTransport(FlakyNet(), attempts=5, base_s=0.01, max_s=0.05, jitter=False)

    async def main():
        async with httpx.AsyncClient(transport=rt) as client:
            return await client.get("http://example/x")

    r = anyio.run(main)
    assert r.status_code == 200
    assert calls["n"] == 3


def test_make_client_uses_retry_by_default() -> None:
    """Smoke test: make_client wires RetryTransport into the AsyncClient."""

    from mantis_agent.http import make_client

    client = make_client(base_url="http://x.example/", retries=True)
    try:
        # The transport hierarchy is internal-ish but we can introspect.
        # httpx stores the transport on `_transport` for the default scheme.
        # We just assert it's a RetryTransport instance.
        assert isinstance(
            client._transport, RetryTransport
        ), f"expected RetryTransport, got {type(client._transport).__name__}"
    finally:
        anyio.run(client.aclose)


def test_make_client_can_disable_retries() -> None:
    """retries=False reverts to plain httpx transport."""

    from mantis_agent.http import make_client

    client = make_client(base_url="http://x.example/", retries=False)
    try:
        assert not isinstance(client._transport, RetryTransport)
    finally:
        anyio.run(client.aclose)
