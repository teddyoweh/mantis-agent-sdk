"""``ToolPermissionContext.signal`` — AbortSignal parity for cancellation.

Closes the Claude SDK gap: their ``can_use_tool`` callback receives a
``ToolPermissionContext`` whose ``.signal`` (an AbortSignal in TS) fires
when the agent is asked to stop. We expose ``anyio.Event`` and wire it
through both ``Agent.cancel()`` and the permission-callback dispatch.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import (
    Agent,
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    Tool,
    ToolPermissionContext,
    ToolUseBlock,
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
from mantis_agent.permissions import PermissionContext
from mantis_agent.providers.mock import MockProvider


# ---------------------------------------------------------------------------
# ToolPermissionContext shape — signal defaults to a fresh, never-fired Event
# ---------------------------------------------------------------------------


def test_signal_is_anyio_event_by_default() -> None:
    """Constructing without args produces a usable, never-fired event."""

    ctx = ToolPermissionContext()
    assert isinstance(ctx.signal, anyio.Event)
    assert ctx.signal.is_set() is False


def test_signal_can_be_supplied_explicitly() -> None:
    """User-supplied event is preserved verbatim."""

    user_event = anyio.Event()
    user_event.set()
    ctx = ToolPermissionContext(signal=user_event)
    assert ctx.signal is user_event
    assert ctx.signal.is_set() is True


def test_two_contexts_have_distinct_signals() -> None:
    """Default signal isn't a shared class-level singleton."""

    a = ToolPermissionContext()
    b = ToolPermissionContext()
    assert a.signal is not b.signal


# ---------------------------------------------------------------------------
# Agent.cancel() flips the signal
# ---------------------------------------------------------------------------


def test_agent_cancel_sets_cancellation_signal() -> None:
    """``Agent.cancel()`` fires the shared event."""

    agent = Agent(model="mock-7b", backend="mock", provider=MockProvider())
    assert agent.cancellation_signal.is_set() is False
    agent.cancel()
    assert agent.cancellation_signal.is_set() is True


def test_agent_cancel_is_idempotent() -> None:
    """Calling cancel() twice doesn't blow up."""

    agent = Agent(model="mock-7b", backend="mock", provider=MockProvider())
    agent.cancel()
    agent.cancel()  # second call should be a no-op
    assert agent.cancellation_signal.is_set() is True


def test_permission_context_inherits_agent_signal() -> None:
    """When the agent wires PermissionContext, the signal is shared."""

    pc = PermissionContext(mode="default")
    agent = Agent(
        model="mock-7b",
        backend="mock",
        provider=MockProvider(),
        permissions=pc,
    )
    assert agent.permissions.signal is agent.cancellation_signal


def test_permission_context_with_preexisting_signal_is_left_alone() -> None:
    """If the user wired their own signal into PermissionContext, don't
    clobber it."""

    user_signal = anyio.Event()
    pc = PermissionContext(mode="default", signal=user_signal)
    agent = Agent(
        model="mock-7b",
        backend="mock",
        provider=MockProvider(),
        permissions=pc,
    )
    assert agent.permissions.signal is user_signal
    assert agent.permissions.signal is not agent.cancellation_signal


# ---------------------------------------------------------------------------
# can_use_tool receives a real ToolPermissionContext (not a raw dict)
# ---------------------------------------------------------------------------


@tool
async def echo(text: str) -> str:
    """Echo the input."""
    return text


def _two_turn_script(args_json: str, final_text: str = "ok") -> tuple[list, list]:
    turn1 = [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(index=0, block=ToolUseBlock(id="c1", name="echo", input={})),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=args_json)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
        MessageStop(),
    ]
    turn2 = [
        MessageStart(message_id="m2", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=final_text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=8, output_tokens=3)),
        MessageStop(),
    ]
    return turn1, turn2


class _TwoTurnMock(MockProvider):
    def __init__(self, t1, t2):
        super().__init__()
        self._t1, self._t2 = t1, t2
        self._turn = 0

    async def stream(self, **kw):
        script = self._t1 if self._turn == 0 else self._t2
        self._turn += 1
        for ev in script:
            yield ev


def test_can_use_tool_receives_ToolPermissionContext() -> None:
    """The third arg to can_use_tool is a typed context object with
    ``.signal``, not the raw ``ctx.extra`` dict."""

    turn1, turn2 = _two_turn_script('{"text": "hi"}')
    provider = _TwoTurnMock(turn1, turn2)

    received_ctx: list = []

    async def can_use(t: Tool, inp, ctx):
        received_ctx.append(ctx)
        return PermissionResultAllow()

    async def main() -> ResultMessage:
        result = None
        async for msg in query(
            prompt="echo hi",
            options=ClaudeAgentOptions(
                model="mock-7b",
                backend="mock",
                tools=[echo],
                max_turns=5,
                can_use_tool=can_use,
                include_memory=False,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result = msg
        return result

    from mantis_agent.providers.base import register
    register("mock", lambda: provider)

    anyio.run(main)
    assert len(received_ctx) == 1
    ctx = received_ctx[0]
    assert isinstance(ctx, ToolPermissionContext)
    assert isinstance(ctx.signal, anyio.Event)
    assert ctx.signal.is_set() is False


def test_can_use_tool_signal_observed_by_callback() -> None:
    """A can_use_tool callback inspecting ctx.signal can flip its
    decision based on whether the agent was cancelled before this call.
    Drives the lower-level Agent path so we can wire a custom
    PermissionContext with a pre-fired signal — ClaudeAgentOptions
    doesn't yet expose a 'permissions' field directly."""

    from mantis_agent import UserMessage

    turn1, turn2 = _two_turn_script('{"text": "hi"}')
    provider = _TwoTurnMock(turn1, turn2)

    cancel_event = anyio.Event()
    cancel_event.set()  # Pre-fire to simulate 'agent.cancel() already happened'

    seen_signal_state: list = []

    async def can_use(t: Tool, inp, ctx):
        seen_signal_state.append(ctx.signal.is_set())
        if ctx.signal.is_set():
            return PermissionResultDeny(message="cancelled")
        return PermissionResultAllow()

    async def main() -> list:
        agent = Agent(
            model="mock-7b",
            backend="mock",
            provider=provider,
            tools=[echo],
            max_turns=5,
            permissions=PermissionContext(
                mode="default", can_use_tool=can_use, signal=cancel_event,
            ),
            include_memory=False,
        )
        try:
            await agent.run([UserMessage(content="echo hi")])
        finally:
            await agent.aclose()
        return agent._permission_denials

    denials = anyio.run(main)
    # The pre-fired signal should have been observed by the callback,
    # which then denied — so we expect a permission denial recorded.
    assert seen_signal_state == [True]
    assert len(denials) == 1
