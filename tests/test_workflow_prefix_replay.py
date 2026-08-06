"""Strict-prefix resume — the fix for stale downstream replay.

The bug this file exists for
----------------------------
Resume used to be *content-keyed*: :func:`mantis_agent.workflow_store.replay_cache`
built ``{cache_key(phase, label, prompt): result}`` and
:func:`mantis_agent.workflow_defs.cache_key` hashes exactly those three values.
Nothing chains one agent's identity to the identity of the agent whose output it
consumed. So in a three-stage pipeline where stage 1's prompt is edited:

* stage 1 misses the cache (its prompt digest changed) and re-runs, producing a
  **different** result;
* stages 2 and 3 hit the cache — their own prompt text never changed — and
  replay results that were computed from the OLD stage 1 output.

The run reports success with an internally inconsistent result set, which is
worse than an outright failure because nothing announces it.
``test_content_keyed_replay_serves_stale_downstream_results`` pins that
behaviour on the legacy path so the regression is documented rather than
folklore; everything else asserts the strict-prefix model that replaces it.

The model under test
--------------------
``step_hash`` folds ``parent_step_hash`` — the digest of every step whose output
this step consumed — into each step's identity. Change stage 1 and stage 2's
hash changes too, even though stage 2's own prompt is byte-identical. Replay
then walks the ledger in order and stops at the FIRST mismatch: everything after
it runs live, whether or not its own hash would have matched. That is "longest
unchanged prefix", and it is the only rule that keeps a resumed pipeline
internally consistent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mantis_agent.workflow_steps import (
    ReplayPlan,
    Step,
    StepLedgerError,
    StepSpec,
    build_ledger,
    ledger_from_dicts,
    plan_replay,
    step_hash,
)
from mantis_agent.workflow_store import (
    RECORD_VERSION,
    load_record,
    prefix_replay,
    replay_cache,
    replay_eligible,
    run_path,
    save_run,
    step_ledger,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Every record this module writes lands in the sandbox, never in ~."""

    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


# ---------------------------------------------------------------------------
# Fixtures: a three-stage pipeline, expressed once
# ---------------------------------------------------------------------------
#
# Scan → Fix → Verify. The shape that matters: Fix and Verify do NOT interpolate
# their parent's output into their own prompt text ("fix what the scan flagged",
# not "fix: <scan output>"). That is the common authoring style — the child re-
# reads the repo, or the briefing already names the earlier phase — and it is
# precisely the shape content-keyed replay gets wrong, because the downstream
# prompt string is unchanged even when the upstream result is not.


def _pipeline(scan_prompt: str = "scan the test logs",
              *, scan_model: str = "sonnet") -> list[StepSpec]:
    return [
        StepSpec(ref="scan", phase="Scan", label="grep-tests",
                 prompt=scan_prompt, model=scan_model, agent_type="explore"),
        StepSpec(ref="fix", phase="Fix", label="patch",
                 prompt="fix everything the scan flagged",
                 model="sonnet", agent_type="general-purpose",
                 input_refs=("scan",)),
        StepSpec(ref="verify", phase="Verify", label="check",
                 prompt="verify the fixes hold",
                 model="sonnet", agent_type="verify",
                 input_refs=("fix",)),
    ]


def _recorded(specs: list[StepSpec], results: list[str],
              *, script_hash: str = "sha256:v1") -> tuple[Step, ...]:
    """A ledger as it would come back off disk: hashes plus outcomes."""

    ledger = build_ledger(specs, script_hash=script_hash)
    return tuple(
        Step(**{**s.__dict__, "status": "done", "result": r})
        for s, r in zip(ledger, results)
    )


def _run_dict(specs: list[StepSpec], results: list[str],
              run_id: str = "wfr1") -> dict[str, Any]:
    """A run snapshot shaped like ``WorkflowRun.to_dict()``."""

    return {
        "id": run_id,
        "name": "demo",
        "status": "done",
        "phases": [
            {"title": s.phase, "agents": [{
                "id": f"a{i + 1}", "label": s.label, "phase": s.phase,
                "status": "done", "prompt": s.prompt, "result": r,
            }]}
            for i, (s, r) in enumerate(zip(specs, results))
        ],
    }


