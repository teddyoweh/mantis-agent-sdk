"""The ``workflow`` tool: named execution, backgrounding, persistence, resume.

Nothing here touches a model — the tool is built with an injected
``agent_runner`` — and MANTIS_AGENT_HOME is redirected at tmp_path, so the
run artifacts these tests write land in the sandbox.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from mantis_agent.jobs import JobManager
from mantis_agent.types import AssistantMessage, TextBlock, Usage
from mantis_agent.workflow_defs import parse_workflow_md
from mantis_agent.workflow_store import (
    RECORD_VERSION,
    list_runs,
    load_record,
    prune_runs,
    redact_inputs,
    replay_cache,
    save_run,
)
from mantis_agent.workflow_tool import (
    format_workflow_report,
    make_workflow_tool,
    prepare_workflow_launch,
    workflows_enabled,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MANTIS_AGENT_DISABLE_WORKFLOWS", raising=False)
    yield


DEMO = """---
name: demo
description: A two-phase demo
---

```json
{
  "inputs": [{"name": "target", "required": true, "description": "the thing"}],
  "phases": [
    {"title": "Scan", "agents": [
      {"label": "a", "agent_type": "explore", "prompt": "scan {target}"},
      {"label": "b", "agent_type": "explore", "prompt": "probe {target}"}
    ]},
    {"title": "Sum", "mode": "sequential", "agents": [
      {"label": "s", "prompt": "sum {phase:Scan}"}
    ]}
  ]
}
```
"""

DEMO_DEF = parse_workflow_md(DEMO, "demo")


def make_runner(text="ok", record=None):
    async def runner(prompt, *, model, agent_type, schema=None):
        if record is not None:
            record.append(prompt)
        yield AssistantMessage(content=[TextBlock(text=f"{text}:{agent_type}")],
                               usage=Usage(input_tokens=10, output_tokens=4))

    return runner


def _tool(**kw):
    kw.setdefault("model", "test-model")
    kw.setdefault("agent_runner", make_runner())
    kw.setdefault("definitions", [DEMO_DEF])
    kw.setdefault("agent_types", [])
    return make_workflow_tool(**kw)


def _call(tool, args):
    # The @tool decorator unpacks a single-``args`` body into kwargs.
    return asyncio.run(tool.fn(**args))


# ---------------------------------------------------------------------------
# resolution + validation
# ---------------------------------------------------------------------------


def test_tool_description_lists_definitions_and_the_opt_in_rule():
    d = _tool().description
    assert "demo: A two-phase demo" in d
    assert "requires: target" in d
    assert "ONLY call this when the user actually asked" in d


def test_schema_enumerates_available_names():
    props = _tool().input_schema["properties"]
    assert props["name"]["enum"] == ["demo"]
    # 'script'/'args' are the model-authored path; the rest drive a named run.
    assert set(props) == {"script", "args", "name", "inputs",
                          "run_in_background", "resume_from"}


def test_neither_script_nor_name_is_required_by_the_schema():
    """The tool explains the either/or itself, which reads better than making
    the model decode a schema validation error."""
    assert "required" not in _tool().input_schema


def test_unknown_name_lists_what_exists():
    out = _call(_tool(), {"name": "ghost"})
    assert "no workflow named 'ghost'" in out and "Available: demo" in out


def test_missing_required_input_is_a_fixable_message_not_a_crash():
    out = _call(_tool(), {"name": "demo", "inputs": {}})
    assert "needs input(s): target" in out and "the thing" in out


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_DISABLE_WORKFLOWS", "1")
    assert workflows_enabled() is False
    out = _call(_tool(), {"name": "demo", "inputs": {"target": "t"}})
    assert "disabled for this session" in out


# ---------------------------------------------------------------------------
# synchronous execution
# ---------------------------------------------------------------------------


def test_foreground_run_returns_the_full_report():
    out = _call(_tool(), {"name": "demo", "inputs": {"target": "T"},
                          "run_in_background": False})
    assert "# Workflow report — demo" in out
    assert "3/3 agents completed" in out
    assert "## Scan" in out and "## Sum" in out


def test_foreground_run_registers_with_the_viewer_before_it_finishes():
    seen = []
    _call(_tool(on_run=seen.append), {"name": "demo", "inputs": {"target": "T"},
                                      "run_in_background": False})
    assert len(seen) == 1
    assert seen[0].run.definition == "demo"


def test_prompts_carry_the_resolved_inputs():
    prompts: list[str] = []
    _call(_tool(agent_runner=make_runner(record=prompts)),
          {"name": "demo", "inputs": {"target": "mantis_agent/agent.py"},
           "run_in_background": False})
    assert any("scan mantis_agent/agent.py" in p for p in prompts)


# ---------------------------------------------------------------------------
# background jobs — identity + monitoring
# ---------------------------------------------------------------------------


def test_background_returns_immediately_with_run_and_job_ids():
    async def go():
        jobs = JobManager()
        seen = []
        tool = _tool(jobs=jobs, on_run=seen.append)
        out = await tool.fn(name="demo", inputs={"target": "T"})
        # the receipt is available before the work is done
        assert "started in the background" in out
        job = jobs.all()[0]
        assert job.kind == "workflow"
        assert f"job: #{job.id}" in out
        # both directions of the link exist immediately
        wf = seen[0]
        assert job.workflow_id == wf.run.id
        assert wf.run.job_id == job.id
        assert wf.run.id in out
        assert "/workflows" in out and "job_output" in out
        await jobs.wait(job.id, timeout_s=5)
        return job, wf

    job, wf = asyncio.run(go())
    assert job.status == "done"
    assert "# Workflow report — demo" in job.result
    assert wf.run.status == "done"


def test_background_job_tracks_workflow_progress():
    """/jobs must show a live workflow moving, not a permanent "starting"."""

    async def go():
        jobs = JobManager()
        tool = _tool(jobs=jobs)
        await tool.fn(name="demo", inputs={"target": "T"})
        job = jobs.all()[0]
        await jobs.wait(job.id, timeout_s=5)
        return job

    job = asyncio.run(go())
    assert job.turn_count == 3 and job.tool_count == 0
    lines = [text for _ts, text in job.events]
    assert any("Scan ·" in ln for ln in lines)
    assert any("Sum ·" in ln for ln in lines)
    assert lines != ["starting"]


def test_background_job_summary_names_the_run():
    async def go():
        jobs = JobManager()
        tool = _tool(jobs=jobs)
        await tool.fn(name="demo", inputs={"target": "T"})
        job = jobs.all()[0]
        await jobs.wait(job.id, timeout_s=5)
        return job

    job = asyncio.run(go())
    assert f"run {job.workflow_id}" in job.summary()


def test_without_a_job_manager_it_runs_inline():
    out = _call(_tool(jobs=None), {"name": "demo", "inputs": {"target": "T"}})
    assert "# Workflow report — demo" in out


def test_progress_events_use_the_task_tool_shape():
    events: list[dict] = []
    _call(_tool(on_progress=events.append),
          {"name": "demo", "inputs": {"target": "T"}, "run_in_background": False})
    phases = [e["phase"] for e in events]
    assert phases.count("start") == 3 and phases.count("end") == 3
    start = next(e for e in events if e["phase"] == "start")
    assert set(start) == {"id", "phase", "type", "desc", "model"}
    assert any(e["phase"] == "turn" and e["tokens"] > 0 for e in events)


# ---------------------------------------------------------------------------
# persistence + history
# ---------------------------------------------------------------------------


def test_completed_run_is_persisted_and_listed():
    _call(_tool(), {"name": "demo", "inputs": {"target": "T"},
                    "run_in_background": False})
    runs = list_runs()
    assert len(runs) == 1
    r = runs[0]
    assert r["definition"] == "demo"
    assert r["status"] == "done"
    assert r["agents"] == 3 and r["phases"] == 2
    rec = load_record(r["run_id"])
    assert rec["version"] == RECORD_VERSION
    assert rec["inputs"] == {"target": "T"}
    assert rec["run"]["phases"][0]["agents"][0]["prompt"].startswith("scan T")


def test_a_stopped_run_is_still_persisted_for_inspection():
    async def stopping(prompt, *, model, agent_type, schema=None):
        raise RuntimeError("boom")
        yield  # pragma: no cover — makes this an async generator

    out = _call(_tool(agent_runner=stopping),
                {"name": "demo", "inputs": {"target": "T"}, "run_in_background": False})
    assert "(FAILED)" in out and "boom" in out
    assert "/workflows resume" in out
    runs = list_runs()
    assert runs and runs[0]["status"] == "error", "a failed run still leaves an artifact"


def test_redaction_keeps_secrets_out_of_the_artifact():
    got = redact_inputs({"target": "t", "api_key": "sk-live-123",
                         "authToken": "abc", "notes": "fine"})
    assert got == {"target": "t", "api_key": "[redacted]",
                   "authToken": "[redacted]", "notes": "fine"}


def test_history_prunes_to_the_cap(tmp_path):
    from mantis_agent.workflow import WorkflowRun

    for i in range(5):
        save_run(WorkflowRun(id=f"w{i}", name="x", status="done"), definition="demo")
    assert len(list_runs()) == 5
    prune_runs(keep=2)
    assert len(list_runs()) == 2


def test_replay_cache_ignores_failed_and_empty_agents():
    from mantis_agent.workflow import AgentRun, Phase, WorkflowRun

    run = WorkflowRun(id="w1", name="x", phases=[Phase(title="P", agents=[
        AgentRun(id="a0", label="good", phase="P", status="done",
                 prompt="p0", result="R0"),
        AgentRun(id="a1", label="bad", phase="P", status="error",
                 prompt="p1", result="R1"),
        AgentRun(id="a2", label="empty", phase="P", status="done",
                 prompt="p2", result="   "),
    ])])
    cache = replay_cache(run.to_dict())
    assert list(cache.values()) == ["R0"]


def test_load_record_survives_a_corrupt_file(tmp_path):
    from mantis_agent.workflow_store import run_path, runs_dir

    runs_dir().mkdir(parents=True, exist_ok=True)
    run_path("wbad").write_text("{not json", encoding="utf-8")
    assert load_record("wbad") is None
    assert list_runs() == []


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_resume_replays_unchanged_agents_for_free():
    _call(_tool(), {"name": "demo", "inputs": {"target": "T"},
                    "run_in_background": False})
    first = list_runs()[0]["run_id"]

    calls: list[str] = []
    out = _call(_tool(agent_runner=make_runner(text="fresh", record=calls)),
                {"name": "demo", "inputs": {"target": "T"},
                 "resume_from": first, "run_in_background": False})
    assert calls == []
    assert "3 replayed from a previous run" in out


def test_resume_reruns_agents_whose_prompt_changed():
    _call(_tool(), {"name": "demo", "inputs": {"target": "T"},
                    "run_in_background": False})
    first = list_runs()[0]["run_id"]

    calls: list[str] = []
    _call(_tool(agent_runner=make_runner(record=calls)),
          {"name": "demo", "inputs": {"target": "DIFFERENT"},
           "resume_from": first, "run_in_background": False})
    # Both Scan prompts embed {target}, so both miss and re-run. The summarizer
    # re-runs too, even though its own prompt is byte-identical (the stub runner
    # returns the same text for the new Scan): replay is strict-prefix, so
    # everything downstream of the first change runs live. Content-keying used to
    # replay it here and hand back a summary of the OLD scan.
    assert calls == ["scan DIFFERENT", "probe DIFFERENT",
                     "sum ok:explore\n\nok:explore"]


def test_resume_from_an_unknown_run_explains_itself():
    out = _call(_tool(), {"name": "demo", "inputs": {"target": "T"},
                          "resume_from": "wnope", "run_in_background": False})
    assert "no persisted workflow run 'wnope'" in out


def test_resume_refuses_a_run_of_a_different_workflow():
    from mantis_agent.workflow import WorkflowRun

    save_run(WorkflowRun(id="wother", name="other", status="done"), definition="other")
    out = _call(_tool(), {"name": "demo", "inputs": {"target": "T"},
                          "resume_from": "wother", "run_in_background": False})
    assert "was workflow 'other'" in out


def test_resume_inherits_stored_inputs_but_never_redacted_ones():
    _call(_tool(), {"name": "demo",
                    "inputs": {"target": "T", "api_key": "sk-secret"},
                    "run_in_background": False})
    rec_id = list_runs()[0]["run_id"]
    assert load_record(rec_id)["inputs"]["api_key"] == "[redacted]"

    launch = prepare_workflow_launch(
        DEMO_DEF, {"target": "T"}, agent_runner=make_runner(), resume_from=rec_id)
    assert launch.inputs["target"] == "T"
    assert "api_key" not in launch.inputs


def test_run_id_and_definition_are_set_before_execution():
    launch = prepare_workflow_launch(DEMO_DEF, {"target": "T"},
                                     agent_runner=make_runner())
    assert launch.run_id.startswith("w")
    assert launch.workflow.run.definition == "demo"
    assert launch.workflow.run.status == "running"


# ---------------------------------------------------------------------------
# report formatting
# ---------------------------------------------------------------------------


def test_report_flags_cancellation_failures_and_replays():
    report = format_workflow_report({
        "definition": "demo", "workflow_id": "w9", "status": "cancelled",
        "job_id": 4, "replayed": 2,
        "agents": [{"label": "a", "status": "done"}, {"label": "b", "status": "error"}],
        "log_lines": ["note this"],
        "phases": [{"title": "Scan", "results": ["r1", "  "]}],
    })
    assert "run w9 · status cancelled · 1/2 agents completed" in report
    assert "2 replayed" in report and "job #4" in report
    assert "NOTE: this run was stopped" in report
    assert "NOTE: b failed." in report
    assert "log: note this" in report
    assert report.count("## Scan") == 1


def test_report_marks_an_empty_phase():
    report = format_workflow_report({"phases": [{"title": "P", "results": []}]})
    assert "(no output)" in report


def test_artifacts_are_plain_readable_json():
    _call(_tool(), {"name": "demo", "inputs": {"target": "T"},
                    "run_in_background": False})
    path = list_runs()[0]["path"]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["run"]["definition"] == "demo"
    assert data["summary"]["phases"] == ["Scan", "Sum"]
