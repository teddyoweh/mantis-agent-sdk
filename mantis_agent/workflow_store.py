"""Durable workflow run history — inspect and resume after the fact.

A workflow run is expensive: dozens of model calls, minutes of wall clock,
sometimes real money. Losing all of that when the session ends is the single
worst property an orchestration layer can have. So every run is written to a
plain JSON file under ``$MANTIS_AGENT_HOME/workflows/runs/`` as it completes —
no database, no schema migrations, no daemon: the same
:meth:`~mantis_agent.workflow.WorkflowRun.to_dict` snapshot the live viewer
renders, wrapped in a small record with the definition name and inputs.

That buys three things:

* **History** — ``/workflows history`` lists past runs long after the session.
* **Inspection** — a finished run drills down exactly like a live one.
* **Resume** — :func:`prefix_replay` compares a stored run's *step ledger*
  against the ledger of the run about to start and decides, step by step, what
  can be replayed for free and what has to be paid for again.

Record versions
---------------
Version 2 adds the step ledger (:mod:`mantis_agent.workflow_steps`) and the
script hash. That is what makes strict-prefix resume possible: a v1 record has
no ledger, so there is no way to tell which stored result depended on which
other stored result. Version 1 records still **load** and still **view** — the
history of an expensive run is worth keeping — but :func:`replay_eligible`
refuses them for replay, because applying prefix rules to a run with no
dependency information would be guesswork dressed up as a cache hit.

:func:`replay_cache` is the older, content-keyed map and stays exactly as it
was for callers that still use it. It is unsound for pipelines — see
:mod:`mantis_agent.workflow_steps` for the failure it produces — and new
callers should use :func:`prefix_replay`.

Secrets never land here: :func:`redact_inputs` blanks anything whose name looks
like a credential before the record is written.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

__all__ = [
    "RECORD_VERSION",
    "bind_run_results",
    "list_runs",
    "load_record",
    "prefix_replay",
    "prune_runs",
    "redact_inputs",
    "replay_cache",
    "replay_eligible",
    "run_path",
    "runs_dir",
    "save_run",
    "step_ledger",
]

_log = logging.getLogger("mantis_agent.workflow_store")

# 1 → 2: the step ledger and the script hash. Bumped, not silently extended,
# because the version is what tells resume whether the dependency information it
# needs is present at all.
RECORD_VERSION = 2

# Records below this cannot be resumed under strict-prefix rules. They remain
# fully readable — history and inspection do not need a ledger.
_MIN_REPLAYABLE_VERSION = 2

_MAX_RETAINED_RUNS = 200

# Marker in a scratch file's name. Two independent guards depend on it: the
# writer builds temp names around it, and every reader skips anything carrying
# it, so a save that dies between "temp written" and "renamed" leaves litter the
# history view refuses to mistake for a run.
_TMP_MARK = ".tmp-"

# pid keeps two processes apart; the counter keeps two threads in one process
# apart (the viewer can snapshot a run while it is auto-persisting).
_TMP_SEQ = itertools.count()

# Input names that must never be written to disk. Matched case-insensitively as
# substrings, so ``openai_api_key`` and ``authToken`` are both caught.
_SECRET_HINTS = (
    "key", "token", "secret", "password", "passwd", "credential",
    "auth", "cookie", "session_id", "private",
)

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def runs_dir() -> Path:
    """``$MANTIS_AGENT_HOME/workflows/runs`` — definitions are ``.md`` one level
    up, so definitions and run artifacts never collide."""

    from .paths import get_mantis_agent_dir  # noqa: PLC0415

    return get_mantis_agent_dir() / "workflows" / "runs"


def run_path(run_id: str) -> Path:
    return runs_dir() / f"{_SAFE_ID.sub('_', str(run_id)) or '_'}.json"


def redact_inputs(inputs: Any) -> dict[str, str]:
    """Copy ``inputs`` with credential-shaped values replaced by ``[redacted]``.

    Conservative by design: a false positive costs a resume its input value,
    a false negative writes a live key into a file that outlives the session."""

    out: dict[str, str] = {}
    if not isinstance(inputs, dict):
        return out
    for k, v in inputs.items():
        key = str(k)
        low = key.lower()
        if any(h in low for h in _SECRET_HINTS):
            out[key] = "[redacted]"
        else:
            out[key] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return out


def save_run(
    run: Any,
    *,
    definition: str = "",
    inputs: Any = None,
    job_id: int | None = None,
    result: Any = None,
    path: Any = None,
    steps: Any = None,
    script_hash: str = "",
) -> str:
    """Persist one run as a record. Returns the written path.

    Best-effort at the call site: the caller should treat a failure here as a
    logging problem, never as a failed workflow.

    ``steps`` is the run's ordered step ledger (see
    :mod:`mantis_agent.workflow_steps`) and is what makes the record resumable —
    omit it and the record is still written, still viewable, and simply refuses
    replay. Ledger entries that carry no outcome yet are bound to the run
    snapshot on the way out (see :func:`bind_run_results`), so a caller that
    builds the ledger up front for hashing does not have to thread results back
    through it by hand. ``script_hash`` records which source produced the run;
    it also feeds every step hash, so a changed script mismatches at step 1."""

    run_dict = run.to_dict() if hasattr(run, "to_dict") else dict(run or {})
    run_id = str(run_dict.get("id") or "run")
    record = {
        "version": RECORD_VERSION,
        "run_id": run_id,
        "definition": definition or run_dict.get("definition") or "",
        "inputs": redact_inputs(inputs),
        "job_id": job_id if job_id is not None else run_dict.get("job_id"),
        "saved_at": time.time(),
        "status": run_dict.get("status", ""),
        "run": run_dict,
    }
    if script_hash:
        record["script_hash"] = str(script_hash)
    if steps:
        # A ledger that cannot be serialized must not cost the caller its run
        # record — the identity data is an optimisation for the NEXT run, the
        # run dict is the only copy of what just happened.
        try:
            record["steps"] = [s.to_dict() if hasattr(s, "to_dict") else dict(s)
                               for s in bind_run_results(steps, run_dict)]
        except Exception as e:  # noqa: BLE001
            _log.debug("dropping unserializable step ledger for %s: %r", run_id, e)
    if result is not None:
        record["summary"] = _result_summary(result)
    target = Path(path) if path is not None else run_path(run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target, json.dumps(record, indent=2))
    prune_runs()
    return str(target)


def _atomic_write_text(target: Path, text: str) -> None:
    """Write ``text`` to ``target`` all-or-nothing.

    A run record is the only surviving artifact of an expensive workflow, so a
    partial write is worse than no write: a truncated file is unparseable, which
    loses the ENTIRE history for that run rather than just the newest update.
    So the payload goes to a sibling scratch file first (same directory, hence
    same filesystem, hence a rename that cannot fall back to a copy), is flushed
    all the way to the platter, and only then replaces the target —
    :func:`os.replace` is atomic on POSIX and on Windows, so a reader sees the
    old record or the new one, never half of either.

    Raises whatever the filesystem raised, after cleaning up the scratch file;
    :func:`save_run`'s callers already treat that as a logging failure."""

    tmp = target.with_name(f"{target.name}{_TMP_MARK}{os.getpid()}-{next(_TMP_SEQ)}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover — tmpfs/pseudo-fs in containers
                # fsync is unavailable on some filesystems. The replace below is
                # still atomic there; we just lose the power-loss guarantee.
                pass
        os.replace(tmp, target)
    except BaseException:
        # Never leave litter behind — including on KeyboardInterrupt, which is
        # exactly the "crash mid-write" case this function exists for.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _result_summary(result: Any) -> dict[str, Any]:
    """The few fields worth keeping out of an execution result — enough to show
    a history row without re-reading the whole run."""

    if not isinstance(result, dict):
        return {}
    return {
        "replayed": result.get("replayed", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "phases": [p.get("title", "") for p in (result.get("phases") or [])],
    }


def load_record(run_id: str) -> dict[str, Any] | None:
    """Load one persisted record by run id. ``None`` if absent or unreadable."""

    p = run_path(run_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        # Unreadable or unparseable — a record truncated by an older, non-atomic
        # writer, or hand-edited. Say so once at debug level and hand the caller
        # a miss: history is observability, it must never raise into a workflow.
        _log.debug("unreadable workflow record %s: %r", p, e)
        return None
    return data if isinstance(data, dict) else None


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first summaries of persisted runs.

    Each entry: ``run_id``, ``definition``, ``status``, ``saved_at``, ``job_id``,
    ``name``, ``agents``, ``phases``. Malformed files and in-flight scratch
    files are skipped — a corrupt or half-written artifact must not break the
    history view, and a temp file is a duplicate of a run that is about to
    appear under its real name."""

    d = runs_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json"), key=_mtime, reverse=True):
        if _TMP_MARK in p.name:
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            _log.debug("skipping unreadable workflow record %s: %r", p, e)
            continue
        if not isinstance(rec, dict):
            continue
        run = rec.get("run") or {}
        phases = run.get("phases") or []
        out.append({
            "run_id": rec.get("run_id", ""),
            "definition": rec.get("definition", ""),
            "status": rec.get("status") or run.get("status", ""),
            "saved_at": rec.get("saved_at", 0.0),
            "job_id": rec.get("job_id"),
            "name": run.get("name", ""),
            "phases": len(phases),
            "agents": sum(len(ph.get("agents") or []) for ph in phases),
            "path": str(p),
        })
        if len(out) >= max(1, limit):
            break
    return out


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def prune_runs(keep: int = _MAX_RETAINED_RUNS) -> int:
    """Drop the oldest records past ``keep``. Returns how many were removed."""

    d = runs_dir()
    if not d.is_dir():
        return 0
    files = sorted(d.glob("*.json"), key=_mtime, reverse=True)
    removed = 0
    for p in files[max(0, keep):]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# The step ledger — strict-prefix resume
# ---------------------------------------------------------------------------


def _run_dict(record: Any) -> dict[str, Any]:
    """The run snapshot, whether handed a whole record or a bare run dict."""

    if isinstance(record, dict) and "run" in record:
        run = record.get("run") or {}
        return run if isinstance(run, dict) else {}
    return record if isinstance(record, dict) else {}


def _agents_in_order(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Every agent of a run, phase by phase, in the order it was recorded."""

    out: list[dict[str, Any]] = []
    for ph in run.get("phases") or []:
        if not isinstance(ph, dict):
            continue
        for a in ph.get("agents") or []:
            if isinstance(a, dict):
                out.append(a)
    return out


def bind_run_results(steps: Any, run: Any) -> tuple[Any, ...]:
    """Attach each step's outcome from the run snapshot it belongs to.

    The engine builds a ledger *before* it runs anything — that is where the
    hashes come from — so the entries arrive here with identity and no outcome.
    Matching is by ``(phase, label)`` in recorded order, which is the same order
    the ledger is in, so a repeated label inside a fan-out still pairs up
    one-for-one. A step that matches nothing is left as it is: unbound means not
    replayable, which stops the prefix walk there rather than replaying a result
    that may belong to a different agent.

    Steps that already carry an outcome are never touched — a caller that
    tracked results itself is authoritative over this convenience."""

    from dataclasses import replace as _replace  # noqa: PLC0415

    from .workflow_steps import Step, step_from_dict  # noqa: PLC0415

    pending: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in _agents_in_order(_run_dict(run)):
        pending.setdefault((str(a.get("phase") or ""), str(a.get("label") or "")),
                           []).append(a)

    out: list[Any] = []
    for entry in steps:
        step = entry if isinstance(entry, Step) else step_from_dict(entry)
        if step.status or step.result or step.result_ref:
            out.append(step)
            continue
        queue = pending.get((step.phase, step.label)) or []
        agent = queue.pop(0) if queue else None
        if agent is None:
            out.append(step)
            continue
        agent_id = str(agent.get("id") or "")
        text = str(agent.get("result") or "")
        out.append(_replace(
            step,
            status=str(agent.get("status") or ""),
            result=text,
            # With an id, the payload is referenced rather than copied; the
            # in-memory step still carries it so a caller can replay at once.
            result_ref=agent_id if (agent_id and text) else "",
        ))
    return tuple(out)


def step_ledger(record: Any) -> tuple[Any, ...]:
    """The persisted step ledger, with ``result_ref`` payloads resolved.

    Returns ``()`` for a record that has no ledger or an unreadable one — this
    is a reader, and a reader that raises takes down the history view. Use
    :func:`replay_eligible` when the *reason* matters."""

    from .workflow_steps import ledger_from_dicts  # noqa: PLC0415

    if not isinstance(record, dict):
        return ()
    try:
        ledger = ledger_from_dicts(record.get("steps") or [])
    except ValueError as e:
        _log.debug("unreadable step ledger in %s: %r", record.get("run_id"), e)
        return ()
    return _resolve_refs(ledger, _run_dict(record))


def _resolve_refs(ledger: tuple[Any, ...], run: dict[str, Any]) -> tuple[Any, ...]:
    """Fill in results the ledger stored by reference into the run snapshot."""

    from dataclasses import replace as _replace  # noqa: PLC0415

    by_id = {str(a.get("id") or ""): a for a in _agents_in_order(run)}
    out = []
    for step in ledger:
        if step.result or not step.result_ref:
            out.append(step)
            continue
        agent = by_id.get(step.result_ref)
        if agent is None:
            # The reference dangles — the run dict was trimmed, or the record
            # was hand-edited. No result means not replayable, which is the
            # conservative outcome rather than an exception.
            out.append(step)
            continue
        out.append(_replace(step, result=str(agent.get("result") or ""),
                            status=step.status or str(agent.get("status") or "")))
    return tuple(out)


def replay_eligible(record: Any) -> tuple[bool, str]:
    """``(ok, reason)`` — may this record be used for strict-prefix resume?

    The reason is written to be shown to a user verbatim, because a resume that
    quietly re-runs everything is as confusing as one that quietly replays."""

    from .workflow_steps import ledger_from_dicts  # noqa: PLC0415

    if not isinstance(record, dict):
        return False, "no persisted run to resume from"
    version = record.get("version")
    version = version if isinstance(version, int) and not isinstance(version, bool) else 0
    if version < _MIN_REPLAYABLE_VERSION:
        return False, (
            f"record version {version} predates the step ledger — resume cannot "
            f"tell which stored result depended on which, so everything re-runs"
        )
    if not record.get("steps"):
        return False, "record has no step ledger — everything re-runs"
    try:
        ledger_from_dicts(record["steps"])
    except ValueError as e:
        return False, f"record step ledger is unreadable ({e}) — everything re-runs"
    return True, ""


def prefix_replay(record: Any, planned: Any) -> Any:
    """Plan a resume of ``record`` for the ledger ``planned`` about to run.

    Returns a :class:`~mantis_agent.workflow_steps.ReplayPlan`: what replays,
    what re-runs, and why for every step. A record that cannot be trusted turns
    into a plan that runs everything with the refusal stated, never into an
    exception — resume is a workflow's slow path, not its failure path."""

    from .workflow_steps import plan_replay  # noqa: PLC0415

    ok, reason = replay_eligible(record)
    recorded = step_ledger(record) if ok else ()
    return plan_replay(recorded, tuple(planned), refuse="" if ok else reason)


def replay_cache(record: Any) -> dict[str, str]:
    """Build the ``{cache_key: result}`` map that powers legacy resume.

    Only agents that finished ``done`` with a stored prompt AND a non-empty
    result are eligible: replaying an errored or cancelled agent would launder
    a failure into a success, and replaying an empty result would silently drop
    a phase's input.

    Content-keyed, and therefore **unsound for pipelines**: an agent whose
    prompt text is unchanged hits this cache even when the agent that produced
    its input re-ran and produced something different. :func:`prefix_replay` is
    the correct path; this one is kept, unchanged, for callers that have not
    moved yet."""

    from .workflow_defs import cache_key  # noqa: PLC0415

    if isinstance(record, dict) and "run" in record:
        run = record.get("run") or {}
    else:
        run = record if isinstance(record, dict) else {}
    out: dict[str, str] = {}
    for ph in run.get("phases") or []:
        for a in ph.get("agents") or []:
            if a.get("status") != "done":
                continue
            prompt = a.get("prompt") or ""
            result = a.get("result") or ""
            if not prompt or not result.strip():
                continue
            out[cache_key(a.get("phase") or ph.get("title") or "",
                          a.get("label") or "", prompt)] = result
    return out
