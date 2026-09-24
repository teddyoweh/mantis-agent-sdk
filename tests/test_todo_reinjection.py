"""Todo re-injection (T2): the live todo list is refreshed into context each
turn (one reminder, not an accumulating pile)."""

from __future__ import annotations

import anyio

from mantis_agent import Agent, UserMessage
from mantis_agent.agent import _TODO_SENTINEL, _render_todo_reminder
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


def _text_turn() -> list:
    return [
        MessageStart(message_id="m", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="ok")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=2)),
        MessageStop(),
    ]


def _todo_reminders(messages) -> list:
    return [m for m in messages
            if isinstance(m, UserMessage) and getattr(m, "isMeta", False)
            and isinstance(m.content, str) and _TODO_SENTINEL in m.content]


def test_render_reminder_glyphs() -> None:
    r = _render_todo_reminder([
        {"content": "A", "status": "completed"},
        {"content": "B", "status": "in_progress"},
        {"content": "C", "status": "pending"},
    ])
    assert "[x] A" in r and "[→] B" in r and "[ ] C" in r
    assert _TODO_SENTINEL in r


def test_reinjected_and_refreshed_not_accumulated() -> None:
    todos: list[dict] = [{"content": "step 1", "status": "pending"}]

    async def main():
        provider = MockProvider(scripted_events=_text_turn())
        agent = Agent(model="mock-7b", provider=provider,
                      include_env=False, include_memory=False, include_recall=False,
                      todos=todos)
        messages = [UserMessage(content="go")]
        async for _ in agent.run_iter(messages):
            pass
        # simulate the tool updating the todo, then another user turn
        todos[0]["status"] = "completed"
        messages.append(UserMessage(content="next"))
        async for _ in agent.run_iter(messages):
            pass
        await agent.aclose()

        # Tail projection only: persisted history never carries the reminder
        # (rewriting it mid-history broke local prefix caches) …
        assert _todo_reminders(messages) == []
        # … while every request ends with exactly one, reflecting live state.
        first, second = provider.calls[0]["messages"], provider.calls[-1]["messages"]
        assert len(_todo_reminders(second)) == 1
        assert _todo_reminders(first)[0] is first[-1] and "[ ] step 1" in first[-1].content
        assert second[-1] is _todo_reminders(second)[0] and "[x] step 1" in second[-1].content

    anyio.run(main)


def test_legacy_persisted_reminder_is_not_resent() -> None:
    """A session saved by an older build carries a todo reminder mid-history;
    the request drops it so only the fresh tail copy is sent."""
    todos: list[dict] = [{"content": "step 1", "status": "completed"}]

    async def main():
        provider = MockProvider(scripted_events=_text_turn())
        agent = Agent(model="mock-7b", provider=provider,
                      include_env=False, include_memory=False, include_recall=False,
                      todos=todos)
        stale = UserMessage(content=_render_todo_reminder(
            [{"content": "step 1", "status": "pending"}]), isMeta=True)
        messages = [UserMessage(content="go"), stale, UserMessage(content="next")]
        async for _ in agent.run_iter(messages):
            pass
        await agent.aclose()
        sent = provider.calls[0]["messages"]
        assert len(_todo_reminders(sent)) == 1 and "[x] step 1" in sent[-1].content

    anyio.run(main)


def test_no_todos_no_reminder() -> None:
    async def main():
        agent = Agent(model="mock-7b", provider=MockProvider(scripted_events=_text_turn()),
                      include_env=False, include_memory=False, include_recall=False)
        messages = [UserMessage(content="go")]
        async for _ in agent.run_iter(messages):
            pass
        await agent.aclose()
        assert _todo_reminders(messages) == []

    anyio.run(main)
