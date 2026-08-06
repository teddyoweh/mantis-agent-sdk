"""Subagent runs as real nodes in the activity tree.

The gap this closes (plan §9): a ``task`` run had ``_RUN_COUNTER`` identity but
no node, and ``_update_job_progress`` wrote its progress onto the *parent job* —
so a child's turns, tools and results were indistinguishable from its parent's.
These tests pin the three things that fixes:

* a child gets its OWN node, parented to the tool call that invoked it rather
  than to the session root;
* progress lands on that child node *as well as* on the parent job, which is
  what the live inspector still reads;
* none of it can change the outcome of the run — a registry that raises from
  every entry point must be invisible to the subagent.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent.activity import ActivityRegistry
from mantis_agent.builtin_tools import CODING_TOOLS
from mantis_agent.providers.mock import MockProvider
from mantis_agent.subagent import make_pair_tool, make_task_tool


def _explore() -> list:
    return [t for t in CODING_TOOLS if getattr(t, "is_read_only", False)]


def _session_registry() -> tuple[ActivityRegistry, str, str]:
    """A registry holding a session root and one live ``tool`` node under it."""

    reg = ActivityRegistry(session_id="S1")
    root = reg.create_node("session", "S1", title="session")
    tool = reg.create_node("tool", "call_1", title="task", parent_id=root)
    reg.set_status(tool, "running")
    return reg, root, tool


def _subagent_nodes(reg: ActivityRegistry) -> list:
    return [n for n in reg.nodes.values() if n.kind == "subagent"]


# ---------------------------------------------------------------------------
# task → a child node under the invoking tool node
# ---------------------------------------------------------------------------


def test_task_run_creates_child_node_under_the_invoking_tool_node() -> None:
    reg, root, tool = _session_registry()
    t = make_task_tool(model="mock", provider=MockProvider(default_text="found it"),
                       tools=_explore(), registry=reg)

    out = anyio.run(lambda: t.fn(prompt="where is auth enforced?", description="find auth"))

    assert out == "found it"
    nodes = _subagent_nodes(reg)
    assert len(nodes) == 1
    node = nodes[0]
    assert node.id.startswith("sub:")
    # The child hangs off the tool call that spawned it — NOT the session root,
    # which is what "collapsing child into parent" looked like before.
    assert node.parent_id == tool
    assert node.parent_id != root
    assert [n.id for n in reg.children(tool)] == [node.id]


def test_child_node_carries_type_model_and_allowlist_size() -> None:
    reg, _root, _tool = _session_registry()
    t = make_task_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg)

    anyio.run(lambda: t.fn(prompt="probe", description="Audit CORS",
                           subagent_type="explore"))

    node = _subagent_nodes(reg)[0]
    assert "explore" in node.title          # resolved AgentType.name
    assert "Audit CORS" in node.title       # the model's own description
    assert node.model == "mock"
    assert node.detail == "%d tools" % len(_explore())


def test_child_node_reaches_a_terminal_status() -> None:
    reg, _root, _tool = _session_registry()
    t = make_task_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg)

    anyio.run(lambda: t.fn(prompt="probe"))

    node = _subagent_nodes(reg)[0]
    assert node.status == "done"
    assert node.started_at is not None and node.ended_at is not None


def test_explicit_parent_wins_over_discovery() -> None:
    reg, root, _tool = _session_registry()
    other = reg.create_node("tool", "call_9", title="task", parent_id=root)
    t = make_task_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg,
                       activity_parent_id=lambda: other)

    anyio.run(lambda: t.fn(prompt="probe"))

    assert _subagent_nodes(reg)[0].parent_id == other


def test_without_a_tool_node_the_child_falls_back_to_the_session_root() -> None:
    reg = ActivityRegistry(session_id="S1")
    root = reg.create_node("session", "S1", title="session")
    t = make_task_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg)

    anyio.run(lambda: t.fn(prompt="probe"))

    assert _subagent_nodes(reg)[0].parent_id == root


def test_two_runs_get_two_distinct_nodes() -> None:
    reg, _root, tool = _session_registry()
    t = make_task_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg)

    anyio.run(lambda: t.fn(prompt="one"))
    anyio.run(lambda: t.fn(prompt="two"))

    ids = {n.id for n in _subagent_nodes(reg)}
    assert len(ids) == 2


def test_a_raising_parent_callable_is_not_a_failure() -> None:
    reg, _root, _tool = _session_registry()

    def _boom() -> str:
        raise RuntimeError("no parent for you")

    t = make_task_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg, activity_parent_id=_boom)

    assert anyio.run(lambda: t.fn(prompt="probe")) == "ok"
    assert _subagent_nodes(reg)[0].parent_id is None


def test_a_backgrounded_run_hangs_off_its_job_node() -> None:
    from mantis_agent.jobs import JobManager

    reg, _root, _tool = _session_registry()

    async def _main() -> None:
        jm = JobManager()
        t = make_task_tool(model="mock", provider=MockProvider(default_text="bg"),
                           tools=_explore(), registry=reg, jobs=jm)
        out = await t.fn(prompt="probe", description="d", run_in_background=True)
        assert "background job" in out
        await jm.wait(1, timeout_s=10.0)

    anyio.run(_main)

    node = _subagent_nodes(reg)[0]
    # The job outlives the tool call, so the job's node — not the tool node —
    # owns the child. The job manager here has no registry of its own, which is
    # exactly the dangling-parent case the registry treats as a root.
    assert node.parent_id == "job:1"
    assert node.status == "done"


# ---------------------------------------------------------------------------
# progress reaches the CHILD's node, not only the parent job
# ---------------------------------------------------------------------------


def test_progress_lands_on_the_child_node() -> None:
    reg, _root, _tool = _session_registry()
    t = make_task_tool(model="mock", provider=MockProvider(default_text="the answer"),
                       tools=_explore(), registry=reg)

    anyio.run(lambda: t.fn(prompt="probe"))

    node = _subagent_nodes(reg)[0]
    assert node.recent, "the child's own node saw no activity"
    assert any("the answer" in line for line in node.recent)
    assert node.activity


def test_parent_job_progress_is_still_written() -> None:
    """The live inspector reads the job's counters — they must not move."""

    from mantis_agent.subagent import _update_job_progress
    from mantis_agent.types import AssistantMessage, TextBlock, ToolUseBlock

    class _Job:
        def __init__(self) -> None:
            self.turn_count = 0
            self.tool_count = 0
            self.last_tool = ""
            self.last_event = ""
            self.events: list = []

    reg, _root, _tool = _session_registry()
    node_id = reg.create_node("subagent", 42, title="explore")
    job = _Job()
    msg = AssistantMessage(content=[
        TextBlock(text="looking at the gate"),
        ToolUseBlock(id="tu1", name="grep", input={"pattern": "Allow-Origin"}),
    ])

    _update_job_progress(job, msg, reg=reg, node_id=node_id)

    # Parent job: unchanged behaviour.
    assert job.turn_count == 1 and job.tool_count == 1 and job.last_tool == "grep"
    assert "grep" in job.last_event
    # Child node: the same lines, on its own node.
    node = reg.node(node_id)
    assert any("looking at the gate" in line for line in node.recent)
    assert any("grep" in line for line in node.recent)


