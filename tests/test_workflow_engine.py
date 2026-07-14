"""Workflow engine + shared view helpers.

Everything runs against a FAKE agent runner — no network, no model, no tokens.
A controllable clock (a plain counter) makes every duration assertion exact.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.types import AssistantMessage, TextBlock, ToolUseBlock, Usage
from mantis_agent.workflow import (
    AgentRun,
    Phase,
    Workflow,
    WorkflowError,
    WorkflowRun,
)
from mantis_agent import workflow_view as wv


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


# ---------------------------------------------------------------------------
# Fake runners
# ---------------------------------------------------------------------------


def make_static_runner(*, out_tokens=10, in_tokens=100, text="done", tools=()):
    """A runner that yields one AssistantMessage with fixed usage/tools."""

    async def runner(prompt, *, model, agent_type, schema=None):
        content = [ToolUseBlock(id=f"t{i}", name=name, input={}) for i, name in enumerate(tools)]
        content.append(TextBlock(text=f"{text}:{prompt}"))
        yield AssistantMessage(
            content=content,
            usage=Usage(input_tokens=in_tokens, output_tokens=out_tokens),
        )

    return runner


# ---------------------------------------------------------------------------
# view helpers
# ---------------------------------------------------------------------------


def test_format_number():
    assert wv.format_number(1321) == "1.3k"
    assert wv.format_number(999) == "999"
    assert wv.format_number(1000) == "1k"
    assert wv.format_number(2_500_000) == "2.5m"


def test_format_duration():
    assert wv.format_duration(0) == "0s"
    assert wv.format_duration(5_000) == "5s"
    assert wv.format_duration(59_000) == "59s"
    assert wv.format_duration(60_000) == "1m 0s"
    assert wv.format_duration(125_000) == "2m 5s"
    assert wv.format_duration(-10) == "0s"


def test_status_glyph():
    assert wv.status_glyph("done") == "✓"
    assert wv.status_glyph("error") == "✗"
    assert wv.status_glyph("paused") == "⏸"
    assert wv.status_glyph("queued") == "◇"
    # running is a spinner frame
    assert wv.status_glyph("running", frame=0) in ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def test_accumulate_usage_rule():
    """input = latest (cumulative-per-turn); output = sum (per-turn)."""
    acc = wv.accumulate_usage(None, Usage(input_tokens=100, output_tokens=10))
    assert acc.inputTokens == 100
    assert acc.outputTokens == 10
    acc = wv.accumulate_usage(acc, Usage(input_tokens=250, output_tokens=15))
    # input keeps the LATEST turn (250), output SUMS (10+15)
    assert acc.inputTokens == 250
    assert acc.outputTokens == 25


def test_accumulate_usage_cache_fields():
    acc = wv.accumulate_usage(
        None,
        Usage(input_tokens=100, output_tokens=5,
              cache_read_input_tokens=40, cache_creation_input_tokens=20),
    )
    assert acc.cacheReadInputTokens == 40
    assert acc.cacheCreationInputTokens == 20
    # total = latest_input + cache + cumulative_output
    assert wv.total_tokens(acc) == 100 + 20 + 40 + 5


def test_format_agent_row_shape():
    ar = AgentRun(id="a1", label="researcher", model="gpt", status="running",
                  started=0.0, summary="digging into the codebase")
    ar.usage = wv.accumulate_usage(None, Usage(input_tokens=1300, output_tokens=21))
    row = wv.format_agent_row(ar, selected=True, viewed=True, now=5000.0,
                              width=200, color=False, show_model=True)
    assert "researcher" in row
    assert "digging into the codebase" in row
    assert "5s" in row               # elapsed 5000ms
    assert "tok" in row
    assert "gpt" in row              # model shown in detail
    assert row.startswith("❯")       # selection caret


def test_format_agent_row_truncates_to_width():
    ar = AgentRun(id="a1", label="x" * 50, status="done", started=0.0, ended=1000.0)
    row = wv.format_agent_row(ar, width=20, color=False, now=1000.0)
    assert wv._visible_len(row) <= 20


def test_format_phase_rail():
    phases = [Phase(title="Research", status="done"),
              Phase(title="Build", status="running"),
              Phase(title="Ship", status="queued")]
    rail = wv.format_phase_rail(phases, sel=1, color=False)
    assert "Research" in rail and "Build" in rail and "Ship" in rail
    assert "›" in rail


# ---------------------------------------------------------------------------
# engine: grouping
# ---------------------------------------------------------------------------


def test_phase_and_agent_grouping():
    clock = iter(range(0, 1_000_000, 100))
    wf = Workflow("wf", agent_runner=make_static_runner(),
                  clock=lambda: next(clock), model="m")

    async def go():
        with wf.phase("Research", detail="dig"):
            await wf.agent("q1", label="a")
            await wf.agent("q2", label="b")
        with wf.phase("Build"):
            await wf.agent("q3", label="c")

    anyio.run(go)

    assert [p.title for p in wf.run.phases] == ["Research", "Build"]
    research = wf.run.phases[0]
    assert research.detail == "dig"
    assert [a.label for a in research.agents] == ["a", "b"]
    assert wf.run.phases[1].agents[0].label == "c"
    # ids are 'a'+base36, monotonically assigned
    assert [a.id for a in wf.run.all_agents()] == ["a0", "a1", "a2"]
    # results flow back
    assert all(a.status == "done" for a in wf.run.all_agents())
    assert research.status == "done"


def test_agent_returns_final_text():
    wf = Workflow("wf", agent_runner=make_static_runner(text="RESULT"), clock=_counter())

    async def go():
        return await wf.agent("hello", label="a")

    out = anyio.run(go)
    assert out == "RESULT:hello"


# ---------------------------------------------------------------------------
# engine: parallel / pipeline
# ---------------------------------------------------------------------------


def test_parallel_barrier_and_order():
    # Each agent sleeps a different amount; results must still be input-ordered.
    async def runner(prompt, *, model, agent_type, schema=None):
        await anyio.sleep(0.02 if prompt == "slow" else 0.0)
        yield AssistantMessage(content=[TextBlock(text=prompt)],
                               usage=Usage(input_tokens=1, output_tokens=1))

    wf = Workflow("wf", agent_runner=runner, clock=_counter())

    async def go():
        return await wf.parallel([
            lambda: wf.agent("slow", label="s"),
            lambda: wf.agent("fast", label="f"),
        ])

    results = anyio.run(go)
    assert results == ["slow", "fast"]  # order preserved despite timing
    assert len(wf.run.all_agents()) == 2


def test_pipeline_per_item_independence():
    async def runner(prompt, *, model, agent_type, schema=None):
        yield AssistantMessage(content=[TextBlock(text=prompt.upper())],
                               usage=Usage(input_tokens=1, output_tokens=1))

    wf = Workflow("wf", agent_runner=runner, clock=_counter())

    async def go():
        return await wf.pipeline(
            ["a", "b"],
            lambda x: wf.agent(f"stage1-{x}", label=f"s1-{x}"),
            lambda x: wf.agent(f"stage2-{x}", label=f"s2-{x}"),
        )

    results = anyio.run(go)
    # two stages, each uppercases the whole prompt string
    assert results == ["STAGE2-STAGE1-A", "STAGE2-STAGE1-B"]
    # 2 items * 2 stages = 4 agent runs
    assert len(wf.run.all_agents()) == 4


def test_concurrency_cap_enforced():
    live = 0
    peak = 0

    async def runner(prompt, *, model, agent_type, schema=None):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await anyio.sleep(0.01)
        live -= 1
        yield AssistantMessage(content=[TextBlock(text=prompt)],
                               usage=Usage(input_tokens=1, output_tokens=1))

    wf = Workflow("wf", agent_runner=runner, concurrency=2, clock=_counter())

    async def go():
        await wf.parallel([lambda i=i: wf.agent(f"p{i}", label=f"a{i}") for i in range(8)])

    anyio.run(go)
    assert peak <= 2
    assert peak == 2  # actually reached the cap


# ---------------------------------------------------------------------------
# engine: budget accounting
# ---------------------------------------------------------------------------


def test_per_agent_budget_accounting():
    # Two turns per agent so we can check the cumulative-output / latest-input rule.
    async def runner(prompt, *, model, agent_type, schema=None):
        yield AssistantMessage(content=[TextBlock(text="t1")],
                               usage=Usage(input_tokens=100, output_tokens=10))
        yield AssistantMessage(content=[TextBlock(text="t2")],
                               usage=Usage(input_tokens=300, output_tokens=20))

    wf = Workflow("wf", agent_runner=runner, model="m", clock=_counter())

    async def go():
        await wf.agent("go", label="a")

    anyio.run(go)
    ar = wf.run.all_agents()[0]
    assert ar.turns == 2
    assert ar.usage.inputTokens == 300      # latest turn
    assert ar.usage.outputTokens == 30      # summed
    # workflow-level rollup sees both turns (BudgetTracker sums raw usage)
    assert wf.budget_tracker.turns == 2
    assert wf.budget_tracker.input_tokens == 400   # 100 + 300 (tracker sums)
    assert wf.budget_tracker.output_tokens == 30


# ---------------------------------------------------------------------------
# engine: control plane
# ---------------------------------------------------------------------------


def test_stop_cancels_running_agents():
    started = anyio.Event()

    async def runner(prompt, *, model, agent_type, schema=None):
        started.set()
        await anyio.sleep(10)  # long — will be cancelled
        yield AssistantMessage(content=[TextBlock(text="never")],
                               usage=Usage(input_tokens=1, output_tokens=1))

    wf = Workflow("wf", agent_runner=runner, clock=_counter())

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: wf.agent("x", label="a"))
            await started.wait()
            wf.stop()

    anyio.run(go)
    assert wf.run.status == "cancelled"
    ar = wf.run.all_agents()[0]
    assert ar.status == "cancelled"


def test_stop_when_not_running_raises():
    wf = Workflow("wf", agent_runner=make_static_runner(), clock=_counter())
    wf.run.status = "done"
    with pytest.raises(WorkflowError) as ei:
        wf.stop()
    assert ei.value.code == "not_running"


def test_cancel_unknown_agent_raises_not_found():
    wf = Workflow("wf", agent_runner=make_static_runner(), clock=_counter())
    with pytest.raises(WorkflowError) as ei:
        wf.cancel("nope")
    assert ei.value.code == "not_found"


def test_cancel_specific_agent():
    started = anyio.Event()

    async def runner(prompt, *, model, agent_type, schema=None):
        started.set()
        await anyio.sleep(10)
        yield AssistantMessage(content=[TextBlock(text="x")], usage=Usage())

    wf = Workflow("wf", agent_runner=runner, clock=_counter())

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: wf.agent("x", label="a"))
            await started.wait()
            wf.cancel(wf.run.all_agents()[0].id)

    anyio.run(go)
    assert wf.run.all_agents()[0].status == "cancelled"


def test_pause_resume_accumulates_paused_ms():
    ticks = iter([0, 100, 1000, 1100, 1200, 1300, 1400])  # controllable clock

    def clk():
        return next(ticks)

    wf = Workflow("wf", agent_runner=make_static_runner(), clock=clk)
    # construction consumed tick 0 (started) ... simulate a manual pause/resume
    wf.pause()      # _paused_at = next tick
    assert wf._paused
    wf.resume()     # dur added
    assert not wf._paused
    assert wf.run.total_paused_ms > 0


def test_pause_gates_new_agents():
    order = []

    async def runner(prompt, *, model, agent_type, schema=None):
        order.append(prompt)
        yield AssistantMessage(content=[TextBlock(text=prompt)], usage=Usage())

    wf = Workflow("wf", agent_runner=runner, clock=_counter())

    async def go():
        wf.pause()
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: wf.agent("gated", label="a"))
            await anyio.sleep(0.02)
            # still paused: agent hasn't run
            assert order == []
            wf.resume()

    anyio.run(go)
    assert order == ["gated"]


def test_skip_queued_agent():
    wf = Workflow("wf", agent_runner=make_static_runner(), clock=_counter())
    # Register an agent object manually in queued state, then skip it.
    ph = wf._get_phase("main")
    ar = AgentRun(id="a99", label="skipme", phase="main")
    ph.agents.append(ar)
    wf._agents_by_id[ar.id] = ar
    wf.skip_agent("a99")
    assert ar.status == "cancelled"


def test_skip_unknown_raises():
    wf = Workflow("wf", agent_runner=make_static_runner(), clock=_counter())
    with pytest.raises(WorkflowError) as ei:
        wf.skip_agent("ghost")
    assert ei.value.code == "not_found"


def test_retry_agent_reruns():
    calls = {"n": 0}

    async def runner(prompt, *, model, agent_type, schema=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        yield AssistantMessage(content=[TextBlock(text="recovered")],
                               usage=Usage(input_tokens=1, output_tokens=1))

    wf = Workflow("wf", agent_runner=runner, clock=_counter())

    async def go():
        try:
            await wf.agent("x", label="a")
        except RuntimeError:
            pass
        aid = wf.run.all_agents()[0].id
        return await wf.retry_agent(aid)

    out = anyio.run(go)
    assert out == "recovered"
    ar = wf.run.all_agents()[0]
    assert ar.status == "done"
    assert ar.error is None
    assert calls["n"] == 2


def test_retry_running_raises():
    wf = Workflow("wf", agent_runner=make_static_runner(), clock=_counter())
    ph = wf._get_phase("main")
    ar = AgentRun(id="a5", label="x", phase="main", status="running")
    ph.agents.append(ar)

    async def go():
        await wf.retry_agent("a5")

    with pytest.raises(WorkflowError) as ei:
        anyio.run(go)
    assert ei.value.code == "not_running"


# ---------------------------------------------------------------------------
# engine: persistence
# ---------------------------------------------------------------------------


def test_save_roundtrip(tmp_path):
    wf = Workflow("my-flow", agent_runner=make_static_runner(text="hi", tools=("grep",)),
                  model="m", clock=_counter())

    async def go():
        with wf.phase("Research", detail="d"):
            await wf.agent("q", label="a")
        wf.finish()

    anyio.run(go)
    path = wf.save(tmp_path / "wf.json")
    loaded = Workflow.load(path)
    assert isinstance(loaded, WorkflowRun)
    assert loaded.name == "my-flow"
    assert loaded.status == "done"
    assert [p.title for p in loaded.phases] == ["Research"]
    a = loaded.phases[0].agents[0]
    assert a.label == "a"
    assert a.status == "done"
    assert a.tool_count == 1
    assert a.usage.outputTokens == wf.run.phases[0].agents[0].usage.outputTokens


def test_save_default_path_uses_mantis_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "h"))
    wf = Workflow("wf", agent_runner=make_static_runner(), clock=_counter())
    path = wf.save()
    assert "workflows" in path
    assert path.endswith(".json")
    assert (tmp_path / "h").exists()


def test_on_event_fires():
    events = []
    wf = Workflow("wf", agent_runner=make_static_runner(),
                  on_event=lambda run: events.append(run.status), clock=_counter())

    async def go():
        await wf.agent("x", label="a")

    anyio.run(go)
    assert events  # observer saw state changes
    assert events[-1] == "running"  # run stays running until finish()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _counter():
    """A monotone ms clock as a zero-arg callable."""
    c = iter(range(0, 100_000_000, 10))
    return lambda: next(c)