def _actions(plan: ReplayPlan) -> list[str]:
    return [p.action for p in plan.steps]


# ---------------------------------------------------------------------------
# 1. The bug, pinned
# ---------------------------------------------------------------------------


def test_content_keyed_replay_serves_stale_downstream_results():
    """Legacy content-keying replays stages computed from a *changed* parent.

    This is the defect, asserted as-is. ``cache_key`` sees only (phase, label,
    prompt); Fix's and Verify's prompts are untouched by an edit to Scan, so
    their keys still hit and hand back results derived from the old Scan output.
    """

    from mantis_agent.workflow_defs import cache_key

    old = _pipeline("scan the test logs")
    record = {"version": 1, "run_id": "wfr1",
              "run": _run_dict(old, ["OLD scan", "fixed OLD", "verified OLD"])}
    cache = replay_cache(record)

    edited = _pipeline("scan the test logs AND the build logs")
    # Scan misses — its prompt digest changed. Correct, and the only part the
    # old scheme got right.
    assert cache_key("Scan", "grep-tests", edited[0].prompt) not in cache
    # Fix and Verify hit, and what they hand back was computed from "OLD scan".
    assert cache[cache_key("Fix", "patch", edited[1].prompt)] == "fixed OLD"
    assert cache[cache_key("Verify", "check", edited[2].prompt)] == "verified OLD"


def test_prefix_replay_reruns_everything_after_an_edited_step():
    """The fix: edit Scan and Fix/Verify re-run, because their identity chains."""

    old = _pipeline("scan the test logs")
    recorded = _recorded(old, ["OLD scan", "fixed OLD", "verified OLD"])

    edited = build_ledger(_pipeline("scan the test logs AND the build logs"),
                          script_hash="sha256:v1")
    plan = plan_replay(recorded, edited)

    assert _actions(plan) == ["run", "run", "run"]
    assert plan.replayed == 0 and plan.rerun == 3
    assert plan.first_change == 1
    # Nothing stale survives into the resumed run.
    assert plan.cache() == {}
    assert "identity changed" in plan.steps[0].reason
    assert "downstream of step 1" in plan.steps[1].reason
    assert "downstream of step 1" in plan.steps[2].reason


def test_middle_edit_replays_the_prefix_and_reruns_the_suffix():
    """Longest unchanged prefix: Scan replays, Fix changed, Verify follows it."""

    old = _pipeline()
    recorded = _recorded(old, ["scan out", "fix out", "verify out"])

    changed = _pipeline()
    changed[1] = StepSpec(**{**changed[1].__dict__, "prompt": "fix ONLY auth"})
    plan = plan_replay(recorded, build_ledger(changed, script_hash="sha256:v1"))

    assert _actions(plan) == ["replay", "run", "run"]
    assert plan.replayed == 1 and plan.rerun == 2
    assert plan.first_change == 2
    assert plan.results() == {1: "scan out"}


# ---------------------------------------------------------------------------
# 2. Hash chaining
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("position", [0, 1, 2])
def test_a_change_invalidates_exactly_the_suffix(position):
    """Changing step N leaves 1..N-1 identical and changes N..end."""

    base = build_ledger(_pipeline(), script_hash="sha256:v1")
    mutated = _pipeline()
    mutated[position] = StepSpec(
        **{**mutated[position].__dict__, "prompt": mutated[position].prompt + " (edited)"}
    )
    after = build_ledger(mutated, script_hash="sha256:v1")

    assert [s.step_hash for s in base[:position]] == [s.step_hash for s in after[:position]]
    for i in range(position, len(base)):
        assert base[i].step_hash != after[i].step_hash, f"step {i + 1} should have moved"


@pytest.mark.parametrize("field_name,value", [
    ("model", "opus"),
    ("agent_type", "verify"),   # the scan step is already "explore"
    ("effort", "high"),
    ("isolation", "worktree"),
    ("label", "renamed"),
    ("phase", "Renamed"),
])
def test_every_identity_field_participates(field_name, value):
    """Model, persona, effort and isolation all change what a step *is*."""

    base = build_ledger(_pipeline(), script_hash="sha256:v1")
    mutated = _pipeline()
    mutated[0] = StepSpec(**{**mutated[0].__dict__, field_name: value})
    after = build_ledger(mutated, script_hash="sha256:v1")

    assert base[0].step_hash != after[0].step_hash
    # …and the change propagates, which is the whole point of chaining.
    assert base[1].step_hash != after[1].step_hash


