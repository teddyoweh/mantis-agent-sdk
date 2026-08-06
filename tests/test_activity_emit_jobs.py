"""Emission helpers, and the JobManager wiring built on them.

Two contracts are under test here, and the second one is the one that matters.

1. ``activity/emit.py`` is total: every helper accepts ``reg=None`` and returns,
   and no helper can propagate an exception out of the registry.
2. ``JobManager`` is *unchanged* by instrumentation. Without a registry it makes
   zero emission calls and behaves byte-for-byte as before; with a registry that
   raises on every single call, jobs still complete, still report their real
   status, and still deliver their results.
"""

from __future__ import annotations

import asyncio

import anyio
import pytest

from mantis_agent.activity import emit
from mantis_agent.activity.events import (
    NodeAction,
    NodeActivity,
    NodeCreated,
    NodeStatus,
    NodeUsage,
)
from mantis_agent.activity.registry import ActivityRegistry
from mantis_agent.jobs import JobManager
from mantis_agent.types import Usage


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


class Boom:
    """A registry that fails at everything, as hostilely as it can."""

    def __init__(self) -> None:
        self.calls = 0

    def apply(self, ev):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("the registry is on fire")


class Recorder:
    """Collects the events a registry fans out, in order."""

    def __init__(self) -> None:
        self.events = []

    def __call__(self, ev) -> None:  # noqa: ANN001
        self.events.append(ev)

    def kinds(self) -> list:
        return [type(ev).__name__ for ev in self.events]


def _reg() -> ActivityRegistry:
    return ActivityRegistry(session_id="ses:test")


# ---------------------------------------------------------------------------
# emit.py — the None guard
# ---------------------------------------------------------------------------


def test_every_helper_is_a_noop_without_a_registry() -> None:
    assert emit.node_created(None, "job:1", None, "task", "t") is None
    assert emit.node_status(None, "job:1", "running") is None
    assert emit.node_activity(None, "job:1", "hi") is None
    assert emit.node_usage(None, "job:1", Usage(input_tokens=5)) is None
    assert emit.node_action(None, "job:1", "stop", "user") is None


def test_every_helper_swallows_a_registry_that_raises() -> None:
    reg = Boom()
    emit.node_created(reg, "job:1", None, "task", "t")
    emit.node_status(reg, "job:1", "running")
    emit.node_activity(reg, "job:1", "hi")
    emit.node_usage(reg, "job:1", Usage(input_tokens=5))
    emit.node_action(reg, "job:1", "stop", "user")
    assert reg.calls == 5  # it was really called, and really raised, five times


def test_helpers_tolerate_an_object_that_is_not_a_registry_at_all() -> None:
    for reg in (object(), "not a registry", 7):
        emit.node_created(reg, "job:1", None, "task", "t")
        emit.node_status(reg, "job:1", "done")
        emit.node_activity(reg, "job:1", "hi")
        emit.node_usage(reg, "job:1", {"input_tokens": 1})
        emit.node_action(reg, "job:1", "stop", "user")


# ---------------------------------------------------------------------------
# emit.py — what lands in the tree
# ---------------------------------------------------------------------------


def test_node_created_carries_metadata_and_ignores_unknown_fields() -> None:
    reg = _reg()
    emit.node_created(
        reg,
        "sub:4",
        "job:1",
        "subagent",
        "dig deep",
        model="mock",
        isolation="worktree",
        actions=("stop",),
        detail="a detail",
        # Not a NodeCreated field. Dropping the field must not drop the node:
        # losing the node would lose every event that hangs off it.
        nonsense_field=object(),
    )
    node = reg.node("sub:4")
    assert node is not None
    assert (node.parent_id, node.kind, node.title) == ("job:1", "subagent", "dig deep")
    assert (node.model, node.isolation, node.detail) == ("mock", "worktree", "a detail")
    assert node.actions == frozenset({"stop"})


def test_node_status_records_error_text_and_terminal_state() -> None:
    reg = _reg()
    emit.node_created(reg, "job:1", None, "task", "t")
    emit.node_status(reg, "job:1", "error", "RuntimeError: kaput")
    node = reg.node("job:1")
    assert node.status == "error"
    assert node.error == "RuntimeError: kaput"


