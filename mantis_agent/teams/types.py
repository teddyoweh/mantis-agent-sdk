"""Team value types and the team error family.

This module is deliberately inert: structs, constants, validation helpers and
exceptions, no I/O and no imports from the rest of the package beyond
:mod:`mantis_agent.errors`. :mod:`mantis_agent.teams.tasks` owns the durable
store; everything it hands back or raises is defined here, so a caller can type
against teams without pulling in ``sqlite3``.

Two conventions carry over from the rest of the SDK and are worth stating:

* **Structs are frozen.** A :class:`Task` is a *snapshot* of a row, not a
  handle on it. Mutating a snapshot could never move the database, and making
  that impossible is cheaper than documenting it — every state change goes
  through a :class:`~mantis_agent.teams.tasks.TaskStore` method that returns a
  fresh snapshot.
* **Errors carry their evidence.** ``TaskCycleError`` holds the cycle path,
  ``TaskClaimConflictError`` holds the current holder, and
  ``TaskDependencyBlockedError`` separates *pending*, *failed* and *missing*
  dependencies. A caller (or a peer's system prompt) should never have to parse
  an error string to learn what went wrong.
"""

from __future__ import annotations

import re
import uuid

import msgspec

from ..errors import AgentError

__all__ = [
    "ACTIVE_CLAIM_STATUSES",
    "DEFAULT_LEASE_SECONDS",
    "MAX_ID_LENGTH",
    "MAX_TITLE_LENGTH",
    "TASK_STATUSES",
    "TERMINAL_STATUSES",
    "Blockers",
    "ClaimResult",
    "Completion",
    "Failure",
    "InboxFullError",
    "PeerNotFoundError",
    "Task",
    "TaskClaimConflictError",
    "TaskCycleError",
    "TaskDependencyBlockedError",
    "TaskLeaseExpiredError",
    "TaskNotFoundError",
    "TaskStateError",
    "TeamError",
    "TeamNotFoundError",
    "TeamStoreCorruptError",
    "new_task_id",
    "validate_actor",
    "validate_id",
    "validate_title",
]


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------
#
# The vocabulary is fixed at eight words (plan §5) and validated on every write.
# A store that accepts an unknown status is a store whose claimability rules
# silently stop applying, which is exactly the class of bug the claim exists to
# prevent — so an unknown status is a hard error, never a pass-through.

TASK_STATUSES = frozenset(
    {"open", "claimed", "in_progress", "blocked", "review", "done", "failed"}
)

#: Statuses that mean "a peer currently holds this task". These are the only
#: ones a lease applies to, and therefore the only ones expiry can release.
ACTIVE_CLAIM_STATUSES = frozenset({"claimed", "in_progress", "review"})

#: Statuses no lease sweep, claim or dependency re-evaluation may disturb.
TERMINAL_STATUSES = frozenset({"done", "failed"})

#: Claim lease, seconds. Matches ``teams.claimLeaseSeconds`` in plan §12: long
#: enough that a working peer refreshes it comfortably at the 30s heartbeat,
#: short enough that a crashed peer's task is back in the pool within minutes.
DEFAULT_LEASE_SECONDS = 300.0

#: Ids become filenames and SQL parameters elsewhere in the team layer, so they
#: are bounded and character-restricted at the door rather than sanitized at
#: each use. Actors ("peer:infra", "user") get a slightly wider alphabet.
MAX_ID_LENGTH = 128
MAX_TITLE_LENGTH = 400

_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def new_task_id() -> str:
    """A fresh task id. ``task_`` prefix + 16 hex chars.

    Short enough to type in ``/team task claim <id>``, wide enough (64 bits)
    that a team creating tasks for a decade will not collide.
    """

    return "task_" + uuid.uuid4().hex[:16]


