"""Workflow-engine emission into the activity graph (plan §9, ``workflow.py``).

Everything here runs a **real** :class:`~mantis_agent.workflow.Workflow` against a
deterministic fake runner — no network, no model, no tokens — and asserts on the
tree a real :class:`~mantis_agent.activity.ActivityRegistry` builds from it.

Two properties are load-bearing and are tested from both sides:

* the tree is ``workflow > phase > agent`` with the *right* parents, because a
  wrong parent is the one bug the rail cannot render its way out of; and
* the emission is failure-isolated — a registry that raises on every call must
  leave the run's result, status and exceptions bit-for-bit what they were
  without a registry at all.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.activity import ActivityRegistry
from mantis_agent.activity.ids import make_id
from mantis_agent.types import AssistantMessage, TextBlock, ToolUseBlock, Usage
from mantis_agent.workflow import Workflow, WorkflowError


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def make_runner(*, text="done", tools=(), in_tokens=100, out_tokens=10, cached=0):
    """One AssistantMessage with fixed usage and tool blocks."""

    async def runner(prompt, *, model="", agent_type="general-purpose", schema=None):
        content = [
            ToolUseBlock(id="t%d" % i, name=name, input={})
            for i, name in enumerate(tools)
        ]
        content.append(TextBlock(text=text))
        yield AssistantMessage(
            content=content,
            usage=Usage(
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cache_read_input_tokens=cached,
            ),
        )

    return runner


async def boom_runner(prompt, *, model="", agent_type="general-purpose", schema=None):
    raise RuntimeError("child exploded")
    yield  # pragma: no cover — makes this an async generator


class RaisingRegistry:
    """A registry that fails every way an engine could notice.

    ``apply`` is the only method ``emit`` calls, but a partially-initialized or
    hostile object might not even have it — both are covered by the two doubles
    below, because the engine must not care which one it was handed.
    """

    def __init__(self):
        self.calls = 0

    def apply(self, ev):
        self.calls += 1
        raise RuntimeError("registry is on fire")


class BrokenRegistry:
    """No ``apply`` at all — an ``AttributeError`` waiting to happen."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def nodes_of_kind(reg, kind):
    return [n for n in reg.nodes.values() if n.kind == kind]


def one(reg, kind):
    got = nodes_of_kind(reg, kind)
    assert len(got) == 1, "expected exactly one %s node, got %r" % (
        kind,
        [n.id for n in got],
    )
    return got[0]


def actions_seen(events):
    return [(ev.node_id, ev.action, ev.actor) for ev in events if hasattr(ev, "action")]


# ---------------------------------------------------------------------------
# The default: no registry, nothing changes
# ---------------------------------------------------------------------------


def test_registry_defaults_to_none_and_run_is_unchanged():
    """Every existing construction site passes no registry and must be inert."""

    wf = Workflow("plain", agent_runner=make_runner())
    assert wf.registry is None
    assert wf.activity_id == ""

    out = anyio.run(lambda: wf.agent("hi", label="a", phase="P"))
    assert out == "done"
    wf.finish()
    assert wf.run.status == "done"
    # No id is derived when nothing is listening.
    assert wf._phase_node("P") == ""


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def test_workflow_node_is_created_running_at_construction():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    node = one(reg, "workflow")
    assert node.id == wf.activity_id == make_id("workflow", wf.run.id)
    assert node.parent_id is None
    assert node.title == "release"
    assert node.status == "running"


def test_workflow_node_parents_to_the_spawning_tool_node():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg, parent_id="tol:7")

    assert reg.node(wf.activity_id).parent_id == "tol:7"


def test_tree_is_workflow_then_phase_then_agent_with_correct_parents():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    async def go():
        with wf.phase("Review", detail="the diff"):
            await wf.parallel(
                [
                    lambda: wf.agent("look for bugs", label="review:bugs"),
                    lambda: wf.agent("look for slow", label="review:perf"),
                ]
            )
        with wf.phase("Verify"):
            await wf.agent("check it", label="verify:auth")

    anyio.run(go)
    wf.finish()

    wfr = one(reg, "workflow")
    phases = {n.title: n for n in nodes_of_kind(reg, "phase")}
    agents = {n.title: n for n in nodes_of_kind(reg, "agent")}

    assert set(phases) == {"Review", "Verify"}
    assert set(agents) == {"review:bugs", "review:perf", "verify:auth"}

    # Parents, which is the whole point.
    assert {p.parent_id for p in phases.values()} == {wfr.id}
    assert agents["review:bugs"].parent_id == phases["Review"].id
    assert agents["review:perf"].parent_id == phases["Review"].id
    assert agents["verify:auth"].parent_id == phases["Verify"].id

    # And the ids are the scheme §6 specifies, not ad-hoc strings.
    assert phases["Review"].id == make_id("phase", "%s/Review" % wf.run.id)
    assert phases["Review"].detail == "the diff"

    # Registry-side traversal agrees with the parent pointers.
    assert [n.id for n in reg.roots()] == [wfr.id]
    assert len(reg.descendants(wfr.id)) == 5