def test_node_activity_goes_through_registry_sanitization() -> None:
    reg = _reg()
    emit.node_created(reg, "job:1", None, "task", "t")
    emit.node_activity(reg, "job:1", "one\x1b[2Jtwo\nthree")
    assert reg.node("job:1").activity == "onetwo three"


def test_node_usage_reads_structs_mappings_and_objects() -> None:
    class Tracker:
        input_tokens = 3
        output_tokens = 4

    for source in (
        Usage(input_tokens=3, output_tokens=4),
        {"input_tokens": 3, "output_tokens": 4},
        Tracker(),
    ):
        reg = _reg()
        emit.node_created(reg, "job:1", None, "task", "t")
        emit.node_usage(reg, "job:1", source)
        usage = reg.node("job:1").usage
        assert (usage.input_tokens, usage.output_tokens) == (3, 4)


def test_node_usage_accumulates_and_drops_empty_deltas() -> None:
    reg = _reg()
    emit.node_created(reg, "job:1", None, "task", "t")
    emit.node_usage(reg, "job:1", Usage(input_tokens=2), cost_usd=0.5)
    emit.node_usage(reg, "job:1", Usage(input_tokens=3))
    before = reg.stats["applied"]
    emit.node_usage(reg, "job:1", Usage())          # all-zero: not worth a seq
    emit.node_usage(reg, "job:1", None)
    assert reg.stats["applied"] == before
    node = reg.node("job:1")
    assert node.usage.input_tokens == 5
    assert node.cost_usd == pytest.approx(0.5)


def test_node_usage_survives_garbage_field_values() -> None:
    reg = _reg()
    emit.node_created(reg, "job:1", None, "task", "t")
    emit.node_usage(reg, "job:1", {"input_tokens": "seventeen", "output_tokens": 2})
    assert reg.node("job:1").usage.output_tokens == 2


def test_node_action_is_recorded_without_changing_status() -> None:
    reg = _reg()
    seen = Recorder()
    reg.subscribe(seen)
    emit.node_created(reg, "job:1", None, "task", "t")
    emit.node_status(reg, "job:1", "running")
    emit.node_action(reg, "job:1", "stop", "user", "user pressed x")
    assert reg.node("job:1").status == "running"
    action = seen.events[-1]
    assert isinstance(action, NodeAction)
    assert (action.action, action.actor, action.detail) == (
        "stop", "user", "user pressed x",
    )


# ---------------------------------------------------------------------------
# JobManager — no registry means no emission at all
# ---------------------------------------------------------------------------


def test_job_manager_defaults_to_no_registry() -> None:
    jm = JobManager()
    assert jm.registry is None
    assert jm.activity_parent_id is None


def test_without_a_registry_not_one_emission_call_is_made(monkeypatch) -> None:
    calls: list = []
    for name in ("node_created", "node_status", "node_activity", "node_action",
                 "node_usage"):
        monkeypatch.setattr(
            emit, name, lambda *a, _n=name, **k: calls.append(_n)
        )

    async def work() -> str:
        return "ok"

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager()
        job = jm.spawn(work(), desc="compute")
        done = await jm.wait(job.id, timeout_s=5)
        assert done.status == "done" and done.result == "ok"
        hang = jm.spawn(forever(), desc="hangs")
        await jm.emit(hang, "tick")
        assert jm.cancel(hang.id)
        await jm.wait(hang.id, timeout_s=5)
        assert hang.status == "cancelled"

    anyio.run(go)
    assert calls == []


# ---------------------------------------------------------------------------
# JobManager — the four emission points
# ---------------------------------------------------------------------------


def test_spawn_creates_a_running_node() -> None:
    reg = _reg()

    async def work() -> str:
        return "ok"

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(work(), desc="compute the answer")
        node = reg.node("job:1")
        assert node is not None
        assert (node.kind, node.title, node.status) == (
            "task", "compute the answer", "running",
        )
        assert node.parent_id is None
        assert "stop" in node.actions
        assert node.started_at is not None
        await jm.wait(job.id, timeout_s=5)

    anyio.run(go)


