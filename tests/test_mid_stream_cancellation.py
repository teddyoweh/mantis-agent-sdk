"""Mid-stream cancellation via ``ToolPermissionContext.signal``.

This closes the loop on the cancellation signal: when
``Agent.cancel()`` fires (or any caller flips
``ToolPermissionContext.signal``), the executor doesn't just *expose*
the event to ``can_use_tool`` callbacks — it actually:

  * Cancels every in-flight tool task via its per-task CancelScope,
    producing ``ToolResultBlock(content="cancelled by signal",
    is_error=True)`` blocks.
  * Short-circuits any *future* ``add_tool_call`` to the same
    cancellation result without dispatching.
  * Stops the agent's run-loop at the next turn boundary — no more
    model calls are issued after cancel.

The tests use a slow-tool fixture so we can deterministically fire
cancellation mid-flight without races.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import (
    Agent,
    AssistantMessage,
    TextBlock,
    Tool,
    ToolUseBlock,
    Usage,
    UserMessage,
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
    StreamEvent,
    TextDelta,
)
from mantis_agent.hooks import HookContext, HookResult, Hooks
from mantis_agent.providers.mock import MockProvider
from mantis_agent.streaming.executor import StreamingToolExecutor
from mantis_agent.tools import ToolRegistry
from mantis_agent.types import (
    AssistantMessage as InternalAssistantMessage,
    ToolResultBlock,
    UserMessage as InternalUserMessage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(model: str = "mock-7b") -> list[StreamEvent]:
    return [MessageStart(message_id="mock-mid", model=model)]


def _text_block(idx: int, text: str) -> list[StreamEvent]:
    return [
        ContentBlockStart(index=idx, block=TextBlock(text="")),
        ContentBlockDelta(index=idx, delta=TextDelta(text=text)),
        ContentBlockStop(index=idx),
    ]


def _tool_use_block(
    idx: int, call_id: str, name: str, input_json: str = "{}"
) -> list[StreamEvent]:
    return [
        ContentBlockStart(
            index=idx,
            block=ToolUseBlock(id=call_id, name=name, input={}),
        ),
        ContentBlockDelta(index=idx, delta=InputJsonDelta(partial_json=input_json)),
        ContentBlockStop(index=idx),
    ]


def _stop(
    stop_reason: str = "tool_use", usage: Usage | None = None
) -> list[StreamEvent]:
    return [
        MessageDelta(
            stop_reason=stop_reason,
            usage=usage or Usage(input_tokens=10, output_tokens=20),
        ),
        MessageStop(),
    ]


class _GatedTool:
    """Tool body that parks on an ``anyio.Event`` so we can observe
    in-flight state and fire cancellation while it's running.
    """

    def __init__(self, name: str = "gated"):
        self.entered = anyio.Event()
        self.release = anyio.Event()
        self.completed = anyio.Event()
        self.name = name

    def as_tool(self) -> Tool:
        outer = self

        async def _body(**kw) -> str:
            outer.entered.set()
            try:
                await outer.release.wait()
            finally:
                # ``completed`` fires on BOTH the natural-exit path and
                # the cancellation path — tests use it as a "the body
                # is no longer running" signal.
                outer.completed.set()
            return f"{outer.name}:{kw}"

        return Tool(
            name=outer.name,
            description=f"{outer.name} blocked tool",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
            fn=_body,
        )


class _ScriptedTurnMock(MockProvider):
    """Plays a list of scripted turns. Records every call to ``stream()``
    so a test can assert the agent didn't ask for another turn after
    cancellation.
    """

    def __init__(self, turns: list[list[StreamEvent]]):
        super().__init__()
        self._turns = turns
        self.stream_calls = 0

    async def stream(self, **kw):
        idx = self.stream_calls
        self.stream_calls += 1
        if idx >= len(self._turns):
            # Default natural-stop turn for any unexpected extra round-trips.
            for ev in (_msg() + _text_block(0, "unexpected") + _stop("end_turn")):
                yield ev
            return
        for ev in self._turns[idx]:
            yield ev


# ---------------------------------------------------------------------------
# 1. Executor: signal fires mid-flight, in-flight tool gets cancelled.
# ---------------------------------------------------------------------------


def test_executor_cancels_in_flight_tool_when_signal_fires():
    """The headline behavior — a tool body that's parked on an internal
    event sees its CancelScope fire when the cancellation_signal does,
    and produces a ``cancelled by signal`` result block.
    """

    gated = _GatedTool("slow")
    registry = ToolRegistry()
    registry.add(gated.as_tool())
    signal = anyio.Event()

    async def main() -> list[ToolResultBlock]:
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            ex.add_tool_call(ToolUseBlock(id="t1", name="slow", input={}))
            # Wait for the body to enter so we cancel a TRULY in-flight tool.
            with anyio.fail_after(2.0):
                await gated.entered.wait()
            signal.set()
            return await ex.wait_all()

    results = anyio.run(main)
    assert len(results) == 1
    assert results[0].tool_use_id == "t1"
    assert results[0].is_error is True
    assert results[0].content == "cancelled by signal"
    # The body's own cleanup should have run (anyio cancellation is
    # cooperative — finally blocks fire).
    assert gated.completed.is_set()


# ---------------------------------------------------------------------------
# 2. Executor: signal already set before any add_tool_call.
# ---------------------------------------------------------------------------


def test_executor_short_circuits_when_signal_already_set():
    """If the signal fires *before* anyone calls add_tool_call, every
    subsequent call short-circuits with no body execution at all.
    """

    gated = _GatedTool("slow")
    registry = ToolRegistry()
    registry.add(gated.as_tool())
    signal = anyio.Event()
    signal.set()  # pre-fire

    async def main() -> list[ToolResultBlock]:
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            ex.add_tool_call(ToolUseBlock(id="t1", name="slow", input={}))
            ex.add_tool_call(ToolUseBlock(id="t2", name="slow", input={}))
            return await ex.wait_all()

    results = anyio.run(main)
    assert [r.content for r in results] == [
        "cancelled by signal",
        "cancelled by signal",
    ]
    assert all(r.is_error for r in results)
    # The body never entered — short-circuit happened in add_tool_call.
    assert not gated.entered.is_set()


# ---------------------------------------------------------------------------
# 3. Executor: signal fired AFTER tools finished — results preserved.
# ---------------------------------------------------------------------------


def test_executor_late_signal_does_not_clobber_finished_results():
    """A signal that arrives *after* every tool already produced its
    real result doesn't replace those results with cancellation blocks.
    The watcher's cancel() of an already-completed CancelScope is a no-op.
    """

    @tool
    async def quick(value: str) -> str:
        return f"done:{value}"

    registry = ToolRegistry()
    registry.add(quick)
    signal = anyio.Event()

    async def main() -> list[ToolResultBlock]:
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            ex.add_tool_call(ToolUseBlock(id="q1", name="quick", input={"value": "x"}))
            # Let the body finish.
            results = await ex.wait_all()
            # NOW fire the signal — too late to do anything useful.
            signal.set()
            return results

    results = anyio.run(main)
    assert len(results) == 1
    assert results[0].is_error is False
    assert results[0].content == "done:x"


# ---------------------------------------------------------------------------
# 4. Executor: clean exit with signal never fired — watcher unparks.
# ---------------------------------------------------------------------------


def test_executor_clean_exit_when_signal_never_fires():
    """If the executor's caller exits without firing the signal, the
    watcher task must unpark cleanly on ``__aexit__``. Without the
    explicit ``self._watcher_scope.cancel()`` in __aexit__, the
    task group would block forever waiting on ``signal.wait()``.

    This test fail_after-wraps the run to catch that regression
    immediately if the wiring breaks.
    """

    @tool
    async def quick(value: str) -> str:
        return f"done:{value}"

    registry = ToolRegistry()
    registry.add(quick)
    signal = anyio.Event()

    async def main() -> list[ToolResultBlock]:
        with anyio.fail_after(3.0):
            async with StreamingToolExecutor(
                registry, cancellation_signal=signal
            ) as ex:
                ex.add_tool_call(
                    ToolUseBlock(id="q1", name="quick", input={"value": "a"})
                )
                return await ex.wait_all()

    results = anyio.run(main)
    assert len(results) == 1
    assert results[0].is_error is False


# ---------------------------------------------------------------------------
# 5. Executor: None signal → no watcher, normal behavior.
# ---------------------------------------------------------------------------


def test_executor_with_no_signal_runs_normally():
    """``cancellation_signal=None`` (the default) must not break the
    executor — no watcher is spawned and tools run as usual.
    """

    @tool
    async def quick(value: str) -> str:
        return f"done:{value}"

    registry = ToolRegistry()
    registry.add(quick)

    async def main() -> list[ToolResultBlock]:
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(
                ToolUseBlock(id="q1", name="quick", input={"value": "z"})
            )
            return await ex.wait_all()

    results = anyio.run(main)
    assert len(results) == 1
    assert results[0].is_error is False
    assert results[0].content == "done:z"


# ---------------------------------------------------------------------------
# 6. Executor: mid-burst cancellation — completed kept, in-flight cancelled,
# post-signal queued short-circuit.
# ---------------------------------------------------------------------------


def test_executor_partial_progress_when_signal_fires_mid_burst():
    """Realistic scenario: 3 tools dispatched. The first finished. The
    second is mid-flight. We fire the signal — the second cancels, the
    third (added AFTER the signal) short-circuits. Insertion order is
    preserved.
    """

    @tool
    async def quick(value: str) -> str:
        return f"done:{value}"

    slow = _GatedTool("slow")
    registry = ToolRegistry()
    registry.add(quick)
    registry.add(slow.as_tool())
    signal = anyio.Event()

    async def main() -> list[ToolResultBlock]:
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            # 1. Quick tool — finishes immediately.
            ex.add_tool_call(
                ToolUseBlock(id="q1", name="quick", input={"value": "a"})
            )
            # 2. Slow tool — parks on release.
            ex.add_tool_call(ToolUseBlock(id="s1", name="slow", input={}))
            with anyio.fail_after(2.0):
                await slow.entered.wait()
            # Fire cancellation while slow tool is mid-flight.
            signal.set()
            # Yield once so the watcher task observes the signal and
            # fires per-task scope.cancel() — without this, the next
            # ``add_tool_call`` may race the watcher and slip through.
            await anyio.sleep(0)
            # 3. Another tool added AFTER cancel — short-circuits.
            ex.add_tool_call(
                ToolUseBlock(id="q2", name="quick", input={"value": "b"})
            )
            return await ex.wait_all()

    results = anyio.run(main)
    assert len(results) == 3
    # Insertion-order preserved.
    assert [r.tool_use_id for r in results] == ["q1", "s1", "q2"]
    # 1. Completed quick tool keeps its real result.
    assert results[0].is_error is False
    assert results[0].content == "done:a"
    # 2. In-flight slow tool got cancelled.
    assert results[1].is_error is True
    assert results[1].content == "cancelled by signal"
    # 3. Post-signal call short-circuits with the same message.
    assert results[2].is_error is True
    assert results[2].content == "cancelled by signal"


# ---------------------------------------------------------------------------
# 7. Executor: signal cancellation distinguishable from sibling-abort.
# ---------------------------------------------------------------------------


def test_signal_cancellation_message_differs_from_sibling_abort():
    """The two cancel paths produce different ``ToolResultBlock.content``
    strings so a UI can show "cancelled by user" vs "aborted by sibling
    tool error" appropriately.
    """

    gated = _GatedTool("slow")
    registry = ToolRegistry()
    registry.add(gated.as_tool())

    # Path A: signal cancellation → "cancelled by signal".
    signal = anyio.Event()

    async def signal_path():
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            ex.add_tool_call(ToolUseBlock(id="t1", name="slow", input={}))
            with anyio.fail_after(2.0):
                await gated.entered.wait()
            signal.set()
            return await ex.wait_all()

    res_signal = anyio.run(signal_path)
    assert res_signal[0].content == "cancelled by signal"

    # Path B: sibling abort — same gated tool but a peer tool with
    # abort_siblings_on_error raises. (Re-use the same registry but
    # add an aborter.)
    async def _crash(**_):
        raise RuntimeError("boom")

    aborter = Tool(
        name="aborter",
        description="raises and aborts siblings",
        input_schema={"type": "object", "properties": {}},
        fn=_crash,
        abort_siblings_on_error=True,
    )
    reg2 = ToolRegistry()
    gated_b = _GatedTool("slow_b")
    reg2.add(gated_b.as_tool())
    reg2.add(aborter)

    async def abort_path():
        async with StreamingToolExecutor(reg2) as ex:
            ex.add_tool_call(ToolUseBlock(id="t1", name="slow_b", input={}))
            # Make sure the slow tool enters its body before we let
            # the aborter fire.
            with anyio.fail_after(2.0):
                await gated_b.entered.wait()
            ex.add_tool_call(ToolUseBlock(id="t2", name="aborter", input={}))
            return await ex.wait_all()

    res_abort = anyio.run(abort_path)
    # Find the slow_b result — order is preserved (t1 first, t2 second).
    slow_b_result = res_abort[0]
    assert slow_b_result.tool_use_id == "t1"
    assert slow_b_result.is_error is True
    assert slow_b_result.content == "aborted by sibling tool error"


# ---------------------------------------------------------------------------
# 8. Agent.cancel() before run() prevents any model call.
# ---------------------------------------------------------------------------


def test_agent_cancel_before_run_prevents_model_call():
    """If cancellation fires before the loop body executes its first
    turn, the agent emits the Stop hook and returns without ever
    asking the provider for a stream.
    """

    stop_fired: list[HookContext] = []

    async def on_stop(ctx: HookContext) -> HookResult:
        stop_fired.append(ctx)
        return HookResult()

    provider = _ScriptedTurnMock(
        [_msg() + _text_block(0, "hello") + _stop("end_turn")]
    )

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[],
            max_turns=3,
            include_memory=False,
            hooks=Hooks(stop=on_stop),
        )
        try:
            agent.cancel()  # Fire BEFORE run().
            messages: list = []
            async for m in agent.run_iter([UserMessage(content="hi")]):
                messages.append(m)
            return messages
        finally:
            await agent.aclose()

    yielded = anyio.run(main)
    # Provider should never have been called.
    assert provider.stream_calls == 0
    # No assistant message produced.
    assert not any(isinstance(m, InternalAssistantMessage) for m in yielded)
    # Stop hook fired once.
    assert len(stop_fired) == 1


# ---------------------------------------------------------------------------
# 9. Agent.cancel() mid-tool: in-flight body cancelled, no second model call.
# ---------------------------------------------------------------------------


def test_agent_cancel_mid_tool_cancels_body_and_stops_loop():
    """End-to-end: agent yields an assistant turn with a tool_use. We
    call ``agent.cancel()`` while the body is parked. The executor
    cancels the body, the agent yields a tool_result UserMessage with
    a ``cancelled by signal`` error block, and the next iteration of
    the run loop short-circuits — the provider's stream() is called
    exactly ONCE.
    """

    gated = _GatedTool("slow")
    turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "slow", "{}")
        + _stop("tool_use")
    )
    # If the loop accidentally calls stream() a second time, we'd see
    # this end_turn turn and the test would assert >1.
    turn2 = _msg() + _text_block(0, "should not run") + _stop("end_turn")

    provider = _ScriptedTurnMock([turn1, turn2])

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[gated.as_tool()],
            max_turns=5,
            include_memory=False,
        )
        try:
            yielded: list = []

            async def consumer():
                async for m in agent.run_iter([UserMessage(content="go")]):
                    yielded.append(m)

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # Wait for the body to enter its parked state.
                with anyio.fail_after(2.0):
                    await gated.entered.wait()
                # Cancel — the executor's watcher cancels the per-task
                # scope, the body bails, the agent yields a cancelled
                # tool_result, and the run loop's top-of-iteration
                # check stops the next turn.
                agent.cancel()
                # We don't release the gate — the body is being
                # cancelled, not naturally completing.
        finally:
            await agent.aclose()

        return yielded

    yielded = anyio.run(main)

    # Provider was called exactly once — no second turn happened after cancel.
    assert provider.stream_calls == 1

    # Yielded shape: assistant(tool_use) → user(tool_result error).
    types = [type(m).__name__ for m in yielded]
    assert types == ["AssistantMessage", "UserMessage"], f"got {types}"
    # The tool_result is a cancellation error block.
    user_msg = yielded[1]
    assert isinstance(user_msg, InternalUserMessage)
    blocks = user_msg.content
    assert len(blocks) == 1
    assert blocks[0].is_error is True
    assert blocks[0].content == "cancelled by signal"
    # The body's finally fired during cancellation.
    assert gated.completed.is_set()


# ---------------------------------------------------------------------------
# 10. Agent.cancel() between turns: no further provider calls.
# ---------------------------------------------------------------------------


def test_agent_cancel_after_completed_turn_stops_loop():
    """A natural-stop turn finishes normally; then the caller fires
    cancel(). The next turn iteration sees the signal and exits via
    the top-of-loop check without burning another model call.

    Drives the case where the user wants to stop AFTER a clean turn,
    without losing the result they already saw.
    """

    @tool
    async def echo(value: str) -> str:
        return f"echo:{value}"

    # Turn 1: produces a tool_use, the tool runs cleanly.
    turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "echo", '{"value": "hi"}')
        + _stop("tool_use")
    )
    # Turn 2 would be a normal end_turn — but we cancel before it
    # runs and the loop bails first.
    turn2 = _msg() + _text_block(0, "follow-up") + _stop("end_turn")
    provider = _ScriptedTurnMock([turn1, turn2])

    stop_fired: list = []

    async def on_stop(ctx: HookContext) -> HookResult:
        stop_fired.append(ctx)
        return HookResult()

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[echo],
            max_turns=5,
            include_memory=False,
            hooks=Hooks(stop=on_stop),
        )
        try:
            yielded: list = []
            async for m in agent.run_iter([UserMessage(content="go")]):
                yielded.append(m)
                # After we see the first tool-result UserMessage, fire
                # cancel(). The next outer-loop iteration must see the
                # signal and bail before calling provider again.
                if isinstance(m, InternalUserMessage) and any(
                    isinstance(b, ToolResultBlock) for b in m.content
                ):
                    agent.cancel()
            return yielded
        finally:
            await agent.aclose()

    yielded = anyio.run(main)
    # Provider called exactly once (turn 1). Turn 2 never happened.
    assert provider.stream_calls == 1
    # The tool ran cleanly — no error result.
    user_msg = next(m for m in yielded if isinstance(m, InternalUserMessage))
    assert user_msg.content[0].is_error is False
    assert user_msg.content[0].content == "echo:hi"
    # Stop hook fired exactly once on the cancelled-loop exit path.
    assert len(stop_fired) == 1


# ---------------------------------------------------------------------------
# 11. Agent.cancel() also short-circuits queued tool dispatches in the SAME
# turn (multi-tool burst).
# ---------------------------------------------------------------------------


def test_agent_cancel_during_multi_tool_burst_short_circuits_later_tools():
    """When the model emits two tool_use blocks back-to-back in one
    turn and we cancel after the first one starts, the second tool —
    arriving on the stream AFTER cancellation — short-circuits.
    """

    slow_a = _GatedTool("aaa")
    slow_b = _GatedTool("bbb")

    turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "aaa", "{}")
        + _tool_use_block(1, "c2", "bbb", "{}")
        + _stop("tool_use")
    )

    class _PausableMock(MockProvider):
        """Yields the script but holds at a checkpoint so we can fire
        cancel() between the two tool_use blocks."""

        def __init__(self):
            super().__init__()
            self.stream_calls = 0
            self.pause_after_first_tool_stop = anyio.Event()

        async def stream(self, **kw):
            self.stream_calls += 1
            if self.stream_calls > 1:
                # Unscripted extra turn — natural-stop.
                for ev in (
                    _msg() + _text_block(0, "extra") + _stop("end_turn")
                ):
                    yield ev
                return
            # Yield turn1 with a pause after the first tool block's stop.
            for i, ev in enumerate(turn1):
                yield ev
                # The first ContentBlockStop for the first tool block
                # is at index 3 in our turn1 layout (start, delta, stop).
                if i == 3:
                    await self.pause_after_first_tool_stop.wait()
                else:
                    await anyio.sleep(0)

    provider = _PausableMock()

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[slow_a.as_tool(), slow_b.as_tool()],
            max_turns=3,
            include_memory=False,
        )
        try:
            yielded: list = []

            async def consumer():
                async for m in agent.run_iter([UserMessage(content="go")]):
                    yielded.append(m)

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # First tool entered.
                with anyio.fail_after(2.0):
                    await slow_a.entered.wait()
                # Cancel BEFORE the second tool block is even yielded
                # by the stream.
                agent.cancel()
                # Now let the stream proceed — it'll emit the second
                # tool_use block and MessageStop. The agent's
                # add_tool_call for c2 should short-circuit (signal
                # already fired).
                provider.pause_after_first_tool_stop.set()
        finally:
            await agent.aclose()
        return yielded

    yielded = anyio.run(main)
    # Provider was called exactly once.
    assert provider.stream_calls == 1
    # The user message after the assistant turn contains TWO blocks
    # (one for each tool_use), both cancelled.
    user_msg = next(m for m in yielded if isinstance(m, InternalUserMessage))
    assert len(user_msg.content) == 2
    # First tool (was in-flight) cancelled.
    assert user_msg.content[0].tool_use_id == "c1"
    assert user_msg.content[0].is_error is True
    assert user_msg.content[0].content == "cancelled by signal"
    # Second tool (dispatched AFTER cancel) short-circuited.
    assert user_msg.content[1].tool_use_id == "c2"
    assert user_msg.content[1].is_error is True
    assert user_msg.content[1].content == "cancelled by signal"
    # Second tool's body never entered.
    assert not slow_b.entered.is_set()
