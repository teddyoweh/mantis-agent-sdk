"""Retention must trim the tree, not delete it.

The defect this pins: ``_evict_terminal`` walked ``self.nodes`` in creation
order, so the SESSION ROOT was the first candidate. A root is a rollup kind, so
once its children were all terminal it was terminal too with no live descendant
— and one ``_remove_subtree`` call took the whole session. Measured before the
fix, with default config: 201 nodes -> 0, and every node created afterwards was
parented to an id that no longer existed.

That is the difference between "the rail shows the last 200 results" and "the
rail goes blank the moment a session goes idle".
"""

from __future__ import annotations

from mantis_agent.activity.registry import ActivityRegistry

CAP = 200  # terminal_node_retention default


def _session() -> tuple[ActivityRegistry, str]:
    reg = ActivityRegistry(session_id="ses:01J8ABC")
    sid = reg.create_node(kind="session", local="01J8ABC", title="session",
                          parent_id=None)
    return reg, sid


def _finish(reg: ActivityRegistry, sid: str, n: int, start: int = 1) -> None:
    for i in range(start, start + n):
        nid = reg.create_node(kind="job", local=str(i), title=f"job {i}",
                              parent_id=sid)
        reg.set_status(nid, "done")


def test_the_session_root_is_never_evicted() -> None:
    reg, sid = _session()
    _finish(reg, sid, CAP + 60)
    assert reg.node(sid) is not None, "retention deleted the session root"


def test_retention_holds_at_the_cap_instead_of_emptying() -> None:
    reg, sid = _session()
    _finish(reg, sid, CAP + 60)
    # The old behaviour collapsed to 0 here.
    assert len(reg) > CAP // 2, f"tree collapsed to {len(reg)}"
    assert len(reg) <= CAP + 1  # +1 for the root itself


def test_the_newest_results_are_the_ones_kept() -> None:
    reg, sid = _session()
    _finish(reg, sid, CAP + 59)
    titles = [c.title for c in reg.children(sid)]
    assert f"job {CAP + 59}" in titles, "newest result was evicted"
    assert "job 1" not in titles, "oldest result was retained over the newest"


def test_live_work_is_never_evicted() -> None:
    reg, sid = _session()
    live = reg.create_node(kind="job", local="live", title="still running",
                           parent_id=sid)
    _finish(reg, sid, CAP + 120)
    assert reg.node(live) is not None, "a running job was evicted"


def test_a_finished_parent_with_a_live_child_survives() -> None:
    # The shape of a phase that has handed off: dropping it would orphan live work.
    reg, sid = _session()
    phase = reg.create_node(kind="job", local="phase", title="phase", parent_id=sid)
    child = reg.create_node(kind="job", local="child", title="child", parent_id=phase)
    reg.set_status(phase, "done")
    _finish(reg, sid, CAP + 60, start=1000)
    assert reg.node(phase) is not None
    assert reg.node(child) is not None


def test_nodes_created_after_eviction_still_have_a_parent() -> None:
    reg, sid = _session()
    _finish(reg, sid, CAP + 60)
    late = reg.create_node(kind="job", local="late", title="late", parent_id=sid)
    node = reg.node(late)
    assert node is not None
    assert node.parent_id == sid
    assert reg.node(node.parent_id) is not None, "parented to a vanished node"


# --------------------------------------------------------------------------
# a scoped id must name its scope
# --------------------------------------------------------------------------


def test_a_phase_id_names_its_run_even_for_a_long_run_id() -> None:
    """The scope half is machine-generated, so it must never be digested.

    A 36-char run id used to come back digested inside the phase
    (``b7d1f0a2-1c4e-af8b946f32``) while the run's own node kept the full value,
    so the two disagreed and nothing could get from a phase back to its run —
    which is the single property ``wfp:<run>/<phase>`` scoping exists for.
    Latent only because every real generator mints short base36 counters.
    """
    from mantis_agent.activity import ids

    run = "b7d1f0a2-1c4e-4d3a-9f77-8a2b6c5d4e3f"
    run_local = ids.parse_id(ids.make_id("workflow", run))[1]
    phase_scope = ids.parse_id(ids.make_id("phase", f"{run}/Review"))[1].split("/")[0]
    assert phase_scope == run_local


def test_an_unsafe_scope_is_still_normalized() -> None:
    import os.path

    from mantis_agent.activity import ids

    local = ids.parse_id(ids.make_id("phase", "../../../etc/cron.d/pwn/Review"))[1]
    assert ".." not in local.split("/")
    assert os.path.normpath("/root/" + local).startswith("/root/")


def test_a_long_scope_does_not_starve_the_name() -> None:
    from mantis_agent.activity import ids

    a = ids.make_id("phase", "x" * 70 + "/レビュー")
    b = ids.make_id("phase", "x" * 70 + "/审查代码")
    assert a != b, "two phases in one run collapsed onto the same id"