def test_spawned_node_uses_the_job_kind_and_parent_of_the_moment() -> None:
    reg = _reg()

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(registry=reg)
        jm.activity_parent_id = "trn:7"
        jm.spawn(forever(), desc="poll the deploy", kind="watch")
        try:
            node = reg.node("job:1")
            assert node.kind == "watch"
            assert node.parent_id == "trn:7"
            # A monitor is the one kind with somewhere to open to.
            assert node.actions == frozenset({"stop", "open"})
        finally:
            jm.cancel_all()

    anyio.run(go)


def test_streamed_events_become_node_activity() -> None:
    reg = _reg()

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(registry=reg)  # deliberately no on_stream callback
        job = jm.spawn(forever(), desc="watch", kind="watch")
        await jm.emit(job, "3 new lines")
        await jm.emit(job, "1 new line")
        try:
            node = reg.node("job:1")
            assert node.activity == "1 new line"
            assert node.recent == ("3 new lines", "1 new line")
            assert job.stream_count == 2  # the engine's own bookkeeping is intact
        finally:
            jm.cancel_all()

    anyio.run(go)


@pytest.mark.parametrize("outcome", ["done", "error"])
def test_terminal_status_is_mirrored(outcome: str) -> None:
    reg = _reg()

    async def work() -> str:
        if outcome == "error":
            raise RuntimeError("kaput")
        return "the answer"

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(work(), desc="compute")
        await jm.wait(job.id, timeout_s=5)
        return job

    job = anyio.run(go)
    node = reg.node("job:1")
    assert job.status == outcome
    assert node.status == outcome
    if outcome == "error":
        assert node.error is not None and "kaput" in node.error
    else:
        assert node.error is None


def test_timeout_is_mirrored_with_its_reason() -> None:
    reg = _reg()

    async def slow() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(slow(), desc="slowpoke", max_runtime_s=0.01)
        await jm.wait(job.id, timeout_s=5)
        assert job.status == "timeout"

    anyio.run(go)
    node = reg.node("job:1")
    assert node.status == "timeout"
    assert node.error and "was stopped" in node.error
    assert node.ended_at is not None


def test_cancel_records_a_stop_action_then_the_resulting_status() -> None:
    reg = _reg()
    seen = Recorder()
    reg.subscribe(seen)

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(forever(), desc="hangs")
        await asyncio.sleep(0.01)  # let it actually start
        assert jm.cancel(job.id)
        await jm.wait(job.id, timeout_s=5)
        assert job.status == "cancelled"

    anyio.run(go)
    assert seen.kinds() == [
        "NodeCreated", "NodeStatus", "NodeAction", "NodeStatus",
    ]
    assert seen.events[2].action == "stop" and seen.events[2].actor == "user"
    assert seen.events[3].status == "cancelled"
    assert reg.node("job:1").status == "cancelled"


def test_cancel_before_the_first_step_still_reports_cancelled() -> None:
    reg = _reg()

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(forever(), desc="never starts")
        jm.cancel(job.id)  # cancelled before the runner takes its first step
        await jm.wait(job.id, timeout_s=5)
        assert job.status == "cancelled"

    anyio.run(go)
    assert reg.node("job:1").status == "cancelled"


def test_cancel_all_stops_every_live_job_exactly_once() -> None:
    reg = _reg()
    seen = Recorder()
    reg.subscribe(seen)

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(registry=reg)
        for i in range(3):
            jm.spawn(forever(), desc=f"job {i}")
        await asyncio.sleep(0.01)
        assert jm.cancel_all() == 3
        for j in jm.all():
            await jm.wait(j.id, timeout_s=5)
        # Once the tasks are actually done, cancelling again is a no-op — so no
        # second stop action is recorded for any of them.
        assert jm.cancel_all() == 0

    anyio.run(go)
    actions = [e for e in seen.events if isinstance(e, NodeAction)]
    assert sorted(a.node_id for a in actions) == ["job:1", "job:2", "job:3"]
    assert all(n.status == "cancelled" for n in reg.nodes.values())


