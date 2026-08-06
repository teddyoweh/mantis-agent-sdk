"""Team task DAG: atomic claims, leases, dependency gating, cycles, failure.

The claim is the correctness property of the whole team feature — two peers
must never do the same work — so the centrepiece here is a real thread race
against a real SQLite file, not a mocked one. Everything else in this suite is
bookkeeping around that: leases so a dead peer releases instead of blocking a
task forever, dependency gating so work starts in order, cycle rejection at
creation, and failure that blocks dependents *loudly*.

Time is injected (``clock=``) everywhere except the race, where the point is to
run against the wall clock the way peers do.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from mantis_agent.teams import (
    DEFAULT_LEASE_SECONDS,
    SCHEMA_VERSION,
    Task,
    TaskClaimConflictError,
    TaskCycleError,
    TaskDependencyBlockedError,
    TaskLeaseExpiredError,
    TaskNotFoundError,
    TaskStateError,
    TaskStore,
    new_task_id,
)


class FakeClock:
    """A monotonically advanced wall clock. Tests own time explicitly."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock) -> TaskStore:
    return TaskStore(str(tmp_path / "tasks.db"), clock=clock)


def _chain(store: TaskStore, *titles: str) -> list[Task]:
    """Create a linear dependency chain a -> b -> c (b depends on a, ...)."""

    out: list[Task] = []
    prev: tuple[str, ...] = ()
    for title in titles:
        task = store.create_task("t1", title, task_id=title, depends_on=prev)
        out.append(task)
        prev = (title,)
    return out


# -- creation and identity -------------------------------------------------------


def test_create_task_defaults_to_open_and_unassigned(store: TaskStore) -> None:
    task = store.create_task("t1", "write the docs", created_by="peer:lead")
    assert task.status == "open"
    assert task.assignee is None
    assert task.team_id == "t1"
    assert task.created_by == "peer:lead"
    assert store.get(task.id) == task


def test_new_task_id_is_unique_and_safe() -> None:
    ids = {new_task_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("task_") for i in ids)


def test_duplicate_task_id_rejected(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="dup")
    with pytest.raises(ValueError, match="already exists"):
        store.create_task("t1", "b", task_id="dup")


def test_blank_title_rejected(store: TaskStore) -> None:
    with pytest.raises(ValueError, match="title"):
        store.create_task("t1", "   ")


def test_memory_path_rejected(tmp_path) -> None:
    # ``:memory:`` gives every connection its own database, which would make
    # claims silently non-atomic across peers. Refusing it is the whole point.
    with pytest.raises(ValueError, match="in-memory"):
        TaskStore(":memory:")


def test_schema_version_recorded_and_reopened(tmp_path, clock) -> None:
    path = str(tmp_path / "tasks.db")
    TaskStore(path, clock=clock).create_task("t1", "a", task_id="a")
    again = TaskStore(path, clock=clock)
    assert again.get("a").title == "a"
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and int(row[0]) == SCHEMA_VERSION


# -- atomic claims ---------------------------------------------------------------


def test_claim_marks_assignee_and_lease(store: TaskStore, clock: FakeClock) -> None:
    task = store.create_task("t1", "a", task_id="a")
    result = store.claim(task.id, "peer:infra")
    assert result.task.status == "claimed"
    assert result.task.assignee == "peer:infra"
    assert result.task.claimed_at == clock.now
    assert result.task.lease_expires == clock.now + DEFAULT_LEASE_SECONDS
    assert result.reclaimed is False
    assert result.partial_work is False


