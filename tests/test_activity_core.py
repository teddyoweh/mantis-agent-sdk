"""Activity graph core: ids, status vocabulary, event envelope, registry.

These four modules are pure — no I/O, no asyncio, no UI — so every bound and
every transition is directly assertable. The suite is organized the way the
plan's §16 lists them: identity, vocabulary, envelope, then the registry's
structure / bounds / isolation guarantees.
"""

from __future__ import annotations

import msgspec
import pytest

from mantis_agent.activity import events as A
from mantis_agent.activity import ids, status
from mantis_agent.activity.registry import (
    ACTIVITY_MAX,
    ActivityConfig,
    ActivityRegistry,
    TITLE_MAX,
    sanitize_text,
)

# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------


def test_every_kind_round_trips() -> None:
    for kind in ids.KINDS:
        nid = ids.make_id(kind, "x1")
        assert ids.parse_id(nid) == (kind, "x1")
        assert ids.kind_of(nid) == kind
        assert ids.local_of(nid) == "x1"
        assert ids.is_id(nid)


def test_prefix_table_is_bijective() -> None:
    assert len(ids.PREFIX_KINDS) == len(ids.KIND_PREFIXES)
    assert {len(p) for p in ids.KIND_PREFIXES.values()} == {3}


def test_documented_examples() -> None:
    # The exact ids §6 specifies.
    assert ids.make_id("job", 3) == "job:3"
    assert ids.make_id("workflow", "4f2a") == "wfr:4f2a"
    assert ids.make_id("phase", "4f2a/Review") == "wfp:4f2a/Review"
    assert ids.make_id("agent", "4f2a/a7") == "wfa:4f2a/a7"
    assert ids.make_id("subagent", 12) == "sub:12"
    assert ids.make_id("swarm", "refactor-auth") == "swm:refactor-auth"
    assert ids.make_id("candidate", "refactor-auth/2") == "cnd:refactor-auth/2"
    assert ids.make_id("schedule", "nightly-triage") == "cro:nightly-triage"
    assert ids.make_id("run", "nightly-triage/1722650400") == "run:nightly-triage/1722650400"


def test_local_part_is_normalized() -> None:
    # A phase is identified by its (model-authored) title.
    assert ids.make_id("phase", "4f2a/Review the code") == "wfp:4f2a/Review-the-code"
    assert ids.make_id("job", " 7 ") == "job:7"
    assert ids.make_id("job", "a\x1b[2Jb") == "job:a-2Jb"
    assert ids.make_id("job", "a::b") == "job:a-b"
    # Bounded, so a hostile title cannot produce an unbounded id.
    assert len(ids.local_of(ids.make_id("job", "z" * 500))) == 64


def test_make_id_rejects_unknown_kind_and_empty_local() -> None:
    with pytest.raises(ids.InvalidIdError):
        ids.make_id("nonesuch", 1)
    with pytest.raises(ids.InvalidIdError):
        ids.make_id("job", "")
    with pytest.raises(ids.InvalidIdError):
        ids.make_id("job", "   ")
    with pytest.raises(ids.InvalidIdError):
        ids.make_id("job", "///")
    # Also a ValueError, so existing `except ValueError` guards keep working.
    assert issubclass(ids.InvalidIdError, ValueError)
    assert issubclass(ids.InvalidIdError, ids.ActivityError)


@pytest.mark.parametrize(
    "bad",
    ["", "3", "job", "job:", ":3", "xyz:3", "job:a b", "job:a\x1bb", "JOB:3"],
)
def test_parse_id_rejects_malformed(bad: str) -> None:
    with pytest.raises(ids.InvalidIdError):
        ids.parse_id(bad)
    assert ids.try_parse_id(bad) is None
    assert not ids.is_id(bad)


def test_parse_id_rejects_non_string() -> None:
    with pytest.raises(ids.InvalidIdError):
        ids.parse_id(3)  # type: ignore[arg-type]


def test_ids_do_not_collide_across_engines() -> None:
    # The whole point of namespacing: Job.id and _RUN_COUNTER both start at 1.
    assert ids.make_id("job", 1) != ids.make_id("subagent", 1)


def _nodes() -> list:
    return [
        "job:3",
        "sub:3",
        "wfr:4f2a",
        "wfp:4f2a/Review",
        "wfa:4f2a/a7",
        "cro:nightly-triage",
    ]


def test_resolve_ref_exact_id_and_hash_shorthand() -> None:
    n = _nodes()
    assert ids.resolve_ref("job:3", n) == "job:3"
    assert ids.resolve_ref("  job:3 ", n) == "job:3"
    assert ids.resolve_ref("#job:3", n) == "job:3"


def test_resolve_ref_ambiguity_returns_none_but_refs_lists_both() -> None:
    n = _nodes()
    assert ids.resolve_ref("3", n) is None
    assert ids.resolve_refs("3", n) == ["job:3", "sub:3"]
    # A kind hint is exactly how /job 3 disambiguates.
    assert ids.resolve_ref("3", n, kind="job") == "job:3"
    assert ids.resolve_ref("#3", n, kind="subagent") == "sub:3"


