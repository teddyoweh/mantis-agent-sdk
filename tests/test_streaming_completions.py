"""Completion-streaming API on ``StreamingToolExecutor``.

The executor has always streamed tool DISPATCH (calls fire mid-stream
as their JSON closes), but until the 1.0 rewrite of the streaming tool
dispatch path it BATCHED results — callers had to ``await wait_all()``
to see anything. That blocks the fast tool's result behind the slowest
sibling, which is fine for the agent's internal turn but terrible for
live UIs ("show me each tool finishing the moment it does").

These tests pin the new public surface:

  * ``iter_completions()`` yields ``(idx, ToolResultBlock)`` pairs in
    **completion** order, with ``idx`` being the original
    ``add_tool_call`` insertion ordinal so callers can correlate.
  * ``wait_one()`` is the single-shot variant — returns the next
    completion or ``None`` if the executor has closed.
  * Completion-order != insertion-order: a fast tool added second
    completes before a slow tool added first.
  * Every result path emits a completion — happy path, exceptions,
    timeouts, permission denials, missing tools, signal-cancellation,
    sibling-aborts, exit-time backfills.
  * Closing the executor (``async with`` exit) terminates
    ``iter_completions`` cleanly via EndOfStream.
  * ``calls`` and ``pending`` properties give consumers what they need
    to render progress (how many tools, how many still running).
  * ``wait_all()`` continues to work in lockstep with completion
    streaming — both observe the same results.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import Tool, ToolUseBlock, tool
from mantis_agent.permissions import PermissionContext
from mantis_agent.streaming.executor import StreamingToolExecutor
from mantis_agent.tools import ToolRegistry
from mantis_agent.types import ToolResultBlock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Gated:
    """Tool body parked on an anyio Event so we can deterministically order
    completions and observe in-flight state. ``release`` opens the gate."""

    def __init__(self, name: str):
        self.name = name
        self.release = anyio.Event()
        self.entered = anyio.Event()

    def as_tool(self) -> Tool:
        outer = self

        async def _body(**kw) -> str:
            outer.entered.set()
            await outer.release.wait()
            return f"{outer.name}:done"

        return Tool(
            name=outer.name,
            description=f"{outer.name} gated tool",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
            fn=_body,
        )


def _block(call_id: str, name: str, **input_) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=input_)


# ---------------------------------------------------------------------------
# 1. Completion order ≠ insertion order
# ---------------------------------------------------------------------------


def test_iter_completions_yields_in_completion_order_not_insertion_order():
    """Three tools added in order [slow, fast, medium]. iter_completions
    must yield fast first, medium second, slow last — with the original
    insertion indices preserved.
    """

    slow = _Gated("slow")
    fast = _Gated("fast")
    medium = _Gated("medium")
    registry = ToolRegistry()
    for g in (slow, fast, medium):
        registry.add(g.as_tool())

    async def main() -> list[tuple[int, str, str]]:
        seen: list[tuple[int, str, str]] = []
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("a", "slow"))
            ex.add_tool_call(_block("b", "fast"))
            ex.add_tool_call(_block("c", "medium"))

            async def consumer():
                async for idx, result in ex.iter_completions():
                    seen.append((idx, result.tool_use_id, result.content))
                    if len(seen) == 3:
                        return

            async with anyio.create_task_group() as tg:
                tg.start_soon(consumer)
                # All three tools entered their bodies before we release.
                with anyio.fail_after(2.0):
                    await slow.entered.wait()
                    await fast.entered.wait()
                    await medium.entered.wait()
                # Release in fast, medium, slow order.
                fast.release.set()
                await anyio.sleep(0.05)
                medium.release.set()
                await anyio.sleep(0.05)
                slow.release.set()
                # Wait for the executor to drain so iter_completions
                # gets the last item before we exit the with block.
                await ex.wait_all()
                # Consumer breaks after 3rd completion — task group exits.
        return seen

    seen = anyio.run(main)
    # 3 completions, fast → medium → slow.
    assert [tup[2] for tup in seen] == ["fast:done", "medium:done", "slow:done"]
    # Insertion indices: fast was 1, medium was 2, slow was 0.
    assert [tup[0] for tup in seen] == [1, 2, 0]
    # Correlate tool_use_id back to the original block.
    assert [tup[1] for tup in seen] == ["b", "c", "a"]


# ---------------------------------------------------------------------------
# 2. iter_completions terminates when the executor exits
# ---------------------------------------------------------------------------


def test_iter_completions_terminates_when_executor_exits():
    """Consumer iterating completions sees EndOfStream and the loop
    exits cleanly when the executor leaves its async-with block."""

    @tool
    async def quick(x: int) -> str:
        return f"out={x}"

    registry = ToolRegistry()
    registry.add(quick)

    async def main() -> list[tuple[int, str]]:
        seen: list[tuple[int, str]] = []

        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("c1", "quick", x=1))
            ex.add_tool_call(_block("c2", "quick", x=2))
            # Block-consume both, then expect the iterator to terminate
            # when the executor exits.
            async for idx, r in ex.iter_completions():
                seen.append((idx, r.content))
                if len(seen) == 2:
                    break

        # After exit, iter_completions on the same executor should
        # terminate immediately (channel closed in __aexit__).
        post_exit: list[tuple[int, ToolResultBlock]] = []
        async for item in ex.iter_completions():
            post_exit.append(item)
        return seen, post_exit  # type: ignore[return-value]

    seen, post_exit = anyio.run(main)  # type: ignore[misc]
    assert sorted(seen) == [(0, "out=1"), (1, "out=2")]
    assert post_exit == []


# ---------------------------------------------------------------------------
# 3. wait_one returns next completion, then None after close
# ---------------------------------------------------------------------------


def test_wait_one_returns_next_completion_then_none():
    """``wait_one`` is the single-shot variant. Repeated calls drain
    completions one at a time; once the executor closes with nothing
    left, the next ``wait_one`` returns ``None``."""

    @tool
    async def echo(value: str) -> str:
        return f"echo:{value}"

    registry = ToolRegistry()
    registry.add(echo)

    async def main() -> tuple[list[tuple[int, str]], object]:
        collected: list[tuple[int, str]] = []
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("c1", "echo", value="a"))
            ex.add_tool_call(_block("c2", "echo", value="b"))
            for _ in range(2):
                item = await ex.wait_one()
                assert item is not None
                idx, result = item
                collected.append((idx, result.content))
            # Drain inside, executor closes, the next wait_one returns None.
            # We capture this AFTER exit below.
        # After exit the channel is closed; wait_one returns None.
        post = await ex.wait_one()
        return collected, post

    collected, post = anyio.run(main)
    assert sorted(collected) == [(0, "echo:a"), (1, "echo:b")]
    assert post is None


# ---------------------------------------------------------------------------
# 4. Every result path emits a completion
# ---------------------------------------------------------------------------


def test_completion_emitted_for_unknown_tool():
    """Missing tools short-circuit but still emit a completion event so
    UI consumers don't get stuck waiting on a tool that never ran."""

    registry = ToolRegistry()  # no tools registered

    async def main():
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("ghost", "missing"))
            item = await ex.wait_one()
            assert item is not None
            idx, result = item
            assert idx == 0
            assert result.tool_use_id == "ghost"
            assert result.is_error
            assert "does not exist" in result.content

    anyio.run(main)


