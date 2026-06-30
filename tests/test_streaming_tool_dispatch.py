"""Mid-stream tool dispatch: tools fire the moment their input JSON
closes — *not* after ``MessageStop``.

This is the speed unlock the roadmap calls out under
``Tool use → Streaming tool dispatch``. Concretely:

  * For every ``ContentBlockStop`` event that closes a ``tool_use`` block,
    ``Agent.run_iter`` must dispatch the tool to the live
    ``StreamingToolExecutor`` immediately. The body starts running
    concurrently with the remainder of the stream (more deltas, more
    tool_use blocks, ``MessageStop``).
  * Pre-flight hooks + permissions still run before dispatch, mid-stream.
  * Result ordering is preserved (insertion order = stream order =
    assistant.content order).
  * Multi-tool turns parallelize: dispatch happens block-by-block, not
    all-at-once at the end of the stream.
  * Malformed input JSON still surfaces as a ``StreamProtocolError`` from
    ``assembler.finalize()`` — no broken dispatch.

The tests use a *slow* mock provider: each event is held behind a gate or
an explicit ``anyio.sleep`` so we can deterministically observe ordering
without races.
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
from mantis_agent.errors import StreamProtocolError
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
from mantis_agent.permissions import Deny, PermissionContext
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import (
    AssistantMessage as InternalAssistantMessage,
    ToolResultBlock,
    UserMessage as InternalUserMessage,
)


# ---------------------------------------------------------------------------
# Event-list builders (same shape as test_streaming_mode / test_run_loop)
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
    idx: int, call_id: str, name: str, input_json: str
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


# ---------------------------------------------------------------------------
# Slow provider — yields events on a checkpoint gate so tests can probe
# ordering deterministically.
# ---------------------------------------------------------------------------


class _GatedStreamMock(MockProvider):
    """Mock that pauses the stream at named checkpoints.

    Pass a list of (event, checkpoint_label_or_None) tuples per turn. When
    a checkpoint is hit, the mock awaits the event in ``self.gates[label]``
    before yielding the next event. After the gate fires it continues.

    Each test sets gates exactly when needed — no fail_after / sleep
    polling required.
    """

    def __init__(self, turns: list[list[tuple[StreamEvent, str | None]]]):
        super().__init__()
        self._turns = turns
        self._turn = 0
        self.gates: dict[str, anyio.Event] = {}
        self.checkpoints_reached: list[str] = []

    def gate(self, label: str) -> anyio.Event:
        ev = self.gates.get(label)
        if ev is None:
            ev = anyio.Event()
            self.gates[label] = ev
        return ev

    async def stream(self, **kw):
        if self._turn >= len(self._turns):
            # No more scripted turns — return a trivial natural-stop turn.
            for ev in (_msg() + _text_block(0, "done") + _stop("end_turn")):
                yield ev
            return
        script = self._turns[self._turn]
        self._turn += 1
        for ev, checkpoint in script:
            yield ev
            if checkpoint is not None:
                self.checkpoints_reached.append(checkpoint)
                await self.gate(checkpoint).wait()
            else:
                # Always give other tasks a chance to interleave.
                await anyio.sleep(0)


# ---------------------------------------------------------------------------
# Tools used across the tests
# ---------------------------------------------------------------------------


class _GatedTool:
    """Tool body that blocks on an event so we can probe in-flight state."""

    def __init__(self, name: str = "gated"):
        self.entered = anyio.Event()
        self.release = anyio.Event()
        self.name = name

    def as_tool(self) -> Tool:
        outer = self

        async def _body(**kw) -> str:
            outer.entered.set()
            await outer.release.wait()
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


@tool
async def echo(value: str) -> str:
    """Return the value unchanged."""
    return value


@tool
async def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


# ---------------------------------------------------------------------------
# 1. Tool body starts BEFORE MessageStop is observed.
# ---------------------------------------------------------------------------


def test_tool_dispatched_before_message_stop_arrives():
    """The core invariant: a tool body must run BEFORE the agent
    finishes consuming the provider stream.

    The mock holds the stream open at ``after_tool_stop`` (right after
    the tool_use ContentBlockStop event). At that checkpoint the agent
    has dispatched the tool. We wait for the tool body to enter,
    THEN release the stream so it can produce MessageStop.

    If dispatch only happened after MessageStop, the tool body would
    never enter while we're still holding the stream — a fail_after
    would trigger.
    """

    gated = _GatedTool("slow_one")

    turn1_script: list[tuple[StreamEvent, str | None]] = []
    # Open of message + tool_use block
    for ev in _msg():
        turn1_script.append((ev, None))
    for ev in _tool_use_block(0, "c1", "slow_one", "{}"):
        turn1_script.append((ev, None))
    # Pause RIGHT AFTER the tool_use ContentBlockStop — before MessageStop.
    # The label re-tags the last event so we wait after it yields.
    turn1_script[-1] = (turn1_script[-1][0], "after_tool_stop")
    # Then finish the message normally.
    for ev in _stop("tool_use"):
        turn1_script.append((ev, None))

    turn2 = (
        [(ev, None) for ev in _msg()]
        + [(ev, None) for ev in _text_block(0, "done")]
        + [(ev, None) for ev in _stop("end_turn")]
    )

    async def main():
        provider = _GatedStreamMock([turn1_script, turn2])
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[gated.as_tool()],
            max_turns=5,
            include_memory=False,
        )
        try:
            seen: list = []

            async def consumer():
                async for m in agent.run_iter([UserMessage(content="go")]):
                    seen.append(m)

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # The stream is paused right after the tool_use block
                # closed. The tool body MUST have entered by now —
                # mid-stream dispatch is the whole point.
                with anyio.fail_after(2.0):
                    await gated.entered.wait()
                # Tool is mid-execution; the stream is still paused.
                # No AssistantMessage has been yielded yet — finalize()
                # hasn't run because MessageStop hasn't arrived.
                assert not any(
                    isinstance(m, InternalAssistantMessage) for m in seen
                ), "AssistantMessage yielded before the stream finished"
                # Now release the stream so MessageStop can land.
                provider.gate("after_tool_stop").set()
                # Release the tool so the executor can produce its result.
                gated.release.set()
        finally:
            await agent.aclose()

        # Final shape: assistant(tool_use) → user(tool_result) → assistant(text).
        types = [type(m).__name__ for m in seen]
        assert types == [
            "AssistantMessage",
            "UserMessage",
            "AssistantMessage",
        ], f"got {types}"
        result_msg = seen[1]
        assert isinstance(result_msg, InternalUserMessage)
        results = result_msg.content
        assert len(results) == 1
        assert results[0].tool_use_id == "c1"
        assert results[0].is_error is False
        assert "slow_one" in results[0].content

    anyio.run(main)


# ---------------------------------------------------------------------------
# 2. Two parallel tools — both start before MessageStop arrives.
# ---------------------------------------------------------------------------


def test_parallel_tools_both_start_mid_stream():
    """When the model emits two tool_use blocks in the same turn, both
    bodies must start RUNNING before ``MessageStop`` is observed — even
    if the second tool_use block hasn't been emitted yet at the time the
    first one is dispatched.
    """

    tool_a = _GatedTool("aaa")
    tool_b = _GatedTool("bbb")

    turn1: list[tuple[StreamEvent, str | None]] = []
    for ev in _msg():
        turn1.append((ev, None))
    # First tool block.
    for ev in _tool_use_block(0, "c1", "aaa", "{}"):
        turn1.append((ev, None))
    # Pause AFTER the first tool block closed — tool A should be running
    # by the time the consumer continues.
    turn1[-1] = (turn1[-1][0], "after_a_stop")
    # Second tool block.
    for ev in _tool_use_block(1, "c2", "bbb", "{}"):
        turn1.append((ev, None))
    # Pause AFTER the second tool block closed — both tools should be
    # in flight by now.
    turn1[-1] = (turn1[-1][0], "after_b_stop")
    for ev in _stop("tool_use"):
        turn1.append((ev, None))

    turn2 = (
        [(ev, None) for ev in _msg()]
        + [(ev, None) for ev in _text_block(0, "ok")]
        + [(ev, None) for ev in _stop("end_turn")]
    )

    async def main():
        provider = _GatedStreamMock([turn1, turn2])
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[tool_a.as_tool(), tool_b.as_tool()],
            max_turns=5,
            include_memory=False,
        )
        try:
            seen: list = []

            async def consumer():
                async for m in agent.run_iter([UserMessage(content="go")]):
                    seen.append(m)

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # Tool A must enter while we're still paused after its
                # ContentBlockStop event.
                with anyio.fail_after(2.0):
                    await tool_a.entered.wait()
                assert not tool_b.entered.is_set(), (
                    "tool B started before its block was emitted"
                )
                # Let the stream produce tool B's block.
                provider.gate("after_a_stop").set()
                # Tool B should now enter, while we're paused after ITS
                # ContentBlockStop and BEFORE MessageStop.
                with anyio.fail_after(2.0):
                    await tool_b.entered.wait()
                # Release both tools and the stream.
                tool_a.release.set()
                tool_b.release.set()
                provider.gate("after_b_stop").set()
        finally:
            await agent.aclose()

        # Result order matches stream order (c1, then c2).
        result_msg = seen[1]
        results = result_msg.content
        assert [r.tool_use_id for r in results] == ["c1", "c2"]
        assert all(not r.is_error for r in results)

    anyio.run(main)


# ---------------------------------------------------------------------------
# 3. PreToolUse hook runs mid-stream and its input mutation reaches the
#    dispatched tool.
# ---------------------------------------------------------------------------


def test_pretooluse_hook_mutates_input_mid_stream():
    """The hook fires while the stream is still arriving. The mutated
    input must be the one the tool body sees."""

    seen_inputs: list = []

    @tool
    async def record(a: int, b: int) -> str:
        """Record the input the tool body actually saw."""
        seen_inputs.append({"a": a, "b": b})
        return str(a + b)

    async def pre(ctx: HookContext) -> HookResult:
        # Rewrite 1+2 → 10+20
        return HookResult(mutated_input={"a": 10, "b": 20})

    turn1: list[tuple[StreamEvent, str | None]] = []
    for ev in _msg():
        turn1.append((ev, None))
    for ev in _tool_use_block(0, "c1", "record", '{"a": 1, "b": 2}'):
        turn1.append((ev, None))
    # Pause to make sure the tool actually fired mid-stream.
    turn1[-1] = (turn1[-1][0], "after_stop")
    for ev in _stop("tool_use"):
        turn1.append((ev, None))

    turn2 = (
        [(ev, None) for ev in _msg()]
        + [(ev, None) for ev in _text_block(0, "ok")]
        + [(ev, None) for ev in _stop("end_turn")]
    )

    async def main():
        provider = _GatedStreamMock([turn1, turn2])
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[record],
            max_turns=5,
            include_memory=False,
            hooks=Hooks(pre_tool_use=pre),
        )
        try:
            seen: list = []

            async def consumer():
                async for m in agent.run_iter([UserMessage(content="go")]):
                    seen.append(m)

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # Wait briefly to make sure the dispatch loop has had time
                # to fire the tool. Then release the rest of the stream.
                await anyio.sleep(0.05)
                provider.gate("after_stop").set()
        finally:
            await agent.aclose()

        assert seen_inputs == [{"a": 10, "b": 20}]
        result = seen[1].content[0]
        assert result.content == "30"
        assert result.is_error is False

    anyio.run(main)


# ---------------------------------------------------------------------------
# 4. Permission deny short-circuits mid-stream — the body never runs.
# ---------------------------------------------------------------------------


def test_permission_deny_mid_stream_skips_dispatch():
    """A Deny decision converts the call into an is_error result block
    immediately when the tool_use closes — the body NEVER runs."""

    seen_bodies: list = []

    @tool
    async def secret(value: str) -> str:
        """Should never run."""
        seen_bodies.append(value)
        return value

    async def can_use(t: Tool, inp, ctx):
        return Deny(reason="not in this test")

    turn1: list[tuple[StreamEvent, str | None]] = []
    for ev in _msg():
        turn1.append((ev, None))
    for ev in _tool_use_block(0, "c1", "secret", '{"value": "x"}'):
        turn1.append((ev, None))
    for ev in _stop("tool_use"):
        turn1.append((ev, None))

    turn2 = (
        [(ev, None) for ev in _msg()]
        + [(ev, None) for ev in _text_block(0, "ok")]
        + [(ev, None) for ev in _stop("end_turn")]
    )

    async def main():
        provider = _GatedStreamMock([turn1, turn2])
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[secret],
            max_turns=5,
            include_memory=False,
            permissions=PermissionContext(mode="default", can_use_tool=can_use),
        )
        try:
            seen: list = []
            async for m in agent.run_iter([UserMessage(content="go")]):
                seen.append(m)
        finally:
            await agent.aclose()

        assert seen_bodies == []  # tool body never ran
        result = seen[1].content[0]
        assert result.is_error is True
        assert "permission denied" in result.content

    anyio.run(main)


# ---------------------------------------------------------------------------
# 5. Tool fires mid-stream while subsequent text deltas keep arriving.
# ---------------------------------------------------------------------------


def test_tool_runs_concurrently_with_post_tool_text_deltas():
    """The model can emit text deltas AFTER a tool_use block closes.
    The tool body must already be running by the time those deltas
    arrive — mid-stream dispatch lets the tool overlap with the rest of
    the assistant's output."""

    gated = _GatedTool("slow")

    turn1: list[tuple[StreamEvent, str | None]] = []
    for ev in _msg():
        turn1.append((ev, None))
    for ev in _tool_use_block(0, "c1", "slow", "{}"):
        turn1.append((ev, None))
    # ContentBlockStart for the trailing text block
    turn1.append((ContentBlockStart(index=1, block=TextBlock(text="")), None))
    # Pause after the start of the trailing text — the tool should be
    # running by now (its ContentBlockStop already fired).
    turn1[-1] = (turn1[-1][0], "before_text_delta")
    turn1.append(
        (ContentBlockDelta(index=1, delta=TextDelta(text="(tail)")), None)
    )
    turn1.append((ContentBlockStop(index=1), None))
    for ev in _stop("tool_use"):
        turn1.append((ev, None))

    turn2 = (
        [(ev, None) for ev in _msg()]
        + [(ev, None) for ev in _text_block(0, "fin")]
        + [(ev, None) for ev in _stop("end_turn")]
    )

    async def main():
        provider = _GatedStreamMock([turn1, turn2])
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[gated.as_tool()],
            max_turns=5,
            include_memory=False,
        )
        try:
            seen: list = []

            async def consumer():
                async for m in agent.run_iter([UserMessage(content="go")]):
                    seen.append(m)

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # Tool body should already be running by the time we
                # observe the "before_text_delta" checkpoint.
                with anyio.fail_after(2.0):
                    await gated.entered.wait()
                provider.gate("before_text_delta").set()
                gated.release.set()
        finally:
            await agent.aclose()

        assistant = seen[0]
        assert isinstance(assistant, InternalAssistantMessage)
        text = "".join(b.text for b in assistant.content if isinstance(b, TextBlock))
        assert text == "(tail)"
        tool_uses = [b for b in assistant.content if isinstance(b, ToolUseBlock)]
        assert [t.name for t in tool_uses] == ["slow"]

    anyio.run(main)