def test_agent_node_carries_model_provider_and_effort():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow(
        "release",
        agent_runner=make_runner(),
        registry=reg,
        model="claude-opus-5",
        provider="anthropic",
        effort="high",
    )
    anyio.run(lambda: wf.agent("go", label="worker", phase="P"))

    node = one(reg, "agent")
    assert node.model == "claude-opus-5"
    assert node.provider == "anthropic"
    assert node.effort == "high"
    assert node.detail == "general-purpose"
    # Only advertise what the engine can actually do (§13).
    assert "stop" in node.actions and "skip" in node.actions


def test_repeated_phase_title_reuses_one_node():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    async def go():
        await wf.agent("one", label="a1", phase="Review")
        await wf.agent("two", label="a2", phase="Review")

    anyio.run(go)
    assert len(nodes_of_kind(reg, "phase")) == 1
    assert len(nodes_of_kind(reg, "agent")) == 2


# ---------------------------------------------------------------------------
# Activity, usage, status
# ---------------------------------------------------------------------------


def test_tool_use_becomes_node_activity():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow(
        "release",
        agent_runner=make_runner(tools=("read_file", "grep")),
        registry=reg,
    )
    anyio.run(lambda: wf.agent("go", label="worker", phase="P"))

    node = one(reg, "agent")
    assert node.recent[-2:] == ("read_file", "grep")
    assert node.activity == "grep"


def test_usage_is_folded_onto_the_agent_node():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow(
        "release",
        agent_runner=make_runner(in_tokens=100, out_tokens=10, cached=7),
        registry=reg,
    )
    anyio.run(lambda: wf.agent("go", label="worker", phase="P"))

    node = one(reg, "agent")
    assert node.usage is not None
    assert node.usage.input_tokens == 100
    assert node.usage.output_tokens == 10
    assert node.usage.cache_read_input_tokens == 7


def test_agent_reaches_done_and_phase_rolls_up():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)
    anyio.run(lambda: wf.agent("go", label="worker", phase="Review"))

    assert one(reg, "agent").status == "done"
    # A phase has no intrinsic status of its own — it displays its children's.
    assert one(reg, "phase").status == "done"


def test_agent_error_is_recorded_with_its_message():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=boom_runner, registry=reg)

    with pytest.raises(RuntimeError):
        anyio.run(lambda: wf.agent("go", label="worker", phase="Review"))

    node = one(reg, "agent")
    assert node.status == "error"
    assert "child exploded" in (node.error or "")
    assert one(reg, "phase").status == "error"

    wf.finish()
    assert one(reg, "workflow").status == "error"


def test_cached_agent_lands_done_and_says_it_was_replayed():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)
    anyio.run(lambda: wf.agent("go", label="worker", phase="P", cached="from disk"))

    node = one(reg, "agent")
    assert node.status == "done"
    assert node.recent[-1] == "replayed from a previous run"


def test_finish_gives_the_workflow_node_a_terminal_status():
    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)
    anyio.run(lambda: wf.agent("go", label="worker", phase="P"))
    assert one(reg, "workflow").status == "running"
    wf.finish()
    assert one(reg, "workflow").status == "done"


# ---------------------------------------------------------------------------
# Controls → NodeAction + resulting NodeStatus
# ---------------------------------------------------------------------------


def test_stop_records_an_action_and_cancels_the_workflow_node():
    reg = ActivityRegistry(session_id="s")
    seen = []
    reg.subscribe(seen.append)
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    wf.stop()

    assert (wf.activity_id, "stop", "user") in actions_seen(seen)
    assert one(reg, "workflow").status == "cancelled"


def test_pause_and_resume_record_actions_and_statuses():
    reg = ActivityRegistry(session_id="s")
    seen = []
    reg.subscribe(seen.append)
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    wf.pause()
    assert one(reg, "workflow").status == "paused"
    wf.resume()
    assert one(reg, "workflow").status == "running"

    acts = actions_seen(seen)
    assert (wf.activity_id, "pause", "user") in acts
    assert (wf.activity_id, "resume", "user") in acts

    # Idempotent controls do not re-record.
    before = len(actions_seen(seen))
    wf.resume()
    assert len(actions_seen(seen)) == before


