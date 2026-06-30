"""``PermissionResultAllow.updated_input`` rewrites the tool input.

Real production use case: a ``can_use_tool`` callback wants to sanitize
arguments (PII redaction, path sandboxing, default injection) before
the tool runs. Claude SDK's ``PermissionResultAllow(updated_input=...)``
lets it. We honor it now.
"""

from __future__ import annotations

import anyio

from mantis_agent import (
    Agent,
    PermissionResultAllow,
    PermissionResultDeny,
    TextBlock,
    Tool,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
    tool,
)
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
from mantis_agent.permissions import Allow, PermissionContext
from mantis_agent.providers.mock import MockProvider


def _tool_call_then_text(call_input: str, final_text: str) -> tuple[list, list]:
    """Two scripted turns: tool call, then text answer."""

    turn1 = [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(
            index=0,
            block=ToolUseBlock(id="c1", name="add", input={}),
        ),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=call_input)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
        MessageStop(),
    ]
    turn2 = [
        MessageStart(message_id="m2", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=final_text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=12, output_tokens=4)),
        MessageStop(),
    ]
    return turn1, turn2


@tool
async def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


def test_permission_allow_with_updated_input_rewrites_args() -> None:
    """Model calls add(2, 3). The can_use_tool callback rewrites to (100, 200).
    Tool should see {a: 100, b: 200} and return "300"."""

    turn1, turn2 = _tool_call_then_text('{"a": 2, "b": 3}', "done")

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def can_use_tool(t: Tool, inp, ctx) -> PermissionResultAllow:
        # Sanitize / rewrite: replace whatever the model sent with our values.
        return PermissionResultAllow(updated_input={"a": 100, "b": 200})

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add],
            max_turns=5,
            permissions=PermissionContext(mode="default", can_use_tool=can_use_tool),
            include_memory=False,
        )
        try:
            msgs = await agent.run([UserMessage(content="add 2 and 3")])
        finally:
            await agent.aclose()

        # The tool_result block should contain "300" (100 + 200), proving the
        # updated_input was honored — not "5" (the model's original args).
        result_block = msgs[2].content[0]
        assert isinstance(result_block, ToolResultBlock)
        assert result_block.is_error is False
        assert result_block.content == "300", (
            f"updated_input was not honored: got {result_block.content!r}"
        )

    anyio.run(main)


def test_internal_allow_with_updated_input_also_rewrites() -> None:
    """Same behavior when can_use_tool returns the internal Allow struct
    (not the Claude-shape PermissionResultAllow) with updated_input set."""

    turn1, turn2 = _tool_call_then_text('{"a": 1, "b": 1}', "done")

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def can_use(t, inp, ctx) -> Allow:
        return Allow(updated_input={"a": 7, "b": 5})

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add],
            max_turns=5,
            permissions=PermissionContext(mode="default", can_use_tool=can_use),
            include_memory=False,
        )
        try:
            msgs = await agent.run([UserMessage(content="x")])
        finally:
            await agent.aclose()

        result_block = msgs[2].content[0]
        assert result_block.content == "12"  # 7 + 5

    anyio.run(main)


def test_permission_deny_via_claude_shape() -> None:
    """can_use_tool returns Claude-shape PermissionResultDeny — agent loop
    should bridge it to an internal Deny and short-circuit to is_error."""

    turn1, turn2 = _tool_call_then_text('{"a": 1, "b": 1}', "done")

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def can_use(t, inp, ctx) -> PermissionResultDeny:
        return PermissionResultDeny(message="sanitized: blocked")

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add],
            max_turns=5,
            permissions=PermissionContext(mode="default", can_use_tool=can_use),
            include_memory=False,
        )
        try:
            msgs = await agent.run([UserMessage(content="x")])
        finally:
            await agent.aclose()

        result_block = msgs[2].content[0]
        assert result_block.is_error is True
        assert "sanitized: blocked" in result_block.content

    anyio.run(main)


def test_default_allow_without_updated_input_passes_through() -> None:
    """Regression: when can_use_tool returns plain Allow (no updated_input),
    the original model-provided args reach the tool unchanged."""

    turn1, turn2 = _tool_call_then_text('{"a": 4, "b": 4}', "done")

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def can_use(t, inp, ctx) -> PermissionResultAllow:
        return PermissionResultAllow()  # No updated_input — original wins.

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[add],
            max_turns=5,
            permissions=PermissionContext(mode="default", can_use_tool=can_use),
            include_memory=False,
        )
        try:
            msgs = await agent.run([UserMessage(content="x")])
        finally:
            await agent.aclose()

        assert msgs[2].content[0].content == "8"  # 4 + 4 unchanged

    anyio.run(main)