# ---------------------------------------------------------------------------
# 6. Malformed input JSON falls through to ``finalize()`` and raises
#    ``StreamProtocolError`` — no half-dispatch.
# ---------------------------------------------------------------------------


def test_malformed_tool_input_json_raises_at_finalize():
    """A tool_use block whose input JSON is malformed must NOT be
    dispatched. The agent surfaces ``StreamProtocolError`` from the
    assembler's ``finalize`` call so the whole turn errors out — same
    behavior as the pre-streaming path."""

    @tool
    async def never(value: str) -> str:
        """Should never be called with malformed input."""
        return value

    bodies: list = []

    @tool
    async def witness(value: str) -> str:
        """Records that it ran — used to prove dispatch did NOT happen."""
        bodies.append(value)
        return value

    events: list[StreamEvent] = []
    events.extend(_msg())
    events.append(
        ContentBlockStart(
            index=0,
            block=ToolUseBlock(id="bad", name="witness", input={}),
        )
    )
    events.append(ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json='{not valid')))
    events.append(ContentBlockStop(index=0))
    events.extend(_stop("tool_use"))

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=MockProvider(scripted_events=events),
            tools=[witness, never],
            max_turns=2,
            include_memory=False,
        )
        try:
            with pytest.raises(StreamProtocolError):
                msgs = [UserMessage(content="go")]
                async for _ in agent.run_iter(msgs):
                    pass
        finally:
            await agent.aclose()

        assert bodies == []  # never dispatched

    anyio.run(main)


