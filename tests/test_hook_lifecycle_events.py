"""Lifecycle hook events that used to be declared but never fired.

``hooks.py`` ships a 27-event vocabulary and a ``DISPATCHED_EVENTS`` set naming
the ones the runtime genuinely emits. The value of that set is entirely in its
honesty, so this file tests it as an executable claim rather than as
documentation: every event listed there must be provably fired by real runtime
code, and every event NOT listed must still report ``is_dispatched() == False``.

Covered here (the non-blocking additions):

* ``PostCompact`` — ``mantis_agent/compact.py``
* ``SessionStart`` / ``SessionEnd`` — ``mantis_agent/session.py``

``Stop`` / ``PreToolUse`` / ... are covered by the pre-existing agent-loop
tests; this file only re-asserts that the *newly wired* ones fire exactly once
with a populated context.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent.compact import SimpleCompactor, run_manual_compaction
from mantis_agent.hooks import (
    DISPATCHED_EVENTS,
    HOOK_EVENTS,
    RESERVED_EVENTS,
    HookContext,
    HookDispatcher,
    HookResult,
    Hooks,
)
from mantis_agent.session import (
    InMemorySessionStore,
    Session,
    resume_session,
)
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """Collects every context a hook was handed, so a test can assert both
    *that* an event fired and *what* it carried."""

    def __init__(self) -> None:
        self.calls: list[HookContext] = []

    async def __call__(self, ctx: HookContext) -> HookResult | None:
        self.calls.append(ctx)
        return None

    @property
    def n(self) -> int:
        return len(self.calls)

    def only(self) -> HookContext:
        assert self.n == 1, f"expected exactly one dispatch, got {self.n}"
        return self.calls[0]


def _long_history(n: int = 12) -> list[Any]:
    out: list[Any] = []
    for i in range(n):
        out.append(UserMessage(content=f"msg {i}"))
        out.append(AssistantMessage(content=[TextBlock(text=f"reply {i}")]))
    return out


async def _summ(_prompt: str) -> str:
    return "Summary of prior conversation: the earlier turns."


# ---------------------------------------------------------------------------
# PostCompact — compact.py
# ---------------------------------------------------------------------------


def test_post_compact_fires_once_with_the_new_message_list() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(post_compact=rec))
    comp = SimpleCompactor(_summ, keep_recent_turns=4, dispatcher=disp)
    original = _long_history()

    out = anyio.run(lambda: comp.compact(list(original)))

    assert out is not original
    assert len(out) < len(original)
    ctx = rec.only()
    assert ctx.event == "PostCompact"
    # The snapshot must be the POST-compaction list — that is the entire point
    # of the event, and the easiest thing to get wrong is handing back the
    # pre-compaction list the caller already had.
    assert ctx.messages_snapshot is out
    assert ctx.arbitrary["before_count"] == len(original)
    assert ctx.arbitrary["after_count"] == len(out)
    assert ctx.arbitrary["trigger"] == "auto"


def test_post_compact_does_not_fire_when_nothing_was_compacted() -> None:
    """A no-op compaction must not announce a compaction that did not happen."""

    rec = _Recorder()
    disp = HookDispatcher(Hooks(post_compact=rec))
    comp = SimpleCompactor(_summ, keep_recent_turns=8, dispatcher=disp)
    short = [UserMessage(content="hi")]

    out = anyio.run(lambda: comp.compact(short))

    assert out is short  # unchanged, by identity
    assert rec.n == 0


def test_post_compact_does_not_fire_when_the_summarizer_fails() -> None:
    async def boom(_prompt: str) -> str:
        raise RuntimeError("summarizer down")

    rec = _Recorder()
    disp = HookDispatcher(Hooks(post_compact=rec))
    comp = SimpleCompactor(boom, keep_recent_turns=4, dispatcher=disp)
    history = _long_history()

    out = anyio.run(lambda: comp.compact(history))

    assert out is history
    assert rec.n == 0


def test_post_compact_does_not_fire_on_an_empty_summary() -> None:
    async def blank(_prompt: str) -> str:
        return "   "

    rec = _Recorder()
    disp = HookDispatcher(Hooks(post_compact=rec))
    comp = SimpleCompactor(blank, keep_recent_turns=4, dispatcher=disp)
    history = _long_history()

    assert anyio.run(lambda: comp.compact(history)) is history
    assert rec.n == 0


def test_manual_compaction_fires_post_compact_tagged_manual() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(post_compact=rec))
    history = _long_history()

    out, note = anyio.run(
        lambda: run_manual_compaction(history, _summ, dispatcher=disp)
    )

    assert "compacted" in note
    ctx = rec.only()
    assert ctx.arbitrary["trigger"] == "manual"
    assert ctx.messages_snapshot is out


def test_compactor_without_a_dispatcher_is_unchanged() -> None:
    """The dispatcher is opt-in; the default construction must not change."""

    comp = SimpleCompactor(_summ, keep_recent_turns=4)
    history = _long_history()
    out = anyio.run(lambda: comp.compact(history))
    assert out is not history
    assert len(out) < len(history)


def test_post_compact_hook_exception_does_not_break_compaction() -> None:
    """PostCompact is observability — a raising hook is swallowed and the
    compacted list is still returned."""

    async def boom(_ctx: HookContext) -> HookResult:
        raise ValueError("bad dashboard hook")

    disp = HookDispatcher(Hooks(post_compact=boom))
    comp = SimpleCompactor(_summ, keep_recent_turns=4, dispatcher=disp)
    history = _long_history()

    out = anyio.run(lambda: comp.compact(history))

    assert out is not history
    assert len(out) < len(history)


# ---------------------------------------------------------------------------
# SessionStart / SessionEnd — session.py
# ---------------------------------------------------------------------------


def test_session_create_fires_session_start_once() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_start=rec))

    async def go() -> Session:
        return await Session.create(
            InMemorySessionStore(), "s1", title="t", dispatcher=disp
        )

    sess = anyio.run(go)
    ctx = rec.only()
    assert ctx.event == "SessionStart"
    assert ctx.arbitrary["session_id"] == sess.id == "s1"
    assert ctx.arbitrary["reason"] == "create"
    assert ctx.arbitrary["message_count"] == 0


def test_session_load_fires_session_start_with_reason_resume() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_start=rec))
    store = InMemorySessionStore()

    async def go() -> None:
        seed = await Session.create(store, "s2")  # no dispatcher: silent
        seed.append(UserMessage(content="hello"))
        await seed.save()
        await Session.load(store, "s2", dispatcher=disp)

    anyio.run(go)
    ctx = rec.only()
    assert ctx.arbitrary["reason"] == "resume"
    assert ctx.arbitrary["message_count"] == 1
    assert ctx.messages_snapshot is not None
    assert len(ctx.messages_snapshot) == 1


def test_resume_session_helper_fires_session_start_once() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_start=rec))
    store = InMemorySessionStore()

    async def go() -> None:
        seed = await Session.create(store, "s3")
        seed.extend([UserMessage(content="a"), UserMessage(content="b")])
        await seed.save()
        await resume_session(store, "s3", 1, dispatcher=disp)

    anyio.run(go)
    # Exactly one: the load. Truncating an already-started session is not a
    # second start.
    assert rec.only().arbitrary["reason"] == "resume"


def test_fork_fires_session_start_for_the_child() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_start=rec))
    store = InMemorySessionStore()

    async def go() -> tuple[Session, Session]:
        parent = await Session.create(store, "p1", dispatcher=disp)
        parent.extend([UserMessage(content="a"), UserMessage(content="b")])
        await parent.save()
        return parent, await parent.fork("c1")

    parent, child = anyio.run(go)

    assert [c.arbitrary["reason"] for c in rec.calls] == ["create", "fork"]
    assert rec.calls[1].arbitrary["session_id"] == child.id == "c1"
    assert rec.calls[1].arbitrary["message_count"] == 2


def test_truncated_fork_fires_session_start_for_the_child() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_start=rec))
    store = InMemorySessionStore()

    async def go() -> Session:
        parent = await Session.create(store, "p2", dispatcher=disp)
        parent.extend([UserMessage(content="a"), UserMessage(content="b")])
        await parent.save()
        return await parent.fork("c2", checkpoint=1)

    child = anyio.run(go)
    assert rec.calls[-1].arbitrary["reason"] == "fork"
    assert rec.calls[-1].arbitrary["session_id"] == child.id
    assert rec.calls[-1].arbitrary["message_count"] == 1


def test_session_close_fires_session_end_once_and_is_idempotent() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_end=rec))

    async def go() -> None:
        sess = await Session.create(InMemorySessionStore(), "s4", dispatcher=disp)
        sess.append(UserMessage(content="hi"))
        await sess.close()
        await sess.close()  # second close must be a no-op
        await sess.close()

    anyio.run(go)
    ctx = rec.only()
    assert ctx.event == "SessionEnd"
    assert ctx.arbitrary["reason"] == "closed"
    assert ctx.arbitrary["message_count"] == 1


def test_async_with_fires_session_end_on_clean_exit() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_end=rec))

    async def go() -> None:
        async with await Session.create(
            InMemorySessionStore(), "s5", dispatcher=disp
        ) as sess:
            assert sess.id == "s5"

    anyio.run(go)
    assert rec.only().arbitrary["reason"] == "closed"


def test_async_with_fires_session_end_on_error() -> None:
    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_end=rec))

    async def go() -> None:
        async with await Session.create(
            InMemorySessionStore(), "s6", dispatcher=disp
        ):
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        anyio.run(go)

    ctx = rec.only()
    assert ctx.arbitrary["reason"] == "error"
    assert ctx.arbitrary["error"] == "boom"
    assert ctx.arbitrary["error_type"] == "ValueError"


def test_session_end_fires_on_the_cancellation_path() -> None:
    """The one failure mode a cleanup hook cannot tolerate: the session is torn
    down because the surrounding task was cancelled, and the event is lost."""

    rec = _Recorder()
    disp = HookDispatcher(Hooks(session_end=rec))
    entered = anyio.Event()

    async def body() -> None:
        async with await Session.create(
            InMemorySessionStore(), "s7", dispatcher=disp
        ):
            entered.set()
            await anyio.sleep_forever()

    async def go() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(body)
            await entered.wait()
            tg.cancel_scope.cancel()

    anyio.run(go)

    ctx = rec.only()
    assert ctx.event == "SessionEnd"
    assert ctx.arbitrary["reason"] == "cancelled"
    # A cancellation is not an error — nothing failed, the operator stopped it.
    assert "error" not in ctx.arbitrary


def test_session_end_hook_still_runs_when_it_awaits_under_cancellation() -> None:
    """The shielded dispatch must let a hook that itself awaits finish, rather
    than being cut short by the cancellation that triggered the teardown."""

    finished: list[str] = []

    async def slow(_ctx: HookContext) -> None:
        await anyio.sleep(0.01)
        finished.append("done")

    disp = HookDispatcher(Hooks(session_end=slow))
    entered = anyio.Event()

    async def body() -> None:
        async with await Session.create(
            InMemorySessionStore(), "s8", dispatcher=disp
        ):
            entered.set()
            await anyio.sleep_forever()

    async def go() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(body)
            await entered.wait()
            tg.cancel_scope.cancel()

    anyio.run(go)
    assert finished == ["done"]


def test_session_without_a_dispatcher_fires_nothing() -> None:
    """Opt-in: the historical constructor signature keeps working untouched."""

    async def go() -> int:
        store = InMemorySessionStore()
        sess = await Session.create(store, "s9")
        sess.append(UserMessage(content="x"))
        await sess.save()
        loaded = await Session.load(store, "s9")
        await loaded.close()
        async with loaded:
            pass
        return len(loaded.messages)

    assert anyio.run(go) == 1


# ---------------------------------------------------------------------------
# StopFailure plumbing — hooks.py
# ---------------------------------------------------------------------------


def test_dispatch_run_end_routes_to_stop_on_success() -> None:
    stop = _Recorder()
    fail = _Recorder()
    disp = HookDispatcher(Hooks(stop=stop, stop_failure=fail))
    msgs = [UserMessage(content="hi")]

    anyio.run(lambda: disp.dispatch_run_end(msgs))

    assert fail.n == 0
    ctx = stop.only()
    assert ctx.event == "Stop"
    assert ctx.messages_snapshot is msgs


def test_dispatch_run_end_routes_to_stop_failure_on_error() -> None:
    stop = _Recorder()
    fail = _Recorder()
    disp = HookDispatcher(Hooks(stop=stop, stop_failure=fail))
    msgs = [UserMessage(content="hi")]

    anyio.run(
        lambda: disp.dispatch_run_end(
            msgs, error=RuntimeError("provider exploded"), agent_id="a1"
        )
    )

    assert stop.n == 0
    ctx = fail.only()
    assert ctx.event == "StopFailure"
    assert ctx.agent_id == "a1"
    assert ctx.arbitrary["error"] == "provider exploded"
    assert ctx.arbitrary["error_type"] == "RuntimeError"
    assert ctx.messages_snapshot is msgs


def test_dispatch_run_end_lets_the_caller_override_the_error_text() -> None:
    fail = _Recorder()
    disp = HookDispatcher(Hooks(stop_failure=fail))

    anyio.run(
        lambda: disp.dispatch_run_end(
            None,
            error=RuntimeError("token=sk-secret"),
            arbitrary={"error": "[redacted]"},
        )
    )

    ctx = fail.only()
    assert ctx.arbitrary["error"] == "[redacted]"
    assert ctx.arbitrary["error_type"] == "RuntimeError"


def test_dispatch_run_end_is_a_noop_without_hooks() -> None:
    disp = HookDispatcher(Hooks())
    res = anyio.run(lambda: disp.dispatch_run_end([], error=RuntimeError("x")))
    assert isinstance(res, HookResult)
    assert res.block is False


def test_stop_failure_hook_exception_never_escapes() -> None:
    """StopFailure is not a blocking event: a raising hook must not turn one
    failure into two, even with fail_closed on."""

    async def boom(_ctx: HookContext) -> HookResult:
        raise ValueError("hook bug")

    disp = HookDispatcher(Hooks(stop_failure=boom), fail_closed=True)
    res = anyio.run(lambda: disp.dispatch_run_end([], error=RuntimeError("x")))
    assert res.block is False


# ---------------------------------------------------------------------------
# The honesty of DISPATCHED_EVENTS
# ---------------------------------------------------------------------------


def test_reserved_is_the_exact_complement_of_dispatched() -> None:
    assert RESERVED_EVENTS == frozenset(HOOK_EVENTS) - DISPATCHED_EVENTS
    assert DISPATCHED_EVENTS <= frozenset(HOOK_EVENTS)
    assert not (RESERVED_EVENTS & DISPATCHED_EVENTS)


def test_is_dispatched_agrees_with_the_sets() -> None:
    for ev in DISPATCHED_EVENTS:
        assert HookDispatcher.is_dispatched(ev), ev
    for ev in RESERVED_EVENTS:
        assert not HookDispatcher.is_dispatched(ev), ev
    assert not HookDispatcher.is_dispatched("NotAnEvent")


def test_newly_wired_events_are_declared_dispatched() -> None:
    for ev in ("PostCompact", "SessionStart", "SessionEnd"):
        assert ev in DISPATCHED_EVENTS, ev


def test_stop_failure_is_still_honestly_reserved() -> None:
    """``dispatch_run_end`` can fire StopFailure, but no built-in runtime path
    calls it yet — agent.py owns the ``Stop`` call sites. Until that wiring
    lands the set must keep saying so; flipping this assertion is the reminder
    to move the event into DISPATCHED_EVENTS at the same time."""

    assert "StopFailure" in RESERVED_EVENTS


@pytest.mark.parametrize(
    "event",
    ["PostCompact", "SessionStart", "SessionEnd"],
)
def test_every_newly_dispatched_event_actually_fires(event: str) -> None:
    """Executable form of the DISPATCHED_EVENTS claim: drive the real runtime
    code and assert the event arrives with a populated context."""

    rec = _Recorder()
    field = {
        "PostCompact": "post_compact",
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
    }[event]
    disp = HookDispatcher(Hooks(**{field: rec}))

    async def go() -> None:
        if event == "PostCompact":
            comp = SimpleCompactor(_summ, keep_recent_turns=4, dispatcher=disp)
            await comp.compact(_long_history())
        else:
            async with await Session.create(
                InMemorySessionStore(), "sx", dispatcher=disp
            ):
                pass

    anyio.run(go)

    ctx = rec.only()
    assert ctx.event == event
    assert ctx.messages_snapshot is not None
    assert ctx.arbitrary  # never an empty bag — every event carries context
