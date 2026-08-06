"""``workflow`` — run orchestration the model writes, or a NAMED definition.

The model-facing door to the orchestration stack. Two ways in:

* ``script`` — the model writes the orchestration itself, in the restricted
  Python subset :mod:`mantis_agent.workflow_script` defines. This reaches the
  whole engine: phases, ``parallel``, ``pipeline``, loops that run until a
  search goes dry, per-agent model and persona choices.
* ``name`` — run a definition someone wrote down as a ``.md`` file, with
  inputs. Declarative, reviewable, and the right shape for orchestration a team
  wants to keep. See :mod:`mantis_agent.workflow_defs`.

This module previously offered only the second, on the reasoning that
model-authored code should not be ``exec()``-ed in the user's process and that
"everything that matters about a workflow is data, not control flow". The first
half of that is still respected — scripts run through an AST allowlist with no
imports, no dunder access and no filesystem, and the agents they spawn inherit
the parent's permissions and budget unchanged, so authoring orchestration never
widens what the agents inside it may do. The second half turned out to be too
strong: a declarative definition cannot express *loop until two consecutive
rounds find nothing new*, or *scale the fan-out to the remaining budget*, and
those are the shapes that make an engine worth having. Note what the sandbox is
NOT: a security boundary against a hostile script. The author is the session's
own model, which in most configurations can already run shell commands. It is a
guardrail that keeps a confused or injected script inside its job.

Shape of a call
---------------
1. Resolve ``name`` against the definitions (project > user > builtin).
2. Validate inputs; a missing required input comes back as a fixable message,
   not an exception.
3. Build the live :class:`~mantis_agent.workflow.Workflow` **before** running,
   so the run id exists immediately and ``on_run`` can register it with the
   viewer while it is still queued.
4. Detach it through the :class:`~mantis_agent.jobs.JobManager` and return the
   run id + job id **now**. The model keeps working; ``/workflows`` and
   ``/jobs`` show the same run from both angles; completion arrives as a job
   notification like any other background task.

Cost + opt-in
-------------
A workflow spawns many agents and can burn a lot of tokens, so the tool
description is explicit that it is only for work the user actually asked to be
orchestrated. ``$MANTIS_AGENT_DISABLE_WORKFLOWS=1`` turns the tool into a
clear refusal for environments that want it off entirely.

Tests inject a fake ``agent_runner``; nothing here touches a model on its own.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .tools import Tool, tool
from .workflow import Workflow, make_agent_runner, wrap_runner_with_progress
from .workflow_steps import (
    HASH_PREFIX,
    REPLAY,
    RUN,
    ReplayPlan,
    Step,
    StepPlan,
    fold_parents,
    step_hash,
)
from .workflow_view import total_tokens
from .workflow_defs import (
    WorkflowDefinition,
    WorkflowDefinitionError,
    discover_workflow_definitions,
    execute_definition,
    resolve_inputs,
)

__all__ = [
    "WorkflowLaunch",
    "attach_job_progress",
    "format_workflow_report",
    "make_workflow_tool",
    "prepare_workflow_launch",
    "workflows_enabled",
]

_DISABLE_ENV = "MANTIS_AGENT_DISABLE_WORKFLOWS"

_log = logging.getLogger("mantis_agent.workflow_tool")


def workflows_enabled() -> bool:
    """False when ``$MANTIS_AGENT_DISABLE_WORKFLOWS`` is set to a truthy value."""

    return os.environ.get(_DISABLE_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Strict-prefix resume
# ---------------------------------------------------------------------------
#
# Resume used to be content-keyed: ``execute_definition`` looks each agent up by
# ``cache_key(phase, label, prompt)`` and replays anything whose three values
# match a persisted run. That is correct for independent fan-out and WRONG for
# pipelines. Edit stage 1 of Scan → Fix → Verify and resume: stage 1 misses and
# re-runs, producing a different result, while stages 2 and 3 hit — their own
# prompt text never changed — and hand back results computed from the OLD stage
# 1. The run reports success with an internally inconsistent result set, which
# is worse than a failure because nothing announces it.
#
# :mod:`mantis_agent.workflow_steps` fixes the model (chained ``step_hash``, stop
# replaying at the first mismatch); this is the wiring that puts it on the path
# the product actually takes. The engine hands a definition's steps over one at a
# time — a phase's prompts are rendered from the *previous* phase's output, so
# there is no ledger to build up front — so the plan is built incrementally,
# through the one hook ``execute_definition`` already offers: the cache lookup.


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return HASH_PREFIX + hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()


def _definition_digest(defn: WorkflowDefinition) -> str:
    """Content hash of everything about a definition that changes what it does."""

    return _digest({
        "name": defn.name,
        "model": defn.model,
        "default_agent_type": defn.default_agent_type,
        "briefing": defn.briefing,
        "phases": [p.to_dict() for p in defn.phases],
    })


def _phase_salts(defn: WorkflowDefinition) -> dict[str, str]:
    """``{phase title: digest}`` — the part of a step's identity the prompt misses.

    The cache lookup only carries phase, label and a prompt digest, so an edit to
    a spec's ``model`` or ``agent_type`` would otherwise be invisible to replay:
    same prompt, different agent, stale hit. Folding the phase's whole spec (plus
    the definition-wide model and default persona) into every step of that phase
    closes it. Per phase rather than per definition on purpose — editing the last
    phase must not invalidate the first, which is the entire point of a *prefix*."""

    base = {"model": defn.model, "default_agent_type": defn.default_agent_type}
    return {ph.title: _digest({"defn": base, "phase": ph.to_dict()}) for ph in defn.phases}


def _split_cache_key(key: Any) -> tuple[str, str, str]:
    """``cache_key`` is ``phase \\x00 label \\x00 prompt-digest``; take it apart."""

    parts = str(key).split("\x00")
    if len(parts) != 3:
        return str(key), "", ""
    return parts[0], parts[1], parts[2]


def _recorded_phases(ledger: Any) -> list[tuple[str, dict[str, list[Any]]]]:
    """Group a persisted ledger into consecutive phases, indexed by step hash.

    A list of buckets per hash rather than one entry: a fan-out may legitimately
    contain two steps with identical identity, and they replay one-for-one."""

    groups: list[tuple[str, dict[str, list[Any]]]] = []
    for step in ledger:
        if not groups or groups[-1][0] != step.phase:
            groups.append((step.phase, {}))
        groups[-1][1].setdefault(step.step_hash, []).append(step)
    return groups


# Reasons, written to be read by the user in the resume report. The first must
# stay equal to workflow_steps' own "identity changed" so ReplayPlan.report_lines
# tags that group `changed` rather than the generic `re-run`.
_CHANGED = "identity changed"
_UNCHANGED = "unchanged"
_INCOMPLETE = "recorded step did not finish"


class _PrefixResume(dict):
    """The resume decision, made one step at a time as the engine asks.

    Passed to ``execute_definition`` as its ``cache``: every agent it is about to
    run looks itself up here first, in dependency order, and this decides. Two
    jobs, both of which need to see every step:

    * **Replay** — hand back a recorded result while the prefix is intact, and
      nothing at all once it has broken. "Broken" is sticky and global: after the
      first mismatch every later step runs live *even when its own hash matches*,
      because the steps ahead of it no longer produced what it was recorded
      against. That is the whole correctness fix.
    * **Record** — build the ledger this run persists, so the *next* resume has
      the dependency information this one needed.

    Identity is :func:`mantis_agent.workflow_steps.step_hash` with the phase salt
    as ``script_hash`` and the previous phases folded into ``parent_step_hash``,
    so a change anywhere upstream changes every hash downstream of it. Steps are
    hashed ``independent=True`` — position *within* a phase is dropped, because a
    definition's phases are barriers and the agents inside one all read the same
    context, so which one the scheduler happened to start first is not part of
    what a step *is*. Ordering that does matter is carried by the phase chain.

    Every entry point is failure-isolated. This is instrumentation bolted onto a
    working engine: a bug in here must cost a resume its cache, never cost the
    user a completed run."""

    def __init__(self, defn: WorkflowDefinition, record: Any = None) -> None:
        super().__init__()
        self._salts = _phase_salts(defn)
        self._fallback_salt = _definition_digest(defn)
        self._groups: list[tuple[str, dict[str, list[Any]]]] = []
        self._refuse = ""
        self._candidates: dict[str, str] = {}
        if record is not None:
            self._load(record)
        self._planned: list[Step] = []
        self._plans: list[StepPlan] = []
        self._broken = False
        self._first_change: int | None = None
        self._phase: str | None = None
        self._phase_hashes: list[str] = []
        self._parent = ""
        self._group_index = -1
        self._available: dict[str, list[Any]] = {}
        self._phase_miss = ""

    def _load(self, record: Any) -> None:
        """Read the recorded ledger, or record *why* it cannot be used.

        A v1 record has no ledger at all, so there is no way to tell which stored
        result depended on which — prefix rules over it would be guesswork
        dressed up as a cache hit. It still loads and still views; it is refused
        only for replay, and the refusal is carried through to the report rather
        than being silently turned into a slow run."""

        from .workflow_store import replay_eligible, step_ledger  # noqa: PLC0415

        try:
            ok, why = replay_eligible(record)
            if not ok:
                self._refuse = why
                return
            ledger = step_ledger(record)
            self._groups = _recorded_phases(ledger)
            self._candidates = {s.step_hash: s.result for s in ledger if s.replayable}
        except Exception as e:  # noqa: BLE001 — an unreadable record re-runs, never raises
            _log.debug("unusable step ledger for resume: %r", e)
            self._refuse = "the recorded step ledger could not be read — everything re-runs"

    # -- the engine's view -------------------------------------------------

    def __bool__(self) -> bool:
        # ``execute_definition`` does ``cache = cache or {}``, and an empty dict
        # is falsy. Every run needs the recording half even when there is nothing
        # to replay, so this object is always worth keeping.
        return True

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        try:
            return self._decide(key, default)
        except Exception as e:  # noqa: BLE001 — see the class docstring
            _log.debug("resume bookkeeping failed at %r: %r", key, e)
            self._broken = True
            return default

    def _decide(self, key: Any, default: Any) -> Any:
        phase, label, prompt_digest = _split_cache_key(key)
        if phase != self._phase:
            self._open_phase(phase)
        step_id = len(self._planned) + 1
        digest = step_hash(
            script_hash=self._salts.get(phase, self._fallback_salt),
            phase=phase,
            label=label,
            prompt=prompt_digest,
            parent_step_hash=self._parent,
            independent=True,
        )
        result = ""
        if self._refuse:
            action, reason = RUN, self._refuse
        elif self._broken:
            action, reason = RUN, f"downstream of step {self._first_change}"
        else:
            queue = self._available.get(digest) or []
            was = queue.pop(0) if queue else None
            if was is None:
                action, reason = RUN, (self._phase_miss or _CHANGED)
            elif not was.replayable:
                action, reason = RUN, _INCOMPLETE
            else:
                action, reason, result = REPLAY, _UNCHANGED, was.result
            if action == RUN:
                self._broken = True
                self._first_change = step_id

        self._phase_hashes.append(digest)
        self._planned.append(Step(
            step_id=step_id, step_hash=digest, parent_step_hash=self._parent,
            phase=phase, label=label, independent=True,
        ))
        self._plans.append(StepPlan(
            step_id=step_id, step_hash=digest, action=action, reason=reason,
            phase=phase, label=label, independent=True, result=result,
        ))
        return result if action == REPLAY else default

    def _open_phase(self, title: str) -> None:
        """Seal the phase that just ended and line the next one up.

        ``execute_definition`` runs phases strictly one after another and feeds
        each one's results into the next phase's context, so a phase boundary is
        exactly where the dependency lives: folding the finished phase's hashes
        into ``parent`` is what makes an edit in phase 1 move every identity in
        phases 2 and 3."""

        if self._phase is not None:
            self._parent = fold_parents([self._parent, *self._phase_hashes])
        self._phase = title
        self._phase_hashes = []
        self._group_index += 1
        group = (self._groups[self._group_index]
                 if 0 <= self._group_index < len(self._groups) else None)
        if group is None:
            self._available, self._phase_miss = {}, "not in the recorded run"
        elif group[0] != title:
            self._available = {}
            self._phase_miss = f"phase order changed (recorded {group[0]!r} here)"
        else:
            self._available = {h: list(v) for h, v in group[1].items()}
            self._phase_miss = ""

    # -- what the caller reads back ---------------------------------------

    @property
    def refused(self) -> str:
        """Why the recorded run could not be replayed from at all, if it could not."""

        return self._refuse

    @property
    def candidates(self) -> dict[str, str]:
        """``{step_hash: result}`` the record offers — an upper bound, not a promise."""

        return dict(self._candidates)

    def ledger(self) -> tuple[Step, ...]:
        """This run's ordered step ledger, for persistence.

        Outcomes are left empty: :func:`workflow_store.bind_run_results` pairs
        them with the run snapshot by ``(phase, label)`` on the way to disk, so
        the result of every agent is stored once rather than twice."""

        return tuple(self._planned)

    def plan(self) -> ReplayPlan:
        """What replayed, what re-ran, and why — ready to be shown."""

        return ReplayPlan(steps=tuple(self._plans), eligible=not self._refuse,
                          reason=self._refuse, first_change=self._first_change)


# ---------------------------------------------------------------------------
# Launch — a prepared, not-yet-running workflow
# ---------------------------------------------------------------------------


@dataclass
class WorkflowLaunch:
    """A workflow that is built and registered but has not started.

    Splitting *prepare* from *execute* is what makes backgrounding honest: the
    run id, the phase rail and the control handle all exist before the first
    token is spent, so the tool can return an id immediately and the viewer has
    something real to show while the run is still queued."""

    definition: WorkflowDefinition
    workflow: Workflow
    inputs: dict[str, str]
    resume_from: str = ""
    cache: dict[str, str] = field(default_factory=dict)
    valid_agent_types: tuple[str, ...] | None = None
    persist: bool = True
    resume: Any = None

    def __post_init__(self) -> None:
        # Present even for a first run: the ledger it records is what makes the
        # NEXT resume strict-prefix rather than a guess.
        if self.resume is None:
            self.resume = _PrefixResume(self.definition)

    @property
    def run_id(self) -> str:
        return self.workflow.run.id

    async def execute(self) -> dict[str, Any]:
        """Run to completion, persist the artifact, and return the result dict.

        Persistence is best-effort and happens even when the run failed or was
        stopped — a stopped run you can inspect afterwards is worth more than a
        clean failure that leaves nothing behind."""

        try:
            result = await execute_definition(
                self.definition,
                workflow=self.workflow,
                inputs=self.inputs,
                cache=self.resume,
                valid_agent_types=self.valid_agent_types,
            )
        except BaseException:
            # Close the run out before persisting: a saved artifact stuck at
            # "running" would read as a live workflow forever.
            run = self.workflow.run
            if run.status == "running":
                run.status = "error"
            if run.ended is None:
                run.ended = self.workflow._clock()
            self._report_resume(None)
            self._persist(None)
            raise
        self._report_resume(result)
        self._persist(result)
        return result

    def _report_resume(self, result: Any) -> None:
        """State what replayed and what re-ran, on the run and in the report.

        A resume that does not explain itself is not an improvement: a user who
        cannot tell which results are fresh cannot trust any of them. This fires
        for a failed run too — a partial resume is exactly when knowing which
        results were recycled matters most."""

        if not self.resume_from:
            return
        try:
            lines = self.resume.plan().report_lines(self.resume_from)
            for line in lines:
                self.workflow.log(line)
            if isinstance(result, dict):
                result["log_lines"] = list(self.workflow.run.log_lines)
        except Exception as e:  # noqa: BLE001 — a report must never fail a run
            _log.debug("resume report failed for %s: %r", self.run_id, e)

    def _persist(self, result: Any) -> None:
        if not self.persist:
            return
        # The ledger is an optimisation for the NEXT run; the run snapshot is the
        # only copy of what just happened. Build the identity half first and drop
        # it on its own if it fails, so a bug in resume bookkeeping can never be
        # what loses an expensive run its record.
        steps: tuple[Any, ...] = ()
        digest = ""
        try:
            steps = self.resume.ledger()
            digest = _definition_digest(self.definition)
        except Exception as e:  # noqa: BLE001
            _log.debug("dropping step ledger for %s: %r", self.run_id, e)
        try:
            from .workflow_store import save_run  # noqa: PLC0415

            save_run(
                self.workflow.run,
                definition=self.definition.name,
                inputs=self.inputs,
                job_id=self.workflow.run.job_id,
                result=result,
                steps=steps,
                script_hash=digest,
            )
        except Exception:  # noqa: BLE001 — history is observability, never control flow
            pass


def prepare_workflow_launch(
    defn: WorkflowDefinition,
    raw_inputs: Any = None,
    *,
    agent_runner: Any,
    model: str = "",
    budget: Any = None,
    concurrency: int | None = None,
    on_event: Any = None,
    resume_from: str = "",
    valid_agent_types: Any = None,
    persist: bool = True,
) -> WorkflowLaunch:
    """Validate inputs, build the :class:`Workflow`, and plan any resume.

    Resume is **strict-prefix**, not content-keyed: see :class:`_PrefixResume`.
    A record that cannot support those rules (a v1 record, written before the
    step ledger existed) is still loaded, still merges its inputs and is still
    viewable — it is refused for *replay* only, with the reason carried into the
    run log and the report instead of everything quietly re-running.

    Raises :class:`WorkflowDefinitionError` when a required input is missing or
    the named resume record cannot be read — both are user-fixable, so they
    carry a message meant to be shown verbatim."""

    resume: Any = None
    given = dict(raw_inputs) if isinstance(raw_inputs, dict) else raw_inputs
    if resume_from:
        from .workflow_store import load_record  # noqa: PLC0415

        record = load_record(resume_from)
        if record is None:
            raise WorkflowDefinitionError(
                f"no persisted workflow run {resume_from!r} to resume from"
            )
        stored = record.get("definition") or ""
        if stored and stored != defn.name:
            raise WorkflowDefinitionError(
                f"run {resume_from!r} was workflow {stored!r}, not {defn.name!r}"
            )
        resume = _PrefixResume(defn, record)
        # Stored inputs fill the gaps BEFORE validation, so a bare
        # ``/workflows resume <id>`` repeats the original run without making the
        # user retype its arguments. Redacted values are never resurrected —
        # they're gone, and a required one has to be supplied again.
        inherited = {k: v for k, v in (record.get("inputs") or {}).items()
                     if v != "[redacted]"}
        if inherited:
            merged = dict(inherited)
            merged.update(given if isinstance(given, dict) else {})
            if not isinstance(given, dict) and given:
                merged["objective"] = str(given)
            given = merged

    inputs = resolve_inputs(defn, given)

    wf = Workflow(
        name=defn.name,
        agent_runner=agent_runner,
        on_event=on_event,
        budget=budget,
        model=model or defn.model,
        concurrency=concurrency,
    )
    wf.run.definition = defn.name
    wf.run.resumed_from = resume_from or ""
    if resume is not None:
        if resume.refused:
            wf.log(f"resuming from {resume_from} — {resume.refused}")
        else:
            wf.log(f"resuming from {resume_from} — strict-prefix replay against "
                   f"{len(resume.candidates)} recorded result(s)")

    return WorkflowLaunch(
        definition=defn,
        workflow=wf,
        inputs=inputs,
        resume_from=resume_from or "",
        cache=resume.candidates if resume is not None else {},
        valid_agent_types=tuple(valid_agent_types) if valid_agent_types else None,
        persist=persist,
        resume=resume,
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_workflow_report(result: dict[str, Any], *, definition: str = "") -> str:
    """Render an execution result as the text report the parent model reads."""

    lines: list[str] = []
    name = result.get("definition") or definition or result.get("name") or "workflow"
    lines.append(f"# Workflow report — {name}")
    agents = result.get("agents") or []
    done = [a for a in agents if a.get("status") == "done"]
    replayed = int(result.get("replayed") or 0)
    head = (
        f"run {result.get('workflow_id', '?')} · status {result.get('status', '?')} "
        f"· {len(done)}/{len(agents)} agents completed"
    )
    if replayed:
        head += f" · {replayed} replayed from a previous run"
    job_id = result.get("job_id")
    if job_id is not None:
        head += f" · job #{job_id}"
    lines.append(head)
    if result.get("status") == "cancelled":
        lines.append("NOTE: this run was stopped before completing.")
    failed = [a for a in agents if a.get("status") == "error"]
    if failed:
        lines.append(
            "NOTE: " + ", ".join(str(a.get("label")) for a in failed) + " failed."
        )
    for line in result.get("log_lines") or []:
        lines.append(f"log: {line}")

    for ph in result.get("phases") or []:
        title = ph.get("title") or "phase"
        lines.append(f"\n## {title}")
        outs = [o for o in (ph.get("results") or []) if (o or "").strip()]
        if not outs:
            lines.append("(no output)")
        for out in outs:
            lines.append(out.strip())
    return "\n".join(lines)


def _launch_receipt(launch: WorkflowLaunch, job: Any) -> str:
    """What the model gets back the instant a background workflow starts."""

    defn = launch.definition
    lines = [
        f"Workflow '{defn.name}' started in the background.",
        f"run: {launch.run_id} · job: #{getattr(job, 'id', '?')} · "
        f"{len(defn.phases)} phases · ~{defn.min_agents}+ agents",
    ]
    if launch.resume_from:
        refused = getattr(launch.resume, "refused", "")
        lines.append(
            f"resumed from {launch.resume_from} — {refused}" if refused else
            f"resumed from {launch.resume_from} — up to {len(launch.cache)} recorded "
            "result(s) can replay; the report names exactly which ones did."
        )
    lines.append(
        "It runs while you keep working; the full report arrives as a job "
        "notification when it finishes. The user can watch it live with "
        "/workflows (phase rail, per-agent status, stop/pause) or /jobs. "
        f"Read the result early with job_output({getattr(job, 'id', 0)})."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema + factory
# ---------------------------------------------------------------------------


_SCRIPT_HELP = """\
Orchestration you write, in a small Python subset. Use this when the shape of \
the work is not a plain fan-out. Start with a literal `meta` dict, then use:

  await agent(prompt, label=, phase=, agent_type=, model=, schema=) -> str
  await parallel([thunks]) -> list      # barrier: waits for all
  await pipeline(items, *stages) -> list  # per-item chains, NO barrier
  phase(title) · log(msg) · args · budget.total/spent()/remaining() · json