def test_resolve_ref_local_last_segment_and_prefix_tiers() -> None:
    n = _nodes()
    assert ids.resolve_ref("4f2a", n) == "wfr:4f2a"           # exact local
    assert ids.resolve_ref("Review", n) == "wfp:4f2a/Review"  # last segment
    assert ids.resolve_ref("review", n) == "wfp:4f2a/Review"  # case-insensitive
    assert ids.resolve_ref("nightly", n) == "cro:nightly-triage"  # prefix
    assert ids.resolve_ref("nope", n) is None
    assert ids.resolve_ref("", n) is None
    assert ids.resolve_refs("nope", n) == []


def test_resolve_ref_ignores_unparseable_entries() -> None:
    assert ids.resolve_ref("3", ["job:3", "garbage"]) == "job:3"


def test_resolve_ref_accepts_a_registry_nodes_mapping() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("job", 3, title="pytest")
    # Iterating a dict yields its keys, so the live map works as-is.
    assert ids.resolve_ref("3", reg.nodes) == nid


# ---------------------------------------------------------------------------
# status vocabulary + mappers
# ---------------------------------------------------------------------------


def test_vocabulary_partitions_cleanly() -> None:
    assert len(status.STATUSES) == 9
    assert status.TERMINAL | status.ACTIVE == status.STATUS_SET
    assert not (status.TERMINAL & status.ACTIVE)
    assert status.HARD_TERMINAL <= status.TERMINAL
    for s in status.STATUSES:
        assert status.is_terminal(s) is not status.is_active(s)


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("running", status.RUNNING),
        ("done", status.DONE),
        ("error", status.ERROR),
        ("cancelled", status.CANCELLED),
        ("timeout", status.TIMEOUT),
    ],
)
def test_from_job_status_covers_the_whole_job_vocabulary(raw: str, expect: str) -> None:
    assert status.from_job_status(raw) == expect


@pytest.mark.parametrize("bad", ["", "queued", "weird", None, 7, object()])
def test_from_job_status_unknown_falls_back_to_pending(bad: object) -> None:
    # Unknown means "we do not know", and pending is the answer that cannot
    # make a parent claim completion it can't vouch for.
    assert status.from_job_status(bad) == status.PENDING


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("queued", status.PENDING),
        ("running", status.RUNNING),
        ("done", status.DONE),
        ("error", status.ERROR),
        ("cancelled", status.CANCELLED),
    ],
)
def test_from_workflow_status_covers_the_engine_vocabulary(raw, expect) -> None:
    assert status.from_workflow_status(raw) == expect
    assert status.from_phase_status(raw) == expect
    assert status.from_agent_run(raw) == expect


def test_from_workflow_status_unknown_and_paused() -> None:
    assert status.from_workflow_status("nonsense") == status.PENDING
    assert status.from_phase_status(None) == status.PENDING
    assert status.from_workflow_status("running", paused=True) == status.PAUSED
    # Pausing a finished run does not un-finish it.
    assert status.from_workflow_status("done", paused=True) == status.DONE


class _FakeAgentRun:
    """Duck type of ``workflow.AgentRun`` — status plus the private pause flag."""

    def __init__(self, st: str, paused: bool = False) -> None:
        self.status = st
        self._paused = paused


def test_from_agent_run_reads_the_object() -> None:
    assert status.from_agent_run(_FakeAgentRun("running")) == status.RUNNING
    assert status.from_agent_run(_FakeAgentRun("running", True)) == status.PAUSED
    assert status.from_agent_run(_FakeAgentRun("queued")) == status.PENDING
    assert status.from_agent_run(_FakeAgentRun("bogus")) == status.PENDING
    assert status.from_agent_run(object()) == status.PENDING
    # An override beats the object's own flag.
    assert status.from_agent_run(_FakeAgentRun("running", True), paused=False) == (
        status.RUNNING
    )
    # A skip is recorded by the engine as `cancelled` + membership in _skip;
    # the set is the only place the distinction lives.
    assert status.from_agent_run(_FakeAgentRun("cancelled"), skipped=True) == (
        status.SKIPPED
    )


def test_normalize_default_is_overridable() -> None:
    assert status.normalize("running") == status.RUNNING
    assert status.normalize("nope") == status.PENDING
    assert status.normalize("nope", status.ERROR) == status.ERROR