def test_completion_emitted_for_tool_exception():
    """A user tool that raises produces an is_error completion event."""

    @tool
    async def boom() -> str:
        raise ValueError("nope")

    registry = ToolRegistry()
    registry.add(boom)

    async def main():
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("c1", "boom"))
            item = await ex.wait_one()
            assert item is not None
            idx, result = item
            assert idx == 0
            assert result.is_error
            assert "nope" in result.content or "ValueError" in result.content

    anyio.run(main)


def test_completion_emitted_for_permission_denial():
    """Denials via can_use_tool emit a completion event the same as
    happy-path completions — UI shouldn't have to special-case them."""

    @tool
    async def writefile(path: str) -> str:
        return f"wrote {path}"

    registry = ToolRegistry()
    registry.add(writefile)

    async def deny(_tool, _input, _ctx):
        return False, "danger zone"

    async def main():
        async with StreamingToolExecutor(registry, can_use_tool=deny) as ex:
            ex.add_tool_call(_block("c1", "writefile", path="/etc/shadow"))
            item = await ex.wait_one()
            assert item is not None
            idx, result = item
            assert idx == 0
            assert result.is_error
            assert "permission denied" in result.content
            assert "danger zone" in result.content

    anyio.run(main)


def test_completion_emitted_for_signal_cancellation():
    """When ``cancellation_signal`` fires while a tool is in flight, the
    cancellation produces a ``cancelled by signal`` completion event."""

    gated = _Gated("slow")
    registry = ToolRegistry()
    registry.add(gated.as_tool())
    signal = anyio.Event()

    async def main():
        seen: list[tuple[int, str]] = []
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            ex.add_tool_call(_block("c1", "slow"))
            with anyio.fail_after(2.0):
                await gated.entered.wait()
            signal.set()
            async for idx, r in ex.iter_completions():
                seen.append((idx, r.content))
                break
        return seen

    seen = anyio.run(main)
    assert seen == [(0, "cancelled by signal")]