def test_script_hash_moves_every_step():
    """A changed script invalidates from step 1, so a resume is a full re-run."""

    a = build_ledger(_pipeline(), script_hash="sha256:aaa")
    b = build_ledger(_pipeline(), script_hash="sha256:bbb")
    assert [s.step_hash for s in a] != [s.step_hash for s in b]
    assert all(x.step_hash != y.step_hash for x, y in zip(a, b))


def test_parent_hash_is_recorded_and_order_insensitive():
    """``input_refs`` is a set of dependencies, not a sequence."""

    one = build_ledger([
        StepSpec(ref="a", phase="P", label="a", prompt="a"),
        StepSpec(ref="b", phase="P", label="b", prompt="b"),
        StepSpec(ref="c", phase="Q", label="c", prompt="c", input_refs=("a", "b")),
    ], script_hash="h")
    two = build_ledger([
        StepSpec(ref="a", phase="P", label="a", prompt="a"),
        StepSpec(ref="b", phase="P", label="b", prompt="b"),
        StepSpec(ref="c", phase="Q", label="c", prompt="c", input_refs=("b", "a")),
    ], script_hash="h")

    assert one[2].step_hash == two[2].step_hash
    assert one[2].parent_step_hash and one[2].parent_step_hash != one[0].step_hash


def test_ledger_is_deterministic_across_builds():
    """Same specs in, same hashes out — replay is meaningless otherwise."""

    a = build_ledger(_pipeline(), script_hash="sha256:v1")
    b = build_ledger(_pipeline(), script_hash="sha256:v1")
    assert [s.step_hash for s in a] == [s.step_hash for s in b]
    assert [s.step_id for s in a] == [1, 2, 3]


def test_step_hash_is_a_pure_function_of_its_arguments():
    """The free function is usable without a ledger, and prefixed for legibility."""

    kw = dict(script_hash="h", step_id=1, phase="P", label="l", prompt="p",
              model="m", agent_type="t", effort="", isolation="",
              input_refs=("x",), parent_step_hash="")
    assert step_hash(**kw) == step_hash(**kw)
    assert step_hash(**kw).startswith("sha256:")
    assert step_hash(**{**kw, "input_refs": ("y",)}) != step_hash(**kw)


def test_field_boundaries_cannot_be_forged():
    """Moving text across a field boundary must not collide.

    A naive ``"\\x00".join(fields)`` digest lets ``label="a", prompt="b"`` and
    ``label="a\\x00b", prompt=""`` hash the same. Two different steps with one
    identity is a silent stale replay, so the encoding has to be unambiguous.
    """

    kw = dict(script_hash="h", step_id=1, phase="P", model="", agent_type="",
              effort="", isolation="", input_refs=(), parent_step_hash="")
    assert step_hash(label="a", prompt="b", **kw) != step_hash(label="a\x00b", prompt="", **kw)


# ---------------------------------------------------------------------------
# 3. Prefix semantics
# ---------------------------------------------------------------------------


def test_replay_never_resumes_after_the_first_mismatch():
    """A later step whose hash still matches STILL re-runs.

    Step 3 here declares no dependency on step 2, so its own hash is untouched
    by the edit. Content-keying would replay it. Prefix replay does not: once
    the run has diverged, a step that "looks the same" may still be executing
    against a world the earlier steps changed.
    """

    specs = [
        StepSpec(ref="a", phase="P", label="a", prompt="a"),
        StepSpec(ref="b", phase="P", label="b", prompt="b"),
        StepSpec(ref="c", phase="P", label="c", prompt="c"),
        StepSpec(ref="d", phase="P", label="d", prompt="d"),
    ]
    recorded = _recorded(specs, ["ra", "rb", "rc", "rd"], script_hash="h")

    edited = list(specs)
    edited[1] = StepSpec(**{**edited[1].__dict__, "prompt": "b!"})
    after = build_ledger(edited, script_hash="h")

    # Steps 3 and 4 are byte-identical to the recording…
    assert after[2].step_hash == recorded[2].step_hash
    assert after[3].step_hash == recorded[3].step_hash
    # …and re-run anyway.
    plan = plan_replay(recorded, after)
    assert _actions(plan) == ["replay", "run", "run", "run"]
    assert [p.reason for p in plan.steps][2:] == ["downstream of step 2"] * 2