# ---------------------------------------------------------------------------
# rollup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "children,expect",
    [
        ([], status.PENDING),
        ([status.PENDING], status.PENDING),
        ([status.PENDING, status.PENDING], status.PENDING),
        ([status.RUNNING], status.RUNNING),
        ([status.DONE, status.RUNNING, status.ERROR], status.RUNNING),
        ([status.BLOCKED, status.PENDING], status.BLOCKED),
        ([status.BLOCKED, status.ERROR], status.BLOCKED),
        ([status.PAUSED, status.PENDING], status.PAUSED),
        ([status.PAUSED, status.BLOCKED], status.BLOCKED),
        ([status.ERROR, status.PENDING], status.ERROR),
        ([status.ERROR, status.DONE], status.ERROR),
        ([status.TIMEOUT, status.DONE], status.ERROR),
        ([status.DONE], status.DONE),
        ([status.DONE, status.CANCELLED], status.DONE),
        ([status.DONE, status.SKIPPED, status.CANCELLED], status.DONE),
        ([status.DONE, status.PENDING], status.RUNNING),
        ([status.SKIPPED, status.PENDING], status.RUNNING),
        (["bogus"], status.PENDING),
        (["bogus", status.DONE], status.RUNNING),
    ],
)
def test_roll_up_ladder(children, expect) -> None:
    assert status.roll_up(children) == expect


@pytest.mark.parametrize(
    "agent_states",
    [
        ["queued"],
        ["queued", "queued"],
        ["running", "queued"],
        ["running", "done"],
        ["error", "done"],
        ["error", "queued"],
        ["done", "cancelled"],
        ["done"],
        ["done", "queued"],
        ["cancelled"],
    ],
)
def test_roll_up_matches_the_existing_phase_roll_up(agent_states) -> None:
    """Parity with ``workflow.Phase.roll_up``, the implementation this
    generalizes. The only translation is the vocabulary itself."""

    from mantis_agent.workflow import AgentRun, Phase

    phase = Phase(title="p")
    for i, st in enumerate(agent_states):
        phase.agents.append(AgentRun(id="a%d" % i, label="l", status=st))
    phase.roll_up()
    expected = status.from_phase_status(phase.status)
    assert status.roll_up([status.from_agent_run(a) for a in phase.agents]) == expected


def test_resolve_status_containers_versus_engine_owned() -> None:
    kids = [status.RUNNING, status.DONE]
    # A phase has no intrinsic state of its own.
    assert status.resolve_status("phase", status.PENDING, kids) == status.RUNNING
    # A job reports what its engine says, children or not.
    assert status.resolve_status("job", status.DONE, kids) == status.DONE
    assert status.resolve_status("agent", status.RUNNING, []) == status.RUNNING
    # An empty container falls back to its own value.
    assert status.resolve_status("phase", status.PENDING, []) == status.PENDING
    # A hard verdict on a container is never undone by its children.
    assert status.resolve_status("phase", status.CANCELLED, kids) == status.CANCELLED
    assert status.resolve_status("swarm", status.SKIPPED, kids) == status.SKIPPED
    # ... but a soft one is.
    assert status.resolve_status("phase", status.DONE, kids) == status.RUNNING
    assert status.resolve_status("phase", "bogus", []) == status.PENDING


def test_rollup_kinds_exclude_engine_owned_kinds() -> None:
    assert "job" not in status.ROLLUP_KINDS
    assert "agent" not in status.ROLLUP_KINDS
    assert {"phase", "swarm", "team"} <= status.ROLLUP_KINDS


# ---------------------------------------------------------------------------
# event envelope
# ---------------------------------------------------------------------------


def _one_of_each() -> list:
    return [
        A.NodeCreated(
            seq=1,
            ts=1700000000.5,
            node_id="wfa:4f2a/a7",
            parent_id="wfp:4f2a/Review",
            kind="agent",
            title="review:bugs",
            detail="scan for unchecked tool results",
            source="model",
            model="claude-opus-5",
            provider="anthropic",
            isolation="worktree",
            effort="high",
            permission_mode="acceptEdits",
            transcript_ref="/tmp/t.jsonl",
            actions=("stop", "message"),
        ),
        A.NodeStatus(seq=2, ts=2.0, node_id="job:3", status="error", error="boom"),
        A.NodeActivity(seq=3, ts=3.0, node_id="job:3", text="reading agent.py"),
        A.NodeUsage(
            seq=4,
            ts=4.0,
            node_id="job:3",
            input_tokens=12,
            output_tokens=3,
            cache_read_tokens=1,
            cost_usd=0.14,
        ),
        A.NodeAction(seq=5, ts=5.0, node_id="job:3", action="stop", actor="user"),
    ]


@pytest.mark.parametrize("ev", _one_of_each())
def test_envelope_round_trips_through_the_tagged_union(ev) -> None:
    raw = A.encode_event(ev)
    back = A.decode_event(raw)
    assert back == ev
    assert type(back) is type(ev)
    # The discriminator is where events.py puts it.
    assert msgspec.json.decode(raw)["type"] == ev.__struct_config__.tag


