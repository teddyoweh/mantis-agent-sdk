"""Coordinator tool — the model-facing entry to the workflow engine.

Everything runs against a FAKE agent_runner (injected via ``agent_runner=``):
no network, no model, no tokens. We assert the tool decomposes an objective into
parallel workers, aggregates their reports, runs a verify phase, and returns a
structured synthesis — plus the engine's cancellation/budget flow.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.budget import Budget
from mantis_agent.coordinator import make_coordinate_tool
from mantis_agent.types import AssistantMessage, TextBlock, ToolUseBlock, Usage
from mantis_agent.workflow import Workflow, _parse_verdict


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


# ---------------------------------------------------------------------------
# Fake runners (shape matches Agent.run_iter: async iterator of Messages)
# ---------------------------------------------------------------------------


def make_fake_runner(*, verdict="PASS", tools=("grep",), in_tokens=100, out_tokens=10):
    """One turn per run. The 'verify' persona emits a VERDICT line; every other
    persona emits a short report that echoes its prompt + type."""

    async def runner(prompt, *, model, agent_type, schema=None):
        content = [ToolUseBlock(id=f"t{i}", name=n, input={}) for i, n in enumerate(tools)]
        if agent_type == "verify":
            text = f"Checked the findings.\nVERDICT: {verdict}"
        else:
            text = f"[{agent_type}] report for: {prompt.splitlines()[0]}"
        content.append(TextBlock(text=text))
        yield AssistantMessage(
            content=content,
            usage=Usage(input_tokens=in_tokens, output_tokens=out_tokens),
        )

    return runner


def _call(tool, **kwargs):
    return anyio.run(lambda: tool.fn(**kwargs))


# ---------------------------------------------------------------------------
# Tool: decomposition + aggregation + verification
# ---------------------------------------------------------------------------


def test_signature_and_description():
    t = make_coordinate_tool(
        model="m", provider=None, backend=None, tools=None,
        permissions=None, budget=None, agent_types=None,
        agent_runner=make_fake_runner(),
    )
    assert t.name == "coordinate"
    # description steers the model: coordinate for decomposable, task for single.
    assert "task" in t.description and "parallel" in t.description.lower()


def test_requires_objective():
    t = make_coordinate_tool(model="m", tools=None, agent_runner=make_fake_runner())
    assert "objective" in _call(t, objective="   ")
    assert "objective" in _call(t, objective="")


def test_decomposes_subtasks_into_parallel_workers():
    """Explicit subtasks each become a research worker; a verify agent follows.

    We capture the workflow to inspect its phases/agents."""
    captured = {}
    orig_init = Workflow.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        captured["wf"] = self

    Workflow.__init__ = spy_init  # type: ignore[method-assign]
    try:
        t = make_coordinate_tool(model="m", tools=None, agent_runner=make_fake_runner())
        report = _call(
            t,
            objective="audit auth across modules",
            subtasks=[
                {"task": "look at login.py", "subagent_type": "explore", "label": "login"},
                {"task": "look at session.py", "subagent_type": "explore", "label": "session"},
                {"task": "design the fix", "subagent_type": "plan", "label": "fix"},
            ],
        )
    finally:
        Workflow.__init__ = orig_init  # type: ignore[method-assign]

    wf = captured["wf"]
    # Two phases: Research (3 parallel workers) then Verification (1).
    assert [p.title for p in wf.run.phases] == ["Research", "Verification"]
    research = wf.run.phases[0]
    assert [a.label for a in research.agents] == ["login", "session", "fix"]
    assert [a.agent_type for a in research.agents] == ["explore", "explore", "plan"]
    verify_phase = wf.run.phases[1]
    assert [a.agent_type for a in verify_phase.agents] == ["verify"]
    # Structured synthesis surfaced in the report text.
    assert "## Findings" in report
    assert "login" in report and "session" in report and "fix" in report
    assert "VERDICT: PASS" in report


def test_default_decomposition_fans_out_explore_and_plan():
    """No subtasks → coordinator still fans out (explore + plan) in parallel."""
    captured = {}
    orig_init = Workflow.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        captured["wf"] = self

    Workflow.__init__ = spy_init  # type: ignore[method-assign]
    try:
        t = make_coordinate_tool(model="m", tools=None, agent_runner=make_fake_runner())
        _call(t, objective="make the thing fast")
    finally:
        Workflow.__init__ = orig_init  # type: ignore[method-assign]

    research = captured["wf"].run.phases[0]
    assert {a.agent_type for a in research.agents} == {"explore", "plan"}


def test_workers_run_in_parallel_under_cap():
    """The research phase actually overlaps its workers (barrier fan-out)."""
    live = 0
    peak = 0

    async def runner(prompt, *, model, agent_type, schema=None):
        nonlocal live, peak
        if agent_type != "verify":
            live += 1
            peak = max(peak, live)
            await anyio.sleep(0.02)
            live -= 1
        yield AssistantMessage(content=[TextBlock(text=f"{agent_type} ok\nVERDICT: PASS")],
                               usage=Usage(input_tokens=1, output_tokens=1))

    t = make_coordinate_tool(model="m", tools=None, agent_runner=runner)
    _call(
        t,
        objective="obj",
        subtasks=[{"task": f"t{i}", "subagent_type": "explore"} for i in range(4)],
    )
    assert peak >= 2  # workers overlapped rather than running serially


def test_verify_false_skips_verification():
    captured = {}
    orig_init = Workflow.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        captured["wf"] = self

    Workflow.__init__ = spy_init  # type: ignore[method-assign]
    try:
        t = make_coordinate_tool(model="m", tools=None, agent_runner=make_fake_runner())
        report = _call(t, objective="obj", subtasks=[{"task": "a"}], verify=False)
    finally:
        Workflow.__init__ = orig_init  # type: ignore[method-assign]

    assert [p.title for p in captured["wf"].run.phases] == ["Research"]
    assert "Verification" not in report


def test_unknown_subagent_type_falls_back_to_explore():
    captured = {}
    orig_init = Workflow.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        captured["wf"] = self

    Workflow.__init__ = spy_init  # type: ignore[method-assign]
    try:
        t = make_coordinate_tool(model="m", tools=None, agent_runner=make_fake_runner())
        _call(t, objective="obj", subtasks=[{"task": "a", "subagent_type": "bogus"}])
    finally:
        Workflow.__init__ = orig_init  # type: ignore[method-assign]

    assert captured["wf"].run.phases[0].agents[0].agent_type == "explore"


# ---------------------------------------------------------------------------
# Tool: progress streaming (the /workflows live viewer shape)
# ---------------------------------------------------------------------------


def test_on_progress_streams_task_tool_event_shape():
    events: list[dict] = []
    t = make_coordinate_tool(
        model="m", tools=None, on_progress=events.append,
        agent_runner=make_fake_runner(tools=("grep", "read_file")),
    )
    _call(t, objective="obj", subtasks=[{"task": "a", "subagent_type": "explore"}])

    phases = [e.get("phase") for e in events]
    assert "start" in phases and "tool" in phases and "turn" in phases and "end" in phases
    # Every event carries an id; each run's start/end are balanced.
    assert all("id" in e for e in events)
    assert phases.count("start") == phases.count("end")
    # A tool event names the child's tool call.
    tool_names = {e.get("tool") for e in events if e.get("phase") == "tool"}
    assert {"grep", "read_file"} <= tool_names


# ---------------------------------------------------------------------------
# Engine: structured synthesis dict, budget + cancellation flow
# ---------------------------------------------------------------------------


def test_coordinate_returns_structured_synthesis():
    wf = Workflow("wf", agent_runner=make_fake_runner(verdict="PARTIAL"),
                  model="m", clock=_counter())

    async def go():
        return await wf.coordinate(
            "obj",
            [{"label": "a", "prompt": "p1", "agent_type": "explore"},
             {"label": "b", "prompt": "p2", "agent_type": "plan"}],
        )

    result = anyio.run(go)
    assert result["status"] == "done"
    assert result["verdict"] == "PARTIAL"
    assert [f["label"] for f in result["findings"]] == ["a", "b"]
    assert all(f["report"] for f in result["findings"])
    # explore + plan + verify all completed.
    assert len(result["agents"]) == 3
    assert {a["agent_type"] for a in result["agents"]} == {"explore", "plan", "verify"}
    assert result["over_budget"] is False


def test_budget_exhaustion_skips_verification():
    """A workflow budget breached during research skips the verify pass."""
    wf = Workflow(
        "wf",
        agent_runner=make_fake_runner(),
        model="m",
        budget=Budget(max_turns=1),   # 2 research workers => 2 turns > cap
        clock=_counter(),
    )

    async def go():
        return await wf.coordinate(
            "obj",
            [{"label": "a", "prompt": "p1", "agent_type": "explore"},
             {"label": "b", "prompt": "p2", "agent_type": "explore"}],
        )

    result = anyio.run(go)
    assert result["over_budget"] is True
    assert result["verdict"] is None
    assert result["verification"] is None
    # No Verification phase was ever created.
    assert [p.title for p in wf.run.phases] == ["Research"]


def test_cancellation_marks_run_cancelled_and_skips_verify():
    started = anyio.Event()

    async def runner(prompt, *, model, agent_type, schema=None):
        started.set()
        await anyio.sleep(10)  # long — will be cancelled by stop()
        yield AssistantMessage(content=[TextBlock(text="never")], usage=Usage())

    wf = Workflow("wf", agent_runner=runner, model="m", clock=_counter())

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: wf.coordinate("obj", [{"label": "a", "prompt": "p", "agent_type": "explore"}])
            )
            await started.wait()
            wf.stop()

    anyio.run(go)
    assert wf.run.status == "cancelled"
    assert all(a.status == "cancelled" for a in wf.run.all_agents())
    # Verification never ran (run was stopped).
    assert [p.title for p in wf.run.phases] == ["Research"]


# ---------------------------------------------------------------------------
# verdict parsing
# ---------------------------------------------------------------------------


def test_parse_verdict():
    assert _parse_verdict("blah\nVERDICT: PASS") == "PASS"
    assert _parse_verdict("VERDICT: fail") == "FAIL"
    assert _parse_verdict("VERDICT: PARTIAL\nmore") == "PARTIAL"
    # last explicit verdict wins
    assert _parse_verdict("VERDICT: PASS ... VERDICT: FAIL") == "FAIL"
    # bare-token fallback
    assert _parse_verdict("everything looks partial to me") == "PARTIAL"
    assert _parse_verdict(None) is None
    assert _parse_verdict("no verdict here") is None


def _counter():
    c = iter(range(0, 100_000_000, 10))
    return lambda: next(c)
