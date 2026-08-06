"""The activity tree must exist in a real session, not only in principle.

Every engine now emits, but for three rounds the emission was inert in the
product because nothing constructed an ``ActivityRegistry`` and nothing handed
one to ``JobManager`` — the "correct module, zero callers" failure, one level up
from where it was last caught. These tests pin the wiring itself, because a
registry nobody builds records nothing.
"""

from __future__ import annotations

import asyncio

from mantis_agent.activity.registry import ActivityRegistry
from mantis_agent.jobs import JobManager
from mantis_agent.tui import MantisTUI


def _tui() -> MantisTUI:
    return MantisTUI(model="mock", backend="mock", api_key=None, system=None,
                     max_tokens=100, temperature=None, max_turns=10)


def test_a_session_builds_an_activity_registry() -> None:
    tui = _tui()
    assert getattr(tui, "activity", None) is not None, "no registry on the session"
    assert isinstance(tui.activity, ActivityRegistry)


def test_the_job_manager_is_handed_that_registry() -> None:
    tui = _tui()
    assert tui._jobs.registry is tui.activity, "JobManager got a different tree"


def test_jobs_spawned_in_a_session_appear_in_the_tree() -> None:
    tui = _tui()

    async def go() -> None:
        async def work() -> str:
            return "ok"

        tui._jobs.spawn(work(), desc="pytest -q", kind="task")
        await asyncio.sleep(0.05)

    asyncio.run(go())
    titles = [n.title for n in tui.activity.nodes.values()]
    assert "pytest -q" in titles


def test_a_broken_registry_never_breaks_a_job() -> None:
    """The whole reason emission is safe to add to working engines."""

    class Exploding:
        def apply(self, *a, **k):
            raise RuntimeError("registry is on fire")

    async def go() -> str:
        jm = JobManager(registry=Exploding())

        async def work() -> str:
            return "done anyway"

        job = jm.spawn(work(), desc="x", kind="task")
        await asyncio.sleep(0.05)
        return job.status

    assert asyncio.run(go()) == "done"


# --------------------------------------------------------------------------
# turn parenting — what makes the tree a tree
# --------------------------------------------------------------------------


def test_work_spawned_in_a_turn_is_parented_to_that_turn() -> None:
    """Without this every job is a root and the 'tree' is a flat list.

    Parent links are the entire reason the activity graph exists: they are what
    let a workflow's agents, a task's subagent and the shell job it started read
    as one piece of work instead of three unrelated rows.
    """
    tui = _tui()

    async def go() -> None:
        tui._begin_activity_turn()

        async def work() -> str:
            return "ok"

        tui._jobs.spawn(work(), desc="pytest -q", kind="task")
        await asyncio.sleep(0.05)
        tui._end_activity_turn()

    asyncio.run(go())
    reg = tui.activity
    roots = [n for n in reg.nodes.values() if n.parent_id is None]
    assert len(roots) == 1 and roots[0].kind == "turn", \
        f"expected one turn root, got {[(n.id, n.kind) for n in roots]}"
    job = next(n for n in reg.nodes.values() if n.kind == "task")
    assert job.parent_id == roots[0].id


def test_a_turn_with_live_background_work_does_not_read_as_done() -> None:
    # Rollup: the displayed status comes from the children, while the turn's own
    # verdict is kept in `intrinsic_status` so neither is lost.
    tui = _tui()

    async def go() -> tuple:
        tui._begin_activity_turn()

        async def forever() -> str:
            await asyncio.sleep(30)
            return ""

        tui._jobs.spawn(forever(), desc="long build", kind="task")
        await asyncio.sleep(0.05)
        tui._end_activity_turn()
        await asyncio.sleep(0.02)
        turn = next(n for n in tui.activity.nodes.values() if n.kind == "turn")
        during = (turn.status, turn.intrinsic_status)
        tui._jobs.cancel_all()
        await asyncio.sleep(0.05)
        return during, tui.activity.node(turn.id).status

    (status, intrinsic), after = asyncio.run(go())
    assert status == "running", "turn claimed to be finished while work ran"
    assert intrinsic == "done", "the turn's own verdict was lost"
    assert after != "running", "turn never settled once its work ended"


def test_turn_helpers_are_safe_without_a_registry() -> None:
    tui = _tui()
    tui.activity = None
    tui._begin_activity_turn()          # must not raise
    tui._end_activity_turn()
    assert tui._jobs.activity_parent_id is None
