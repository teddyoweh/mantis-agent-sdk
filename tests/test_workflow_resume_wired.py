"""Resume, end to end, through the path the product actually takes.

:mod:`mantis_agent.workflow_steps` had the correct model — chained ``step_hash``,
replay the longest unchanged prefix — and :mod:`mantis_agent.workflow_store`
persisted the ledger, but ``workflow_tool`` still resumed off the old
content-keyed ``replay_cache``. So the bug was still live in the product, and
only a test that goes through :func:`prepare_workflow_launch` catches it.

The scenario, which is the reason strict-prefix exists: a three-stage pipeline
Scan → Fix → Verify where the later prompts refer to the earlier work rather than
embedding it ("fix what the scan flagged"), which is how anyone would write it
since the child re-reads the repo anyway. Edit Scan's prompt and resume. Scan
misses and re-runs, producing something different. Under content-keying Fix and
Verify hit — their own prompt text never changed — and hand back results computed
from the *old* scan, while the run reports success. Nothing announces it, which
is what makes it worse than a failure.

Every test here drives the real ``workflow`` tool with a stub runner and a
redirected MANTIS_AGENT_HOME, so what it exercises is the wiring, not a
re-statement of the hashing unit tests next door in
``test_workflow_prefix_replay.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from mantis_agent.types import AssistantMessage, TextBlock, Usage
from mantis_agent.workflow_defs import parse_workflow_md
from mantis_agent.workflow_store import list_runs, load_record, run_path, runs_dir, save_run
from mantis_agent.workflow_tool import make_workflow_tool, prepare_workflow_launch


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MANTIS_AGENT_DISABLE_WORKFLOWS", raising=False)
    yield


# ---------------------------------------------------------------------------
# A three-stage pipeline whose downstream prompts do NOT embed upstream output
# ---------------------------------------------------------------------------

PIPELINE = """---
name: chain
description: Scan then fix then verify
---

```json
{
  "inputs": [{"name": "target", "required": true, "description": "what to work on"}],
  "phases": [
    {"title": "Scan", "mode": "sequential", "agents": [
      {"label": "grep-tests", "prompt": "%(scan)s"}
    ]},
    {"title": "Fix", "mode": "sequential", "agents": [
      {"label": "patch", "prompt": "fix everything the scan flagged in {target}"}
    ]},
    {"title": "Verify", "mode": "sequential", "agents": [
      {"label": "check", "prompt": "verify the fix in {target}"}
    ]}
  ]
}
```
"""


def _chain(scan: str = "scan the test logs in {target}"):
    """The pipeline definition, with Scan's prompt as the one editable knob."""

    return parse_workflow_md(PIPELINE % {"scan": scan}, "chain")


def make_runner(text="ok", record=None):
    async def runner(prompt, *, model, agent_type, schema=None):
        if record is not None:
            record.append(prompt)
        yield AssistantMessage(content=[TextBlock(text=f"{text}:{agent_type}")],
                               usage=Usage(input_tokens=10, output_tokens=4))

    return runner


def _tool(defn, **kw):
    kw.setdefault("model", "test-model")
    kw.setdefault("definitions", [defn])
    kw.setdefault("agent_types", [])
    kw.setdefault("agent_runner", make_runner())
    return make_workflow_tool(**kw)


def _run(defn, *, calls=None, text="ok", **args):
    args.setdefault("name", defn.name)
    args.setdefault("run_in_background", False)
    tool = _tool(defn, agent_runner=make_runner(text=text, record=calls))
    return asyncio.run(tool.fn(**args))


def _labels(calls: list[str]) -> list[str]:
    """Which stages actually spent a model call, in order."""

    out = []
    for prompt in calls:
        head = prompt.split()[0]
        out.append({"scan": "Scan", "fix": "Fix", "verify": "Verify"}.get(head, head))
    return out


# ---------------------------------------------------------------------------
# The staleness scenario
# ---------------------------------------------------------------------------


def test_editing_stage_one_reruns_stages_two_and_three():
    """The headline bug. Content-keyed resume replayed Fix and Verify here."""

    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]

    calls: list[str] = []
    edited = _chain("scan the test logs AND the build logs in {target}")
    out = _run(edited, calls=calls, text="fresh",
               inputs={"target": "auth.py"}, resume_from=first)

    assert _labels(calls) == ["Scan", "Fix", "Verify"], (
        "Fix and Verify say nothing about the scan output, so their prompts are "
        "byte-identical across the two runs — they must still re-run, because "
        "the scan they were recorded against no longer exists"
    )
    assert f"resume {first} — 0 replayed, 3 re-run" in out
    assert "replayed from a previous run" not in out


