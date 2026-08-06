"""Subagent ceilings + ledger — ``mantis_agent.subagent_limits``.

No network, no agents, no model. Everything here exercises the limiter
directly, because the point of the module is that it is the *one* thing all
three spawners take: if it only worked when driven through ``task``,
``coordinate`` and ``Workflow`` it would not be shareable in the first place.

Timing is avoided wherever it can be: the concurrency tests synchronize on the
limiter's own ``waiting`` counter and on ``anyio.Event`` rather than on sleeps,
so they are deterministic under a loaded CI box. The two that do use a clock
(wall-clock ceiling, acquire timeout) use sub-100ms values and only assert that
the bound *fired*, never how fast.
"""

from __future__ import annotations

import dataclasses
import functools

import anyio
import pytest

from mantis_agent.errors import AgentError
from mantis_agent.subagent_limits import (
    AUTO,
    AgentBudgetLedger,
    ChildTimeoutError,
    ConcurrencyLimitError,
    SessionAgentLimitError,
    SpawnDepthExceededError,
    SpawnRateExceededError,
    SubagentLimiter,
    SubagentLimitError,
    SubagentLimits,
    current_depth,
    current_spawn_context,
    default_cpu_cap,
    reset_shared_limiter,
    set_shared_limiter,
    shared_limiter,
)