def test_an_unfinished_recorded_step_breaks_the_prefix():
    """An errored or empty step is not a result; downstream of it is unknown."""

    specs = _pipeline()
    ledger = build_ledger(specs, script_hash="h")
    recorded = (
        Step(**{**ledger[0].__dict__, "status": "done", "result": "scan out"}),
        Step(**{**ledger[1].__dict__, "status": "error", "result": ""}),
        Step(**{**ledger[2].__dict__, "status": "done", "result": "verify out"}),
    )
    plan = plan_replay(recorded, ledger)

    assert _actions(plan) == ["replay", "run", "run"]
    assert "did not finish" in plan.steps[1].reason
    assert plan.first_change == 2


def test_a_whitespace_only_result_is_not_replayable():
    """Replaying an empty result silently drops a phase's input."""

    ledger = build_ledger(_pipeline(), script_hash="h")
    recorded = tuple(
        Step(**{**s.__dict__, "status": "done", "result": r})
        for s, r in zip(ledger, ["scan out", "   \n ", "verify out"])
    )
    assert _actions(plan_replay(recorded, ledger)) == ["replay", "run", "run"]


def test_a_longer_plan_than_the_recording_reruns_the_tail():
    """New steps appended to a workflow have nothing to replay from."""

    specs = _pipeline()
    recorded = _recorded(specs[:2], ["scan out", "fix out"], script_hash="h")
    plan = plan_replay(recorded, build_ledger(specs, script_hash="h"))

    assert _actions(plan) == ["replay", "replay", "run"]
    assert "not in the recorded run" in plan.steps[2].reason
    assert plan.first_change == 3


def test_an_identical_rerun_replays_everything():
    """The happy path: nothing changed, nothing is paid for twice."""

    specs = _pipeline()
    recorded = _recorded(specs, ["scan out", "fix out", "verify out"], script_hash="h")
    plan = plan_replay(recorded, build_ledger(specs, script_hash="h"))

    assert _actions(plan) == ["replay"] * 3
    assert plan.replayed == 3 and plan.rerun == 0
    assert plan.first_change is None
    assert plan.results() == {1: "scan out", 2: "fix out", 3: "verify out"}
    assert plan.complete is True


def test_refusing_a_record_reruns_everything_with_a_stated_reason():
    """A refusal is not a silent full re-run; it names itself."""

    specs = _pipeline()
    recorded = _recorded(specs, ["a", "b", "c"], script_hash="h")
    plan = plan_replay(recorded, build_ledger(specs, script_hash="h"),
                       refuse="record predates the step ledger")

    assert _actions(plan) == ["run"] * 3
    assert plan.eligible is False
    assert plan.reason == "record predates the step ledger"
    assert all("record predates the step ledger" in p.reason for p in plan.steps)


# ---------------------------------------------------------------------------
# 4. independent=True — the opt-in fan-out exemption
# ---------------------------------------------------------------------------


def _fanout(files: list[str], *, independent: bool) -> list[StepSpec]:
    """One reviewer per file, all consuming the same Scan output."""

    return [StepSpec(ref="scan", phase="Scan", label="scan", prompt="list the files")] + [
        StepSpec(ref=f"rev-{f}", phase="Review", label=f"review-{f}",
                 prompt=f"review {f}", input_refs=("scan",), independent=independent)
        for f in files
    ]


def test_independent_siblings_survive_a_sibling_change():
    """Ten reviewers over ten files do not depend on each other."""

    old = _fanout(["a.py", "b.py", "c.py"], independent=True)
    recorded = _recorded(old, ["files", "ra", "rb", "rc"], script_hash="h")

    edited = _fanout(["a.py", "b.py", "c.py"], independent=True)
    edited[1] = StepSpec(**{**edited[1].__dict__, "prompt": "review a.py CAREFULLY"})
    plan = plan_replay(recorded, build_ledger(edited, script_hash="h"))

    assert _actions(plan) == ["replay", "run", "replay", "replay"]
    assert plan.results()[3] == "rb"