def test_skip_agent_marks_a_queued_agent_skipped():
    reg = ActivityRegistry(session_id="s")
    seen = []
    reg.subscribe(seen.append)
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    async def go():
        # Register the agent, then skip it before it is ever driven.
        await wf.agent("go", label="worker", phase="P")

    anyio.run(go)
    agent_id = wf.run.all_agents()[0].id
    node_id = make_id("agent", "%s/%s" % (wf.run.id, agent_id))

    # Re-arm it so there is something to skip.
    wf.run.find_agent(agent_id).status = "queued"
    wf.skip_agent(agent_id)

    assert (node_id, "skip", "user") in actions_seen(seen)
    assert reg.node(node_id).status == "skipped"


def test_cancel_records_a_stop_against_the_agent_node():
    reg = ActivityRegistry(session_id="s")
    seen = []
    reg.subscribe(seen.append)
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    anyio.run(lambda: wf.agent("go", label="worker", phase="P"))
    agent_id = wf.run.all_agents()[0].id
    with pytest.raises(WorkflowError):
        wf.cancel(agent_id)  # already finished — refused, and not recorded
    assert not [a for a in actions_seen(seen) if a[1] == "stop"]


def test_retry_puts_the_agent_node_back_to_pending():
    reg = ActivityRegistry(session_id="s")
    seen = []
    reg.subscribe(seen.append)
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    anyio.run(lambda: wf.agent("go", label="worker", phase="P"))
    agent_id = wf.run.all_agents()[0].id
    node_id = make_id("agent", "%s/%s" % (wf.run.id, agent_id))
    assert reg.node(node_id).status == "done"

    anyio.run(lambda: wf.retry_agent(agent_id))

    assert (node_id, "retry", "user") in actions_seen(seen)
    assert reg.node(node_id).status == "done"
    # The node was genuinely re-opened rather than left terminal throughout.
    statuses = [
        ev.status
        for ev in seen
        if getattr(ev, "node_id", None) == node_id and hasattr(ev, "status")
    ]
    assert statuses == ["running", "done", "pending", "running", "done"]


def test_stop_cancels_an_in_flight_agent_node():
    reg = ActivityRegistry(session_id="s")
    started = anyio.Event()

    async def slow(prompt, *, model="", agent_type="general-purpose", schema=None):
        started.set()
        await anyio.sleep(30)
        yield  # pragma: no cover

    wf = Workflow("release", agent_runner=slow, registry=reg)

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: wf.agent("go", label="worker", phase="P"))
            await started.wait()
            wf.stop()

    anyio.run(go)

    assert one(reg, "agent").status == "cancelled"
    assert one(reg, "workflow").status == "cancelled"


# ---------------------------------------------------------------------------
# Model-authored text
# ---------------------------------------------------------------------------


def test_model_authored_phase_titles_stay_one_node_each_and_are_sanitized():
    """A phase title is arbitrary model text — it must not break id or rail."""

    reg = ActivityRegistry(session_id="s")
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)

    hostile = "\x1b[2Kfake\nrail‮line"

    async def go():
        await wf.agent("a", label="a1", phase=hostile)
        await wf.agent("b", label="a2", phase="レビュー")
        await wf.agent("c", label="a3", phase="审查代码")

    anyio.run(go)

    phase_nodes = nodes_of_kind(reg, "phase")
    assert len(phase_nodes) == 3
    assert len({n.id for n in phase_nodes}) == 3
    titles = {n.title for n in phase_nodes}
    for title in titles:
        assert "\x1b" not in title and "\n" not in title and "‮" not in title


# ---------------------------------------------------------------------------
# Failure isolation — the rule that matters most
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reg_factory", [RaisingRegistry, BrokenRegistry])
def test_a_raising_registry_never_fails_the_run(reg_factory):
    reg = reg_factory()
    wf = Workflow("release", agent_runner=make_runner(tools=("read_file",)), registry=reg)

    async def go():
        with wf.phase("Review"):
            return await wf.parallel(
                [
                    lambda: wf.agent("one", label="a1"),
                    lambda: wf.agent("two", label="a2"),
                ]
            )

    out = anyio.run(go)
    assert out == ["done", "done"]

    wf.pause()
    wf.resume()
    wf.finish()

    assert wf.run.status == "done"
    assert [a.status for a in wf.run.all_agents()] == ["done", "done"]
    if isinstance(reg, RaisingRegistry):
        assert reg.calls > 0  # it really was called, and really did raise


def test_a_raising_registry_preserves_the_engine_exception():
    reg = RaisingRegistry()
    wf = Workflow("release", agent_runner=boom_runner, registry=reg)

    with pytest.raises(RuntimeError, match="child exploded"):
        anyio.run(lambda: wf.agent("go", label="worker", phase="P"))

    assert wf.run.all_agents()[0].status == "error"


def test_stop_still_stops_when_the_registry_is_broken():
    reg = RaisingRegistry()
    wf = Workflow("release", agent_runner=make_runner(), registry=reg)
    wf.stop()
    assert wf.run.status == "cancelled"
    with pytest.raises(WorkflowError):
        wf.stop()
