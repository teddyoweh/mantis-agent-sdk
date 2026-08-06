"""Step identity — what makes a workflow step *the same step* across two runs.

Resume used to be content-keyed: :func:`mantis_agent.workflow_defs.cache_key`
hashes ``(phase, label, prompt)``, and any agent whose three values matched a
persisted run replayed its stored result. That is correct for independent
fan-out and **wrong for pipelines**, which are the engine's headline feature.

Consider Scan → Fix → Verify, where Fix's prompt says "fix everything the scan
flagged" rather than embedding the scan output verbatim — the ordinary way to
write it, since the child re-reads the repo anyway. Edit Scan's prompt and
resume: Scan misses the cache and re-runs, producing a *different* result. Fix
and Verify hit, because their own prompt text never changed, and hand back
results computed from the OLD scan. The run reports success with an internally
inconsistent result set — worse than a failure, because nothing announces it.

The fix is one extra term in the hash::

    step_hash = H(script_hash, step_id, phase, label, prompt, model, agent_type,
                  effort, isolation, sorted(input_refs), parent_step_hash)

``parent_step_hash`` folds in the digest of every step whose output this step
consumed. Change Scan and Fix's identity moves too, even though Fix's prompt is
byte-identical. Replay then walks the ledger in deterministic order and stops at
the **first** mismatch: everything after it runs live, whether or not its own
hash would have matched. "Longest unchanged prefix" is the only rule that keeps
a resumed pipeline internally consistent, because a step that looks unchanged
may still be acting on a world an earlier step has since altered.

``independent=True`` opts one step back into content-keyed replay, for genuine
fan-out — ten reviewers over ten files do not depend on each other, and one of
them changing should not invalidate the other nine. It is opt-in on purpose:
the engine cannot verify the claim, so the user is asserting it. Independence is
a statement about *siblings* only — an independent step still chains its
ancestors, so a change upstream of it invalidates it like anything else.

This module is pure: no I/O, no engine, no config. It takes step descriptions in
and hands hashes and a replay plan back, which is what makes it testable to the
level a correctness fix deserves. Record shapes live in
:mod:`mantis_agent.workflow_store`; the engine wiring lives in
:mod:`mantis_agent.workflow`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

__all__ = [
    "HASH_PREFIX",
    "ReplayPlan",
    "Step",
    "StepLedgerError",
    "StepPlan",
    "StepSpec",
    "build_ledger",
    "fold_parents",
    "ledger_from_dicts",
    "plan_replay",
    "step_hash",
]

# Digests carry their algorithm so a stored hash stays legible (and a future
# algorithm change is a visible mismatch rather than a silent collision).
HASH_PREFIX = "sha256:"

# The ordinal slot for a step that declared itself independent. Substituting a
# constant for the position is exactly what "content-keyed" means: the step's
# identity stops depending on where it sits among its siblings, and keeps
# depending on everything else, ancestors included.
_ANY_ORDINAL = "*"

REPLAY = "replay"
RUN = "run"

_REASON_UNCHANGED = "unchanged"
_REASON_CHANGED = "identity changed"
_REASON_MISSING = "not in the recorded run"
_REASON_INCOMPLETE = "recorded step did not finish"
_REASON_NO_MATCH = "no matching recorded step"


class StepLedgerError(ValueError):
    """A ledger could not be built or read.

    Raised for ordering faults that would make chaining unsound — a forward
    reference, a duplicated name — and for a persisted ledger that cannot be
    parsed. Never raised for "this step changed": that is an outcome, not an
    error."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepSpec:
    """One agent invocation, described before it has an identity.

    ``ref`` is the name other steps use in their ``input_refs`` — supply it only
    for steps something downstream consumes. ``input_refs`` names those
    dependencies; entries that match no earlier ``ref`` (``input:target``, say)
    still participate in the hash as literal text, so a step that reads a
    different input is a different step even though nothing in the ledger
    changed."""

    ref: str = ""
    phase: str = ""
    label: str = ""
    prompt: str = ""
    model: str = ""
    agent_type: str = ""
    effort: str = ""
    isolation: str = ""
    input_refs: tuple[str, ...] = ()
    independent: bool = False