def test_the_rerun_stages_see_the_new_upstream_run_not_the_recorded_one():
    """Not just 'they re-ran' — they re-ran into a *consistent* result set."""

    _run(_chain(), text="OLD", inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]

    edited = _chain("scan the test logs AND the build logs in {target}")
    _run(edited, text="NEW", inputs={"target": "auth.py"}, resume_from=first)

    latest = load_record(list_runs()[0]["run_id"])
    results = [a["result"] for ph in latest["run"]["phases"] for a in ph["agents"]]
    assert results == ["NEW:general-purpose"] * 3, (
        "a resumed run that mixes old and new agent results is the failure this "
        "whole mechanism exists to prevent"
    )


def test_editing_the_last_stage_still_replays_the_prefix():
    """Strict, not blunt: a change at the end must not invalidate the start."""

    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]

    edited = parse_workflow_md(
        (PIPELINE % {"scan": "scan the test logs in {target}"}).replace(
            "verify the fix in {target}", "verify the fix in {target}, HARDER"),
        "chain")
    calls: list[str] = []
    _run(edited, calls=calls, inputs={"target": "auth.py"}, resume_from=first)
    assert _labels(calls) == ["Verify"]


def test_an_unchanged_resume_replays_every_stage():
    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]

    calls: list[str] = []
    out = _run(_chain(), calls=calls, inputs={"target": "auth.py"}, resume_from=first)
    assert calls == []
    assert "3 replayed from a previous run" in out


def test_a_changed_input_breaks_the_prefix_where_it_first_bites():
    """The definition is untouched; only the argument moved."""

    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]

    calls: list[str] = []
    _run(_chain(), calls=calls, inputs={"target": "billing.py"}, resume_from=first)
    assert _labels(calls) == ["Scan", "Fix", "Verify"]


# ---------------------------------------------------------------------------
# Ordering inside a phase is not part of what a step IS
# ---------------------------------------------------------------------------

FANOUT = """---
name: fan
description: One pipeline phase, two stages, two items
---

```json
{
  "inputs": [{"name": "files", "required": true, "description": "one per line"}],
  "phases": [
    {"title": "Chain", "mode": "pipeline", "over": "input:files", "stages": [
      {"label": "s1", "prompt": "stage one on {item}"},
      {"label": "s2", "prompt": "stage two on {item}"}
    ]}
  ]
}
```
"""


def _slow_on(name: str):
    """A runner that makes one item's first stage finish last."""

    async def runner(prompt, *, model, agent_type, schema=None):
        if name in prompt:
            for _ in range(8):
                await asyncio.sleep(0)
        yield AssistantMessage(content=[TextBlock(text="ok")],
                               usage=Usage(input_tokens=1, output_tokens=1))

    return runner


def test_a_phase_that_finishes_out_of_order_still_replays_whole():
    """Two items racing through one pipeline phase finish in whichever order the
    scheduler picks, so a *positional* match would break the prefix on nothing
    more than timing. Steps are hashed independent-of-ordinal within a phase and
    chained across phases, which is the distinction that matters: which sibling
    went first is not part of what a step is, but which phase produced its input
    very much is."""

    defn = parse_workflow_md(FANOUT, "fan")
    tool = make_workflow_tool(model="m", definitions=[defn], agent_types=[],
                              agent_runner=_slow_on("stage one on a.py"))
    asyncio.run(tool.fn(name="fan", inputs={"files": "a.py\nb.py"},
                        run_in_background=False))
    first = list_runs()[0]["run_id"]
    ledger = load_record(first)["steps"]
    assert [s["label"] for s in ledger][:2] == ["s1·1", "s1·2"]
    assert [s["label"] for s in ledger][2:] == ["s2·2", "s2·1"], (
        "the recording must have b.py reaching stage two first for this test to "
        "be testing anything"
    )

    calls: list[str] = []
    out = _run(defn, calls=calls, inputs={"files": "a.py\nb.py"}, resume_from=first)
    assert calls == []
    assert "4 replayed from a previous run" in out