# ---------------------------------------------------------------------------
# 7. Unknown tool — mid-stream dispatch still produces the canonical
#    "not found" error block; no exception escapes.
# ---------------------------------------------------------------------------


def test_unknown_tool_dispatches_to_not_found_result():
    """If the model emits a tool_use for a name not in the registry,
    the executor returns a ``is_error=True`` result block with a
    ``"not found"`` message. The model can then react in its next
    turn."""

    turn1 = (
        _msg()
        + _tool_use_block(0, "c1", "no_such_tool", '{"x": 1}')
        + _stop("tool_use")
    )
    turn2 = _msg() + _text_block(0, "ok") + _stop("end_turn")

    class _Two(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=_Two(),
            tools=[echo],  # NOT no_such_tool
            max_turns=5,
            include_memory=False,
        )
        try:
            seen: list = []
            async for m in agent.run_iter([UserMessage(content="go")]):
                seen.append(m)
        finally:
            await agent.aclose()

        result_msg = seen[1]
        assert isinstance(result_msg, InternalUserMessage)
        result = result_msg.content[0]
        assert result.is_error is True
        assert "not found" in result.content

    anyio.run(main)


# ---------------------------------------------------------------------------
# 8. PostToolUse hook still fires (after the executor completes).
# ---------------------------------------------------------------------------