def test_completion_emitted_for_pre_signal_short_circuit():
    """When the signal is already set BEFORE add_tool_call, the fast-fail
    path still emits a completion event so iter_completions sees it."""

    gated = _Gated("slow")
    registry = ToolRegistry()
    registry.add(gated.as_tool())
    signal = anyio.Event()
    signal.set()

    async def main():
        seen: list[tuple[int, str]] = []
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            ex.add_tool_call(_block("c1", "slow"))
            ex.add_tool_call(_block("c2", "slow"))
            async for idx, r in ex.iter_completions():
                seen.append((idx, r.content))
                if len(seen) == 2:
                    break
        return seen

    seen = anyio.run(main)
    assert sorted(seen) == [(0, "cancelled by signal"), (1, "cancelled by signal")]


# ---------------------------------------------------------------------------
# 5. wait_all + iter_completions observe identical results
# ---------------------------------------------------------------------------


def test_wait_all_and_iter_completions_agree_on_results():
    """The two surfaces must report the same outcomes. wait_all is in
    insertion order; iter_completions is in completion order; collected
    + sorted by idx they must match exactly."""

    fast = _Gated("fast")
    slow = _Gated("slow")
    registry = ToolRegistry()
    registry.add(fast.as_tool())
    registry.add(slow.as_tool())

    async def main():
        completions: list[tuple[int, ToolResultBlock]] = []
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("c1", "slow"))
            ex.add_tool_call(_block("c2", "fast"))

            async def consume():
                async for item in ex.iter_completions():
                    completions.append(item)
                    if len(completions) == 2:
                        return

            async with anyio.create_task_group() as tg:
                tg.start_soon(consume)
                with anyio.fail_after(2.0):
                    await fast.entered.wait()
                    await slow.entered.wait()
                fast.release.set()
                # Let fast finish before slow.
                await anyio.sleep(0.05)
                slow.release.set()
                wait_all_results = await ex.wait_all()
                # Consumer hits len==2 and exits — task group joins cleanly.
        return completions, wait_all_results

    completions, wait_all_results = anyio.run(main)
    # Completion order: fast (idx=1) then slow (idx=0).
    assert [idx for idx, _ in completions] == [1, 0]
    # By-index reconciliation matches wait_all order.
    by_idx = {idx: r for idx, r in completions}
    for i, r in enumerate(wait_all_results):
        assert by_idx[i].tool_use_id == r.tool_use_id
        assert by_idx[i].content == r.content