def test_envelope_omits_defaults_and_is_frozen() -> None:
    ev = A.NodeStatus(seq=1, ts=1.0, node_id="job:3", status="done")
    payload = msgspec.json.decode(A.encode_event(ev))
    assert "error" not in payload  # omit_defaults keeps journal lines small
    with pytest.raises(AttributeError):
        ev.status = "error"  # type: ignore[misc]


def test_union_decode_dispatches_on_the_tag() -> None:
    for ev in _one_of_each():
        assert isinstance(A.decode_event(A.encode_event(ev)), type(ev))


# ---------------------------------------------------------------------------
# registry — sanitization
# ---------------------------------------------------------------------------


def test_sanitize_neutralizes_terminal_control() -> None:
    assert sanitize_text("\x1b[2J\x1b[Hwiped") == "wiped"
    assert sanitize_text("\x1b]0;title\x07x") == "x"
    assert sanitize_text("a\x00b\x7fc") == "a b c"
    assert sanitize_text("one\ntwo\r\nthree\tfour") == "one two three four"
    assert sanitize_text("a‮b​c﻿") == "abc"
    assert sanitize_text(None) == ""
    assert sanitize_text("") == ""
    assert sanitize_text(17) == "17"


def test_sanitize_truncates() -> None:
    out = sanitize_text("z" * 500, 10)
    assert len(out) == 10 and out.endswith("…")
    assert sanitize_text("z" * 500, 0) == "z" * 500  # 0 disables the cap


def test_registry_sanitizes_on_ingest_not_on_render() -> None:
    reg = ActivityRegistry()
    seen: list = []
    reg.subscribe(seen.append)
    nid = reg.create_node("job", 1, title="\x1b[2Jrail\nforgery", detail="d\x00d")
    reg.set_status(nid, "error", error="\x1b[31mboom")
    reg.add_activity(nid, "x" * (ACTIVITY_MAX + 50))
    node = reg.node(nid)
    assert node.title == "rail forgery"
    assert node.detail == "d d"
    assert node.error == "boom"
    assert len(node.activity) == ACTIVITY_MAX
    # Subscribers never observe the hostile form either.
    assert seen[0].title == "rail forgery"
    assert seen[1].error == "boom"
    long_title = reg.create_node("job", 2, title="t" * 300)
    assert len(reg.node(long_title).title) == TITLE_MAX


# ---------------------------------------------------------------------------
# registry — sequencing and time
# ---------------------------------------------------------------------------


def test_seq_is_assigned_by_the_registry_and_is_monotonic() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("job", 1, title="a")
    evs = [reg.set_status(nid, "running"), reg.add_activity(nid, "x")]
    assert [e.seq for e in evs] == [2, 3]
    assert reg.last_seq == 3
    assert reg.node(nid).created_seq == 1


def test_replayed_seq_is_preserved_and_advances_the_counter() -> None:
    reg = ActivityRegistry()
    ev = reg.apply(
        A.NodeCreated(seq=99, ts=5.0, node_id="job:1", parent_id=None, kind="job", title="t")
    )
    assert ev.seq == 99 and ev.ts == 5.0
    assert reg.last_seq == 99
    assert reg.set_status("job:1", "done").seq == 100


def test_timestamps_default_to_wall_clock_and_normalize_monotonic() -> None:
    wall = [1000.0]
    mono = [10.0]
    reg = ActivityRegistry(clock=lambda: wall[0], monotonic=lambda: mono[0])
    assert reg.origin == (10.0, 1000.0)
    nid = reg.create_node("job", 1, title="a")
    assert reg.node(nid).created_at == 1000.0
    # A job's `started` is a monotonic reading; normalizing it against the
    # captured origin pair yields a stable wall-clock time.
    assert reg.to_wallclock(12.5) == 1002.5
    assert reg.to_wallclock_ms(12500.0) == 1002.5
    wall[0] = 1005.0
    reg.set_status(nid, "running")
    assert reg.node(nid).started_at == 1005.0
    wall[0] = 1009.0
    reg.set_status(nid, "done")
    node = reg.node(nid)
    assert node.ended_at == 1009.0
    assert node.elapsed_s(now=1_000_000.0) == 4.0


def test_explicit_ts_wins_over_the_clock() -> None:
    reg = ActivityRegistry(clock=lambda: 1.0)
    nid = reg.create_node("job", 1, title="a", ts=42.0)
    assert reg.node(nid).created_at == 42.0


# ---------------------------------------------------------------------------
# registry — structure
# ---------------------------------------------------------------------------