def test_posttooluse_hook_fires_after_mid_stream_dispatched_tool_completes():
    """PostToolUse hook must still fire for mid-stream-dispatched tools
    once they complete — same contract as the pre-streaming path."""

    post_calls: list = []

    async def post(ctx: HookContext) -> HookResult:
        post_calls.append((ctx.tool.name, ctx.output))
        return HookResult()

    turn1 = (
        _msg() + _tool_use_block(0, "c1", "add", '{"a": 7, "b": 8}') + _stop("tool_use")
    )
    turn2 = _msg() + _text_block(0, "done") + _stop("end_turn")

    class _Two(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=_Two(),
            tools=[add],
            max_turns=5,
            include_memory=False,
            hooks=Hooks(post_tool_use=post),
        )
        try:
            msgs = await agent.run([UserMessage(content="go")])
        finally:
            await agent.aclose()

        assert post_calls == [("add", "15")]
        result_msg = msgs[2]
        assert result_msg.content[0].content == "15"

    anyio.run(main)


# ---------------------------------------------------------------------------
# 9. Result ordering is preserved when tools complete out-of-order.
# ---------------------------------------------------------------------------


def test_results_preserve_stream_order_even_when_tools_finish_out_of_order():
    """Tool A is slow, tool B is fast. The stream emits A then B. The
    result list (and the trailing UserMessage's content) must still be
    [A, B] — never reordered by completion time."""

    a_done = anyio.Event()

    @tool
    async def slow(value: int) -> str:
        """Wait until B has finished, then return."""
        await a_done.wait()
        return f"slow:{value}"

    @tool
    async def fast(value: int) -> str:
        """Mark A free to finish, then return."""
        a_done.set()
        return f"fast:{value}"

    turn1 = (
        _msg()
        + _tool_use_block(0, "ca", "slow", '{"value": 1}')
        + _tool_use_block(1, "cb", "fast", '{"value": 2}')
        + _stop("tool_use")
    )
    turn2 = _msg() + _text_block(0, "ok") + _stop("end_turn")

    class _Two(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=_Two(),
            tools=[slow, fast],
            max_turns=5,
            include_memory=False,
        )
        try:
            msgs = await agent.run([UserMessage(content="go")])
        finally:
            await agent.aclose()

        result_msg = msgs[2]
        ids = [r.tool_use_id for r in result_msg.content]
        assert ids == ["ca", "cb"], f"expected [ca, cb] in stream order, got {ids}"
        contents = [r.content for r in result_msg.content]
        assert contents == ["slow:1", "fast:2"]

    anyio.run(main)