# ---------------------------------------------------------------------------
# 6. Sibling-abort cascades emit completions for every tool
# ---------------------------------------------------------------------------


def test_sibling_abort_emits_completion_for_each_killed_tool():
    """When one tool errors and has abort_siblings_on_error, every
    in-flight peer is cancelled and emits its own completion event.
    iter_completions must surface all of them, not just the trigger."""

    raised = anyio.Event()

    async def _bomb(**_):
        raised.set()
        raise RuntimeError("blew up")

    bomb = Tool(
        name="bomb",
        description="exits with error",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        fn=_bomb,
        abort_siblings_on_error=True,
    )

    peer_a = _Gated("peer_a")
    peer_b = _Gated("peer_b")
    registry = ToolRegistry()
    registry.add(bomb)
    registry.add(peer_a.as_tool())
    registry.add(peer_b.as_tool())

    async def main():
        seen: list[tuple[int, str, bool]] = []
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("p1", "peer_a"))
            ex.add_tool_call(_block("p2", "peer_b"))
            with anyio.fail_after(2.0):
                await peer_a.entered.wait()
                await peer_b.entered.wait()
            ex.add_tool_call(_block("kaboom", "bomb"))
            async for idx, r in ex.iter_completions():
                seen.append((idx, r.tool_use_id, bool(r.is_error)))
                if len(seen) == 3:
                    break
        return seen

    seen = anyio.run(main)
    # All three completions arrived (peers cancelled + bomb errored).
    ids = sorted(t[1] for t in seen)
    assert ids == ["kaboom", "p1", "p2"]
    # Every one is an error block.
    assert all(t[2] for t in seen)


# ---------------------------------------------------------------------------
# 7. ``calls`` and ``pending`` introspection
# ---------------------------------------------------------------------------


def test_calls_and_pending_properties_track_dispatch_state():
    """``calls`` snapshots every added block in insertion order;
    ``pending`` reflects in-flight count, dropping to 0 as tools finish."""

    gated = _Gated("g")
    registry = ToolRegistry()
    registry.add(gated.as_tool())

    async def main():
        async with StreamingToolExecutor(registry) as ex:
            assert ex.calls == ()
            assert ex.pending == 0
            ex.add_tool_call(_block("c1", "g"))
            ex.add_tool_call(_block("c2", "g"))
            assert len(ex.calls) == 2
            assert [c.id for c in ex.calls] == ["c1", "c2"]
            # Both bodies should have started (pending=2 once they reach
            # the await point).
            with anyio.fail_after(2.0):
                await gated.entered.wait()
            # Pending is async-state — could be 1 or 2 depending on
            # how anyio scheduled. We just check it's nonzero.
            assert ex.pending >= 1
            gated.release.set()
            await ex.wait_all()
            assert ex.pending == 0
            # calls list is immutable (tuple) — preserved post-drain.
            assert len(ex.calls) == 2

    anyio.run(main)


# ---------------------------------------------------------------------------
# 8. iter_completions during ongoing dispatch (interleaved)
# ---------------------------------------------------------------------------


def test_iter_completions_yields_as_dispatch_grows():
    """Realistic mid-stream flow: add a tool, observe its completion,
    add another tool, observe THAT completion. The iterator stays open
    across multiple add_tool_call cycles."""

    @tool
    async def quick(value: str) -> str:
        return f"r:{value}"

    registry = ToolRegistry()
    registry.add(quick)

    async def main():
        seen: list[tuple[int, str]] = []
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("c1", "quick", value="one"))
            first = await ex.wait_one()
            assert first is not None
            seen.append((first[0], first[1].content))

            ex.add_tool_call(_block("c2", "quick", value="two"))
            second = await ex.wait_one()
            assert second is not None
            seen.append((second[0], second[1].content))
        return seen

    seen = anyio.run(main)
    assert seen == [(0, "r:one"), (1, "r:two")]


# ---------------------------------------------------------------------------
# 9. iter_completions handles a tool that times out
# ---------------------------------------------------------------------------