def test_tree_parents_children_and_traversal() -> None:
    reg = ActivityRegistry()
    ses = reg.create_node("session", "s1", title="session")
    ph = reg.create_node("phase", "4f2a/Review", title="Review", parent_id=ses)
    a1 = reg.create_node("agent", "4f2a/a1", title="bugs", parent_id=ph)
    a2 = reg.create_node("agent", "4f2a/a2", title="perf", parent_id=ph)
    job = reg.create_node("job", 3, title="pytest")

    assert [n.id for n in reg.roots()] == [ses, job]
    assert [n.id for n in reg.children(ph)] == [a1, a2]
    assert [n.id for n in reg.descendants(ses)] == [ph, a1, a2]
    assert [n.id for n in reg.ancestors(a1)] == [ph, ses]
    assert [(d, n.id) for d, n in reg.tree(ses)] == [(0, ses), (1, ph), (2, a1), (2, a2)]
    assert [n.id for n in reg.filter(lambda n: n.kind == "agent")] == [a1, a2]
    assert len(reg) == 5 and job in reg and "job:99" not in reg
    assert reg.tree("job:99") == []


def test_duplicate_creation_is_ignored() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("job", 1, title="first")
    reg.create_node("job", 1, title="second")
    assert reg.node(nid).title == "first"
    assert reg.stats["dropped_duplicate"] == 1
    assert len(reg) == 1


def test_self_parenting_is_refused_and_the_node_becomes_a_root() -> None:
    reg = ActivityRegistry()
    reg.apply(
        A.NodeCreated(
            seq=0, ts=0.0, node_id="job:1", parent_id="job:1", kind="job", title="t"
        )
    )
    assert reg.node("job:1").parent_id is None
    assert reg.stats["rejected_parent"] == 1


def test_a_dangling_parent_link_still_yields_a_reachable_root() -> None:
    reg = ActivityRegistry()
    kid = reg.create_node("agent", "4f2a/a1", title="a", parent_id="wfp:4f2a/Review")
    assert [n.id for n in reg.roots()] == [kid]
    # ... and the parent arriving late inherits the child for free.
    ph = reg.create_node("phase", "4f2a/Review", title="Review")
    assert [n.id for n in reg.children(ph)] == [kid]
    assert [n.id for n in reg.roots()] == [ph]


def test_unknown_status_is_coerced_to_pending_and_counted() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("job", 1, title="a")
    ev = reg.set_status(nid, "frobnicated")
    assert ev.status == status.PENDING
    assert reg.node(nid).status == status.PENDING
    assert reg.stats["unknown_status"] == 1


# ---------------------------------------------------------------------------
# registry — rollup on the ancestor chain
# ---------------------------------------------------------------------------


def test_status_rolls_up_the_whole_ancestor_chain() -> None:
    reg = ActivityRegistry()
    ses = reg.create_node("session", "s1", title="session")
    ph = reg.create_node("phase", "4f2a/Review", title="Review", parent_id=ses)
    a1 = reg.create_node("agent", "4f2a/a1", title="bugs", parent_id=ph)
    a2 = reg.create_node("agent", "4f2a/a2", title="perf", parent_id=ph)

    assert reg.node(ph).status == status.PENDING
    reg.set_status(a1, status.RUNNING)
    assert reg.node(ph).status == status.RUNNING
    assert reg.node(ses).status == status.RUNNING

    reg.set_status(a1, status.DONE)
    assert reg.node(ph).status == status.RUNNING  # a2 still pending
    reg.set_status(a2, status.BLOCKED)
    assert reg.node(ph).status == status.BLOCKED
    assert reg.node(ses).status == status.BLOCKED

    reg.set_status(a2, status.ERROR, error="nope")
    assert reg.node(ph).status == status.ERROR
    assert reg.node(ses).status == status.ERROR
    # The agents keep their own truth; only the containers roll up.
    assert reg.node(a1).status == status.DONE
    assert reg.node(a2).intrinsic_status == status.ERROR


def test_a_container_keeps_its_own_hard_verdict() -> None:
    reg = ActivityRegistry()
    ph = reg.create_node("phase", "4f2a/Review", title="Review")
    a1 = reg.create_node("agent", "4f2a/a1", title="bugs", parent_id=ph)
    reg.set_status(ph, status.CANCELLED)
    reg.set_status(a1, status.DONE)
    assert reg.node(ph).status == status.CANCELLED


def test_a_terminal_node_that_restarts_clears_its_end() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("agent", "4f2a/a1", title="a")
    reg.set_status(nid, status.RUNNING, ts=1.0)
    reg.set_status(nid, status.ERROR, ts=2.0)
    assert reg.node(nid).ended_at == 2.0
    reg.set_status(nid, status.PENDING, ts=3.0)  # retry_agent resets to queued
    assert reg.node(nid).ended_at is None


def test_counts_are_maintained_incrementally() -> None:
    reg = ActivityRegistry()
    a = reg.create_node("job", 1, title="a")
    b = reg.create_node("job", 2, title="b")
    reg.set_status(a, status.RUNNING)
    reg.set_status(b, status.DONE)
    counts = reg.counts()
    assert set(counts) == set(status.STATUSES)  # every key present, zeros included
    assert counts[status.RUNNING] == 1 and counts[status.DONE] == 1
    assert counts[status.PENDING] == 0
    assert [n.id for n in reg.active()] == [a]
    # The incremental counts must agree with a full walk at all times.
    walked: dict = {s: 0 for s in status.STATUSES}
    for n in reg.nodes.values():
        walked[n.status] += 1
    assert walked == counts