def test_second_claim_conflicts_with_holder_named(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    with pytest.raises(TaskClaimConflictError) as excinfo:
        store.claim("a", "peer:two")
    err = excinfo.value
    assert err.task_id == "a"
    assert err.holder == "peer:one"
    assert err.status == "claimed"
    assert store.try_claim("a", "peer:two") is None


def test_claim_of_unknown_task_raises(store: TaskStore) -> None:
    with pytest.raises(TaskNotFoundError):
        store.claim("nope", "peer:one")


def test_twenty_concurrent_claimants_yield_exactly_one_winner(tmp_path) -> None:
    """The correctness property, tested the only way that means anything.

    Twenty threads, one real SQLite file, one barrier so they all hit
    ``BEGIN IMMEDIATE`` together. Exactly one may come back with a claim, and
    the row must agree with whoever that was.
    """

    store = TaskStore(str(tmp_path / "tasks.db"))
    store.create_task("t1", "the contested task", task_id="hot")

    n = 20
    barrier = threading.Barrier(n)
    winners: list[str] = []
    losers: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(i: int) -> None:
        peer = f"peer:{i}"
        try:
            barrier.wait(timeout=30)
            claimed = store.try_claim("hot", peer)
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            with lock:
                errors.append(exc)
            return
        with lock:
            (winners if claimed is not None else losers).append(peer)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(losers) == n - 1
    row = store.get("hot")
    assert row.assignee == winners[0]
    assert row.status == "claimed"
    assert row.reclaim_count == 0


def test_concurrent_claim_next_never_double_assigns(tmp_path) -> None:
    """Same race, but through the scheduler entry point: N peers pulling from a
    pool of M tasks must partition it, never overlap."""

    store = TaskStore(str(tmp_path / "tasks.db"))
    for i in range(8):
        store.create_task("t1", f"task-{i}", task_id=f"task-{i}")

    n = 16
    barrier = threading.Barrier(n)
    got: list[tuple[str, str]] = []
    lock = threading.Lock()

    def run(i: int) -> None:
        peer = f"peer:{i}"
        barrier.wait(timeout=30)
        while True:
            claimed = store.claim_next("t1", peer)
            if claimed is None:
                return
            with lock:
                got.append((claimed.task.id, peer))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    ids = [task_id for task_id, _ in got]
    assert sorted(ids) == sorted(f"task-{i}" for i in range(8))
    assert len(set(ids)) == len(ids), f"a task was claimed twice: {got}"


# -- leases, heartbeats, reclaim -------------------------------------------------


def test_heartbeat_extends_the_lease(store: TaskStore, clock: FakeClock) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    clock.advance(120)
    expires = store.heartbeat("a", "peer:one")
    assert expires == clock.now + DEFAULT_LEASE_SECONDS
    assert store.get("a").heartbeat_at == clock.now
    # Still held well past the original lease, because the peer is alive.
    clock.advance(DEFAULT_LEASE_SECONDS - 1)
    with pytest.raises(TaskClaimConflictError):
        store.claim("a", "peer:two")


def test_expired_lease_lets_another_peer_reclaim_with_partial_work_warning(
    store: TaskStore, clock: FakeClock
) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:dead")
    store.start("a", "peer:dead")
    clock.advance(DEFAULT_LEASE_SECONDS + 1)

    result = store.claim("a", "peer:live")
    assert result.reclaimed is True
    assert result.partial_work is True
    assert result.previous_assignee == "peer:dead"
    assert "peer:dead" in result.warning and "partial" in result.warning.lower()
    task = result.task
    assert task.assignee == "peer:live"
    assert task.status == "claimed"
    assert task.reclaim_count == 1
    assert task.last_assignee == "peer:dead"


def test_heartbeat_after_expiry_refuses_to_resurrect_the_claim(
    store: TaskStore, clock: FakeClock
) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:dead")
    clock.advance(DEFAULT_LEASE_SECONDS + 1)
    with pytest.raises(TaskLeaseExpiredError):
        store.heartbeat("a", "peer:dead")
    assert store.get("a").status == "open"


def test_heartbeat_by_non_holder_refused(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    with pytest.raises(TaskClaimConflictError):
        store.heartbeat("a", "peer:two")


def test_sweep_expired_releases_dead_claims(store: TaskStore, clock: FakeClock) -> None:
    store.create_task("t1", "a", task_id="a")
    store.create_task("t1", "b", task_id="b")
    store.claim("a", "peer:dead")
    store.claim("b", "peer:live")
    clock.advance(DEFAULT_LEASE_SECONDS - 5)
    store.heartbeat("b", "peer:live")
    clock.advance(10)

    released = store.sweep_expired("t1")
    assert released == ("a",)
    assert store.get("a").status == "open"
    assert store.get("a").partial_work is True
    assert store.get("b").assignee == "peer:live"


def test_release_returns_the_task_without_flagging_partial_work(
    store: TaskStore,
) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    task = store.release("a", "peer:one", reason="reassigning")
    assert task.status == "open"
    assert task.assignee is None
    assert task.partial_work is False
    assert store.claim("a", "peer:two").reclaimed is False


def test_completion_requires_the_live_claim(store: TaskStore, clock: FakeClock) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    with pytest.raises(TaskClaimConflictError):
        store.complete("a", "peer:two")
    clock.advance(DEFAULT_LEASE_SECONDS + 1)
    with pytest.raises(TaskLeaseExpiredError):
        store.complete("a", "peer:one")


def test_custom_lease_seconds_per_claim(store: TaskStore, clock: FakeClock) -> None:
    store.create_task("t1", "a", task_id="a")
    result = store.claim("a", "peer:one", lease_seconds=30)
    assert result.task.lease_expires == clock.now + 30
    clock.advance(31)
    assert store.claim("a", "peer:two").reclaimed is True


# -- dependencies ----------------------------------------------------------------


def test_dependency_gates_the_claim_then_unblocks_on_completion(
    store: TaskStore,
) -> None:
    _chain(store, "a", "b")
    assert [t.id for t in store.claimable("t1")] == ["a"]

    with pytest.raises(TaskDependencyBlockedError) as excinfo:
        store.claim("b", "peer:two")
    assert excinfo.value.pending == ("a",)

    store.claim("a", "peer:one")
    # Claimed is not done: b stays gated.
    assert [t.id for t in store.claimable("t1")] == []

    done = store.complete("a", "peer:one", result="ok")
    assert done.task.status == "done"
    assert done.unblocked == ("b",)
    assert [t.id for t in store.claimable("t1")] == ["b"]
    assert store.claim("b", "peer:two").task.assignee == "peer:two"


def test_claim_next_skips_gated_tasks(store: TaskStore) -> None:
    _chain(store, "a", "b")
    first = store.claim_next("t1", "peer:one")
    assert first is not None and first.task.id == "a"
    assert store.claim_next("t1", "peer:two") is None


def test_missing_dependency_is_reported_not_silently_claimable(
    store: TaskStore,
) -> None:
    store.create_task("t1", "b", task_id="b", depends_on=("ghost",))
    with pytest.raises(TaskDependencyBlockedError) as excinfo:
        store.claim("b", "peer:one")
    assert excinfo.value.missing == ("ghost",)
    assert store.blockers("b").missing == ("ghost",)


def test_dependencies_are_deduplicated_and_ordered(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    task = store.create_task("t1", "b", task_id="b", depends_on=("a", "a"))
    assert task.depends_on == ("a",)


def test_add_dependency_regates_an_open_task(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.create_task("t1", "b", task_id="b")
    assert {t.id for t in store.claimable("t1")} == {"a", "b"}
    task = store.add_dependency("b", "a")
    assert task.depends_on == ("a",)
    assert [t.id for t in store.claimable("t1")] == ["a"]


# -- cycles ----------------------------------------------------------------------


def test_self_dependency_rejected_with_path(store: TaskStore) -> None:
    with pytest.raises(TaskCycleError) as excinfo:
        store.create_task("t1", "a", task_id="a", depends_on=("a",))
    assert excinfo.value.path == ("a", "a")
    assert "a -> a" in str(excinfo.value)


def test_cycle_rejected_at_creation_with_the_cycle_path(store: TaskStore) -> None:
    # b depends on a (a does not exist yet — forward references are legal),
    # c depends on b; creating a with a dependency on c closes the loop.
    store.create_task("t1", "b", task_id="b", depends_on=("a",))
    store.create_task("t1", "c", task_id="c", depends_on=("b",))
    with pytest.raises(TaskCycleError) as excinfo:
        store.create_task("t1", "a", task_id="a", depends_on=("c",))
    assert excinfo.value.path == ("a", "c", "b", "a")
    assert "a -> c -> b -> a" in str(excinfo.value)
    # The rejected task was not written.
    with pytest.raises(TaskNotFoundError):
        store.get("a")


def test_add_dependency_rejects_a_cycle_with_path(store: TaskStore) -> None:
    _chain(store, "a", "b", "c")
    with pytest.raises(TaskCycleError) as excinfo:
        store.add_dependency("a", "c")
    assert excinfo.value.path == ("a", "c", "b", "a")
    assert store.get("a").depends_on == ()


# -- failure blocks dependents ---------------------------------------------------


def test_failed_task_blocks_dependents_with_a_reason(store: TaskStore) -> None:
    _chain(store, "a", "b", "c")
    store.claim("a", "peer:one")
    outcome = store.fail("a", "peer:one", reason="compile error")

    assert outcome.task.status == "failed"
    assert outcome.blocked == ("b", "c")
    b = store.get("b")
    assert b.status == "blocked"
    assert b.blocked_by == "a"
    assert "compile error" in b.blocked_reason
    # Transitive dependents point at the root cause, not at their neighbour.
    assert store.get("c").blocked_by == "a"
    assert store.claimable("t1") == []
    with pytest.raises(TaskDependencyBlockedError) as excinfo:
        store.claim("b", "peer:two")
    assert excinfo.value.failed == ("a",)


def test_blocked_dependents_resume_when_the_failure_is_retried(
    store: TaskStore,
) -> None:
    _chain(store, "a", "b", "c")
    store.claim("a", "peer:one")
    store.fail("a", "peer:one", reason="flaky")

    reopened = store.reopen("a", reason="retrying")
    assert reopened.task.status == "open"
    assert store.get("b").status == "open"
    assert store.get("b").blocked_reason == ""
    assert store.get("c").status == "open"

    store.claim("a", "peer:one")
    done = store.complete("a", "peer:one")
    assert done.unblocked == ("b",)
    assert [t.id for t in store.claimable("t1")] == ["b"]


def test_fail_requires_the_claim_and_a_reason(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    with pytest.raises(ValueError, match="reason"):
        store.fail("a", "peer:one", reason="  ")
    with pytest.raises(TaskClaimConflictError):
        store.fail("a", "peer:two", reason="nope")


def test_done_task_cannot_be_failed_or_reclaimed(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    store.complete("a", "peer:one")
    with pytest.raises(TaskStateError):
        store.fail("a", "peer:one", reason="too late")
    with pytest.raises(TaskClaimConflictError) as excinfo:
        store.claim("a", "peer:two")
    assert excinfo.value.status == "done"


# -- listing and stats -----------------------------------------------------------


def test_list_and_stats_scope_by_team(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.create_task("t1", "b", task_id="b")
    store.create_task("t2", "x", task_id="x")
    store.claim("a", "peer:one")

    assert [t.id for t in store.list_tasks("t1")] == ["a", "b"]
    assert [t.id for t in store.list_tasks("t1", status="open")] == ["b"]
    assert [t.id for t in store.list_tasks()] == ["a", "b", "x"]
    assert store.stats("t1") == {"open": 1, "claimed": 1}
    assert store.claimable("t2")[0].id == "x"


def test_start_moves_claim_to_in_progress(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    task = store.start("a", "peer:one")
    assert task.status == "in_progress"
    with pytest.raises(TaskStateError):
        store.start("a", "peer:one")


def test_complete_records_result_and_artifacts(store: TaskStore) -> None:
    store.create_task("t1", "a", task_id="a")
    store.claim("a", "peer:one")
    task = store.complete(
        "a", "peer:one", result="shipped", artifacts=("/tmp/diff.patch",)
    ).task
    assert task.result == "shipped"
    assert task.artifacts == ("/tmp/diff.patch",)
    assert store.get("a").artifacts == ("/tmp/diff.patch",)


# -- async surface ---------------------------------------------------------------


def test_async_store_wraps_every_call(tmp_path) -> None:
    anyio = pytest.importorskip("anyio")
    from mantis_agent.teams import AsyncTaskStore

    store = AsyncTaskStore(TaskStore(str(tmp_path / "tasks.db")))

    async def main() -> None:
        await store.create_task("t1", "a", task_id="a")
        result = await store.claim("a", "peer:one")
        assert result.task.assignee == "peer:one"
        await store.heartbeat("a", "peer:one")
        done = await store.complete("a", "peer:one", result="ok")
        assert done.task.status == "done"
        assert [t.id for t in await store.list_tasks("t1")] == ["a"]

    anyio.run(main)