def test_every_node_is_reachable_by_the_id_scheme() -> None:
    reg = _reg()

    async def work() -> str:
        return "ok"

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(work(), desc="compute")
        await jm.wait(job.id, timeout_s=5)
        return job

    job = anyio.run(go)
    from mantis_agent.activity.ids import make_id, resolve_ref

    assert make_id("job", job.id) in reg.nodes
    assert resolve_ref(str(job.id), reg.nodes, kind="job") == "job:1"


def test_emitted_events_are_exactly_the_four_points() -> None:
    reg = _reg()
    seen = Recorder()
    reg.subscribe(seen)

    async def work() -> str:
        return "ok"

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(work(), desc="compute")
        await jm.emit(job, "halfway")
        await jm.wait(job.id, timeout_s=5)

    anyio.run(go)
    assert seen.kinds() == [
        "NodeCreated",   # spawn
        "NodeStatus",    # spawn: running
        "NodeActivity",  # emit
        "NodeStatus",    # terminal
    ]
    assert isinstance(seen.events[0], NodeCreated)
    assert isinstance(seen.events[2], NodeActivity)
    assert not any(isinstance(e, NodeUsage) for e in seen.events)
    assert [e.node_id for e in seen.events] == ["job:1"] * 4
    assert [e.seq for e in seen.events] == [1, 2, 3, 4]
    assert isinstance(seen.events[-1], NodeStatus)


# ---------------------------------------------------------------------------
# The one that matters: a broken registry is not a broken job
# ---------------------------------------------------------------------------


def test_a_registry_that_raises_on_every_call_cannot_fail_a_job() -> None:
    reg = Boom()

    async def work() -> str:
        return "the answer"

    async def boom() -> str:
        raise RuntimeError("kaput")

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(registry=reg)

        ok = jm.spawn(work(), desc="compute")
        got = await jm.wait(ok.id, timeout_s=5)
        assert got.status == "done" and got.result == "the answer"

        bad = jm.spawn(boom(), desc="explodes")
        await jm.wait(bad.id, timeout_s=5)
        assert bad.status == "error" and "kaput" in bad.result

        hang = jm.spawn(forever(), desc="hangs")
        await jm.emit(hang, "still going")       # emission path raises internally
        assert hang.stream_count == 1
        assert jm.cancel(hang.id)
        await jm.wait(hang.id, timeout_s=5)
        assert hang.status == "cancelled"

    anyio.run(go)
    assert reg.calls > 0  # every one of those raised, and none of it mattered


def test_a_broken_registry_still_delivers_terminal_callbacks() -> None:
    events: list = []

    async def work() -> str:
        return "ok"

    async def go():
        jm = JobManager(on_event=events.append, registry=Boom())
        job = jm.spawn(work(), desc="compute")
        await jm.wait(job.id, timeout_s=5)

    anyio.run(go)
    assert len(events) == 1 and events[0].status == "done"


def test_a_broken_registry_does_not_stop_the_stream_callback() -> None:
    streamed: list = []

    async def forever() -> str:
        await asyncio.sleep(999)
        return ""

    async def go():
        jm = JobManager(on_stream=lambda job, text: streamed.append(text),
                        registry=Boom())
        job = jm.spawn(forever(), desc="watch", kind="watch")
        await jm.emit(job, "one")
        await jm.emit(job, "two")
        jm.cancel_all()
        await jm.wait(job.id, timeout_s=5)

    anyio.run(go)
    assert streamed == ["one", "two"]


def test_a_subscriber_that_raises_cannot_fail_a_job() -> None:
    reg = _reg()

    def hostile(ev) -> None:  # noqa: ANN001
        raise RuntimeError("no")

    reg.subscribe(hostile)

    async def work() -> str:
        return "ok"

    async def go():
        jm = JobManager(registry=reg)
        job = jm.spawn(work(), desc="compute")
        await jm.wait(job.id, timeout_s=5)
        return job

    job = anyio.run(go)
    assert job.status == "done" and job.result == "ok"
    assert reg.stats["subscriber_errors"] >= 1
    assert reg.node("job:1").status == "done"