# ---------------------------------------------------------------------------
# registry — bounds
# ---------------------------------------------------------------------------


def test_recent_ring_is_bounded() -> None:
    reg = ActivityRegistry(config=ActivityConfig(max_recent_per_node=3))
    nid = reg.create_node("job", 1, title="a")
    for i in range(10):
        reg.add_activity(nid, "line %d" % i)
    node = reg.node(nid)
    assert node.recent == ("line 7", "line 8", "line 9")
    assert node.activity == "line 9"


def test_activity_beyond_the_rate_budget_is_coalesced() -> None:
    # A frozen monotonic clock puts every event in one window.
    reg = ActivityRegistry(
        config=ActivityConfig(max_events_per_second_per_node=3, max_recent_per_node=8),
        monotonic=lambda: 100.0,
    )
    nid = reg.create_node("job", 1, title="a")
    seen: list = []
    reg.subscribe(seen.append)
    applied = [reg.add_activity(nid, "l%d" % i) for i in range(10)]
    assert sum(1 for a in applied if a is not None) == 3
    assert reg.stats["coalesced"] == 7
    assert len(seen) == 3
    # The freshest line still wins: the rail must never look stuck.
    assert reg.node(nid).activity == "l9"
    assert reg.node(nid).recent == ("l0", "l1", "l2")


def test_the_rate_window_rolls_over() -> None:
    now = [100.0]
    reg = ActivityRegistry(
        config=ActivityConfig(max_events_per_second_per_node=2),
        monotonic=lambda: now[0],
    )
    nid = reg.create_node("job", 1, title="a")
    assert [reg.add_activity(nid, "a%d" % i) is not None for i in range(4)] == [
        True,
        True,
        False,
        False,
    ]
    now[0] = 101.5
    assert reg.add_activity(nid, "b") is not None


def test_live_node_limit_drops_creations_it_cannot_seat() -> None:
    reg = ActivityRegistry(config=ActivityConfig(max_live_nodes=3))
    made = [reg.create_node("job", i, title="j%d" % i) for i in range(3)]
    for nid in made:
        reg.set_status(nid, status.RUNNING)  # nothing is evictable
    reg.create_node("job", 99, title="overflow")
    assert len(reg) == 3
    assert reg.stats["dropped_node_limit"] == 1
    assert reg.node("job:99") is None


def test_a_terminal_node_is_evicted_to_seat_a_new_one() -> None:
    reg = ActivityRegistry(config=ActivityConfig(max_live_nodes=2))
    old = reg.create_node("job", 1, title="old")
    live = reg.create_node("job", 2, title="live")
    reg.set_status(live, status.RUNNING)
    reg.set_status(old, status.DONE)
    new = reg.create_node("job", 3, title="new")
    assert reg.node(old) is None
    assert reg.node(live) is not None and reg.node(new) is not None
    assert reg.stats["evicted"] == 1


def test_terminal_retention_prunes_oldest_first() -> None:
    reg = ActivityRegistry(config=ActivityConfig(terminal_node_retention=2))
    made = [reg.create_node("job", i, title="j%d" % i) for i in range(5)]
    for nid in made:
        reg.set_status(nid, status.DONE)
    assert [n.id for n in reg.nodes.values()] == made[-2:]
    assert reg.stats["evicted"] == 3
    assert reg.counts()[status.DONE] == 2


def test_retention_never_evicts_a_subtree_with_live_work() -> None:
    reg = ActivityRegistry(config=ActivityConfig(terminal_node_retention=1))
    parent = reg.create_node("phase", "4f2a/Review", title="Review")
    child = reg.create_node("agent", "4f2a/a1", title="bugs", parent_id=parent)
    reg.set_status(parent, status.CANCELLED)  # terminal parent ...
    reg.set_status(child, status.RUNNING)     # ... with live work under it
    for i in range(4):
        nid = reg.create_node("job", i, title="j%d" % i)
        reg.set_status(nid, status.DONE)
    assert reg.node(parent) is not None
    assert reg.node(child) is not None
    # Evicting a parent takes its whole (fully terminal) subtree with it.
    reg.set_status(child, status.DONE)
    nid = reg.create_node("job", 99, title="last")
    reg.set_status(nid, status.DONE)
    assert reg.node(parent) is None and reg.node(child) is None


def test_eviction_keeps_counts_and_child_lists_consistent() -> None:
    reg = ActivityRegistry(config=ActivityConfig(terminal_node_retention=1))
    root = reg.create_node("session", "s1", title="s")
    kid = reg.create_node("job", 1, title="k", parent_id=root)
    reg.set_status(kid, status.DONE)
    other = reg.create_node("job", 2, title="o")
    reg.set_status(other, status.DONE)
    assert reg.node(kid) is None
    assert reg.children(root) == []
    walked: dict = {s: 0 for s in status.STATUSES}
    for n in reg.nodes.values():
        walked[n.status] += 1
    assert walked == reg.counts()