def test_independent_siblings_survive_reordering():
    """Content-keyed means position-free: an inserted sibling is not a change."""

    recorded = _recorded(_fanout(["a.py", "b.py"], independent=True),
                         ["files", "ra", "rb"], script_hash="h")
    plan = plan_replay(
        recorded,
        build_ledger(_fanout(["new.py", "a.py", "b.py"], independent=True),
                     script_hash="h"),
    )
    assert _actions(plan) == ["replay", "run", "replay", "replay"]


def test_the_default_is_strict():
    """Same fan-out without the opt-in: a sibling change re-runs the rest."""

    old = _fanout(["a.py", "b.py", "c.py"], independent=False)
    recorded = _recorded(old, ["files", "ra", "rb", "rc"], script_hash="h")

    edited = _fanout(["a.py", "b.py", "c.py"], independent=False)
    edited[1] = StepSpec(**{**edited[1].__dict__, "prompt": "review a.py CAREFULLY"})
    plan = plan_replay(recorded, build_ledger(edited, script_hash="h"))

    assert _actions(plan) == ["replay", "run", "run", "run"]
    assert StepSpec(ref="x", phase="P", label="x", prompt="x").independent is False


def test_an_ancestor_change_still_invalidates_independent_steps():
    """Independence is a claim about SIBLINGS, never about ancestors."""

    old = _fanout(["a.py", "b.py"], independent=True)
    recorded = _recorded(old, ["files", "ra", "rb"], script_hash="h")

    edited = _fanout(["a.py", "b.py"], independent=True)
    edited[0] = StepSpec(**{**edited[0].__dict__, "prompt": "list the CHANGED files"})
    plan = plan_replay(recorded, build_ledger(edited, script_hash="h"))

    assert _actions(plan) == ["run", "run", "run"]
    assert all("ancestor" in p.reason or "identity changed" in p.reason
               for p in plan.steps[1:])


def test_an_independent_step_is_position_free_but_not_identity_free():
    """Its hash drops the ordinal and keeps everything else."""

    a = build_ledger([
        StepSpec(ref="x", phase="P", label="x", prompt="x", independent=True),
    ], script_hash="h")
    b = build_ledger([
        StepSpec(ref="pad", phase="P", label="pad", prompt="pad"),
        StepSpec(ref="x", phase="P", label="x", prompt="x", independent=True),
    ], script_hash="h")
    assert a[0].step_hash == b[1].step_hash
    assert a[0].step_id == 1 and b[1].step_id == 2


def test_a_refused_record_does_not_replay_independent_steps_either():
    recorded = _recorded(_fanout(["a.py"], independent=True), ["files", "ra"],
                         script_hash="h")
    plan = plan_replay(recorded, build_ledger(_fanout(["a.py"], independent=True),
                                              script_hash="h"),
                       refuse="v1 record")
    assert _actions(plan) == ["run", "run"]


# ---------------------------------------------------------------------------
# 5. Ledger construction errors
# ---------------------------------------------------------------------------


def test_a_forward_reference_is_refused():
    """Chaining is only sound over a topologically ordered ledger."""

    with pytest.raises(StepLedgerError) as e:
        build_ledger([
            StepSpec(ref="a", phase="P", label="a", prompt="a", input_refs=("b",)),
            StepSpec(ref="b", phase="P", label="b", prompt="b"),
        ])
    assert "'b'" in str(e.value) and "later" in str(e.value)


def test_a_duplicate_ref_is_refused():
    """Two steps answering to one name makes ``input_refs`` ambiguous."""

    with pytest.raises(StepLedgerError) as e:
        build_ledger([
            StepSpec(ref="a", phase="P", label="a", prompt="a"),
            StepSpec(ref="a", phase="P", label="a2", prompt="a2"),
        ])
    assert "'a'" in str(e.value)


def test_an_unknown_ref_still_contributes_to_identity():
    """``input:target`` names something outside the ledger; it is not dropped."""

    one = build_ledger([StepSpec(ref="a", phase="P", label="a", prompt="a",
                                 input_refs=("input:target",))])
    two = build_ledger([StepSpec(ref="a", phase="P", label="a", prompt="a",
                                 input_refs=("input:other",))])
    assert one[0].step_hash != two[0].step_hash
    assert one[0].parent_step_hash == ""


