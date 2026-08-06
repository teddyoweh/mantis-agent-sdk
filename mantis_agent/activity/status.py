"""One status vocabulary, and the mappers into it.

Every engine disagrees today. ``Job.status`` is
``running | done | error | cancelled | timeout``; a workflow ``AgentRun`` is
``queued | running | done | error | cancelled`` with a separate ``_paused``
flag; a skipped agent is recorded as ``cancelled`` plus membership in
``Workflow._skip``. Translating at call sites is how those vocabularies leaked
into six viewers, so translation lives here and only here.

Two states are genuinely new. ``blocked`` answers "why is nothing happening" —
the single most valuable thing the rail can say — and ``skipped`` finally names
what ``Workflow.skip_agent`` already does.

Rollup generalizes ``Phase.roll_up`` in ``workflow.py``: a container's status is
a function of its children, and the function is a total one, so an empty or
half-started container still has an answer.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple

__all__ = [
    "ACTIVE",
    "BLOCKED",
    "CANCELLED",
    "DONE",
    "ERROR",
    "HARD_TERMINAL",
    "PAUSED",
    "PENDING",
    "ROLLUP_KINDS",
    "RUNNING",
    "SKIPPED",
    "STATUSES",
    "STATUS_SET",
    "TERMINAL",
    "TIMEOUT",
    "from_agent_run",
    "from_job_status",
    "from_phase_status",
    "from_workflow_status",
    "is_active",
    "is_terminal",
    "normalize",
    "resolve_status",
    "roll_up",
]

PENDING = "pending"      # created, not yet started
RUNNING = "running"      # actively executing
PAUSED = "paused"        # suspended, resumable
BLOCKED = "blocked"      # waiting on permission, approval, or a dependency
DONE = "done"            # completed successfully
ERROR = "error"          # completed with failure
CANCELLED = "cancelled"  # terminated by user or parent
TIMEOUT = "timeout"      # terminated by deadline
SKIPPED = "skipped"      # deliberately bypassed

#: Canonical display order — pending first, worst-terminal last.
STATUSES: Tuple[str, ...] = (
    PENDING,
    RUNNING,
    PAUSED,
    BLOCKED,
    DONE,
    ERROR,
    CANCELLED,
    TIMEOUT,
    SKIPPED,
)

STATUS_SET = frozenset(STATUSES)

#: A node in one of these states will never change again on its own.
TERMINAL = frozenset({DONE, ERROR, CANCELLED, TIMEOUT, SKIPPED})

#: The complement — work that is still in the tree's future.
ACTIVE = frozenset({PENDING, RUNNING, PAUSED, BLOCKED})

#: Terminal verdicts imposed from outside. When a container carries one of
#: these it keeps it: cancelling a phase must not be undone by a child that
#: happens to have finished cleanly.
HARD_TERMINAL = frozenset({CANCELLED, TIMEOUT, SKIPPED})

#: Kinds with no intrinsic status of their own — they display their children's.
#: ``job`` and ``agent`` are absent on purpose: an engine owns their state.
ROLLUP_KINDS = frozenset({"session", "turn", "phase", "swarm", "team"})


def is_terminal(status: str) -> bool:
    """True when ``status`` is a final state."""

    return status in TERMINAL


def is_active(status: str) -> bool:
    """True when ``status`` is a non-final state."""

    return status in ACTIVE


def normalize(status: Any, default: str = PENDING) -> str:
    """Coerce anything to a known status.

    Unknown values fall back to ``pending`` rather than to a terminal state on
    purpose: an unrecognized status means "we do not know", and reporting *not
    finished* keeps a parent's rollup honest. A wrong ``done`` would silently
    make a container claim completion it cannot vouch for.
    """

    if isinstance(status, str) and status in STATUS_SET:
        return status
    return default


# ---------------------------------------------------------------------------
# Per-engine mappers
# ---------------------------------------------------------------------------

# ``jobs.Job.status`` — note there is no queued state: a Job is running from
# the instant it is constructed.
_JOB_STATUS = {
    "running": RUNNING,
    "done": DONE,
    "error": ERROR,
    "cancelled": CANCELLED,
    "timeout": TIMEOUT,
}

# ``workflow.WorkflowRun.status`` / ``Phase.status`` / ``AgentRun.status``.
# The extra keys are states the engine does not emit today but the vocabulary
# already has names for, so a future engine change lands as a no-op here.
_WORKFLOW_STATUS = {
    "queued": PENDING,
    "running": RUNNING,
    "done": DONE,
    "error": ERROR,
    "cancelled": CANCELLED,
    "paused": PAUSED,
    "blocked": BLOCKED,
    "skipped": SKIPPED,
    "timeout": TIMEOUT,
    "pending": PENDING,
}


def from_job_status(status: Any) -> str:
    """Map a :class:`mantis_agent.jobs.Job` status into the vocabulary."""

    if isinstance(status, str):
        mapped = _JOB_STATUS.get(status)
        if mapped is not None:
            return mapped
    return PENDING


def from_workflow_status(status: Any, *, paused: bool = False) -> str:
    """Map a ``WorkflowRun.status``. ``paused`` reflects ``Workflow._paused``.

    A paused run keeps executing its in-flight turns, so the engine leaves
    ``status == "running"``; only the gate is closed. The unified vocabulary has
    a word for that and the rail should use it.
    """

    mapped = _WORKFLOW_STATUS.get(status, PENDING) if isinstance(status, str) else PENDING
    if paused and mapped == RUNNING:
        return PAUSED
    return mapped


def from_phase_status(status: Any) -> str:
    """Map a ``Phase.status``. Same vocabulary as the run itself."""

    return _WORKFLOW_STATUS.get(status, PENDING) if isinstance(status, str) else PENDING


def from_agent_run(
    run: Any, *, paused: Optional[bool] = None, skipped: bool = False
) -> str:
    """Map a workflow ``AgentRun`` (or its bare status string).

    Accepts the object so callers do not have to unpack ``_paused``, but stays
    duck-typed so this module never imports ``workflow`` — the dependency must
    point the other way once emission lands.

    ``skipped`` is passed by the caller because the engine records a skip as
    ``cancelled`` plus membership in ``Workflow._skip``; the set is the only
    place the distinction exists.
    """

    if isinstance(run, str):
        raw: Any = run
        run_paused = False
    else:
        raw = getattr(run, "status", None)
        run_paused = bool(getattr(run, "_paused", False))
    if paused is None:
        paused = run_paused
    if skipped:
        return SKIPPED
    mapped = _WORKFLOW_STATUS.get(raw, PENDING) if isinstance(raw, str) else PENDING
    if paused and mapped == RUNNING:
        return PAUSED
    return mapped


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


def roll_up(child_statuses: Iterable[str]) -> str:
    """Derive a container's status from its children.

    The ladder, in order — the first rule that matches wins:

    1. no children → ``pending`` (nothing has been claimed yet)
    2. any ``running`` → ``running``
    3. any ``blocked`` → ``blocked`` (the rail's most valuable signal)
    4. any ``paused`` → ``paused``
    5. any ``error``/``timeout`` → ``error``, *even with children still pending*
    6. every child terminal → ``done``
    7. some child terminal → ``running`` (work has demonstrably begun)
    8. otherwise → ``pending``

    Rules 2, 5, 6 and 7 reproduce ``Phase.roll_up`` exactly; rules 3 and 4 are
    the new states, and rule 5 folding ``timeout`` into ``error`` mirrors the
    original's refusal to call a phase ``done`` on anything but ``done`` and
    ``cancelled``.
    """

    seen = {normalize(s) for s in child_statuses}
    if not seen:
        return PENDING
    if RUNNING in seen:
        return RUNNING
    if BLOCKED in seen:
        return BLOCKED
    if PAUSED in seen:
        return PAUSED
    if ERROR in seen or TIMEOUT in seen:
        return ERROR
    if seen <= TERMINAL:
        return DONE
    if seen & TERMINAL:
        return RUNNING
    return PENDING


def resolve_status(
    kind: str, intrinsic: str, child_statuses: Iterable[str] = ()
) -> str:
    """The status a node should *display*.

    Nodes whose engine owns their state (``job``, ``agent``, ``workflow``,
    ``subagent``, ...) always report it. Container kinds report their children,
    except when they carry a hard terminal verdict of their own — a cancelled
    phase stays cancelled no matter what its children ended up doing — and
    except when they have no children yet, in which case the intrinsic value is
    the only information available.
    """

    own = normalize(intrinsic)
    if kind not in ROLLUP_KINDS:
        return own
    if own in HARD_TERMINAL:
        return own
    kids = list(child_statuses)
    if not kids:
        return own
    return roll_up(kids)