def anyio_test(fn):
    """Run an ``async def test_*`` on a fresh event loop.

    Same thing the rest of the suite does with an inner ``go()`` plus
    ``anyio.run(go)`` — factored out because this file has twenty of them, and
    a per-test loop keeps the limiter's waiters from ever outliving a test.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return anyio.run(functools.partial(fn, *args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def _clean_shared():
    """The shared limiter is process-wide by design; no test may leak one."""

    reset_shared_limiter()
    yield
    reset_shared_limiter()


def roomy(**overrides) -> SubagentLimits:
    """Limits with every ceiling but the one under test moved out of the way."""

    base = dict(
        max_depth=8,
        max_spawns_per_turn=1000,
        max_concurrent_agents=64,
        max_total_agents_per_session=10_000,
        max_child_wall_seconds=0,
    )
    base.update(overrides)
    return SubagentLimits(**base)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_defaults_match_the_plan_table():
    limits = SubagentLimits()
    assert limits.max_depth == 2
    assert limits.max_spawns_per_turn == 12
    assert limits.max_concurrent_agents == 8
    assert limits.max_total_agents_per_session == 200
    assert limits.max_child_wall_seconds == 600.0
    assert limits.max_report_bytes == 32768


def test_settings_keys_are_the_documented_camel_case_ones():
    assert SubagentLimits().to_dict() == {
        "maxDepth": 2,
        "maxSpawnsPerTurn": 12,
        "maxConcurrentAgents": 8,
        "maxTotalAgentsPerSession": 200,
        "maxChildWallSeconds": 600.0,
        "maxReportBytes": 32768,
    }


def test_from_mapping_accepts_whole_document_or_inner_block():
    doc = {"model": "x", "subagents": {"maxDepth": 3, "maxConcurrentAgents": 2}}
    inner = {"maxDepth": 3, "maxConcurrentAgents": 2}
    for data in (doc, inner):
        limits = SubagentLimits.from_mapping(data)
        assert limits.max_depth == 3
        assert limits.max_concurrent_agents == 2
        # Untouched keys keep their defaults.
        assert limits.max_spawns_per_turn == 12


def test_from_mapping_accepts_snake_case_and_ignores_foreign_keys():
    limits = SubagentLimits.from_mapping(
        {
            "subagents": {
                "max_spawns_per_turn": 4,
                # Sub-blocks owned by other phases of the plan must not upset
                # the parse.
                "report": {"neutralize": True},
                "isolation": {"default": "worktree"},
                "personas": {"trustProject": "prompt"},
            }
        }
    )
    assert limits.max_spawns_per_turn == 4
    assert limits.max_depth == 2


@pytest.mark.parametrize("junk", ["", "  ", "lots", None, True, False, [], {}])
def test_junk_settings_values_fall_back_to_the_default(junk):
    # A typo in settings.json must not take the subagent channel down, and a
    # bool must not read as 1 (that would silently cap depth at one child).
    limits = SubagentLimits.from_mapping({"subagents": {"maxSpawnsPerTurn": junk}})
    assert limits.max_spawns_per_turn == 12


def test_values_are_clamped_into_a_survivable_range():
    # ``project``/``local`` settings ship inside a cloned repo.
    hostile = SubagentLimits.from_mapping(
        {"subagents": {"maxConcurrentAgents": 10**9, "maxTotalAgentsPerSession": -5}}
    )
    assert hostile.max_concurrent_agents == 256
    assert hostile.max_total_agents_per_session == 1


def test_env_overrides_settings_and_junk_env_is_ignored():
    settings = {"subagents": {"maxConcurrentAgents": 3, "maxDepth": 1}}
    limits = SubagentLimits.load(
        settings,
        env={"MANTIS_SUBAGENT_MAX_CONCURRENT": "5", "MANTIS_SUBAGENT_MAX_DEPTH": "nope"},
    )
    assert limits.max_concurrent_agents == 5   # env wins
    assert limits.max_depth == 1               # junk env leaves settings alone


def test_auto_concurrency_reproduces_the_workflow_default_cap():
    # ``workflow._default_cap()`` is the third spawner's private cap. The whole
    # point of §7 is that it can be expressed by this config instead, so if the
    # two ever disagree the consolidation is not possible.
    from mantis_agent.workflow import _default_cap

    limits = SubagentLimits(max_concurrent_agents=AUTO)
    assert limits.resolved_max_concurrent_agents() == _default_cap() == default_cpu_cap()
    assert limits.max_concurrent_agents == AUTO  # survives round-tripping
    assert SubagentLimits.from_mapping(
        {"subagents": {"maxConcurrentAgents": "auto"}}
    ).resolved_max_concurrent_agents() == _default_cap()


def test_effective_concurrency_lets_a_spawner_narrow_but_never_widen():
    limits = SubagentLimits(max_concurrent_agents=8)
    assert limits.effective_concurrency(None) == 8
    assert limits.effective_concurrency(3) == 3     # a gentler workflow is fine
    assert limits.effective_concurrency(64) == 8    # a greedier one is not
    assert limits.effective_concurrency("junk") == 8


def test_wall_clock_can_be_switched_off_with_zero():
    assert SubagentLimits().wall_clock_enabled is True
    assert SubagentLimits(max_child_wall_seconds=0).wall_clock_enabled is False


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


@anyio_test
async def test_depth_rejection_names_the_setting():
    limiter = SubagentLimiter(roomy(max_depth=2))
    with pytest.raises(SpawnDepthExceededError) as excinfo:
        await limiter.acquire(3, "sub:9")
    err = excinfo.value
    assert err.setting == "subagents.maxDepth"
    assert err.env_var == "MANTIS_SUBAGENT_MAX_DEPTH"
    assert err.limit == 2 and err.observed == 3
    assert "depth 3" in str(err) and "maxDepth 2" in str(err)
    # No slot, no counter, no ledger entry was consumed by the refusal.
    assert limiter.live == 0
    assert limiter.spawns_this_turn == 0
    assert limiter.total_started == 0
    assert limiter.ledger.started == 0
    assert limiter.ledger.refused == {"maxDepth": 1}


@anyio_test
async def test_depth_is_inherited_from_the_spawn_context_not_the_tool():
    # This is the case the ``task`` tool's excluded-tools set does NOT cover:
    # a child built through the SDK, which never sees the task schema.
    limiter = SubagentLimiter(roomy(max_depth=2))
    assert current_depth() == 0

    async with limiter.spawn(agent_type="explore") as child:
        assert child.depth == 1
        assert current_depth() == 1
        assert current_spawn_context().agent_id == child.agent_id

        async with limiter.spawn(agent_type="explore") as grandchild:
            assert grandchild.depth == 2
            assert grandchild.parent_id == child.agent_id

            with pytest.raises(SpawnDepthExceededError) as excinfo:
                await limiter.acquire(agent_type="explore")
    # The chain in the message is what makes the error actionable.
    assert "root ->" in str(excinfo.value)
    assert limiter.ledger.max_depth_reached == 2


@anyio_test
async def test_max_depth_zero_disables_children_entirely():
    limiter = SubagentLimiter(roomy(max_depth=0))
    with pytest.raises(SpawnDepthExceededError):
        await limiter.acquire()


@anyio_test
async def test_context_is_cleared_after_release():
    limiter = SubagentLimiter(roomy())
    lease = await limiter.acquire(agent_type="plan")
    assert current_spawn_context() is not None
    limiter.release(lease)
    assert current_spawn_context() is None
    assert current_depth() == 0


@anyio_test
async def test_release_with_no_argument_releases_the_current_context():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=1))
    await limiter.acquire(agent_type="explore")
    limiter.release()  # the signature the spawners' ``finally:`` wants
    assert limiter.live == 0
    assert current_spawn_context() is None


# ---------------------------------------------------------------------------
# Per-turn and per-session ceilings
# ---------------------------------------------------------------------------


@anyio_test
async def test_spawns_per_turn_ceiling_and_reset():
    limiter = SubagentLimiter(roomy(max_spawns_per_turn=12))
    for _ in range(12):
        lease = await limiter.acquire(1, None)
        limiter.release(lease)  # releasing does NOT refund the turn's rate

    with pytest.raises(SpawnRateExceededError) as excinfo:
        await limiter.acquire(1, None)
    err = excinfo.value
    assert err.setting == "subagents.maxSpawnsPerTurn"
    assert "12 agents already started this turn" in str(err)
    assert "MANTIS_SUBAGENT_MAX_SPAWNS_PER_TURN" in str(err)

    limiter.begin_turn()
    assert limiter.spawns_this_turn == 0
    lease = await limiter.acquire(1, None)
    assert lease.depth == 1
    assert limiter.total_started == 13  # the session counter is not reset


@anyio_test
async def test_session_total_ceiling_is_not_reset_by_a_new_turn():
    limiter = SubagentLimiter(roomy(max_total_agents_per_session=3))
    for _ in range(3):
        limiter.begin_turn()
        limiter.release(await limiter.acquire(1, None))

    limiter.begin_turn()
    with pytest.raises(SessionAgentLimitError) as excinfo:
        await limiter.acquire(1, None)
    err = excinfo.value
    assert err.setting == "subagents.maxTotalAgentsPerSession"
    assert err.limit == 3 and err.observed == 3
    assert "3 agents have run in this session" in str(err)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@anyio_test
async def test_concurrency_refusal_when_the_caller_will_not_wait():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=2))
    held = [await limiter.acquire(1, None), await limiter.acquire(1, None)]
    assert limiter.live == 2

    with pytest.raises(ConcurrencyLimitError) as excinfo:
        await limiter.acquire(1, None, wait=False)
    err = excinfo.value
    assert err.setting == "subagents.maxConcurrentAgents"
    assert err.limit == 2 and err.observed == 2
    assert "2 agents are already running" in str(err)

    # A refusal must not consume the turn's rate or the session's total: the
    # spawn never happened, and burning the budget for it would make a busy
    # moment look like a runaway fan-out.
    assert limiter.spawns_this_turn == 2
    assert limiter.total_started == 2
    assert limiter.ledger.refused == {"maxConcurrentAgents": 1}

    for lease in held:
        limiter.release(lease)
    assert limiter.live == 0


@anyio_test
async def test_concurrency_acquire_can_time_out():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=1))
    lease = await limiter.acquire(1, None)
    with pytest.raises(ConcurrencyLimitError) as excinfo:
        await limiter.acquire(1, None, timeout=0.05)
    assert "within 0.05s" in str(excinfo.value)
    limiter.release(lease)
    # The timed-out waiter did not walk off with the slot.
    assert limiter.waiting == 0
    second = await limiter.acquire(1, None, wait=False)
    assert second.agent_id != lease.agent_id


@anyio_test
async def test_waiters_are_served_first_in_first_out():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=1))
    order: list[str] = []
    go = anyio.Event()

    async def worker(name: str) -> None:
        lease = await limiter.acquire(1, None, agent_type=name)
        order.append(name)
        await go.wait()
        limiter.release(lease)

    async with anyio.create_task_group() as tg:
        for name in ("a", "b", "c", "d"):
            tg.start_soon(worker, name)
        # Tasks start in submission order and each runs until it blocks in the
        # gate, so waiting for the queue to fill is enough — no sleeps, no
        # timing assumptions.
        while limiter.waiting < 3:
            await anyio.sleep(0)
        go.set()

    assert order == ["a", "b", "c", "d"]
    assert limiter.live == 0


@anyio_test
async def test_concurrent_acquire_under_contention_never_exceeds_the_cap():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=3))
    peak = 0
    done: list[int] = []

    async def worker(i: int) -> None:
        nonlocal peak
        async with limiter.spawn(1, None, agent_type="explore") as lease:
            peak = max(peak, limiter.live)
            assert limiter.live <= 3
            await anyio.sleep(0)  # force interleaving
            done.append(i)
            assert lease.released is False

    async with anyio.create_task_group() as tg:
        for i in range(30):
            tg.start_soon(worker, i)

    assert len(done) == 30
    assert peak == 3            # the cap was actually reached...
    assert limiter.live == 0    # ...and everything came back
    assert limiter.waiting == 0
    assert limiter.ledger.peak_live == 3
    assert limiter.ledger.completed == 30


@anyio_test
async def test_three_spawners_share_one_cap():
    """subagent.py, coordinator.py and workflow.py, all on one limiter."""

    installed = SubagentLimiter(roomy(max_concurrent_agents=4))
    set_shared_limiter(installed)
    assert shared_limiter() is installed
    # A later caller cannot quietly widen the ceiling by asking for more.
    assert shared_limiter(SubagentLimits(max_concurrent_agents=99)) is installed

    seen: list[int] = []

    async def spawner(kind: str, n: int) -> None:
        for _ in range(n):
            async with shared_limiter().spawn(1, None, agent_type=kind):
                seen.append(shared_limiter().live)
                await anyio.sleep(0)

    async with anyio.create_task_group() as tg:
        tg.start_soon(spawner, "task-tool", 6)
        tg.start_soon(spawner, "coordinator", 6)
        tg.start_soon(spawner, "workflow", 6)

    assert max(seen) <= 4
    ledger = installed.ledger
    assert ledger.started == 18
    assert set(ledger.by_type) == {"task-tool", "coordinator", "workflow"}
    assert all(row.live == 0 for row in ledger.by_type.values())


# ---------------------------------------------------------------------------
# Context manager and wall clock
# ---------------------------------------------------------------------------


@anyio_test
async def test_context_manager_releases_on_exception():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=1))

    with pytest.raises(RuntimeError):
        async with limiter.spawn(1, None, agent_type="explore") as lease:
            assert limiter.live == 1
            raise RuntimeError("child blew up")

    assert lease.released is True
    assert limiter.live == 0
    assert limiter.waiting == 0
    assert current_spawn_context() is None
    assert limiter.ledger.failed == 1 and limiter.ledger.completed == 0
    # The slot really came back — a leaked one would hang here forever.
    async with limiter.spawn(1, None, agent_type="explore"):
        pass


@anyio_test
async def test_context_manager_releases_on_cancellation():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=1))

    with anyio.move_on_after(0.05):
        async with limiter.spawn(1, None, agent_type="explore"):
            await anyio.sleep(10)

    assert limiter.live == 0
    async with limiter.spawn(1, None, agent_type="explore"):
        pass


@anyio_test
async def test_wall_clock_ceiling_stops_a_long_child():
    limiter = SubagentLimiter(roomy(max_child_wall_seconds=0.05))
    with pytest.raises(ChildTimeoutError) as excinfo:
        async with limiter.spawn(1, None, agent_type="explore"):
            await anyio.sleep(30)
    err = excinfo.value
    assert err.setting == "subagents.maxChildWallSeconds"
    assert err.env_var == "MANTIS_SUBAGENT_MAX_CHILD_SECONDS"
    assert "wall-clock limit reached" in str(err)
    assert limiter.live == 0
    assert limiter.ledger.failed == 1


@anyio_test
async def test_wall_clock_is_off_when_the_limit_is_zero():
    limiter = SubagentLimiter(roomy(max_child_wall_seconds=0))
    async with limiter.spawn(1, None, agent_type="explore") as lease:
        assert lease.deadline is None
        assert lease.remaining_seconds() is None
        assert lease.expired is False


@anyio_test
async def test_lease_reports_its_remaining_wall_time():
    ticks = iter([0.0, 10.0, 700.0, 700.0])
    limiter = SubagentLimiter(roomy(max_child_wall_seconds=600), clock=lambda: next(ticks))
    lease = await limiter.acquire(1, None)
    assert lease.deadline == 600.0
    assert lease.remaining_seconds() == 590.0
    assert lease.expired is True  # clock has moved past the deadline
    limiter.release(lease)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@anyio_test
async def test_ledger_accounts_for_agents_depth_and_cost():
    limiter = SubagentLimiter(roomy())
    async with limiter.spawn(agent_type="explore") as child:
        child.record_usage(input_tokens=1000, output_tokens=200, cost_usd=0.03)
        async with limiter.spawn(agent_type="plan") as grandchild:
            grandchild.record_usage(input_tokens=50, output_tokens=5, cost_usd=0.9)
            assert limiter.ledger.live == 2
            assert [le.agent_id for le in limiter.live_leases()] == [
                child.agent_id,
                grandchild.agent_id,
            ]

    ledger = limiter.ledger
    assert (ledger.started, ledger.live, ledger.peak_live) == (2, 0, 2)
    assert (ledger.completed, ledger.failed) == (2, 0)
    assert ledger.max_depth_reached == 2
    assert ledger.total_tokens() == 1255
    assert ledger.total_cost_usd() == pytest.approx(0.93)
    # Costliest agent type first — that is the row order ``/agents ledger`` wants.
    assert [name for name, _ in ledger.rows()] == ["plan", "explore"]
    snap = ledger.snapshot()
    assert snap["by_type"]["explore"]["input_tokens"] == 1000
    assert snap["by_type"]["plan"]["cost_usd"] == 0.9
    assert list(snap["by_type"]) == ["plan", "explore"]


@anyio_test
async def test_ledger_counts_refusals_by_limit_name():
    limiter = SubagentLimiter(SubagentLimits(max_depth=1, max_spawns_per_turn=1))
    limiter.release(await limiter.acquire(1, None))
    for _ in range(2):
        with pytest.raises(SpawnRateExceededError):
            await limiter.acquire(1, None)
    limiter.begin_turn()
    with pytest.raises(SpawnDepthExceededError):
        await limiter.acquire(2, None)
    assert limiter.ledger.refused == {"maxSpawnsPerTurn": 2, "maxDepth": 1}


@anyio_test
async def test_release_is_idempotent():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=2))
    lease = await limiter.acquire(1, None)
    lease.release()
    lease.release()
    limiter.release(lease)
    assert limiter.live == 0
    assert limiter.ledger.live == 0
    assert limiter.ledger.completed == 1  # counted exactly once
    # Both slots are still there.
    a = await limiter.acquire(1, None, wait=False)
    b = await limiter.acquire(1, None, wait=False)
    assert a.agent_id != b.agent_id


def test_ledger_stands_alone():
    # It is handed to /agents and the cockpit, so it has to be usable without
    # a limiter at all.
    ledger = AgentBudgetLedger()
    ledger.record_start(agent_type="explore", depth=1)
    ledger.record_usage("explore", input_tokens=10, output_tokens=2, cost_usd=0.001)
    ledger.record_finish(agent_type="explore", ok=False, wall_seconds=1.5)
    ledger.record_finish(agent_type="explore", ok=True)  # a double release
    assert ledger.live == 0            # never negative
    assert ledger.by_type["explore"].live == 0
    assert ledger.by_type["explore"].failed == 1
    assert ledger.snapshot()["by_type"]["explore"]["wall_seconds"] == 1.5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_every_limit_error_is_catchable_as_an_agent_error():
    for cls in (
        SpawnDepthExceededError,
        SpawnRateExceededError,
        ConcurrencyLimitError,
        SessionAgentLimitError,
        ChildTimeoutError,
    ):
        assert issubclass(cls, SubagentLimitError)
        assert issubclass(cls, AgentError)


@anyio_test
async def test_errors_are_structured_and_actionable():
    limiter = SubagentLimiter(SubagentLimits(max_spawns_per_turn=1))
    limiter.release(await limiter.acquire(1, None))
    with pytest.raises(SpawnRateExceededError) as excinfo:
        await limiter.acquire(1, None)
    err = excinfo.value

    text = err.as_tool_error()
    assert text == str(err)
    # Names the ceiling, what was observed, what to do, and the two places the
    # setting can be changed. A model reading this can act on it.
    assert "maxSpawnsPerTurn" in text
    assert "Wait for the running children" in text
    assert "`subagents.maxSpawnsPerTurn`" in text
    assert "MANTIS_SUBAGENT_MAX_SPAWNS_PER_TURN" in text

    assert err.to_dict() == {
        "error": "SpawnRateExceededError",
        "limit": "maxSpawnsPerTurn",
        "setting": "subagents.maxSpawnsPerTurn",
        "env": "MANTIS_SUBAGENT_MAX_SPAWNS_PER_TURN",
        "limit_value": 1,
        "observed": 1,
        "message": text,
    }


def test_limits_are_frozen_so_a_running_limiter_cannot_be_widened():
    limits = SubagentLimits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_concurrent_agents = 99  # type: ignore[misc]
    wider = dataclasses.replace(limits, max_concurrent_agents=16)
    assert wider.max_concurrent_agents == 16 and limits.max_concurrent_agents == 8


@anyio_test
async def test_limiter_snapshot_is_json_shaped():
    limiter = SubagentLimiter(roomy(max_concurrent_agents=2))
    async with limiter.spawn(agent_type="explore"):
        snap = limiter.snapshot()
    assert snap["limits"]["maxConcurrentAgents"] == 2
    assert snap["concurrency_cap"] == 2
    assert snap["live"] == 1
    assert snap["total_started"] == 1
    assert snap["ledger"]["started"] == 1
