"""``run_iter`` must be finalized in the task that consumed it.

``Agent.run_iter`` is an async generator that deliberately holds the streaming
tool executor's anyio task group open ACROSS its yields — that's what lets a
consumer render "tool running…" while the tools drain. The consequence is that
the generator must never be left for the event loop to clean up: abandoning it
(``break``, an exception, or Esc cancelling the consuming task) hands teardown
to the asyncgen shutdown hook, which runs in a DIFFERENT task, and anyio
answers that with

    RuntimeError: Attempted to exit cancel scope in a different task
                  than it was entered in

raised straight into the event loop — which killed the session, not just the
turn. These pin the contract with the same structure the real loop has.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import anyio
import pytest

from mantis_agent.agent import aclose_stream

# The real executor's unwrapping, not a copy of it — that behaviour is part of
# what makes closing clean, so the model here has to use the same code.
from mantis_agent.streaming.executor import _flatten_exception_group as _flatten

try:
    BaseExceptionGroup
except NameError:  # pragma: no cover - Python 3.10 only
    from exceptiongroup import BaseExceptionGroup


class _Executor:
    """The shape that matters: a task group entered/exited manually, with the
    singleton-exception-group unwrapping StreamingToolExecutor does."""

    async def __aenter__(self):
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        return self

    async def __aexit__(self, et, e, tb):
        try:
            await self._tg.__aexit__(et, e, tb)
        except BaseExceptionGroup as eg:
            flat = _flatten(eg)
            if len(flat) == 1:
                raise flat[0] from None
            raise


async def _slow_tool() -> None:
    await anyio.sleep(30)


async def _run_iter():
    """Mirrors Agent.run_iter: tools in flight, yields inside the group."""
    async with _Executor() as ex:
        ex._tg.start_soon(_slow_tool)
        yield "assistant"          # yielded BEFORE tools finish, by design
        yield "final"


# -- the helper --------------------------------------------------------------


def test_closing_the_stream_in_the_consuming_task_is_clean() -> None:
    async def main() -> None:
        stream = _run_iter()
        try:
            async for _ in stream:
                break              # abandon after the first message
        finally:
            await aclose_stream(stream)

    asyncio.run(main())            # no cancel-scope RuntimeError


def test_closing_survives_cancellation_of_the_consumer() -> None:
    """The common trigger IS cancellation — Esc during a turn with tools in
    flight. An unshielded close in an already-cancelled task would re-raise
    before the cleanup ran."""
    async def consume() -> None:
        stream = _run_iter()
        try:
            async for _ in stream:
                await anyio.sleep(5)      # parked when the cancel lands
        finally:
            await aclose_stream(stream)

    async def main() -> None:
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_a_consumer_exception_is_not_masked_by_teardown() -> None:
    """The real error has to survive; a stream close must not replace it with
    an exception group or swallow it."""
    async def main() -> None:
        stream = _run_iter()
        try:
            async for _ in stream:
                raise ValueError("consumer blew up")
        finally:
            await aclose_stream(stream)

    with pytest.raises(ValueError, match="consumer blew up"):
        asyncio.run(main())


def test_closing_is_idempotent_and_tolerates_junk() -> None:
    async def main() -> None:
        stream = _run_iter()
        async for _ in stream:
            break
        await aclose_stream(stream)
        await aclose_stream(stream)      # twice is fine
        await aclose_stream(None)        # never assigned
        await aclose_stream(object())    # no aclose attribute

    asyncio.run(main())


def test_a_fully_drained_stream_closes_cleanly() -> None:
    """The ordinary path — nothing abandoned — must stay unaffected."""
    async def _quick():
        async with _Executor():
            yield 1
            yield 2

    async def main() -> None:
        stream = _quick()
        seen = [v async for v in stream]
        await aclose_stream(stream)
        assert seen == [1, 2]

    asyncio.run(main())


# -- the bug this prevents ---------------------------------------------------


def test_abandoning_without_closing_is_what_broke_the_loop() -> None:
    """Documents the failure mode. Without an explicit close the generator is
    finalized by the loop's asyncgen hook in another task; anyio raises into
    the event loop rather than into anyone's ``except``, so this asserts on the
    loop's exception handler instead of ``pytest.raises``."""
    seen: list[str] = []

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, ctx: seen.append(str(ctx.get("exception") or ctx.get("message"))))
        stream = _run_iter()
        async for _ in stream:
            break                  # abandoned, never closed
        del stream

    asyncio.run(main())
    assert any("different task" in s for s in seen), seen