def test_unnamed_steps_are_allowed():
    """``ref`` is only needed by steps something else depends on."""

    ledger = build_ledger([
        StepSpec(phase="P", label="a", prompt="a"),
        StepSpec(phase="P", label="b", prompt="b"),
    ])
    assert [s.step_id for s in ledger] == [1, 2]


# ---------------------------------------------------------------------------
# 6. The resume report — never replay silently
# ---------------------------------------------------------------------------


def test_the_report_names_what_replayed_and_what_reran():
    specs = _pipeline()
    recorded = _recorded(specs, ["scan out", "fix out", "verify out"], script_hash="h")
    edited = _pipeline()
    edited[1] = StepSpec(**{**edited[1].__dict__, "prompt": "fix ONLY auth"})

    lines = plan_replay(recorded, build_ledger(edited, script_hash="h")).report_lines("wfr1")
    text = "\n".join(lines)

    assert "wfr1" in lines[0]
    assert "1 replayed" in lines[0] and "2 re-run" in lines[0]
    assert "replayed" in text and "re-run" in text
    assert "Scan" in text and "Fix" in text and "Verify" in text
    assert "step 1" in text


def test_the_report_collapses_consecutive_runs_into_ranges():
    """A 200-step resume must not print 200 lines."""

    specs = [StepSpec(ref=f"s{i}", phase="P", label=f"s{i}", prompt=f"p{i}")
             for i in range(12)]
    recorded = _recorded(specs, [f"r{i}" for i in range(12)], script_hash="h")
    edited = list(specs)
    edited[8] = StepSpec(**{**edited[8].__dict__, "prompt": "changed"})

    lines = plan_replay(recorded, build_ledger(edited, script_hash="h")).report_lines()
    assert len(lines) <= 5
    assert any("step 1–8" in ln for ln in lines)
    assert any("step 10–12" in ln for ln in lines)


def test_a_fully_replayed_run_still_reports():
    specs = _pipeline()
    recorded = _recorded(specs, ["a", "b", "c"], script_hash="h")
    lines = plan_replay(recorded, build_ledger(specs, script_hash="h")).report_lines()
    assert "3 replayed" in lines[0] and "0 re-run" in lines[0]


# ---------------------------------------------------------------------------
# 7. The v2 record: ledger persistence and v1 refusal
# ---------------------------------------------------------------------------


def test_record_version_is_two():
    assert RECORD_VERSION == 2


def test_the_ledger_round_trips_through_a_saved_record():
    specs = _pipeline()
    ledger = build_ledger(specs, script_hash="sha256:v1")
    save_run(_run_dict(specs, ["scan out", "fix out", "verify out"]),
             definition="demo", steps=ledger, script_hash="sha256:v1")

    rec = load_record("wfr1")
    assert rec["version"] == 2
    assert rec["script_hash"] == "sha256:v1"
    assert [s["step_hash"] for s in rec["steps"]] == [s.step_hash for s in ledger]

    back = step_ledger(rec)
    assert [s.step_hash for s in back] == [s.step_hash for s in ledger]
    assert [s.step_id for s in back] == [1, 2, 3]
    assert [s.independent for s in back] == [False, False, False]
    assert back[1].input_refs == ("scan",)


def test_saving_without_a_ledger_still_writes_a_v2_record():
    """Callers that have not adopted steps yet keep working — and stay unresumable."""

    save_run(_run_dict(_pipeline(), ["a", "b", "c"]), definition="demo")
    rec = load_record("wfr1")
    assert rec["version"] == 2
    assert "steps" not in rec
    ok, why = replay_eligible(rec)
    assert ok is False and "ledger" in why