# ---------------------------------------------------------------------------
# Identity the prompt digest alone cannot see
# ---------------------------------------------------------------------------


def test_swapping_a_stage_model_or_persona_invalidates_it():
    """Same prompt, different agent: a content-keyed cache replayed it anyway."""

    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]

    swapped = parse_workflow_md(
        (PIPELINE % {"scan": "scan the test logs in {target}"}).replace(
            '{"label": "patch",', '{"label": "patch", "agent_type": "verify",'),
        "chain")
    calls: list[str] = []
    _run(swapped, calls=calls, inputs={"target": "auth.py"},
         resume_from=first)
    assert _labels(calls) == ["Fix", "Verify"], (
        "the Fix stage's prompt never changed — only the persona running it did"
    )


# ---------------------------------------------------------------------------
# v1 records: viewable, never replayable
# ---------------------------------------------------------------------------


def _v1_record_of(run_id: str) -> dict:
    """Rewrite a persisted record as a version-1 one: no ledger, no script hash."""

    rec = load_record(run_id)
    rec.pop("steps", None)
    rec.pop("script_hash", None)
    rec["version"] = 1
    run_path(run_id).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec


def test_a_v1_record_is_refused_for_replay_and_says_why():
    defn = _chain()
    _run(defn, inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    _v1_record_of(first)

    calls: list[str] = []
    out = _run(defn, calls=calls, inputs={"target": "auth.py"}, resume_from=first)
    assert _labels(calls) == ["Scan", "Fix", "Verify"]
    assert "predates the step ledger" in out, (
        "a resume that silently re-runs everything is as confusing as one that "
        "silently replays"
    )
    assert "version 1" in out


def test_a_v1_record_still_loads_and_still_lists():
    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    _v1_record_of(first)

    assert load_record(first)["run"]["phases"][0]["agents"][0]["result"]
    assert [r["run_id"] for r in list_runs()] == [first]
    assert list_runs()[0]["agents"] == 3


def test_a_v1_record_still_hands_its_inputs_to_the_resumed_run():
    """Refused for replay is not refused for everything."""

    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    _v1_record_of(first)

    launch = prepare_workflow_launch(_chain(), None, agent_runner=make_runner(),
                                     resume_from=first)
    assert launch.inputs["target"] == "auth.py"
    assert launch.cache == {}
    assert "predates the step ledger" in launch.workflow.run.log_lines[0]


def test_a_record_with_an_unreadable_ledger_re_runs_rather_than_raising():
    defn = _chain()
    _run(defn, inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    rec = load_record(first)
    rec["steps"] = [{"step_id": "not-an-int", "step_hash": "sha256:x"}]
    run_path(first).write_text(json.dumps(rec), encoding="utf-8")

    calls: list[str] = []
    out = _run(defn, calls=calls, inputs={"target": "auth.py"}, resume_from=first)
    assert _labels(calls) == ["Scan", "Fix", "Verify"]
    assert "unreadable" in out


# ---------------------------------------------------------------------------
# The resume report — never replay silently
# ---------------------------------------------------------------------------


def test_the_report_names_what_replayed_what_changed_and_what_followed():
    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]

    edited = parse_workflow_md(
        (PIPELINE % {"scan": "scan the test logs in {target}"}).replace(
            "fix everything the scan flagged", "fix ONLY the auth findings"),
        "chain")
    out = _run(edited, inputs={"target": "auth.py"}, resume_from=first)

    assert f"resume {first} — 1 replayed, 2 re-run" in out
    assert "replayed  step 1" in out and "Scan/grep-tests" in out
    assert "changed   step 2" in out and "identity changed" in out
    assert "downstream of step 2" in out


def test_the_report_survives_into_the_persisted_record():
    """Whoever reads the artifact next needs to know which results were recycled."""

    _run(_chain(), inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    _run(_chain(), inputs={"target": "auth.py"}, resume_from=first)

    lines = load_record(list_runs()[0]["run_id"])["run"]["log_lines"]
    assert any("strict-prefix replay against 3 recorded result(s)" in x for x in lines)
    assert any("3 replayed, 0 re-run" in x for x in lines)


def test_a_run_that_was_not_resumed_says_nothing_about_resume():
    out = _run(_chain(), inputs={"target": "auth.py"})
    assert "resume " not in out


# ---------------------------------------------------------------------------
# The ledger a run leaves behind
# ---------------------------------------------------------------------------


def test_a_first_run_records_a_ledger_so_the_next_resume_has_one():
    _run(_chain(), inputs={"target": "auth.py"})
    rec = load_record(list_runs()[0]["run_id"])

    assert rec["version"] == 2
    assert rec["script_hash"].startswith("sha256:")
    steps = rec["steps"]
    assert [s["step_id"] for s in steps] == [1, 2, 3]
    assert [s["phase"] for s in steps] == ["Scan", "Fix", "Verify"]
    assert [s["label"] for s in steps] == ["grep-tests", "patch", "check"]
    assert all(s["step_hash"].startswith("sha256:") for s in steps)
    # Outcomes are bound by reference into the run snapshot, never copied: two
    # copies of one agent's output can only ever disagree.
    assert [s["result_ref"] for s in steps] == ["a0", "a1", "a2"]
    assert all("result" not in s for s in steps)


def test_the_chain_moves_every_identity_downstream_of_an_edit():
    _run(_chain(), inputs={"target": "auth.py"})
    before = load_record(list_runs()[0]["run_id"])["steps"]

    _run(_chain("scan the test logs AND the build logs in {target}"),
         inputs={"target": "auth.py"})
    after = load_record(list_runs()[0]["run_id"])["steps"]

    assert [s["step_hash"] for s in before] != [s["step_hash"] for s in after]
    assert all(a["step_hash"] != b["step_hash"] for a, b in zip(before, after)), (
        "Fix and Verify are byte-identical in both definitions; only the chained "
        "parent hash makes their identities move"
    )
    assert [s["step_hash"] for s in before][1:] != [s["step_hash"] for s in after][1:]


def test_a_ledger_the_store_cannot_write_never_costs_the_run_its_record():
    """Instrumentation must not be able to fail a completed workflow."""

    class Boom:
        def to_dict(self):
            raise RuntimeError("no")

    from mantis_agent.workflow import WorkflowRun

    save_run(WorkflowRun(id="wboom", name="x", status="done"),
             definition="chain", steps=[Boom()])
    rec = load_record("wboom")
    assert rec is not None and rec["status"] == "done" and "steps" not in rec


def test_a_ledger_that_cannot_even_be_built_still_leaves_a_record(monkeypatch):
    from mantis_agent import workflow_tool

    def explode(self):
        raise RuntimeError("ledger is broken")

    monkeypatch.setattr(workflow_tool._PrefixResume, "ledger", explode)
    out = _run(_chain(), inputs={"target": "auth.py"})

    assert "status done" in out
    rec = load_record(list_runs()[0]["run_id"])
    assert "steps" not in rec, "the identity half is droppable"
    assert len(rec["run"]["phases"]) == 3, "the run snapshot is not"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_a_broken_resume_planner_cannot_fail_the_run(monkeypatch):
    """A registry problem must never turn a completed job into a failed one."""

    from mantis_agent import workflow_tool

    def explode(self, key, default):
        raise RuntimeError("planner is broken")

    monkeypatch.setattr(workflow_tool._PrefixResume, "_decide", explode)

    defn = _chain()
    _run(defn, inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    calls: list[str] = []
    out = _run(defn, calls=calls, inputs={"target": "auth.py"}, resume_from=first)

    assert "status done" in out
    assert _labels(calls) == ["Scan", "Fix", "Verify"], (
        "with the planner dead there is no evidence any result is still valid, "
        "so everything is paid for again — the safe direction to fail in"
    )


def test_a_broken_resume_report_cannot_fail_the_run(monkeypatch):
    from mantis_agent import workflow_tool

    def explode(self):
        raise RuntimeError("report is broken")

    defn = _chain()
    _run(defn, inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    monkeypatch.setattr(workflow_tool._PrefixResume, "plan", explode)
    out = _run(defn, inputs={"target": "auth.py"}, resume_from=first)
    assert "status done" in out


def test_a_missing_runs_directory_is_not_a_resume_crash():
    defn = _chain()
    _run(defn, inputs={"target": "auth.py"})
    first = list_runs()[0]["run_id"]
    for p in runs_dir().glob("*.json"):
        p.unlink()
    out = _run(defn, inputs={"target": "auth.py"}, resume_from=first)
    assert f"no persisted workflow run {first!r}" in out