Thunks are zero-arg lambdas that CALL agent, and must bind loop variables by \
default argument: [(lambda d=d: agent(f"review {d}", label=d)) for d in dims].

DEFAULT TO pipeline over parallel-then-parallel. pipeline lets item B enter \
stage 2 while item A is still in stage 1; a barrier makes every fast item wait \
for the slowest. Only use parallel between stages when a stage genuinely needs \
ALL prior results at once (dedup across the whole set, an early exit on a total \
count, or a prompt that compares findings to each other).

`return` whatever the caller should act on — it comes back as the result.

Available: loops, conditionals, comprehensions, f-strings, try/except, def, \
lambda. NOT available: imports, file/network access, attributes starting with \
'_'. Anything outside orchestration is an agent's job, where permissions apply.

Example:

  meta = {"name": "review-diff", "phases": [{"title": "Review"}, \
{"title": "Verify"}]}
  phase("Review")
  reviews = await parallel([(lambda d=d: agent(f"Review {args['target']} for \
{d} problems", label=d)) for d in ["correctness", "security", "perf"]])
  phase("Verify")
  checked = await pipeline(reviews, lambda r: agent(f"Try to REFUTE these \
findings, do not just confirm them:\\n{r}", agent_type="verify"))
  return {"reviews": reviews, "verdicts": checked}"""


def _workflow_schema(names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": _SCRIPT_HELP},
            "args": {
                "description": (
                    "Value exposed to the script as `args` — the target, the "
                    "question, a list of paths. Agents start with NO memory of "
                    "this conversation, so put the full paths and constraints here."
                ),
            },
            "name": {
                "type": "string",
                "enum": names,
                "description": (
                    "Run a SAVED workflow definition instead of a script. "
                    "Mutually exclusive with 'script'."
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    "Values for the workflow's declared inputs, e.g. "
                    '{"target": "the diff in mantis_agent/agent.py"}. Each value is '
                    "substituted into the agents' prompts, and every agent starts "
                    "with NO memory of this conversation — so spell out paths, "
                    "names and constraints in full."
                ),
                "additionalProperties": {"type": "string"},
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Detach the run and return a run id + job id immediately "
                    "(default true). Set false only when you cannot continue "
                    "without the report and the workflow is small."
                ),
            },
            "resume_from": {
                "type": "string",
                "description": (
                    "Run id of a previous run of this same workflow. Replay is "
                    "strict-prefix: agents replay from that run's stored results "
                    "while nothing has changed, and from the FIRST change onward "
                    "everything re-runs — including agents whose own prompt is "
                    "unchanged, since they would otherwise be acting on results "
                    "an earlier step has since replaced. The report says which "
                    "agents replayed and which re-ran."
                ),
            },
        },
        # Neither is required at the schema level: the tool itself explains the
        # script-or-name choice, which reads better than a oneOf the model has
        # to decode from a validation error.
    }


def _tool_description(defns: list[WorkflowDefinition]) -> str:
    lines = [
        "Run a multi-agent workflow: phases of subagents that fan out in "
        "parallel, pipeline per item, and verify each other. Returns IMMEDIATELY "
        "with a run id and a background job id; the user watches live progress "
        "with /workflows.",
        "",
        "Two ways to call it:",
        "- script: YOU write the orchestration (see the 'script' parameter). "
        "Use this when the shape is not a plain fan-out — per-item pipelines, "
        "loop until a search goes dry, scale the fan-out to the budget, a "
        "different persona or model per stage.",
        "- name: run one of the saved definitions listed below, with inputs.",
        "",
        "ONLY call this when the user actually asked for orchestration — they "
        "said 'run the <name> workflow', 'use a workflow', 'fan out agents', or "
        "invoked a command that runs one. A workflow spawns many agents and can "
        "cost far more than a single task call, so that scale must be requested, "
        "not inferred. For one focused delegation use `task`; for a decomposition "
        "you want handled for you, `coordinate` runs the standard fan-out → "
        "synthesize → verify shape without you writing it.",
        "",
        "Available named workflows (pass as name):",
    ]
    for d in defns:
        line = f"- {d.name}: {d.description}"
        if d.when_to_use:
            line += f" (use when: {d.when_to_use})"
        req = d.required_input_names()
        if req:
            line += f" [requires: {', '.join(req)}]"
        lines.append(line)
    if not defns:
        lines.append("- (none defined)")
    lines += [
        "",
        "Users add their own under .mantis/workflows/*.md (project) or "
        "$MANTIS_AGENT_HOME/workflows/*.md (user); project definitions override "
        "built-ins of the same name.",
    ]
    return "\n".join(lines)


def make_workflow_tool(
    *,
    model: str,
    provider: Any = None,
    backend: str | None = None,
    tools: Any = None,
    permissions: Any = None,
    budget: Any = None,
    agent_types: Any = None,
    on_progress: Any = None,
    jobs: Any = None,
    on_run: Any = None,
    agent_runner: Any = None,
    concurrency: int | None = None,
    cwd: Any = None,
    definitions: Any = None,
) -> Tool:
    """Build the ``workflow`` tool.

    ``tools`` is the parent's full kit — each child carves its own subset from
    it via its :class:`AgentType` policy, exactly as ``task`` does — and
    ``permissions``/``budget`` are inherited so children stay gated and capped.
    ``on_run(workflow)`` fires the moment the run exists (viewer registration);
    ``on_progress`` gets the task-tool event shape (live subagent rows);
    ``jobs`` is the :class:`~mantis_agent.jobs.JobManager` that backgrounds it.
    ``agent_runner`` / ``definitions`` are injection points for tests."""

    from .subagent import discover_agent_types  # noqa: PLC0415 — avoid import cycle

    defns = (list(definitions) if definitions is not None
             else discover_workflow_definitions(cwd))
    by_name = {d.name: d for d in defns}
    types = list(agent_types) if agent_types is not None else discover_agent_types()
    type_names = tuple(t.name for t in types)
    counter = itertools.count(1)

    @tool(name="workflow", is_read_only=False, is_concurrency_safe=True,
          input_schema=_workflow_schema(sorted(by_name)))
    async def workflow(args: dict) -> str:
        args = args or {}
        if not workflows_enabled():
            return (
                f"workflow: disabled for this session (${_DISABLE_ENV} is set). "
                "Use `task` for a single delegation instead."
            )
        name = str(args.get("name") or "").strip()
        script = args.get("script")
        script = str(script).strip() if isinstance(script, str) else ""

        if script and name:
            return ("workflow: pass EITHER 'script' (orchestration you write) OR "
                    "'name' (a saved workflow), not both.")
        if script:
            return await _run_script(
                script,
                args=args.get("args"),
                model=model, provider=provider, backend=backend, tools=tools,
                permissions=permissions, budget=budget, types=types,
                on_progress=on_progress, on_run=on_run, jobs=jobs,
                agent_runner=agent_runner, concurrency=concurrency,
                counter=counter,
                background=args.get("run_in_background"),
            )
        if not name:
            available = ", ".join(sorted(by_name)) or "(none)"
            return ("workflow: needs either 'script' (orchestration you write) or "
                    f"'name' (one of: {available}).")

        defn = by_name.get(name)
        if defn is None:
            available = ", ".join(sorted(by_name)) or "(none)"
            return f"workflow: no workflow named {name!r}. Available: {available}"

        try:
            base = agent_runner or make_agent_runner(
                model=model,
                tools=list(tools) if tools else None,
                provider=provider,
                backend=backend,
                permissions=permissions,
                budget=budget,
                agent_types=types,
            )
            launch = prepare_workflow_launch(
                defn,
                args.get("inputs"),
                agent_runner=wrap_runner_with_progress(base, on_progress, counter),
                model=model,
                budget=budget,
                concurrency=concurrency,
                resume_from=str(args.get("resume_from") or "").strip(),
                valid_agent_types=type_names,
            )
        except WorkflowDefinitionError as e:
            return f"workflow: {e}"

        background = args.get("run_in_background")
        background = True if background is None else bool(background)

        if background and jobs is not None:
            job = jobs.spawn(
                _run_and_report(launch),
                desc=f"workflow {defn.name}",
                kind="workflow",
                workflow_id=launch.run_id,
            )
            launch.workflow.run.job_id = getattr(job, "id", None)
            attach_job_progress(launch.workflow, job)
            _notify(on_run, launch.workflow)
            return _launch_receipt(launch, job)

        _notify(on_run, launch.workflow)
        return await _run_and_report(launch)

    workflow.description = _tool_description(defns)
    return workflow


def attach_job_progress(wf: Workflow, job: Any) -> None:
    """Mirror a workflow's progress onto its background :class:`Job`.

    Without this a workflow job sits at "starting" in ``/jobs`` for its whole
    life, because job events come from the task tool's message loop and a
    workflow's children never pass through it. Every long operation should have
    an observable status from *both* windows, so fold the phase rollup into the
    job's counters and record a line whenever it actually changes (the engine
    emits on every message — recording each one would drown the event deque)."""

    last = {"line": ""}

    def observe(run: Any) -> None:
        agents = run.all_agents()
        if not agents:
            return
        done = sum(1 for a in agents if a.status in ("done", "cancelled"))
        running = [a for a in agents if a.status == "running"]
        job.tool_count = sum(a.tool_count for a in agents)
        job.turn_count = sum(a.turns for a in agents)
        phase = running[0].phase if running else agents[-1].phase
        line = f"{phase} · {done}/{len(agents)} agents"
        if running:
            line += f" · {running[0].label}"
        if line != last["line"]:
            last["line"] = line
            job.record_event(line)

    wf.on_event = observe


def _notify(on_run: Any, wf: Workflow) -> None:
    if on_run is None:
        return
    try:
        on_run(wf)
    except Exception:  # noqa: BLE001 — registration is observability
        pass


def _describe_exc(exc: BaseException, depth: int = 0) -> str:
    """Flatten a failure into one readable line.

    anyio task groups wrap child failures in an ExceptionGroup whose own
    message ("unhandled errors in a TaskGroup") says nothing useful — unwrap to
    the causes that actually explain what broke."""

    inner = getattr(exc, "exceptions", None)
    if inner and depth < 3:
        parts = [_describe_exc(x, depth + 1) for x in list(inner)[:3]]
        return " · ".join(dict.fromkeys(parts))
    return f"{type(exc).__name__}: {exc}"


async def _run_and_report(launch: WorkflowLaunch) -> str:
    """Execute a launch and render its report — the body of the background job,
    so the job's stored result IS the report the model gets injected.

    A crash inside one child kills the phase (``parallel`` fails fast by
    design), but the run is already persisted by then: report what died and
    where to look rather than losing the agents that did finish."""

    try:
        result = await launch.execute()
    except WorkflowDefinitionError as e:
        return f"workflow {launch.definition.name}: {e}"
    except Exception as e:  # noqa: BLE001 — surfaced to the model as a result
        run = launch.workflow.run
        done = [a for a in run.all_agents() if a.status == "done"]
        failed = [a for a in run.all_agents() if a.status == "error"]
        lines = [
            f"# Workflow report — {launch.definition.name} (FAILED)",
            f"run {run.id} · {_describe_exc(e)}",
            f"{len(done)}/{len(run.all_agents())} agents completed"
            + (f" · failed: {', '.join(f'{a.label} ({a.error})' for a in failed)}"
               if failed else ""),
            "The partial run was saved — inspect it with /workflows history and "
            f"resume it with /workflows resume {run.id}.",
        ]
        for a in done:
            lines.append(f"\n## {a.phase} · {a.label}\n{a.result}")
        return "\n".join(lines)
    return format_workflow_report(result, definition=launch.definition.name)


async def _run_script(
    source: str,
    *,
    args: Any,
    model: str,
    provider: Any,
    backend: str | None,
    tools: Any,
    permissions: Any,
    budget: Any,
    types: Any,
    on_progress: Any,
    on_run: Any,
    jobs: Any,
    agent_runner: Any,
    concurrency: int | None,
    counter: Any,
    background: Any,
) -> str:
    """Run a model-authored orchestration script.

    Children are built through the same runner the named path uses, so a script
    inherits the parent's tools, permissions and budget exactly as a saved
    workflow does — authoring the orchestration does not widen what the agents
    inside it may do.
    """
    from .workflow_script import ScriptError, extract_meta, run_workflow_script  # noqa: PLC0415
    from .workflow_script import validate_script  # noqa: PLC0415

    # Validate BEFORE spawning anything, so a bad script costs nothing and the
    # model gets a line number instead of a half-finished run.
    try:
        meta, _ = extract_meta(validate_script(source))
    except ScriptError as e:
        return f"workflow script rejected — {e}"

    base = agent_runner or make_agent_runner(
        model=model,
        tools=list(tools) if tools else None,
        provider=provider,
        backend=backend,
        permissions=permissions,
        budget=budget,
        agent_types=list(types),
    )
    wf = Workflow(
        name=str(meta.get("name") or "script"),
        agent_runner=wrap_runner_with_progress(base, on_progress, counter),
        budget=budget,
        model=model,
        concurrency=concurrency,
    )

    async def _go() -> str:
        try:
            result = await run_workflow_script(source, wf, args=args)
        except ScriptError as e:
            wf.run.status = "error"
            wf.finish()
            return f"workflow script failed — {e}"
        wf.finish()
        return _format_script_report(wf, meta, result)

    want_bg = True if background is None else bool(background)
    if want_bg and jobs is not None:
        job = jobs.spawn(_go(), desc=f"workflow {wf.name}", kind="workflow",
                         workflow_id=wf.run.id)
        wf.run.job_id = getattr(job, "id", None)
        attach_job_progress(wf, job)
        _notify(on_run, wf)
        return (
            f"workflow '{wf.name}' started in the background\n"
            f"  run {wf.run.id} · job {getattr(job, 'id', '?')}\n"
            "  /workflows to watch it · job_output to collect the result"
        )

    _notify(on_run, wf)
    return await _go()


def _format_script_report(wf: Workflow, meta: dict[str, Any], result: Any) -> str:
    """Render a finished script run: what ran, then what the script returned."""
    lines = [f"# Workflow — {wf.name}"]
    if meta.get("description"):
        lines.append(str(meta["description"]))
    agents = wf.run.all_agents()
    done = [a for a in agents if a.status == "done"]
    tok = sum(total_tokens(a.usage) for a in agents)
    lines.append(
        f"run {wf.run.id} · status {wf.run.status} · "
        f"{len(done)}/{len(agents)} agents completed · ~{tok} tok"
    )
    for ph in wf.run.phases:
        if not ph.agents:
            continue
        lines.append(f"\n## {ph.title}")
        for a in ph.agents:
            head = f"- {a.label} ({a.agent_type}) · {a.status}"
            lines.append(head if not a.error else f"{head} — {a.error}")

    lines.append("\n## Result")
    if result is None:
        lines.append("(the script returned nothing — add a `return` to hand back "
                     "the findings you want to act on)")
    elif isinstance(result, str):
        lines.append(result)
    else:
        try:
            lines.append(json.dumps(result, indent=2, default=str))
        except (TypeError, ValueError):
            lines.append(str(result))
    return "\n".join(lines)