def test_update_job_progress_without_a_registry_is_unchanged() -> None:
    from mantis_agent.subagent import _update_job_progress
    from mantis_agent.types import AssistantMessage, TextBlock

    class _Job:
        turn_count = 0
        tool_count = 0
        last_tool = ""
        last_event = ""

        def __init__(self) -> None:
            self.events: list = []

    job = _Job()
    _update_job_progress(job, AssistantMessage(content=[TextBlock(text="hi")]))
    assert job.turn_count == 1 and "hi" in job.last_event


# ---------------------------------------------------------------------------
# failure isolation — a broken registry must never break the child
# ---------------------------------------------------------------------------


class _HostileRegistry:
    """Raises from every surface an emitter could touch."""

    session_id = "S1"

    @property
    def nodes(self) -> dict:
        raise RuntimeError("registry is on fire")

    def apply(self, ev: Any) -> Any:
        raise RuntimeError("registry is on fire")

    def create_node(self, *a: Any, **kw: Any) -> str:
        raise RuntimeError("registry is on fire")


def test_a_raising_registry_does_not_fail_the_child() -> None:
    t = make_task_tool(model="mock", provider=MockProvider(default_text="still fine"),
                       tools=_explore(), registry=_HostileRegistry())

    out = anyio.run(lambda: t.fn(prompt="probe", description="d"))

    assert out == "still fine"


def test_a_raising_registry_does_not_fail_a_twin() -> None:
    p = make_pair_tool(model="mock", provider=MockProvider(default_text="pushback"),
                       tools=_explore(), registry=_HostileRegistry())

    out = anyio.run(lambda: p.fn(message="review this", peer="skeptic"))

    assert out == "[skeptic] pushback"