def test_a_v1_record_loads_and_views_but_refuses_replay():
    """Old history stays readable; it just cannot be resumed under v2 rules."""

    from mantis_agent.workflow_store import list_runs

    specs = _pipeline()
    legacy = {"version": 1, "run_id": "old1", "definition": "demo",
              "status": "done", "saved_at": 1.0,
              "run": _run_dict(specs, ["a", "b", "c"], run_id="old1")}
    p = run_path("old1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(legacy), encoding="utf-8")

    rec = load_record("old1")
    assert rec is not None and rec["version"] == 1
    assert [r["run_id"] for r in list_runs()] == ["old1"]      # viewable

    ok, why = replay_eligible(rec)
    assert ok is False and "version 1" in why

    plan = prefix_replay(rec, build_ledger(specs, script_hash="h"))
    assert _actions(plan) == ["run", "run", "run"]
    assert plan.eligible is False and "version 1" in plan.reason
    assert plan.cache() == {}


def test_prefix_replay_over_a_v2_record_replays_the_prefix():
    specs = _pipeline()
    ledger = build_ledger(specs, script_hash="sha256:v1")
    save_run(_run_dict(specs, ["scan out", "fix out", "verify out"]),
             definition="demo", steps=ledger, script_hash="sha256:v1")
    rec = load_record("wfr1")

    edited = _pipeline()
    edited[2] = StepSpec(**{**edited[2].__dict__, "prompt": "verify HARDER"})
    plan = prefix_replay(rec, build_ledger(edited, script_hash="sha256:v1"))

    assert _actions(plan) == ["replay", "replay", "run"]
    assert plan.eligible is True
    assert plan.results() == {1: "scan out", 2: "fix out"}


def test_a_result_ref_is_resolved_from_the_run_dict():
    """Big payloads live once, in the run; the ledger points at them."""

    specs = _pipeline()
    ledger = build_ledger(specs, script_hash="h")
    ledger = tuple(Step(**{**s.__dict__, "status": "done", "result_ref": f"a{i + 1}"})
                   for i, s in enumerate(ledger))
    save_run(_run_dict(specs, ["scan out", "fix out", "verify out"]),
             definition="demo", steps=ledger, script_hash="h")

    rec = load_record("wfr1")
    assert all("result" not in s for s in rec["steps"])        # not duplicated
    assert [s.result for s in step_ledger(rec)] == ["scan out", "fix out", "verify out"]

    plan = prefix_replay(rec, build_ledger(specs, script_hash="h"))
    assert _actions(plan) == ["replay"] * 3


def test_a_corrupt_ledger_entry_refuses_replay_rather_than_guessing():
    specs = _pipeline()
    save_run(_run_dict(specs, ["a", "b", "c"]), definition="demo",
             steps=build_ledger(specs, script_hash="h"), script_hash="h")
    p = run_path("wfr1")
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["steps"][1] = {"step_id": "not-an-int"}
    p.write_text(json.dumps(rec), encoding="utf-8")

    loaded = load_record("wfr1")
    ok, why = replay_eligible(loaded)
    assert ok is False and "ledger" in why
    assert _actions(prefix_replay(loaded, build_ledger(specs, script_hash="h"))) == ["run"] * 3


def test_ledger_from_dicts_ignores_unknown_fields():
    """Forward compatibility: a v3 field must not break a v2 reader."""

    ledger = build_ledger(_pipeline(), script_hash="h")
    raw = [{**s.to_dict(), "future_field": 1} for s in ledger]
    assert [s.step_hash for s in ledger_from_dicts(raw)] == [s.step_hash for s in ledger]


# ---------------------------------------------------------------------------
# 8. The legacy surface stays exactly where it was
# ---------------------------------------------------------------------------


def test_replay_cache_keeps_its_signature_and_content_keying():
    """workflow_tool.py still calls this; the v2 record must not disturb it."""

    from mantis_agent.workflow_defs import cache_key

    specs = _pipeline()
    save_run(_run_dict(specs, ["scan out", "fix out", "verify out"]),
             definition="demo", steps=build_ledger(specs, script_hash="h"),
             script_hash="h")
    cache = replay_cache(load_record("wfr1"))
    assert cache[cache_key("Scan", "grep-tests", specs[0].prompt)] == "scan out"


def test_save_run_still_accepts_its_original_call_shape():
    path = save_run(_run_dict(_pipeline(), ["a", "b", "c"]), definition="demo",
                    inputs={"target": "src", "api_key": "sk-live"},
                    job_id=7, result={"replayed": 0, "cost_usd": 1.5, "phases": []})
    rec = load_record("wfr1")
    assert path.endswith("wfr1.json")
    assert rec["inputs"]["api_key"] == "[redacted]"          # redaction unchanged
    assert rec["job_id"] == 7 and rec["summary"]["cost_usd"] == 1.5
