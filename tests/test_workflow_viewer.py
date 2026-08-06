"""The /workflows viewer: drill-down rendering, controls, and empty state.

All pure functions over plain data — no prompt_toolkit, no terminal, no model.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.types import AssistantMessage, TextBlock, Usage
from mantis_agent.workflow import AgentRun, Phase, Workflow, WorkflowRun
from mantis_agent.workflow_view import (
    CONTROLS,
    apply_control,
    control_footer,
    empty_state_lines,
    format_agent_detail,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


def _counter():
    it = iter(range(0, 10_000_000, 1000))
    return lambda: next(it)


def _run_with_agent(**kw):
    ag = AgentRun(id="a0", label="scout", phase="Survey", model="m1",
                  agent_type="explore", **kw)
    return WorkflowRun(id="w1", name="review", definition="review", job_id=7,
                       phases=[Phase(title="Survey", agents=[ag])]), ag


# ---------------------------------------------------------------------------
# drill-down
# ---------------------------------------------------------------------------


def test_detail_shows_identity_lifecycle_cost_prompt_and_result():
    from mantis_agent.types import ModelUsage

    run, ag = _run_with_agent(
        status="done", started=1000.0, ended=4000.0, turns=2, tool_count=5,
        cost_usd=0.0042, prompt="Review the diff in agent.py for bugs",
        result="found one thing",
        usage=ModelUsage(inputTokens=1200, outputTokens=140),
    )
    ag.recent_activities = ["grep", "read", "bash"]
    text = "\n".join(format_agent_detail(run, ag, now=4000.0, color=False))

    assert "Workflow agent · scout" in text
    assert "workflow: review (w1) · job #7" in text
    assert "phase: Survey · explore · m1" in text
    assert "status: done · 3s" in text
    assert "2 turns · 5 tools · 1.3k tok" in text and "$0.0042" in text
    assert "Review the diff in agent.py for bugs" in text
    assert "- grep" in text and "- bash" in text
    assert "found one thing" in text


def test_detail_truncates_a_long_prompt_and_result():
    run, ag = _run_with_agent(status="done", started=0.0, ended=1.0,
                              prompt="x" * 5000, result="y" * 5000)
    text = "\n".join(format_agent_detail(run, ag, prompt_chars=50,
                                         result_chars=40, color=False))
    assert text.count("…(truncated)") == 2
    assert "x" * 51 not in text


def test_detail_marks_a_replayed_agent_and_a_running_one():
    run, ag = _run_with_agent(status="done", replayed=True, started=0.0, ended=1.0)
    assert "status: done (replayed)" in "\n".join(
        format_agent_detail(run, ag, color=False))

    run2, ag2 = _run_with_agent(status="running", started=0.0)
    text = "\n".join(format_agent_detail(run2, ag2, now=2000.0, color=False))
    assert "still working — no result yet" in text


def test_detail_shows_an_error():
    run, ag = _run_with_agent(status="error", error="RuntimeError: nope",
                              started=0.0, ended=1.0)
    text = "\n".join(format_agent_detail(run, ag, color=False))
    assert "error" in text and "RuntimeError: nope" in text


def test_detail_carries_no_hidden_reasoning():
    """The engine only ever records tool names and visible text — assert the
    renderer has no field that could leak model thinking."""
    run, ag = _run_with_agent(status="done", started=0.0, ended=1.0,
                              prompt="p", result="r")
    assert not any("thinking" in ln.lower() or "reasoning" in ln.lower()
                   for ln in format_agent_detail(run, ag, color=False))


# ---------------------------------------------------------------------------
# controls
# ---------------------------------------------------------------------------


def test_footer_documents_every_control():
    footer = control_footer()
    for _action, key, _desc in CONTROLS:
        if key == "enter":
            assert "enter" in footer
        else:
            assert key in footer
    assert "esc" in control_footer(detail=True)


def _live_workflow():
    async def runner(prompt, *, model, agent_type, schema=None):
        yield AssistantMessage(content=[TextBlock(text="ok")],
                               usage=Usage(input_tokens=1, output_tokens=1))

    wf = Workflow("demo", agent_runner=runner, clock=_counter())
    anyio.run(lambda: wf.agent("p", label="one"))
    return wf


def test_controls_on_a_live_run():
    wf = _live_workflow()
    ag = wf.run.all_agents()[0]
    assert "paused demo" in apply_control(wf, wf.run, ag, "pause")
    assert "resumed demo" in apply_control(wf, wf.run, ag, "pause")
    assert "stopping demo" in apply_control(wf, wf.run, ag, "stop")


def test_controls_explain_why_they_are_ineligible():
    wf = _live_workflow()
    ag = wf.run.all_agents()[0]
    # the agent already finished, so a per-turn cancel is not possible
    assert "cannot cancel: agent a0 is done" in apply_control(wf, wf.run, ag, "cancel")
    wf.stop()
    # a second stop on a finished run explains itself instead of raising
    assert "cannot stop" in apply_control(wf, wf.run, ag, "stop")


def test_controls_on_a_history_run_only_allow_save():
    run = WorkflowRun(id="w9", name="old", status="done")
    assert "not live (loaded from history)" in apply_control(None, run, None, "stop")
    assert "saved →" in apply_control(None, run, None, "save")


def test_skip_marks_a_queued_agent_cancelled():
    wf = _live_workflow()
    queued = AgentRun(id="a9", label="later", phase="main", status="queued")
    wf.run.phases[0].agents.append(queued)
    assert "skipping later" in apply_control(wf, wf.run, queued, "skip")
    assert queued.status == "cancelled"


def test_retry_defers_to_the_caller():
    wf = _live_workflow()
    ag = wf.run.all_agents()[0]
    # retry needs to be awaited, so the control plane hands the id back rather
    # than blocking the UI thread
    assert apply_control(wf, wf.run, ag, "retry") == "retry-a0"
    assert "no agent selected" in apply_control(wf, wf.run, None, "retry")


def test_unknown_action_never_raises():
    wf = _live_workflow()
    assert "unknown action" in apply_control(wf, wf.run, None, "explode")


def test_save_of_a_live_run_writes_to_mantis_home():
    wf = _live_workflow()
    msg = apply_control(wf, wf.run, None, "save")
    assert "saved →" in msg and msg.rstrip().endswith(".json")


# ---------------------------------------------------------------------------
# engine: the cached/replay path the resume feature rides on
# ---------------------------------------------------------------------------


def test_cached_agent_never_calls_the_runner_and_is_marked_replayed():
    calls: list[str] = []

    async def runner(prompt, *, model, agent_type, schema=None):
        calls.append(prompt)
        yield AssistantMessage(content=[TextBlock(text="live")], usage=Usage())

    wf = Workflow("demo", agent_runner=runner, clock=_counter())
    out = anyio.run(lambda: wf.agent("p", label="cached", cached="STORED"))

    assert out == "STORED"
    assert calls == []
    ag = wf.run.all_agents()[0]
    assert ag.status == "done" and ag.replayed is True
    assert ag.result == "STORED" and ag.prompt == "p"
    assert ag.started is not None and ag.ended is not None


def test_retry_clears_the_replayed_flag():
    async def runner(prompt, *, model, agent_type, schema=None):
        yield AssistantMessage(content=[TextBlock(text="live")], usage=Usage())

    wf = Workflow("demo", agent_runner=runner, clock=_counter())
    anyio.run(lambda: wf.agent("p", label="one", cached="STORED"))
    ag = wf.run.all_agents()[0]
    anyio.run(lambda: wf.retry_agent(ag.id))
    assert ag.replayed is False and ag.result == "live"


def test_run_snapshot_round_trips_the_new_identity_fields():
    wf = Workflow("demo", agent_runner=None, clock=_counter())
    wf.run.definition = "review"
    wf.run.job_id = 12
    wf.run.resumed_from = "wold"
    wf.run.phases.append(Phase(title="P", agents=[
        AgentRun(id="a0", label="x", phase="P", prompt="the brief", replayed=True)]))
    back = WorkflowRun.from_dict(wf.run.to_dict())
    assert back.definition == "review" and back.job_id == 12
    assert back.resumed_from == "wold"
    assert back.phases[0].agents[0].prompt == "the brief"
    assert back.phases[0].agents[0].replayed is True


# ---------------------------------------------------------------------------
# empty state
# ---------------------------------------------------------------------------


def test_empty_state_teaches_the_command():
    from mantis_agent.workflow_defs import builtin_definitions

    lines = empty_state_lines(builtin_definitions())
    text = "\n".join(lines)
    assert "/workflows run <name> key=value" in text
    assert "review" in text
    assert "/workflows history" in text
    assert "/workflows resume" in text


def test_empty_state_without_definitions_still_teaches():
    text = "\n".join(empty_state_lines([]))
    assert "/workflows run" in text
    assert "Available:" not in text
