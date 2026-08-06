"""Peer agent teams — the shared task DAG.

This package is the foundation the teams plan calls for first in its
implementation order: *"add the task DAG third, and get atomic claims right
before anything else in it. Everything else in the DAG is bookkeeping; the claim
is the correctness property."*

What is here today is exactly that, and nothing else:

* :mod:`mantis_agent.teams.types` — :class:`Task` and the team error family.
* :mod:`mantis_agent.teams.tasks` — :class:`TaskStore`, a SQLite DAG whose
  claims are compare-and-set inside ``BEGIN IMMEDIATE``, with leases,
  heartbeats, reclaim-with-partial-work-warning, dependency gating, cycle
  rejection at creation, and failure that blocks dependents loudly.

What is deliberately *not* here: peers, sessions, inboxes, messaging, approval,
the panel. Those layers sit on top and are the plan's later phases; this one has
no dependency on any of them and can be exercised entirely on its own, which is
the point — a claim that only works when a runtime is present is a claim nobody
can prove.

The module is pure and dependency-free apart from the stdlib, ``msgspec`` for
the structs and (only in :class:`AsyncTaskStore`) ``anyio`` for the thread
dispatch. Nothing in it imports the agent loop, and nothing in the agent loop
imports it yet.
"""

from __future__ import annotations

from .tasks import SCHEMA_VERSION, AsyncTaskStore, TaskStore
from .types import (
    ACTIVE_CLAIM_STATUSES,
    DEFAULT_LEASE_SECONDS,
    TASK_STATUSES,
    TERMINAL_STATUSES,
    Blockers,
    ClaimResult,
    Completion,
    Failure,
    InboxFullError,
    PeerNotFoundError,
    Task,
    TaskClaimConflictError,
    TaskCycleError,
    TaskDependencyBlockedError,
    TaskLeaseExpiredError,
    TaskNotFoundError,
    TaskStateError,
    TeamError,
    TeamNotFoundError,
    TeamStoreCorruptError,
    new_task_id,
    validate_actor,
    validate_id,
    validate_title,
)

__all__ = [
    "ACTIVE_CLAIM_STATUSES",
    "DEFAULT_LEASE_SECONDS",
    "SCHEMA_VERSION",
    "TASK_STATUSES",
    "TERMINAL_STATUSES",
    "AsyncTaskStore",
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
    "TaskStore",
    "TeamError",
    "TeamNotFoundError",
    "TeamStoreCorruptError",
    "new_task_id",
    "validate_actor",
    "validate_id",
    "validate_title",
]