def test_completion_emitted_for_tool_timeout():
    """A tool with ``timeout_s`` that exceeds its budget produces an
    is_error completion with a 'timed out' message."""

    async def _hang(**_):
        await anyio.sleep(10.0)
        return "never"

    hang = Tool(
        name="hang",
        description="too slow",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        fn=_hang,
        timeout_s=0.05,
    )
    registry = ToolRegistry()
    registry.add(hang)

    async def main():
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("c1", "hang"))
            item = await ex.wait_one()
            assert item is not None
            _, result = item
            assert result.is_error
            assert "timed out" in result.content

    anyio.run(main)


# ---------------------------------------------------------------------------
# 10. Cancellation context: in-flight signal triggers completion for ALL
# ---------------------------------------------------------------------------


def test_signal_mid_flight_emits_completions_for_every_in_flight_tool():
    """Three tools running, signal fires — iter_completions sees three
    cancellation events, one per tool, with the correct insertion
    indices."""

    g1 = _Gated("g1")
    g2 = _Gated("g2")
    g3 = _Gated("g3")
    registry = ToolRegistry()
    for g in (g1, g2, g3):
        registry.add(g.as_tool())
    signal = anyio.Event()

    async def main():
        seen: list[tuple[int, str]] = []
        async with StreamingToolExecutor(
            registry, cancellation_signal=signal
        ) as ex:
            ex.add_tool_call(_block("c1", "g1"))
            ex.add_tool_call(_block("c2", "g2"))
            ex.add_tool_call(_block("c3", "g3"))
            with anyio.fail_after(2.0):
                await g1.entered.wait()
                await g2.entered.wait()
                await g3.entered.wait()
            signal.set()
            async for idx, r in ex.iter_completions():
                seen.append((idx, r.content))
                if len(seen) == 3:
                    break
        return seen

    seen = anyio.run(main)
    assert {idx for idx, _ in seen} == {0, 1, 2}
    assert all(content == "cancelled by signal" for _, content in seen)


# ---------------------------------------------------------------------------
# 11. _record_result is idempotent — no double-emission on race backfills
# ---------------------------------------------------------------------------


def test_no_double_emission_for_a_single_call():
    """If the exit-time backfill races with a normal completion (or any
    similar race), the completion channel must NOT see the same idx
    twice. This is _record_result's idempotency guarantee."""

    @tool
    async def quick(x: int) -> str:
        return f"ok={x}"

    registry = ToolRegistry()
    registry.add(quick)

    async def main():
        seen_indices: list[int] = []
        async with StreamingToolExecutor(registry) as ex:
            ex.add_tool_call(_block("c1", "quick", x=1))
            ex.add_tool_call(_block("c2", "quick", x=2))
            await ex.wait_all()  # ensures both finished cleanly

        # Now iterate post-exit. Channel is closed in __aexit__ so
        # iter_completions drains the buffer (any items still queued)
        # and terminates on EndOfStream — no break needed.
        async for idx, _ in ex.iter_completions():
            seen_indices.append(idx)
        return seen_indices

    seen = anyio.run(main)
    assert sorted(seen) == [0, 1]
    # Critically: no duplicates.
    assert len(seen) == len(set(seen))


# ---------------------------------------------------------------------------
# 12. iter_completions on a fresh executor before any tools dispatched
# ---------------------------------------------------------------------------


def test_iter_completions_idle_executor_terminates_on_close():
    """No tools ever added — the iterator should still terminate cleanly
    when the executor's __aexit__ closes the completion channel. The
    consumer's ``async for`` exits without raising."""

    registry = ToolRegistry()

    async def main():
        seen: list = []

        # Build the executor; the test exits the async-with cleanly
        # immediately. After exit, iter_completions must terminate at
        # once because the channel was closed in __aexit__.
        async with StreamingToolExecutor(registry) as ex:
            pass  # never added a tool

        async for item in ex.iter_completions():
            seen.append(item)
        return seen

    seen = anyio.run(main)
    assert seen == []
