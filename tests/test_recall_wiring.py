"""Memory recall is wired into the run loop (Slice 3).

Before each turn, the agent surfaces the ~/.mantis-agent/memory/ topic files
most relevant to the latest user message as an isMeta <system-reminder>, deduped
across the session. Fully OFFLINE via MockProvider + a tmp memory home.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from mantis_agent import (
    Agent,
    MemoryEntry,
    UserMessage,
    save_memory_entry,
    update_memory_index,
)
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


@pytest.fixture
def memory_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)  # enable context
    return tmp_path


def _text_turn(text: str = "ok") -> list:
    return [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=2)),
        MessageStop(),
    ]


def _save(slug, name, description, body) -> None:
    save_memory_entry(MemoryEntry(slug=slug, name=name, description=description,
                                  type="project", body=body))
    update_memory_index()


def _recall_reminders(messages: list) -> list:
    return [m for m in messages
            if isinstance(m, UserMessage) and getattr(m, "isMeta", False)
            and isinstance(m.content, str) and "Memory" in m.content]


def _agent(provider) -> Agent:
    # include_env/memory off so only recall injects an isMeta head.
    return Agent(model="mock-7b", provider=provider, include_env=False,
                 include_memory=False, include_recall=True)


def test_relevant_memory_surfaced_for_query(memory_home) -> None:
    _save("redis_cache", "Redis caching layer",
          "The API caches user sessions in Redis with a 30-minute TTL",
          "Sessions live in Redis db 2, TTL 1800s. Evict with FLUSHDB db 2.")
    _save("unrelated", "Vacation notes", "Where to go in summer",
          "Consider Portugal in July.")

    async def main():
        agent = _agent(MockProvider(scripted_events=_text_turn()))
        messages = [UserMessage(content="how does the redis session cache work?")]
        try:
            async for _ in agent.run_iter(messages):
                pass
        finally:
            await agent.aclose()

        reminders = _recall_reminders(messages)
        assert len(reminders) == 1
        body = reminders[0].content
        assert "Redis db 2" in body            # the relevant memory's body
        assert "Portugal" not in body          # the irrelevant one is not surfaced

    anyio.run(main)


def test_no_recall_when_nothing_relevant(memory_home) -> None:
    _save("redis_cache", "Redis caching", "sessions in redis", "db 2, TTL 1800.")

    async def main():
        agent = _agent(MockProvider(scripted_events=_text_turn()))
        messages = [UserMessage(content="what's the weather in Paris today?")]
        try:
            async for _ in agent.run_iter(messages):
                pass
        finally:
            await agent.aclose()
        assert _recall_reminders(messages) == []

    anyio.run(main)


def test_recall_deduped_across_turns(memory_home) -> None:
    _save("redis_cache", "Redis caching layer",
          "The API caches user sessions in Redis", "db 2, TTL 1800s.")

    async def main():
        agent = _agent(MockProvider(scripted_events=_text_turn()))
        messages = [UserMessage(content="explain the redis cache")]
        try:
            async for _ in agent.run_iter(messages):
                pass
            # Second turn, same topic — must NOT re-surface the same memory.
            messages.append(UserMessage(content="and the redis TTL again?"))
            async for _ in agent.run_iter(messages):
                pass
        finally:
            await agent.aclose()

        assert len(_recall_reminders(messages)) == 1  # surfaced once, not twice

    anyio.run(main)


def test_recall_disabled_by_flag(memory_home) -> None:
    _save("redis_cache", "Redis caching", "sessions in redis", "db 2.")

    async def main():
        agent = Agent(model="mock-7b", provider=MockProvider(scripted_events=_text_turn()),
                      include_env=False, include_memory=False, include_recall=False)
        messages = [UserMessage(content="explain the redis cache")]
        try:
            async for _ in agent.run_iter(messages):
                pass
        finally:
            await agent.aclose()
        assert _recall_reminders(messages) == []

    anyio.run(main)
