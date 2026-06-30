"""End-to-end agent run-loop integration tests via ``MockProvider``.

These exercise the full wired-up flow:

  * Streaming → assembly → tool dispatch
  * StreamingToolExecutor parallel/serial partitioning
  * PreToolUse / PostToolUse / Stop hooks
  * Permission gating (deny + mutation)
  * Budget tracking (USD + turns) and BudgetExceededError
  * Multi-turn loops with tool results fed back

The tests use scripted SSE-style event lists so there's no network and no
flakiness. Real-backend tests live in ``tests/test_real_*.py``.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import (
    Agent,
    AssistantMessage,
    BudgetExceededError,
    TextBlock,
    Tool,
    ToolUseBlock,
    Usage,
    UserMessage,
    tool,
)
from mantis_agent.budget import Budget
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
from mantis_agent.hooks import HookContext, HookResult, Hooks
from mantis_agent.permissions import Allow, Deny, PermissionContext
from mantis_agent.providers.mock import MockProvider


# ---------------------------------------------------------------------------
# Fixture-script builders
# ---------------------------------------------------------------------------


def _msg(model: str = "mock-7b") -> list:
    return [MessageStart(message_id="mock-1", model=model)]


def _text_block(idx: int, text: str) -> list:
    return [
        ContentBlockStart(index=idx, block=TextBlock(text="")),
        ContentBlockDelta(index=idx, delta=TextDelta(text=text)),
        ContentBlockStop(index=idx),
    ]


def _tool_use_block(idx: int, call_id: str, name: str, input_json: str) -> list:
    return [
        ContentBlockStart(
            index=idx,
            block=ToolUseBlock(id=call_id, name=name, input={}),
        ),
        ContentBlockDelta(
            index=idx, delta=InputJsonDelta(partial_json=input_json)
        ),
        ContentBlockStop(index=idx),
    ]


def _stop(stop_reason: str = "end_turn", usage: Usage | None = None) -> list:
    return [
        MessageDelta(stop_reason=stop_reason, usage=usage or Usage(input_tokens=10, output_tokens=20)),
        MessageStop(),
    ]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
async def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


@tool
async def multiply(a: int, b: int) -> str:
    """Multiply two numbers."""
    return str(a * b)


@tool(is_concurrency_safe=False)
async def serial_only(x: int) -> str:
    """Tool that must not run in parallel with itself."""
    return f"serial:{x}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _make_agent(events: list, **kw):
    return Agent(
        model="mock-7b",
        provider=MockProvider(scripted_events=events),
        tools=kw.pop("tools", [add, multiply]),
        max_turns=kw.pop("max_turns", 3),
        **kw,
    )


def test_simple_text_response_no_tools():
    """Assistant emits just text → Stop fires → return."""

    events = (
        _msg()
        + _text_block(0, "Hello there.")
        + _stop()
    )
    stops_fired = []

    async def stop_hook(ctx: HookContext) -> HookResult:
        stops_fired.append(ctx.event)
        return HookResult()

    async def main():
        agent = _make_agent(events, hooks=Hooks(stop=stop_hook))
        try:
            msgs = await agent.run([UserMessage(content="Hi")])
        finally:
            await agent.aclose()
        assert isinstance(msgs[-1], AssistantMessage)
        text = "".join(
            b.text for b in msgs[-1].content if isinstance(b, TextBlock)
        )
        assert text == "Hello there."
        assert stops_fired == ["Stop"]

    anyio.run(main)


def test_single_tool_call_then_text():
    """Assistant calls add(2, 3) → tool runs → assistant emits final text."""

    # First turn: emit a tool call.
    events_turn1 = (
        _msg() + _tool_use_block(0, "c1", "add", '{"a": 2, "b": 3}') + _stop(stop_reason="tool_use")
    )
    # Second turn: emit final answer.
    events_turn2 = (
        _msg() + _text_block(0, "Answer: 5") + _stop()
    )

    # Mock with two scripts — but MockProvider replays the same list each call.
    # So we use a state-flipping provider via subclass for this test.
    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = events_turn1 if self._turn == 0 else events_turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add, multiply],
            max_turns=5,
        )
        try:
            msgs = await agent.run([UserMessage(content="Add 2 and 3")])
        finally:
            await agent.aclose()

        # Expect: [user, assistant(tool_use), user(tool_result), assistant(text)]
        assert len(msgs) == 4
        assert isinstance(msgs[1], AssistantMessage)
        tool_uses = [b for b in msgs[1].content if isinstance(b, ToolUseBlock)]
        assert len(tool_uses) == 1
        assert tool_uses[0].name == "add"

        # Tool result message
        tool_result_block = msgs[2].content[0]
        assert tool_result_block.content == "5"
        assert tool_result_block.is_error is False

        # Final assistant text
        final_text = "".join(
            b.text for b in msgs[-1].content if isinstance(b, TextBlock)
        )
        assert "5" in final_text

    anyio.run(main)


def test_permission_denied_short_circuits():
    """A Deny rule turns the tool call into an is_error result block,
    PostToolUse never fires for it, and the loop continues."""

    events_turn1 = (
        _msg() + _tool_use_block(0, "c1", "add", '{"a": 2, "b": 3}') + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "Got it") + _stop()

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = events_turn1 if self._turn == 0 else events_turn2
            self._turn += 1
            for ev in script:
                yield ev

    posts = []
    denies = []

    async def post_hook(ctx: HookContext) -> HookResult:
        posts.append(ctx.tool.name)
        return HookResult()

    async def can_use(t: Tool, inp, ctx) -> Deny:
        denies.append(t.name)
        return Deny(reason="test denial")

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add],
            max_turns=5,
            hooks=Hooks(post_tool_use=post_hook),
            permissions=PermissionContext(mode="default", can_use_tool=can_use),
        )
        try:
            msgs = await agent.run([UserMessage(content="Add 2 and 3")])
        finally:
            await agent.aclose()

        # The tool was denied, so the result block is is_error.
        tool_result_block = msgs[2].content[0]
        assert tool_result_block.is_error is True
        assert "permission denied" in tool_result_block.content
        # And PostToolUse should NOT have fired for a denied tool.
        assert posts == []
        # can_use was called for `add` once.
        assert denies == ["add"]

    anyio.run(main)


def test_pretooluse_hook_mutates_input():
    """PreToolUse hook rewrites the tool input before dispatch."""

    events_turn1 = (
        _msg() + _tool_use_block(0, "c1", "add", '{"a": 2, "b": 3}') + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "done") + _stop()

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = events_turn1 if self._turn == 0 else events_turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def pre_hook(ctx: HookContext) -> HookResult:
        # Rewrite 2 + 3 to 100 + 200
        return HookResult(mutated_input={"a": 100, "b": 200})

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add],
            max_turns=5,
            hooks=Hooks(pre_tool_use=pre_hook),
        )
        try:
            msgs = await agent.run([UserMessage(content="Add 2 and 3")])
        finally:
            await agent.aclose()

        tool_result_block = msgs[2].content[0]
        assert tool_result_block.content == "300", f"expected 300 got {tool_result_block.content}"

    anyio.run(main)


def test_budget_max_usd_raises():
    """Budget overrun raises BudgetExceededError after the turn finalizes."""

    # 1M output tokens at $0.88/M = $0.88 — well over $0.01 cap.
    huge_usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    events = _msg() + _text_block(0, "ok") + _stop(usage=huge_usage)

    async def main():
        agent = Agent(
            model="llama-3.3-70b-instruct",
            backend="https://api.together.xyz/v1",  # triggers together pricing
            provider=MockProvider(scripted_events=events),
            tools=[],
            max_usd=0.01,  # $0.01 budget — well below the expected cost
            max_turns=3,
        )
        try:
            with pytest.raises(BudgetExceededError):
                await agent.run([UserMessage(content="hi")])
        finally:
            await agent.aclose()

    anyio.run(main)


def test_parallel_tool_dispatch():
    """Two concurrent-safe tools in one turn should both run."""

    events_turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "add", '{"a": 1, "b": 2}')
        + _tool_use_block(1, "c2", "multiply", '{"a": 4, "b": 5}')
        + _stop(stop_reason="tool_use")
    )
    events_turn2 = _msg() + _text_block(0, "done") + _stop()

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = events_turn1 if self._turn == 0 else events_turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add, multiply],
            max_turns=5,
        )
        try:
            msgs = await agent.run([UserMessage(content="Compute")])
        finally:
            await agent.aclose()

        # Tool results: c1 -> "3", c2 -> "20"
        results = msgs[2].content
        results_by_id = {r.tool_use_id: r for r in results}
        assert results_by_id["c1"].content == "3"
        assert results_by_id["c2"].content == "20"
        assert not results_by_id["c1"].is_error
        assert not results_by_id["c2"].is_error

    anyio.run(main)
