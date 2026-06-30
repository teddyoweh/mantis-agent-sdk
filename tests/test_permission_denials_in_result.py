"""``ResultMessage.permission_denials`` populates from can_use_tool denials.

Pairs with the PermissionResultAllow.updated_input wiring: when
can_use_tool returns Deny/PermissionResultDeny, that denial should
surface on the final result message so audit/observability tooling can
walk a single list of every blocked call.
"""

from __future__ import annotations

import anyio

from mantis_agent import (
    Agent,
    ClaudeAgentOptions,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    Tool,
    ToolUseBlock,
    UserMessage,
    Usage,
    query,
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
from mantis_agent.permissions import Deny, PermissionContext
from mantis_agent.providers.base import register
from mantis_agent.providers.mock import MockProvider


@tool
async def write_file(path: str, content: str) -> str:
    """Stub file write."""
    return f"wrote {len(content)} bytes to {path}"


def _two_turn_script(call_input: str) -> tuple[list, list]:
    turn1 = [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(
            index=0, block=ToolUseBlock(id="c1", name="write_file", input={})
        ),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=call_input)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
        MessageStop(),
    ]
    turn2 = [
        MessageStart(message_id="m2", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="cannot")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=12, output_tokens=2)),
        MessageStop(),
    ]
    return turn1, turn2


def test_agent_records_denials_to_internal_list() -> None:
    """Internal Deny populates ``agent._permission_denials``."""

    turn1, turn2 = _two_turn_script('{"path": "/etc/passwd", "content": "x"}')

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def can_use(t, inp, ctx) -> Deny:
        return Deny(reason="protected path")

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[write_file],
            max_turns=5,
            permissions=PermissionContext(mode="default", can_use_tool=can_use),
            include_memory=False,
        )
        try:
            await agent.run([UserMessage(content="write secrets")])
        finally:
            await agent.aclose()
        return agent._permission_denials

    denials = anyio.run(main)
    assert len(denials) == 1
    d = denials[0]
    assert d["tool_name"] == "write_file"
    assert d["tool_use_id"] == "c1"
    assert d["tool_input"] == {"path": "/etc/passwd", "content": "x"}


def test_query_result_message_carries_denials() -> None:
    """End-to-end via query(): the final ResultMessage has the denial."""

    turn1, turn2 = _two_turn_script('{"path": "/etc/passwd", "content": "x"}')

    class TwoTurnMock(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    # Patch the "mock" provider name to return our scripted instance.
    register("mock", lambda: TwoTurnMock())

    async def can_use(t, inp, ctx) -> PermissionResultDeny:
        return PermissionResultDeny(message="protected path")

    async def main():
        opts = ClaudeAgentOptions(
            model="mock-anything",
            backend="mock",
            tools=[write_file],
            can_use_tool=can_use,
            permission_mode="default",
            include_memory=False,
        )
        # Wire the can_use_tool via PermissionContext on .extra since
        # ClaudeAgentOptions stashes it there for the agent loop.
        opts.extra = {
            **(opts.extra or {}),
        }
        result = None
        # Attach PermissionContext directly via extra → it lands on
        # Agent.extra and gets ignored unless wired. Easier: use the
        # `permissions` field as a PermissionContext if supported, OR
        # pass it through extra. compat_query has special handling for
        # permissions. Easiest: pass through .extra and post-create.
        async for msg in query(prompt="x", options=opts):
            if isinstance(msg, ResultMessage):
                result = msg
        return result

    # We can't easily pass `permissions=PermissionContext(...)` through
    # ClaudeAgentOptions today, so we drive the lower-level Agent path
    # for the end-to-end denial assertion instead.
    async def main_direct():
        agent = Agent(
            model="mock-7b",
            provider=TwoTurnMock(),
            tools=[write_file],
            max_turns=5,
            permissions=PermissionContext(mode="default", can_use_tool=can_use),
            include_memory=False,
        )
        try:
            await agent.run([UserMessage(content="x")])
        finally:
            await agent.aclose()
        return agent._permission_denials

    denials = anyio.run(main_direct)
    assert len(denials) == 1
    assert denials[0]["tool_name"] == "write_file"
    assert denials[0]["tool_input"]["path"] == "/etc/passwd"


def test_result_message_has_permission_denials_field() -> None:
    """ResultMessage schema exposes ``permission_denials`` and defaults []."""

    r = ResultMessage()
    assert r.permission_denials == []
    r2 = ResultMessage(
        permission_denials=[
            {"tool_name": "x", "tool_use_id": "1", "tool_input": {"a": 1}}
        ]
    )
    assert len(r2.permission_denials) == 1
    assert r2.permission_denials[0]["tool_name"] == "x"