@dataclass(frozen=True)
class Step:
    """One entry in a run's step ledger: identity plus outcome.

    The identity half (``step_hash`` and the fields that feed it) is what resume
    compares. The outcome half (``status``, ``result``, ``result_ref``) is what
    resume replays, and is empty on a freshly built ledger — the engine fills it
    as steps finish, and :mod:`mantis_agent.workflow_store` binds it to the
    persisted run. ``result_ref`` points at the agent id in the run snapshot so
    a large payload lives once rather than twice."""

    step_id: int
    step_hash: str
    parent_step_hash: str = ""
    ref: str = ""
    phase: str = ""
    label: str = ""
    model: str = ""
    agent_type: str = ""
    effort: str = ""
    isolation: str = ""
    input_refs: tuple[str, ...] = ()
    independent: bool = False
    status: str = ""
    result: str = ""
    result_ref: str = ""

    @property
    def replayable(self) -> bool:
        """``done`` with a non-empty result, and nothing else.

        Replaying an errored or cancelled step would launder a failure into a
        success; replaying an empty one would silently drop a phase's input.
        Both are the reasoning :func:`workflow_store.replay_cache` already
        applies, kept here so the prefix walk cannot forget it."""

        return self.status == "done" and bool(self.result.strip())

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the persisted ledger, omitting defaults.

        Records are read by humans debugging a resume; a step carrying eight
        empty strings hides the two fields that matter."""

        d: dict[str, Any] = {"step_id": self.step_id, "step_hash": self.step_hash}
        names = ["parent_step_hash", "ref", "phase", "label", "model",
                 "agent_type", "effort", "isolation", "status", "result_ref"]
        # A payload that the run snapshot already holds is referenced, never
        # copied: a record with every agent's output stored twice is slow to
        # write and slow to read, and the two copies can only ever disagree.
        if not self.result_ref:
            names.append("result")
        for name in names:
            value = getattr(self, name)
            if value:
                d[name] = value
        if self.input_refs:
            d["input_refs"] = list(self.input_refs)
        if self.independent:
            d["independent"] = True
        return d


def step_from_dict(data: Any) -> Step:
    """Rebuild one :class:`Step` from a persisted entry.

    Unknown keys are ignored so a record written by a later version still
    loads; a missing or malformed identity is fatal, because a step with no
    hash cannot be compared and guessing one would replay the wrong result."""

    if not isinstance(data, dict):
        raise StepLedgerError(f"step ledger entry is {type(data).__name__}, not an object")
    step_id = data.get("step_id")
    digest = data.get("step_hash")
    if not isinstance(step_id, int) or isinstance(step_id, bool):
        raise StepLedgerError(f"step ledger entry has a non-integer step_id: {step_id!r}")
    if not isinstance(digest, str) or not digest:
        raise StepLedgerError(f"step {step_id} has no step_hash")
    refs = data.get("input_refs") or ()
    return Step(
        step_id=step_id,
        step_hash=digest,
        parent_step_hash=str(data.get("parent_step_hash") or ""),
        ref=str(data.get("ref") or ""),
        phase=str(data.get("phase") or ""),
        label=str(data.get("label") or ""),
        model=str(data.get("model") or ""),
        agent_type=str(data.get("agent_type") or ""),
        effort=str(data.get("effort") or ""),
        isolation=str(data.get("isolation") or ""),
        input_refs=tuple(str(r) for r in refs) if isinstance(refs, (list, tuple)) else (),
        independent=bool(data.get("independent")),
        status=str(data.get("status") or ""),
        result=str(data.get("result") or ""),
        result_ref=str(data.get("result_ref") or ""),
    )


def ledger_from_dicts(entries: Any) -> tuple[Step, ...]:
    """Rebuild a whole ledger. Raises :class:`StepLedgerError` on any bad entry.

    All-or-nothing on purpose: a ledger with a hole in it would let the prefix
    walk skip past a step that actually changed."""

    if not isinstance(entries, (list, tuple)):
        raise StepLedgerError("step ledger is not a list")
    return tuple(step_from_dict(e) for e in entries)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _digest(parts: Sequence[str]) -> str:
    # JSON, not "\x00".join: the join lets text migrate across a field boundary
    # and produce the same digest for two different steps (label="a",
    # prompt="b" versus label="a\x00b", prompt=""). JSON escapes every
    # separator it uses, so each field's extent is unambiguous — and the output
    # stays diffable when a record is read by hand.
    blob = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return HASH_PREFIX + hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()


def fold_parents(hashes: Iterable[str]) -> str:
    """Collapse the hashes of every consumed step into one term.

    Sorted before folding: ``input_refs`` is a *set* of dependencies, so
    declaring ``(a, b)`` and ``(b, a)`` must not be two different identities.
    Empty in, empty out — a source step has no parent term."""

    ordered = sorted(h for h in hashes if h)
    if not ordered:
        return ""
    return _digest(["parents", *ordered])


def step_hash(
    *,
    script_hash: str = "",
    step_id: int = 0,
    phase: str = "",
    label: str = "",
    prompt: str = "",
    model: str = "",
    agent_type: str = "",
    effort: str = "",
    isolation: str = "",
    input_refs: Iterable[str] = (),
    parent_step_hash: str = "",
    independent: bool = False,
) -> str:
    """The identity of one step.

    Every argument is part of what the step *is*: swapping the model or the
    persona changes the result even when the prompt does not, and isolation
    changes what the step can see. ``parent_step_hash`` is the chaining term —
    see the module docstring for why it is the whole fix.

    ``independent=True`` drops the ordinal, and only the ordinal: the step stops
    caring where it sits among its siblings and keeps caring about its
    ancestors."""

    return _digest([
        "step/1",
        script_hash,
        _ANY_ORDINAL if independent else str(int(step_id)),
        phase,
        label,
        prompt,
        model,
        agent_type,
        effort,
        isolation,
        *sorted({str(r) for r in input_refs}),
        "\x1e",                      # end of the variable-length refs section
        parent_step_hash,
    ])


def build_ledger(specs: Sequence[StepSpec], *, script_hash: str = "") -> tuple[Step, ...]:
    """Turn an ordered list of step descriptions into an identity ledger.

    ``specs`` must already be in deterministic execution order — program order
    for a script, phase-then-item order for a definition. Ordinals are assigned
    from that order, never from completion order, because two runs that finish
    their agents in different orders must still produce the same ledger.

    Raises :class:`StepLedgerError` when a name is duplicated or a step depends
    on one declared after it: both make ``parent_step_hash`` ambiguous, and an
    ambiguous chain is a stale replay waiting to happen."""

    steps: list[Step] = []
    by_ref: dict[str, str] = {}
    later = _refs_by_position(specs)

    for index, spec in enumerate(specs):
        step_id = index + 1
        parents: list[str] = []
        for ref in sorted({r for r in spec.input_refs if r}):
            known = by_ref.get(ref)
            if known is not None:
                parents.append(known)
            elif later.get(ref, -1) > index:
                raise StepLedgerError(
                    f"step {step_id} ({spec.phase}/{spec.label}) consumes {ref!r}, "
                    f"which is produced by a later step — a ledger must be in "
                    f"dependency order for resume to be sound"
                )
            # Anything else is an external reference (``input:target``). It is
            # not dropped: the ref name itself is hashed below, so pointing a
            # step at a different input still changes its identity.
        digest_parents = fold_parents(parents)
        steps.append(Step(
            step_id=step_id,
            step_hash=step_hash(
                script_hash=script_hash,
                step_id=step_id,
                phase=spec.phase,
                label=spec.label,
                prompt=spec.prompt,
                model=spec.model,
                agent_type=spec.agent_type,
                effort=spec.effort,
                isolation=spec.isolation,
                input_refs=spec.input_refs,
                parent_step_hash=digest_parents,
                independent=spec.independent,
            ),
            parent_step_hash=digest_parents,
            ref=spec.ref,
            phase=spec.phase,
            label=spec.label,
            model=spec.model,
            agent_type=spec.agent_type,
            effort=spec.effort,
            isolation=spec.isolation,
            input_refs=tuple(spec.input_refs),
            independent=spec.independent,
        ))
        if spec.ref:
            by_ref[spec.ref] = steps[-1].step_hash
    return tuple(steps)


def _refs_by_position(specs: Sequence[StepSpec]) -> dict[str, int]:
    """``{ref: index}``, refusing duplicates.

    Two steps answering to one name would make ``input_refs`` resolve to
    whichever came last — a silent, position-dependent chain."""

    out: dict[str, int] = {}
    for index, spec in enumerate(specs):
        if not spec.ref:
            continue
        if spec.ref in out:
            raise StepLedgerError(
                f"step {index + 1} reuses the name {spec.ref!r} (already used by "
                f"step {out[spec.ref] + 1}) — names must be unique to chain"
            )
        out[spec.ref] = index
    return out


# ---------------------------------------------------------------------------
# Prefix replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepPlan:
    """What resume decided about one step, and why it decided it."""

    step_id: int
    step_hash: str
    action: str            # REPLAY | RUN
    reason: str
    phase: str = ""
    label: str = ""
    ref: str = ""
    independent: bool = False
    result: str = ""

    @property
    def replayed(self) -> bool:
        return self.action == REPLAY


@dataclass(frozen=True)
class ReplayPlan:
    """The full decision for a resume, ready to be executed and to be shown.

    ``eligible`` is False when the recorded run could not be used at all (a v1
    record has no ledger, so prefix rules over it would be guesswork); ``reason``
    then says so, and every step runs."""

    steps: tuple[StepPlan, ...] = ()
    eligible: bool = True
    reason: str = ""
    first_change: int | None = None

    @property
    def replayed(self) -> int:
        return sum(1 for s in self.steps if s.replayed)

    @property
    def rerun(self) -> int:
        return sum(1 for s in self.steps if not s.replayed)

    @property
    def complete(self) -> bool:
        """Every step replayed — the resume costs nothing and changes nothing."""

        return bool(self.steps) and self.rerun == 0

    def results(self) -> dict[int, str]:
        """``{step_id: result}`` for the steps that replay."""

        return {s.step_id: s.result for s in self.steps if s.replayed}

    def cache(self) -> dict[str, str]:
        """``{step_hash: result}`` — the map the engine consults per step."""

        return {s.step_hash: s.result for s in self.steps if s.replayed}

    def report_lines(self, run_id: str = "") -> list[str]:
        """A resume that does not explain itself is not an improvement.

        One header plus one line per run of consecutive same-decision steps, so
        a 200-step resume reads as a handful of ranges rather than 200 lines::

            resume wfr1 — 1 replayed, 2 re-run
              replayed  step 1     Scan/grep-tests  (unchanged)
              changed   step 2     Fix/patch  (identity changed)
              re-run    step 3     Verify/check  (downstream of step 2)
        """

        head = f"resume {run_id} — " if run_id else "resume — "
        head += f"{self.replayed} replayed, {self.rerun} re-run"
        if not self.eligible and self.reason:
            head += f" ({self.reason})"
        lines = [head]
        for group in _group(self.steps):
            first, last = group[0], group[-1]
            span = (f"step {first.step_id}" if first is last
                    else f"step {first.step_id}–{last.step_id}")
            if first.replayed:
                tag = "replayed"
            elif first.reason == _REASON_CHANGED:
                tag = "changed"
            else:
                tag = "re-run"
            lines.append(f"  {tag:<9} {span:<12} {_names(group)}  ({first.reason})")
        return lines


def _group(steps: Sequence[StepPlan]) -> list[list[StepPlan]]:
    """Consecutive steps sharing one decision, in order."""

    out: list[list[StepPlan]] = []
    for step in steps:
        key = (step.action, step.reason)
        if out and (out[-1][0].action, out[-1][0].reason) == key:
            out[-1].append(step)
        else:
            out.append([step])
    return out


def _names(group: Sequence[StepPlan]) -> str:
    """``Scan/grep-tests`` for one step, ``Fix/*, Verify/*`` for many."""

    if len(group) == 1:
        one = group[0]
        return f"{one.phase or '?'}/{one.label or '?'}"
    phases: list[str] = []
    for step in group:
        name = step.phase or "?"
        if name not in phases:
            phases.append(name)
    shown = ", ".join(f"{p}/*" for p in phases[:3])
    return shown + ("…" if len(phases) > 3 else "")


def plan_replay(
    recorded: Sequence[Step],
    planned: Sequence[Step],
    *,
    refuse: str = "",
) -> ReplayPlan:
    """Decide, step by step, what replays and what runs live.

    The dependent rule is positional and one-directional: compare ``planned[i]``
    with ``recorded[i]`` and replay while they match, then stop for good. A step
    after the break re-runs even when its own hash still matches, because the
    steps before it did not produce the results it was recorded against.

    Independent steps are matched by content anywhere in the recording, so a
    sibling changing (or being inserted) does not disturb them — but their hash
    still carries their ancestors, so an upstream change invalidates them like
    anything else.

    ``refuse`` forces a full live run with a stated reason: it is how a record
    that cannot be trusted (no ledger, unreadable ledger) becomes a plan instead
    of an exception."""

    by_id = {s.step_id: s for s in recorded}
    content: dict[str, Step] = {}
    for s in recorded:
        # First replayable recording wins: a later duplicate is the same work.
        if s.replayable and s.step_hash not in content:
            content[s.step_hash] = s

    out: list[StepPlan] = []
    prefix_intact = not refuse
    first_change: int | None = None
    ran_refs: dict[str, int] = {}

    for step in planned:
        result = ""
        if refuse:
            action, reason = RUN, refuse
        elif step.independent:
            hit = content.get(step.step_hash)
            if hit is not None:
                action, reason, result = REPLAY, _REASON_UNCHANGED, hit.result
            else:
                action = RUN
                ancestor = _first_ancestor(step, ran_refs)
                reason = (f"ancestor step {ancestor} re-ran" if ancestor
                          else _REASON_NO_MATCH)
        elif not prefix_intact:
            action = RUN
            reason = f"downstream of step {first_change}"
        else:
            was = by_id.get(step.step_id)
            if was is None:
                action, reason = RUN, _REASON_MISSING
            elif was.step_hash != step.step_hash:
                action, reason = RUN, _REASON_CHANGED
            elif not was.replayable:
                action, reason = RUN, _REASON_INCOMPLETE
            else:
                action, reason, result = REPLAY, _REASON_UNCHANGED, was.result
            if action == RUN:
                prefix_intact = False
                first_change = step.step_id

        if action == RUN and step.ref:
            ran_refs[step.ref] = step.step_id
        out.append(StepPlan(
            step_id=step.step_id, step_hash=step.step_hash, action=action,
            reason=reason, phase=step.phase, label=step.label, ref=step.ref,
            independent=step.independent, result=result,
        ))

    return ReplayPlan(steps=tuple(out), eligible=not refuse, reason=refuse,
                      first_change=first_change)


def _first_ancestor(step: Step, ran_refs: dict[str, int]) -> int | None:
    """The earliest already-re-run step this one consumes, if any.

    Only direct parents are checked, and that is enough: a grandparent changing
    changes the parent's hash too, so the parent is in ``ran_refs`` as well."""

    ids = [ran_refs[r] for r in step.input_refs if r in ran_refs]
    return min(ids) if ids else None