# ---------------------------------------------------------------------------
# registry — orphans
# ---------------------------------------------------------------------------


def test_orphan_events_attach_when_the_node_arrives() -> None:
    reg = ActivityRegistry()
    seen: list = []
    reg.subscribe(seen.append)
    assert reg.set_status("job:7", status.RUNNING) is None
    assert reg.add_activity("job:7", "reading") is None
    assert reg.add_usage("job:7", input_tokens=5, cost_usd=0.5) is None
    assert seen == []  # nothing effective, so nothing fanned out
    assert reg.stats["orphans_buffered"] == 3

    reg.create_node("job", 7, title="pytest")
    node = reg.node("job:7")
    assert node.status == status.RUNNING
    assert node.recent == ("reading",)
    assert node.usage.input_tokens == 5 and node.cost_usd == 0.5
    assert reg.stats["orphans_attached"] == 3
    # Creation first, then the buffered events in seq order.
    assert [type(e).__name__ for e in seen] == [
        "NodeCreated",
        "NodeStatus",
        "NodeActivity",
        "NodeUsage",
    ]


def test_orphan_buffer_overflow_drops_oldest_first() -> None:
    reg = ActivityRegistry(config=ActivityConfig(orphan_buffer_size=3))
    for i in range(5):
        reg.add_activity("job:1", "old %d" % i)
    reg.add_activity("job:2", "new")
    reg.create_node("job", 1, title="a")
    reg.create_node("job", 2, title="b")
    # Only the newest three survived, and they are the ones that attached.
    assert reg.stats["dropped_orphan"] == 3
    assert reg.node("job:1").recent == ("old 3", "old 4")
    assert reg.node("job:2").recent == ("new",)


def test_orphans_past_their_ttl_are_dropped() -> None:
    reg = ActivityRegistry(config=ActivityConfig(orphan_ttl_s=10.0))
    reg.add_activity("job:1", "stale", ts=1000.0)
    reg.add_activity("job:1", "fresh", ts=1020.0)
    reg.create_node("job", 1, title="a")
    assert reg.node("job:1").recent == ("fresh",)
    assert reg.stats["dropped_orphan"] == 1


def test_orphans_are_released_when_their_node_is_evicted() -> None:
    reg = ActivityRegistry(config=ActivityConfig(terminal_node_retention=0))
    nid = reg.create_node("job", 1, title="a")
    reg.set_status(nid, status.DONE)  # evicted immediately
    assert reg.node(nid) is None
    reg.add_activity(nid, "late")
    assert reg._orphan_count == 1


# ---------------------------------------------------------------------------
# registry — usage
# ---------------------------------------------------------------------------


def test_usage_accumulates_and_negative_deltas_are_clamped() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("agent", "4f2a/a1", title="bugs")
    reg.add_usage(nid, input_tokens=10, output_tokens=2, cache_read_tokens=1, cost_usd=0.1)
    reg.add_usage(nid, input_tokens=5, output_tokens=1, cost_usd=0.05)
    ev = reg.add_usage(nid, input_tokens=-100, cost_usd=-9.0)
    assert ev.input_tokens == 0 and ev.cost_usd == 0.0
    node = reg.node(nid)
    assert node.usage.input_tokens == 15
    assert node.usage.output_tokens == 3
    assert node.usage.cache_read_input_tokens == 1
    assert abs(node.cost_usd - 0.15) < 1e-9


def test_node_to_dict_is_plain_data() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node(
        "agent", "4f2a/a1", title="bugs", model="claude-opus-5", actions=["stop", "message"]
    )
    reg.add_usage(nid, input_tokens=3)
    reg.set_status(nid, status.RUNNING, ts=5.0)
    d = reg.node(nid).to_dict()
    assert d["id"] == nid and d["model"] == "claude-opus-5"
    assert d["actions"] == ["message", "stop"]
    assert d["usage"]["input_tokens"] == 3
    assert d["started_at"] == 5.0
    assert msgspec.json.decode(msgspec.json.encode(d))["status"] == status.RUNNING


# ---------------------------------------------------------------------------
# registry — actions
# ---------------------------------------------------------------------------


def test_actions_are_recorded_without_mutating_the_node() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("job", 1, title="a")
    reg.set_status(nid, status.RUNNING)
    ev = reg.record_action(nid, "stop", "user", detail="ctrl-c")
    assert ev is not None and ev.action == "stop" and ev.actor == "user"
    # The action explains the transition; the NodeStatus performs it.
    assert reg.node(nid).status == status.RUNNING
    assert reg.record_action("job:404", "stop", "user") is None  # buffered as orphan


# ---------------------------------------------------------------------------
# registry — subscriber isolation
# ---------------------------------------------------------------------------