def validate_id(value: str, *, what: str = "id") -> str:
    """Return *value* if it is a legal identifier, else raise ``ValueError``."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    if len(value) > MAX_ID_LENGTH:
        raise ValueError(f"{what} exceeds {MAX_ID_LENGTH} characters")
    if not _ID_RE.match(value):
        raise ValueError(
            f"{what} {value!r} must match [A-Za-z0-9][A-Za-z0-9._:-]* "
            "(no spaces, slashes or control characters)"
        )
    return value


def validate_actor(value: str, *, what: str = "peer") -> str:
    """Validate a claimant identity — a peer id, ``"user"`` or ``"system"``.

    Same alphabet as :func:`validate_id`; the separate name exists because the
    error message is what a peer sees when it fumbles its own identity, and
    because the two may diverge once peers get namespaced ids.
    """

    return validate_id(value, what=what)


def validate_title(value: str) -> str:
    """Normalize a task title: stripped, non-empty, bounded, no control bytes.

    Titles are rendered into the team panel, so a title containing an ANSI
    escape or a newline is a display-corruption bug waiting to happen. Control
    characters are replaced rather than rejected — a peer that pastes a tab into
    a title should get a title, not an exception.
    """

    if not isinstance(value, str):
        raise ValueError("task title must be a string")
    cleaned = _CONTROL_RE.sub(" ", value).strip()
    if not cleaned:
        raise ValueError("task title must not be empty")
    if len(cleaned) > MAX_TITLE_LENGTH:
        cleaned = cleaned[: MAX_TITLE_LENGTH - 1] + "…"
    return cleaned


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class Task(msgspec.Struct, frozen=True):
    """One unit of team work, as it exists in ``tasks.db``.

    ``depends_on`` is stored in its own table (a task with three dependencies is
    three rows there), but a snapshot carries it inline because every consumer —
    claimability, the panel, cycle checking — needs it alongside the row.

    Claim state is four fields rather than a nested struct so a single ``UPDATE``
    sets or clears all of it inside one transaction:

    ``assignee``/``claimed_at``/``lease_expires``/``heartbeat_at``
        The live claim. All four are ``None`` exactly when the task is unheld.
    ``last_assignee``/``reclaim_count``/``partial_work``
        The *history* of claims, which survives release. ``partial_work`` is the
        warning the next claimant is owed: a peer whose lease expired may have
        got halfway, and the reclaimer must be told so rather than assuming a
        clean slate.
    """

    id: str
    team_id: str
    title: str
    description: str = ""
    created_by: str = ""
    depends_on: tuple[str, ...] = ()
    status: str = "open"
    assignee: str | None = None
    claimed_at: float | None = None
    lease_expires: float | None = None
    heartbeat_at: float | None = None
    reclaim_count: int = 0
    last_assignee: str = ""
    partial_work: bool = False
    blocked_reason: str = ""
    blocked_by: str = ""
    result: str = ""
    artifacts: tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_held(self) -> bool:
        return self.assignee is not None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def lease_remaining(self, now: float) -> float:
        """Seconds left on the lease; ``0.0`` when unheld or already expired."""

        if self.lease_expires is None:
            return 0.0
        return max(0.0, self.lease_expires - now)


class Blockers(msgspec.Struct, frozen=True):
    """Why a task is not claimable, split by cause.

    The split matters because the three have different remedies: *pending* work
    resolves itself, *failed* work needs a retry decision from a human or the
    lead, and *missing* means somebody referenced a task id that was never
    created — a typo, and unlike the other two it will never resolve on its own.
    """

    pending: tuple[str, ...] = ()   # dependencies that exist but are not done
    failed: tuple[str, ...] = ()    # dependencies that failed or are blocked
    missing: tuple[str, ...] = ()   # dependency ids with no such task

    @property
    def clear(self) -> bool:
        return not (self.pending or self.failed or self.missing)


class ClaimResult(msgspec.Struct, frozen=True):
    """A successful claim, plus what the claimant needs to know about it.

    ``reclaimed`` is true when this claim followed an expired lease rather than
    a fresh open task. In that case ``previous_assignee`` names the peer that
    dropped it and ``partial_work`` says the tree may already have been touched.
    ``warning`` is the human-readable version, ready to paste into the peer's
    prompt — a reclaimer that is not told about partial work will redo finished
    work at best, and duplicate a half-applied edit at worst.
    """

    task: Task
    reclaimed: bool = False
    previous_assignee: str = ""
    partial_work: bool = False
    warning: str = ""


class Completion(msgspec.Struct, frozen=True):
    """The result of completing a task: the row, plus who it woke up.

    ``unblocked`` is the point of the whole DAG — the dependents that became
    claimable because of this completion. It is what the runtime turns into
    wake-ups, and it is empty for a leaf task.
    """

    task: Task
    unblocked: tuple[str, ...] = ()
    resumed: tuple[str, ...] = ()   # blocked -> open, cause cleared


class Failure(msgspec.Struct, frozen=True):
    """The result of failing a task: the row, plus the dependents it took down."""

    task: Task
    blocked: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Errors (plan §13)
# ---------------------------------------------------------------------------


class TeamError(AgentError):
    """Base for every team-layer error."""


class TeamNotFoundError(TeamError):
    """No such team id."""


class PeerNotFoundError(TeamError):
    """No such peer in this team."""


class TeamStoreCorruptError(TeamError):
    """A team store is unreadable, or was written by a newer schema."""


class InboxFullError(TeamError):
    """A peer inbox is at its bound and the message cannot be archived away."""


class TaskNotFoundError(TeamError):
    """No such task id."""

    __slots__ = ("task_id",)

    def __init__(self, task_id: str):
        super().__init__(f"task {task_id!r} not found")
        self.task_id = task_id


class TaskCycleError(TeamError):
    """A dependency edge would close a cycle. Carries the path.

    The path starts and ends at the same task and reads in dependency order —
    ``a -> c -> b -> a`` means "a depends on c, c depends on b, b depends on a".
    Reporting it is the difference between a fixable error and a puzzle.
    """

    __slots__ = ("path",)

    def __init__(self, path: tuple[str, ...]):
        super().__init__("task dependency cycle: " + " -> ".join(path))
        self.path = path


class TaskClaimConflictError(TeamError):
    """The compare-and-set lost: someone else holds the task, or it moved on.

    This is the *expected* outcome for nineteen of twenty racing peers, so it is
    cheap to construct and carries the winner's identity rather than a generic
    message. ``holder`` is empty when the task simply is not claimable any more
    (done, failed) rather than held.
    """

    __slots__ = ("task_id", "holder", "status")

    def __init__(self, task_id: str, *, holder: str = "", status: str = ""):
        if holder:
            msg = f"task {task_id!r} is already claimed by {holder!r} (status {status})"
        else:
            msg = f"task {task_id!r} cannot be claimed (status {status})"
        super().__init__(msg)
        self.task_id = task_id
        self.holder = holder
        self.status = status


class TaskLeaseExpiredError(TeamError):
    """A peer acted on a claim whose lease had already expired.

    Distinct from :class:`TaskClaimConflictError` on purpose: the peer did
    nothing wrong except go quiet, and the correct response is to re-claim and
    reconcile rather than to look for the peer that stole the task.
    """

    __slots__ = ("task_id", "peer")

    def __init__(self, task_id: str, peer: str):
        super().__init__(
            f"claim on task {task_id!r} by {peer!r} expired; re-claim before continuing"
        )
        self.task_id = task_id
        self.peer = peer


class TaskDependencyBlockedError(TeamError):
    """A task was claimed before its dependencies allowed it."""

    __slots__ = ("task_id", "pending", "failed", "missing")

    def __init__(self, task_id: str, blockers: Blockers):
        parts = []
        if blockers.pending:
            parts.append("waiting on " + ", ".join(blockers.pending))
        if blockers.failed:
            parts.append("failed upstream: " + ", ".join(blockers.failed))
        if blockers.missing:
            parts.append("unknown dependencies: " + ", ".join(blockers.missing))
        super().__init__(
            f"task {task_id!r} is not claimable — " + "; ".join(parts or ["blocked"])
        )
        self.task_id = task_id
        self.pending = blockers.pending
        self.failed = blockers.failed
        self.missing = blockers.missing


class TaskStateError(TeamError):
    """An illegal state transition (completing a failed task, starting twice).

    Not in the plan's error list because the plan enumerates the *interesting*
    failures; this is the catch-all that keeps the status vocabulary honest, and
    it names both the current and the attempted state so the caller can tell a
    race apart from a logic bug.
    """

    __slots__ = ("task_id", "status", "attempted")

    def __init__(self, task_id: str, status: str, attempted: str):
        super().__init__(
            f"task {task_id!r} is {status}; cannot {attempted}"
        )
        self.task_id = task_id
        self.status = status
        self.attempted = attempted