def test_no_registry_emits_nothing_and_still_works() -> None:
    t = make_task_tool(model="mock", provider=MockProvider(default_text="plain"),
                       tools=_explore())
    assert anyio.run(lambda: t.fn(prompt="probe")) == "plain"


# ---------------------------------------------------------------------------
# failed / cancelled runs
# ---------------------------------------------------------------------------


def _patch_agent(monkeypatch: Any, exc: BaseException) -> None:
    import mantis_agent.subagent as sub

    class _FailingAgent:
        def __init__(self, **kw: Any) -> None:
            pass

        def run_iter(self, messages: Any) -> Any:
            async def _gen() -> Any:
                raise exc
                yield  # pragma: no cover - unreachable, makes this a generator

            return _gen()

    monkeypatch.setattr(sub, "Agent", _FailingAgent)


def test_a_failing_child_lands_on_error(monkeypatch: Any) -> None:
    reg, _root, _tool = _session_registry()
    _patch_agent(monkeypatch, RuntimeError("child exploded"))
    t = make_task_tool(model="mock", provider=MockProvider(), tools=_explore(),
                       registry=reg)

    with pytest.raises(RuntimeError):
        anyio.run(lambda: t.fn(prompt="probe"))

    node = _subagent_nodes(reg)[0]
    assert node.status == "error"
    assert node.error and "child exploded" in node.error


def test_a_cancelled_child_lands_on_cancelled(monkeypatch: Any) -> None:
    import asyncio

    reg, _root, _tool = _session_registry()
    _patch_agent(monkeypatch, asyncio.CancelledError())
    t = make_task_tool(model="mock", provider=MockProvider(), tools=_explore(),
                       registry=reg)

    with pytest.raises(BaseException):  # noqa: B017 - CancelledError shape varies
        anyio.run(lambda: t.fn(prompt="probe"))

    node = _subagent_nodes(reg)[0]
    assert node.status == "cancelled"


# ---------------------------------------------------------------------------
# pair → one long-lived node per twin, so /twin is a filter
# ---------------------------------------------------------------------------


def test_twin_gets_one_long_lived_node_across_exchanges() -> None:
    reg = ActivityRegistry(session_id="S1")
    reg.create_node("session", "S1", title="session")
    p = make_pair_tool(model="mock", provider=MockProvider(default_text="disagree"),
                       tools=_explore(), registry=reg)

    anyio.run(lambda: p.fn(message="first", peer="skeptic"))
    anyio.run(lambda: p.fn(message="second", peer="skeptic"))

    twins = _subagent_nodes(reg)
    assert len(twins) == 1                       # long-lived: not one per call
    node = twins[0]
    assert node.id.startswith("sub:twin/")
    assert "skeptic" in node.id
    assert node.status == "done"
    assert node.model == "mock"


def test_two_peers_are_two_nodes() -> None:
    reg = ActivityRegistry(session_id="S1")
    p = make_pair_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg)

    anyio.run(lambda: p.fn(message="a", peer="skeptic"))
    anyio.run(lambda: p.fn(message="b", peer="security"))

    ids = {n.id for n in _subagent_nodes(reg)}
    assert len(ids) == 2


def test_twin_nodes_are_filterable_by_kind_and_id() -> None:
    reg = ActivityRegistry(session_id="S1")
    p = make_pair_tool(model="mock", provider=MockProvider(default_text="ok"),
                       tools=_explore(), registry=reg)
    anyio.run(lambda: p.fn(message="a", peer="perf"))

    hits = reg.filter(lambda n: n.kind == "subagent" and n.id.startswith("sub:twin/"))
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# SubAgentSpec / as_subagent_tool
# ---------------------------------------------------------------------------


def test_spec_subagent_emits_a_node() -> None:
    from mantis_agent.subagent import SubAgentSpec, as_subagent_tool

    reg, _root, tool = _session_registry()
    spec = SubAgentSpec(name="reviewer", system_prompt="review", model="mock",
                        registry=reg)
    t = as_subagent_tool(spec, parent_provider=MockProvider(default_text="lgtm"))

    out = anyio.run(lambda: t.fn(prompt="review this"))

    assert out == "lgtm"
    node = _subagent_nodes(reg)[0]
    assert node.title.startswith("reviewer")
    assert node.parent_id == tool
    assert node.status == "done"


def test_spec_registry_defaults_to_none() -> None:
    from mantis_agent.subagent import SubAgentSpec

    spec = SubAgentSpec(name="x", system_prompt="y", model="mock")
    assert spec.registry is None