def test_subscribers_see_applied_events_and_can_unsubscribe() -> None:
    reg = ActivityRegistry()
    seen: list = []
    off = reg.subscribe(seen.append)
    nid = reg.create_node("job", 1, title="a")
    reg.set_status(nid, status.DONE)
    assert len(seen) == 2
    off()
    reg.add_activity(nid, "later")
    assert len(seen) == 2
    assert reg.subscriber_count == 0
    off()  # idempotent


def test_a_protocol_style_subscriber_object_works() -> None:
    class Sub:
        def __init__(self) -> None:
            self.seen: list = []

        def on_event(self, ev) -> None:
            self.seen.append(ev)

    reg = ActivityRegistry()
    sub = Sub()
    reg.subscribe(sub)
    reg.create_node("job", 1, title="a")
    assert len(sub.seen) == 1
    assert reg.unsubscribe(sub) is True
    assert reg.unsubscribe(sub) is False


def test_a_raising_subscriber_is_isolated_counted_then_detached() -> None:
    reg = ActivityRegistry(config=ActivityConfig(subscriber_error_threshold=2))
    calls = [0]
    good: list = []

    def broken(ev) -> None:
        calls[0] += 1
        raise RuntimeError("notifier is broken")

    reg.subscribe(broken)
    reg.subscribe(good.append)

    nid = reg.create_node("job", 1, title="a")   # error 1
    reg.set_status(nid, status.RUNNING)          # error 2 -> detached
    reg.add_activity(nid, "x")
    reg.set_status(nid, status.DONE)

    assert calls[0] == 2
    assert reg.stats["subscriber_errors"] == 2
    assert reg.stats["subscriber_detached"] == 1
    assert reg.subscriber_count == 1
    # The healthy subscriber and the tree itself are untouched.
    assert len(good) == 4
    assert reg.node(nid).status == status.DONE


def test_an_async_subscriber_is_rejected_rather_than_leaked() -> None:
    reg = ActivityRegistry(config=ActivityConfig(subscriber_error_threshold=1))

    async def slow(ev) -> None:  # pragma: no cover - never awaited by design
        return None

    reg.subscribe(slow)
    reg.create_node("job", 1, title="a")
    assert reg.stats["subscriber_errors"] == 1
    assert reg.subscriber_count == 0


def test_subscribe_rejects_a_non_callable() -> None:
    reg = ActivityRegistry()
    with pytest.raises(TypeError):
        reg.subscribe(object())


def test_a_registry_with_no_subscribers_still_records_everything() -> None:
    reg = ActivityRegistry()
    nid = reg.create_node("job", 1, title="a")
    reg.set_status(nid, status.RUNNING)
    assert reg.stats["applied"] == 2 and reg.subscriber_count == 0


def test_bookkeeping_survives_a_randomized_event_storm() -> None:
    """Seeded fuzz over every mutation at once.

    Eviction, rollup and the incremental counters all touch the same maps; this
    asserts the invariants that let the rail trust ``counts()`` without walking
    the tree, under a stream that hits every bound simultaneously.
    """

    import random

    rng = random.Random(7)
    reg = ActivityRegistry(
        config=ActivityConfig(
            max_live_nodes=60, terminal_node_retention=15, orphan_buffer_size=16
        )
    )
    known: list = []
    for i in range(2000):
        roll = rng.random()
        if roll < 0.3 or not known:
            parent = rng.choice(known) if known and rng.random() < 0.6 else None
            kind = rng.choice(["session", "phase", "job", "agent", "swarm"])
            known.append(
                reg.create_node(kind, "%s-%d" % (kind, i), title="t%d" % i, parent_id=parent)
            )
            known = known[-120:]
        elif roll < 0.65:
            reg.set_status(rng.choice(known), rng.choice(list(status.STATUSES)))
        elif roll < 0.9:
            reg.add_activity(rng.choice(known), "line %d" % i)
        else:
            reg.add_usage(rng.choice(known), input_tokens=1, cost_usd=0.001)

    walked: dict = {s: 0 for s in status.STATUSES}
    for n in reg.nodes.values():
        walked[n.status] += 1
    assert walked == reg.counts()
    assert len(reg) <= 60
    assert reg._orphan_count <= 16
    assert reg._orphan_count == sum(len(v) for v in reg._orphans.values())
    # No child list may point at an evicted node, and traversal must terminate.
    for nid in list(reg.nodes):
        assert all(c.id in reg.nodes for c in reg.children(nid))
        assert len(reg.node(nid).recent) <= reg.config.max_recent_per_node
    assert len(reg.tree()) == len(reg)


def test_two_registries_are_isolated() -> None:
    a = ActivityRegistry(session_id="a")
    b = ActivityRegistry(session_id="b")
    a.create_node("job", 1, title="a")
    assert len(a) == 1 and len(b) == 0
    assert a.last_seq == 1 and b.last_seq == 0
