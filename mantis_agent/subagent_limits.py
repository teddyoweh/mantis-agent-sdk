"""Ceilings and accounting for child agents — one limiter, every spawner.

Why this module exists
----------------------
This package spawns child agents from **three** independent places:

* ``subagent.py`` — the ``task`` tool and the SDK's ``as_subagent_tool``;
* ``coordinator.py`` — ``_build_workers`` fans a fleet out over a workflow;
* ``workflow.py`` — ``Workflow.parallel`` under its own ``_default_cap()``.

Each one bounds itself. None of them bounds the *others*, and three private
caps are three separate ways to blow past the single number the user actually
set: a coordinate call with a cap of 8 running alongside a workflow with a cap
of 14 is 22 live children, and neither component is doing anything wrong. The
fix is not a fourth cap — it is one limiter the three of them take.

So this module owns the ceilings from the plan's §7 table and nothing else. It
does not spawn anything, it does not know what an ``Agent`` is, and it imports
nothing from the spawners (they will import *it*). That direction matters: a
limiter that depends on its callers cannot be shared by all of them.

The ceilings
------------
=============================  =======  ==========================
Limit                          Default  Scope
=============================  =======  ==========================
``maxDepth``                   2        ancestor chain
``maxSpawnsPerTurn``           12       one parent turn
``maxConcurrentAgents``        8        session-wide, all spawners
``maxTotalAgentsPerSession``   200      session lifetime
``maxChildWallSeconds``        600      one child
``maxReportBytes``             32768    one report (see below)
=============================  =======  ==========================

``maxReportBytes`` is declared here so the whole ceiling set is inspectable
from one object (``/status``, ``/agents ledger``), but it is **enforced** in
:mod:`mantis_agent.child_report`, which reads ``MANTIS_CHILD_REPORT_MAX``.
Two enforcers for one number would be a bug; one declaration and one enforcer
is the arrangement.

Depth lives on the context, not on the tool
-------------------------------------------
``_SUBAGENT_EXCLUDED_TOOLS`` already denies a child the ``task`` tool, so the
*tool* path cannot recurse. It is the only path that cannot. The SDK,
``WrappedAgentTool`` and workflow agents can all construct a child that
constructs a child, and none of them goes anywhere near the task schema. A
depth counter attached to the tool would therefore guard the one door that is
already locked.

So depth rides on a :class:`~contextvars.ContextVar`: whatever runs inside a
lease — any task it spawns, any tool it calls, any agent it builds — sees its
own depth and inherits it automatically. :meth:`SubagentLimiter.acquire` with
no explicit ``depth`` reads that context, so a spawner that never heard of
this module still gets counted correctly the moment it takes the limiter.

Refusals are structured, never silent
-------------------------------------
A truncated fan-out is the worst possible failure: the model believes it
dispatched twelve investigations, eight happened, and the summary it writes is
confidently wrong. Every ceiling here raises instead — a typed error naming the
limit, the observed value, the settings path (``subagents.maxSpawnsPerTurn``)
and the environment variable that governs it, in a sentence the model can act
on. See :meth:`SubagentLimitError.as_tool_error`.

Configuration precedence: defaults < settings mapping < environment. Junk in
either source falls back to the default rather than raising — a typo in a
settings file must not take the subagent channel down — and every value is
clamped into a sane range, so a hostile ``.mantis-agent/settings.json`` cannot
ask for a million concurrent agents.
"""

from __future__ import annotations

import itertools
import os
import time
from collections import deque
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple, Union

import anyio

from .errors import AgentError

__all__ = [
    "AUTO",
    "AgentBudgetLedger",
    "AgentLease",
    "AgentTypeStats",
    "ChildTimeoutError",
    "ChildWallClock",
    "ConcurrencyLimitError",
    "SessionAgentLimitError",
    "SpawnContext",
    "SpawnDepthExceededError",
    "SpawnRateExceededError",
    "SubagentError",
    "SubagentLimitError",
    "SubagentLimiter",
    "SubagentLimits",
    "SETTINGS_BLOCK",
    "current_depth",
    "current_spawn_context",
    "default_cpu_cap",
    "env_var_for",
    "reset_shared_limiter",
    "set_shared_limiter",
    "setting_path_for",
    "shared_limiter",
]


# ---------------------------------------------------------------------------
# Names: one table maps a Python field to its settings key and env var
# ---------------------------------------------------------------------------
#
# Errors quote both spellings, so the mapping cannot live in three places and
# drift. ``subagents.maxDepth`` is the §11 settings path; the env var mirrors
# the ``MANTIS_SUBAGENT_*`` family named in the same section.

_META: Dict[str, Tuple[str, str]] = {
    "max_depth": ("maxDepth", "MANTIS_SUBAGENT_MAX_DEPTH"),
    "max_spawns_per_turn": ("maxSpawnsPerTurn", "MANTIS_SUBAGENT_MAX_SPAWNS_PER_TURN"),
    "max_concurrent_agents": ("maxConcurrentAgents", "MANTIS_SUBAGENT_MAX_CONCURRENT"),
    "max_total_agents_per_session": (
        "maxTotalAgentsPerSession",
        "MANTIS_SUBAGENT_MAX_TOTAL_AGENTS",
    ),
    "max_child_wall_seconds": ("maxChildWallSeconds", "MANTIS_SUBAGENT_MAX_CHILD_SECONDS"),
    "max_report_bytes": ("maxReportBytes", "MANTIS_SUBAGENT_MAX_REPORT_BYTES"),
}

#: Settings block these limits live under, per §11 of the plan.
SETTINGS_BLOCK = "subagents"


def setting_path_for(field_name: str) -> str:
    """``"max_depth"`` → ``"subagents.maxDepth"`` — what an error tells a user
    to edit."""

    key = _META.get(field_name, (field_name, ""))[0]
    return f"{SETTINGS_BLOCK}.{key}"


def env_var_for(field_name: str) -> str:
    """``"max_depth"`` → ``"MANTIS_SUBAGENT_MAX_DEPTH"``."""

    return _META.get(field_name, ("", ""))[1]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SubagentError(AgentError):
    """Base for everything the subagent trust/limits layer raises.

    Inherits :class:`~mantis_agent.errors.AgentError` so the existing
    ``except AgentError`` handlers in the agent loop keep working — a new
    ceiling must not become an unhandled crash in a caller written before it
    existed.
    """


class SubagentLimitError(SubagentError):
    """A ceiling was hit.

    Carries the machine-readable pieces (``limit_field``, ``limit_name``,
    ``setting``, ``env_var``, ``limit``, ``observed``) *and* renders a sentence
    that names all of them, because the primary consumer is a model reading a
    ``tool_result``. "Spawn limit reached" alone tells it nothing it can do;
    "12 already started this turn, raise ``subagents.maxSpawnsPerTurn``" tells
    it both to stop and how to get more.
    """

    __slots__ = ("limit_field", "limit_name", "setting", "env_var", "limit", "observed", "action")

    def __init__(
        self,
        *,
        limit_field: str,
        limit: Union[int, float],
        observed: Union[int, float],
        detail: str,
        action: str = "",
    ) -> None:
        self.limit_field = limit_field
        self.limit_name = _META.get(limit_field, (limit_field, ""))[0]
        self.setting = setting_path_for(limit_field)
        self.env_var = env_var_for(limit_field)
        self.limit = limit
        self.observed = observed
        self.action = action
        parts = [detail.rstrip(".") + "."]
        if action:
            parts.append(action.rstrip(".") + ".")
        parts.append(f"Raise `{self.setting}` (env {self.env_var}) if this fan-out is intended.")
        super().__init__(" ".join(parts))

    def as_tool_error(self) -> str:
        """The string a spawner should hand back as the failing tool result."""

        return str(self)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe shape for the ledger, the activity cockpit and logs."""

        return {
            "error": type(self).__name__,
            "limit": self.limit_name,
            "setting": self.setting,
            "env": self.env_var,
            "limit_value": self.limit,
            "observed": self.observed,
            "message": str(self),
        }


class SpawnDepthExceededError(SubagentLimitError):
    """The ancestor chain is already ``maxDepth`` deep."""


class SpawnRateExceededError(SubagentLimitError):
    """``maxSpawnsPerTurn`` children were already started this parent turn."""


class ConcurrencyLimitError(SubagentLimitError):
    """No slot under ``maxConcurrentAgents`` and the caller would not wait.

    Note that the *default* acquire path waits — that is the entire point of a
    shared semaphore. This error is for callers that asked not to block
    (``wait=False``) or that gave up after ``timeout`` seconds.
    """


class SessionAgentLimitError(SubagentLimitError):
    """``maxTotalAgentsPerSession`` children have run since the session began."""


class ChildTimeoutError(SubagentLimitError):
    """A child outran ``maxChildWallSeconds``."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: ``max_concurrent_agents=AUTO`` derives the cap from the machine instead of
#: pinning a number. This is what lets one config express what
#: ``workflow._default_cap()`` does today — see :func:`default_cpu_cap`.
AUTO = "auto"

# Absolute bounds. Not policy — a blast radius. ``project``/``local`` settings
# live inside the working tree, so these numbers are attacker-influenceable in
# the "I cloned a repo" threat model; clamping means the worst a hostile file
# can do is raise the ceiling to something a laptop survives.
_BOUNDS: Dict[str, Tuple[float, float]] = {
    "max_depth": (0, 32),
    "max_spawns_per_turn": (1, 1024),
    "max_concurrent_agents": (1, 256),
    "max_total_agents_per_session": (1, 100_000),
    "max_child_wall_seconds": (0, 86_400),
    "max_report_bytes": (256, 16 * 1024 * 1024),
}


def default_cpu_cap() -> int:
    """``min(16, cpu - 2)`` with a floor of 1.

    A verbatim copy of ``workflow._default_cap()``. It is duplicated rather
    than imported because ``workflow`` imports half the package and this module
    must stay importable by all three spawners without a cycle; the pairing is
    pinned by a test that asserts the two agree.
    """

    cpu = os.cpu_count() or 2
    return max(1, min(16, cpu - 2))


def _coerce_number(value: Any) -> Optional[float]:
    """Best-effort number out of settings/env. ``None`` for junk.

    Bools are rejected on purpose: ``{"maxDepth": true}`` is a mistake, and
    silently reading it as ``1`` would cap depth in a way nobody could find.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _clamp(field_name: str, value: float) -> float:
    lo, hi = _BOUNDS[field_name]
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class SubagentLimits:
    """The §7 ceilings, as one immutable value.

    Frozen because a limiter hands this object to error messages and to the
    cockpit; a ceiling that can be mutated behind a running limiter's back is a
    ceiling nobody can reason about. Build a changed copy with
    :func:`dataclasses.replace`.
    """

    max_depth: int = 2
    max_spawns_per_turn: int = 12
    max_concurrent_agents: Union[int, str] = 8
    max_total_agents_per_session: int = 200
    max_child_wall_seconds: float = 600.0
    #: Declared here for inspectability; enforced by ``child_report``.
    max_report_bytes: int = 32768

    def __post_init__(self) -> None:
        # Clamp rather than raise: these values arrive from settings files and
        # environment variables as often as from code, and a hard failure there
        # takes out the whole subagent channel over a typo.
        object.__setattr__(self, "max_depth", int(_clamp("max_depth", int(self.max_depth))))
        object.__setattr__(
            self,
            "max_spawns_per_turn",
            int(_clamp("max_spawns_per_turn", int(self.max_spawns_per_turn))),
        )
        if not _is_auto(self.max_concurrent_agents):
            object.__setattr__(
                self,
                "max_concurrent_agents",
                int(_clamp("max_concurrent_agents", int(self.max_concurrent_agents))),
            )
        else:
            object.__setattr__(self, "max_concurrent_agents", AUTO)
        object.__setattr__(
            self,
            "max_total_agents_per_session",
            int(_clamp("max_total_agents_per_session", int(self.max_total_agents_per_session))),
        )
        object.__setattr__(
            self,
            "max_child_wall_seconds",
            float(_clamp("max_child_wall_seconds", float(self.max_child_wall_seconds))),
        )
        object.__setattr__(
            self, "max_report_bytes", int(_clamp("max_report_bytes", int(self.max_report_bytes)))
        )

    # -- derived ----------------------------------------------------------

    def resolved_max_concurrent_agents(self) -> int:
        """The concurrency cap as a number, resolving :data:`AUTO`."""

        if _is_auto(self.max_concurrent_agents):
            return default_cpu_cap()
        return int(self.max_concurrent_agents)

    def effective_concurrency(self, requested: Optional[int] = None) -> int:
        """The cap a spawner should actually use.

        ``Workflow(concurrency=...)`` and ``coordinate(concurrency=...)`` let a
        caller ask for a number. They may ask for **less** than the session cap
        — a workflow that wants to be gentle is fine — but never for more, or
        the shared ceiling is advisory. Hence ``min``.
        """

        cap = self.resolved_max_concurrent_agents()
        if requested is None:
            return cap
        try:
            want = int(requested)
        except (TypeError, ValueError):
            return cap
        return max(1, min(cap, want))

    @property
    def wall_clock_enabled(self) -> bool:
        """``max_child_wall_seconds <= 0`` means "no wall-clock bound"."""

        return self.max_child_wall_seconds > 0

    # -- loading ----------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "SubagentLimits":
        """Build from a settings mapping.

        Accepts either the whole settings document (``{"subagents": {...}}``)
        or the inner block, and both the camelCase spelling from §11 and the
        snake_case field names, because both show up in the wild. Unknown keys
        are ignored — this block is shared with the neutralizer, permissions
        and isolation sub-blocks that other phases own.
        """

        if not isinstance(data, Mapping):
            return cls()
        block = data.get(SETTINGS_BLOCK, data)
        if not isinstance(block, Mapping):
            return cls()

        values: Dict[str, Any] = {}
        for field_name, (json_key, _env) in _META.items():
            raw = block.get(json_key, block.get(field_name, None))
            if raw is None:
                continue
            if field_name == "max_concurrent_agents" and _is_auto(raw):
                values[field_name] = AUTO
                continue
            num = _coerce_number(raw)
            if num is None:
                continue
            values[field_name] = num if field_name == "max_child_wall_seconds" else int(num)
        return cls(**values)

    def with_env(self, env: Optional[Mapping[str, str]] = None) -> "SubagentLimits":
        """Overlay ``MANTIS_SUBAGENT_*`` variables on top of these limits.

        Environment wins over settings: it is the escape hatch for one run
        ("this fan-out really is 40 agents") and for CI.
        """

        source = os.environ if env is None else env
        values: Dict[str, Any] = {}
        for field_name, (_key, env_name) in _META.items():
            if not env_name:
                continue
            raw = source.get(env_name)
            if raw is None:
                continue
            if field_name == "max_concurrent_agents" and _is_auto(raw):
                values[field_name] = AUTO
                continue
            num = _coerce_number(raw)
            if num is None:
                continue
            values[field_name] = num if field_name == "max_child_wall_seconds" else int(num)
        if not values:
            return self
        current = {f: getattr(self, f) for f in _META}
        current.update(values)
        return SubagentLimits(**current)

    @classmethod
    def load(
        cls,
        settings: Optional[Mapping[str, Any]] = None,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> "SubagentLimits":
        """defaults < settings < environment, in one call."""

        return cls.from_mapping(settings).with_env(env)

    def to_dict(self) -> Dict[str, Any]:
        """The §11 JSON shape — what ``/status`` should print."""

        return {_META[f][0]: getattr(self, f) for f in _META}


def _is_auto(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"auto", "cpu", ""}


# ---------------------------------------------------------------------------
# Spawn context — where depth actually lives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnContext:
    """Who is running right now, and how deep.

    Set for the duration of a lease. Because it is a ``ContextVar``, every task
    a child creates inherits it for free — which is exactly why depth counting
    belongs here and not on the ``task`` tool.
    """

    agent_id: str
    agent_type: str
    depth: int
    parent_id: Optional[str] = None
    label: str = ""


_CURRENT: ContextVar[Optional[SpawnContext]] = ContextVar(
    "mantis_subagent_spawn_context", default=None
)


def current_spawn_context() -> Optional[SpawnContext]:
    """The lease context in force on this task, if any."""

    return _CURRENT.get()


def current_depth() -> int:
    """Depth of the currently-running agent.

    The root agent holds no lease and is therefore depth 0; the first child is
    depth 1. A child spawned from here would be ``current_depth() + 1``, which
    is what :meth:`SubagentLimiter.acquire` defaults to.
    """

    ctx = _CURRENT.get()
    return 0 if ctx is None else ctx.depth


class AgentLease:
    """One live child's claim on the session's agent budget.

    Held from the moment a spawner is cleared to start a child until that child
    is done. Releasing is idempotent and safe from anywhere: the concurrency
    slot, the live count, the ledger entry and the context variable all come
    back exactly once.
    """

    __slots__ = (
        "agent_id",
        "agent_type",
        "label",
        "depth",
        "parent_id",
        "started",
        "deadline",
        "_limiter",
        "_token",
        "_released",
    )

    def __init__(
        self,
        *,
        limiter: "SubagentLimiter",
        agent_id: str,
        agent_type: str,
        label: str,
        depth: int,
        parent_id: Optional[str],
        started: float,
        deadline: Optional[float],
    ) -> None:
        self._limiter = limiter
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.label = label
        self.depth = depth
        self.parent_id = parent_id
        self.started = started
        self.deadline = deadline
        self._token: Optional[Token] = None
        self._released = False

    # -- state ------------------------------------------------------------

    @property
    def released(self) -> bool:
        return self._released

    @property
    def child_depth(self) -> int:
        """Depth a child spawned *by this agent* would have."""

        return self.depth + 1

    @property
    def context(self) -> SpawnContext:
        return SpawnContext(
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            depth=self.depth,
            parent_id=self.parent_id,
            label=self.label,
        )

    def elapsed(self) -> float:
        return max(0.0, self._limiter.clock() - self.started)

    def remaining_seconds(self) -> Optional[float]:
        """Seconds left on ``maxChildWallSeconds``; ``None`` when unbounded."""

        if self.deadline is None:
            return None
        return self.deadline - self._limiter.clock()

    @property
    def expired(self) -> bool:
        rem = self.remaining_seconds()
        return rem is not None and rem <= 0

    # -- actions ----------------------------------------------------------

    def record_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Fold this child's usage into the ledger under its agent type."""

        self._limiter.ledger.record_usage(
            self.agent_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )

    def release(self, *, ok: bool = True) -> None:
        """Give the slot back. Idempotent."""

        self._limiter.release(self, ok=ok)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AgentLease(id={self.agent_id!r}, type={self.agent_type!r}, "
            f"depth={self.depth}, released={self._released})"
        )


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class AgentTypeStats:
    """Per-agent-type rollup. ``explore`` being 90% of the bill is the kind of
    thing nobody notices without this row."""

    started: int = 0
    live: int = 0
    completed: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started": self.started,
            "live": self.live,
            "completed": self.completed,
            "failed": self.failed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "wall_seconds": round(self.wall_seconds, 3),
        }


@dataclass
class AgentBudgetLedger:
    """Session-wide accounting for children: how many, how deep, how much.

    The existing docstring in ``subagent.py`` says a fan-out of fan-outs costs
    "compounds invisibly". This is the object that makes it visible — it is
    what ``/agents ledger`` reads. Refusals are counted too: a session that
    keeps bouncing off ``maxSpawnsPerTurn`` is a session whose ceiling is set
    wrong, and that only shows up if somebody keeps score.
    """

    started: int = 0
    live: int = 0
    peak_live: int = 0
    completed: int = 0
    failed: int = 0
    max_depth_reached: int = 0
    by_type: Dict[str, AgentTypeStats] = field(default_factory=dict)
    refused: Dict[str, int] = field(default_factory=dict)

    def stats_for(self, agent_type: str) -> AgentTypeStats:
        key = agent_type or "unknown"
        row = self.by_type.get(key)
        if row is None:
            row = AgentTypeStats()
            self.by_type[key] = row
        return row

    def record_start(self, *, agent_type: str, depth: int, agent_id: str = "") -> None:
        self.started += 1
        self.live += 1
        self.peak_live = max(self.peak_live, self.live)
        self.max_depth_reached = max(self.max_depth_reached, int(depth))
        row = self.stats_for(agent_type)
        row.started += 1
        row.live += 1

    def record_finish(
        self,
        *,
        agent_type: str,
        ok: bool = True,
        wall_seconds: float = 0.0,
        agent_id: str = "",
    ) -> None:
        # Clamped rather than allowed to go negative: a double release is a
        # caller bug, and a ledger that reports -1 live agents helps nobody
        # diagnose it. ``AgentLease.release`` is idempotent so this is belt and
        # braces.
        self.live = max(0, self.live - 1)
        if ok:
            self.completed += 1
        else:
            self.failed += 1
        row = self.stats_for(agent_type)
        row.live = max(0, row.live - 1)
        if ok:
            row.completed += 1
        else:
            row.failed += 1
        row.wall_seconds += max(0.0, float(wall_seconds))

    def record_usage(
        self,
        agent_type: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        row = self.stats_for(agent_type)
        row.input_tokens += int(input_tokens)
        row.output_tokens += int(output_tokens)
        row.cost_usd += float(cost_usd)

    def record_refusal(self, limit_name: str) -> None:
        self.refused[limit_name] = self.refused.get(limit_name, 0) + 1

    def rows(self) -> List[Tuple[str, AgentTypeStats]]:
        """Ledger rows, costliest first — the order ``/agents ledger`` wants."""

        return sorted(
            self.by_type.items(),
            key=lambda kv: (-kv[1].cost_usd, -kv[1].started, kv[0]),
        )

    def total_cost_usd(self) -> float:
        return sum(row.cost_usd for row in self.by_type.values())

    def total_tokens(self) -> int:
        return sum(row.input_tokens + row.output_tokens for row in self.by_type.values())

    def snapshot(self) -> Dict[str, Any]:
        return {
            "started": self.started,
            "live": self.live,
            "peak_live": self.peak_live,
            "completed": self.completed,
            "failed": self.failed,
            "max_depth_reached": self.max_depth_reached,
            "cost_usd": round(self.total_cost_usd(), 6),
            "tokens": self.total_tokens(),
            "by_type": {name: row.to_dict() for name, row in self.rows()},
            "refused": dict(self.refused),
        }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class _Waiter:
    __slots__ = ("event", "granted")

    def __init__(self, event: "anyio.Event") -> None:
        self.event = event
        self.granted = False


class _FifoGate:
    """A first-in-first-out counting gate.

    Hand-rolled rather than ``anyio.Semaphore`` for two reasons. First,
    ordering: waiters are served in arrival order and a released slot is handed
    *directly* to the next waiter, so a burst of coordinator workers cannot
    starve one workflow agent that has been queued for a minute. Second,
    lifetime: nothing is allocated until someone actually waits, so a limiter
    can be constructed at session start — outside any event loop — and still be
    usable from whichever loop later runs.
    """

    __slots__ = ("_capacity", "_held", "_waiters")

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._held = 0
        self._waiters: Deque[_Waiter] = deque()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def held(self) -> int:
        return self._held

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def try_acquire(self) -> bool:
        # Order is actually guaranteed by :meth:`release`, which hands the slot
        # to the head of the queue instead of decrementing and letting whoever
        # wakes first take it — so ``_held`` never drops below capacity while
        # anyone is waiting. The ``not self._waiters`` clause is belt and
        # braces for that invariant: if capacity ever becomes adjustable at
        # runtime, it is what keeps a newcomer from stepping over the queue.
        if self._held < self._capacity and not self._waiters:
            self._held += 1
            return True
        return False

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        if self.try_acquire():
            # Still yield: an acquire that never suspends turns a fan-out loop
            # into a synchronous block under structured concurrency.
            await anyio.lowlevel.checkpoint()
            return True
        if timeout is not None and timeout <= 0:
            return False

        waiter = _Waiter(anyio.Event())
        self._waiters.append(waiter)
        try:
            if timeout is None:
                await waiter.event.wait()
            else:
                with anyio.move_on_after(timeout):
                    await waiter.event.wait()
        except BaseException:
            # Cancellation included. Drop out of the queue, and if the slot was
            # handed to us in the same tick, hand it straight on.
            self._abandon(waiter)
            raise
        if waiter.granted:
            return True
        self._abandon(waiter)
        return False

    def _abandon(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            # Already popped, which only happens on a grant. Give it back so
            # the slot is not lost to a timed-out or cancelled waiter.
            if waiter.granted:
                waiter.granted = False
                self.release()

    def release(self) -> None:
        if self._waiters:
            waiter = self._waiters.popleft()
            waiter.granted = True
            waiter.event.set()
            return  # slot handed over; _held stays put
        self._held = max(0, self._held - 1)


# ---------------------------------------------------------------------------
# Wall clock
# ---------------------------------------------------------------------------


class ChildWallClock:
    """``async with`` bound on one child's wall time.

    Written as a class, not an ``@asynccontextmanager``, on purpose: a cancel
    scope suspended inside an async generator is finalized wherever the garbage
    collector gets to it, which is the one thing anyio scopes cannot survive.
    A plain object enters and exits the scope in the same task, always.
    """

    __slots__ = ("_seconds", "_agent_id", "_agent_type", "_scope", "timed_out")

    def __init__(
        self,
        seconds: Optional[float],
        *,
        agent_id: str = "",
        agent_type: str = "",
    ) -> None:
        self._seconds = seconds
        self._agent_id = agent_id
        self._agent_type = agent_type
        self._scope: Any = None
        #: Set when the bound actually fired — readable by a caller that
        #: catches the error and wants to distinguish "child was slow" from
        #: "child raised".
        self.timed_out = False

    async def __aenter__(self) -> "ChildWallClock":
        if self._seconds is not None and self._seconds > 0:
            self._scope = anyio.move_on_after(self._seconds)
            self._scope.__enter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._scope is None:
            return False
        suppressed = bool(self._scope.__exit__(exc_type, exc, tb))
        if self._scope.cancelled_caught:
            # The body was cancelled by our own scope, so the cancellation has
            # been absorbed here and turning it into a typed error is honest:
            # nothing upstream is still being cancelled.
            self.timed_out = True
            raise self.timeout_error()
        return suppressed

    def timeout_error(self) -> ChildTimeoutError:
        who = self._agent_id or self._agent_type or "child agent"
        return ChildTimeoutError(
            limit_field="max_child_wall_seconds",
            limit=self._seconds if self._seconds is not None else 0,
            observed=self._seconds if self._seconds is not None else 0,
            detail=(
                f"subagent wall-clock limit reached: {who} ran longer than "
                f"{self._seconds:g}s and was stopped"
            ),
            action="Narrow the child's task, or split it across two children",
        )


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------


class _SpawnScope:
    """What :meth:`SubagentLimiter.spawn` returns. See that method."""

    __slots__ = ("_limiter", "_kwargs", "_wall_clock", "_lease", "_clock")

    def __init__(self, limiter: "SubagentLimiter", *, wall_clock: bool, kwargs: Dict[str, Any]):
        self._limiter = limiter
        self._kwargs = kwargs
        self._wall_clock = wall_clock
        self._lease: Optional[AgentLease] = None
        self._clock: Optional[ChildWallClock] = None

    async def __aenter__(self) -> AgentLease:
        lease = await self._limiter.acquire(**self._kwargs)
        self._lease = lease
        if self._wall_clock and lease.deadline is not None:
            self._clock = ChildWallClock(
                self._limiter.limits.max_child_wall_seconds,
                agent_id=lease.agent_id,
                agent_type=lease.agent_type,
            )
            try:
                await self._clock.__aenter__()
            except BaseException:
                self._limiter.release(lease, ok=False)
                raise
        return lease

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        lease = self._lease
        suppressed = False
        timeout_error: Optional[ChildTimeoutError] = None
        try:
            if self._clock is not None:
                try:
                    suppressed = await self._clock.__aexit__(exc_type, exc, tb)
                except ChildTimeoutError as err:
                    timeout_error = err
        finally:
            if lease is not None:
                # Release in a ``finally``: the slot must come back however the
                # body ended — returned, raised, cancelled, or timed out.
                self._limiter.release(
                    lease, ok=(exc_type is None and timeout_error is None)
                )
        if timeout_error is not None:
            raise timeout_error
        return suppressed


class SubagentLimiter:
    """One limiter, shared by every spawner in the session.

    Owns the concurrency gate, the per-turn / per-session counters, the depth
    rule and the ledger. ``subagent.py``, ``coordinator.py`` and
    ``workflow.py`` are meant to take *the same instance* (see
    :func:`shared_limiter`) — that is the whole point, and any spawner holding
    a private one has reintroduced the bug.

    Typical use::

        async with limiter.spawn(agent_type="explore") as lease:
            ...run the child...
            lease.record_usage(input_tokens=n_in, output_tokens=n_out)

    or, for callers that cannot wrap their child in a ``with``::

        lease = await limiter.acquire(depth, parent_id)
        try:
            ...
        finally:
            limiter.release()
    """

    def __init__(
        self,
        limits: Optional[SubagentLimits] = None,
        *,
        ledger: Optional[AgentBudgetLedger] = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.limits = limits or SubagentLimits()
        self.ledger = ledger or AgentBudgetLedger()
        self.clock = clock
        self._gate = _FifoGate(self.limits.resolved_max_concurrent_agents())
        self._ids = itertools.count(1)
        self._turn = 0
        self._spawns_this_turn = 0
        self._total_started = 0
        self._live: Dict[str, AgentLease] = {}

    # -- introspection ----------------------------------------------------

    @property
    def concurrency_cap(self) -> int:
        return self._gate.capacity

    @property
    def live(self) -> int:
        return len(self._live)

    @property
    def waiting(self) -> int:
        return self._gate.waiting

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def spawns_this_turn(self) -> int:
        return self._spawns_this_turn

    @property
    def total_started(self) -> int:
        return self._total_started

    def live_leases(self) -> List[AgentLease]:
        """What ``/agents live`` renders."""

        return sorted(self._live.values(), key=lambda le: le.started)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "limits": self.limits.to_dict(),
            "concurrency_cap": self.concurrency_cap,
            "live": self.live,
            "waiting": self.waiting,
            "turn": self._turn,
            "spawns_this_turn": self._spawns_this_turn,
            "total_started": self._total_started,
            "ledger": self.ledger.snapshot(),
        }

    # -- turn boundary ----------------------------------------------------

    def begin_turn(self) -> int:
        """Reset the per-turn spawn counter. Call once per parent turn.

        ``maxSpawnsPerTurn`` is a *rate* limit, not a session budget: twelve
        children per turn over twenty turns is a normal long session, whereas
        twelve in one turn twice in a row is usually a model that has decided
        fan-out is free.
        """

        self._turn += 1
        self._spawns_this_turn = 0
        return self._turn

    # -- acquire / release ------------------------------------------------

    async def acquire(
        self,
        depth: Optional[int] = None,
        parent_id: Optional[str] = None,
        *,
        agent_type: str = "general-purpose",
        label: str = "",
        agent_id: Optional[str] = None,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> AgentLease:
        """Clear one child to start, or raise a :class:`SubagentLimitError`.

        ``depth`` and ``parent_id`` default to *this* task's spawn context, so
        a nested spawn is counted correctly without the caller threading
        anything through. Pass them explicitly only when reconstructing a
        chain (resuming a job, a subprocess child reporting home).

        Order of checks is deliberate: the cheap, definitely-fatal ones first
        so a caller that is going to be refused is refused before it queues
        behind eight running agents.
        """

        parent = _CURRENT.get()
        if depth is None:
            # Every acquire clears a *child*, so the default is one below
            # whoever is running. The root agent holds no lease and is depth 0,
            # which makes the first child depth 1 and ``maxDepth=2`` mean
            # "parent -> child -> grandchild", as the plan's table says.
            depth = (parent.depth if parent is not None else 0) + 1
        depth = max(0, int(depth))
        if parent_id is None and parent is not None:
            parent_id = parent.agent_id

        limits = self.limits

        # (1) Depth. Nothing about waiting or retrying fixes it.
        if depth > limits.max_depth:
            self.ledger.record_refusal(_META["max_depth"][0])
            raise SpawnDepthExceededError(
                limit_field="max_depth",
                limit=limits.max_depth,
                observed=depth,
                detail=(
                    f"subagent depth limit reached: spawning at depth {depth} exceeds "
                    f"maxDepth {limits.max_depth} (chain: {self._chain_text(parent_id)})"
                ),
                action="Have this agent do the work itself instead of delegating again",
            )

        # (2) Session lifetime. Also terminal for this session.
        if self._total_started >= limits.max_total_agents_per_session:
            self.ledger.record_refusal(_META["max_total_agents_per_session"][0])
            raise SessionAgentLimitError(
                limit_field="max_total_agents_per_session",
                limit=limits.max_total_agents_per_session,
                observed=self._total_started,
                detail=(
                    f"subagent session limit reached: {self._total_started} agents have run "
                    f"in this session (limit {limits.max_total_agents_per_session})"
                ),
                action="Start a fresh session for further delegation",
            )

        # (3) This turn's rate.
        if self._spawns_this_turn >= limits.max_spawns_per_turn:
            self.ledger.record_refusal(_META["max_spawns_per_turn"][0])
            raise SpawnRateExceededError(
                limit_field="max_spawns_per_turn",
                limit=limits.max_spawns_per_turn,
                observed=self._spawns_this_turn,
                detail=(
                    f"subagent spawn limit reached: {self._spawns_this_turn} agents already "
                    f"started this turn (limit {limits.max_spawns_per_turn})"
                ),
                action="Wait for the running children and use their results before spawning more",
            )

        # Reserve before waiting so two tasks racing at the boundary cannot
        # both pass the checks above; rolled back if the gate refuses us.
        self._spawns_this_turn += 1
        self._total_started += 1
        # ``wait=False`` means "take a slot or refuse", which the gate spells
        # as a zero timeout. ``wait=True`` with no timeout queues indefinitely.
        gate_timeout: Optional[float] = 0.0 if not wait else timeout
        try:
            granted = await self._gate.acquire(gate_timeout)
        except BaseException:
            self._spawns_this_turn -= 1
            self._total_started -= 1
            raise
        if not granted:
            self._spawns_this_turn -= 1
            self._total_started -= 1
            self.ledger.record_refusal(_META["max_concurrent_agents"][0])
            raise ConcurrencyLimitError(
                limit_field="max_concurrent_agents",
                limit=self.concurrency_cap,
                observed=self.live,
                detail=(
                    f"subagent concurrency limit reached: {self.live} agents are already "
                    f"running (limit {self.concurrency_cap}) and no slot came free"
                    + (f" within {timeout:g}s" if timeout else "")
                ),
                action="Let a running child finish, or spawn this one after the current batch",
            )

        now = self.clock()
        lease = AgentLease(
            limiter=self,
            agent_id=agent_id or f"sub:{next(self._ids)}",
            agent_type=agent_type or "general-purpose",
            label=label,
            depth=depth,
            parent_id=parent_id,
            started=now,
            deadline=(now + limits.max_child_wall_seconds if limits.wall_clock_enabled else None),
        )
        self._live[lease.agent_id] = lease
        self.ledger.record_start(
            agent_type=lease.agent_type, depth=lease.depth, agent_id=lease.agent_id
        )
        # From here on, anything running in this task is "inside" the child.
        lease._token = _CURRENT.set(lease.context)  # noqa: SLF001 - same module
        return lease

    def release(self, lease: Optional[AgentLease] = None, *, ok: bool = True) -> None:
        """Return a lease's slot. Idempotent, and safe to call twice.

        With no argument it releases the lease bound to the current task's
        context, which is what a ``finally:`` in the spawner usually wants.
        """

        if lease is None:
            ctx = _CURRENT.get()
            if ctx is None:
                return
            lease = self._live.get(ctx.agent_id)
            if lease is None:
                return
        if lease._released:  # noqa: SLF001 - same module
            return
        lease._released = True  # noqa: SLF001

        self._live.pop(lease.agent_id, None)
        token = lease._token  # noqa: SLF001
        lease._token = None  # noqa: SLF001
        if token is not None:
            try:
                _CURRENT.reset(token)
            except (ValueError, RuntimeError):
                # Released from a different context than it was taken in (a
                # task group child, a callback). The token is not ours to
                # reset; the context dies with that task anyway.
                pass
        self.ledger.record_finish(
            agent_type=lease.agent_type,
            ok=ok,
            wall_seconds=max(0.0, self.clock() - lease.started),
            agent_id=lease.agent_id,
        )
        self._gate.release()

    def spawn(
        self,
        depth: Optional[int] = None,
        parent_id: Optional[str] = None,
        *,
        agent_type: str = "general-purpose",
        label: str = "",
        agent_id: Optional[str] = None,
        wait: bool = True,
        timeout: Optional[float] = None,
        wall_clock: bool = True,
    ) -> _SpawnScope:
        """``async with limiter.spawn(...) as lease:`` — acquire, bound the
        child's wall time, and release on the way out however the body ends.

        The release is in a ``finally``, so an exception, a cancellation or a
        wall-clock timeout all give the slot back. Set ``wall_clock=False``
        when the caller enforces its own timeout (the job supervisor does).
        """

        return _SpawnScope(
            self,
            wall_clock=wall_clock,
            kwargs={
                "depth": depth,
                "parent_id": parent_id,
                "agent_type": agent_type,
                "label": label,
                "agent_id": agent_id,
                "wait": wait,
                "timeout": timeout,
            },
        )

    def wall_clock(self, *, agent_id: str = "", agent_type: str = "") -> ChildWallClock:
        """A standalone ``maxChildWallSeconds`` bound, for callers that manage
        their own lease."""

        seconds = self.limits.max_child_wall_seconds if self.limits.wall_clock_enabled else None
        return ChildWallClock(seconds, agent_id=agent_id, agent_type=agent_type)

    # -- helpers ----------------------------------------------------------

    def _chain_text(self, parent_id: Optional[str]) -> str:
        """``root -> sub:1 -> sub:4`` for the depth error, walking live leases."""

        chain: List[str] = []
        seen = set()
        node = parent_id
        while node and node not in seen and len(chain) < 8:
            seen.add(node)
            chain.append(node)
            lease = self._live.get(node)
            node = lease.parent_id if lease is not None else None
        chain.append("root")
        return " -> ".join(reversed(chain))


# ---------------------------------------------------------------------------
# The shared instance
# ---------------------------------------------------------------------------
#
# Process-wide because a Mantis process is a session. The three spawners reach
# for this rather than constructing their own; tests (and any embedder running
# two sessions in one process) can install a private one.

_SHARED: Optional[SubagentLimiter] = None


def shared_limiter(limits: Optional[SubagentLimits] = None) -> SubagentLimiter:
    """The session's limiter, created on first use.

    ``limits`` applies only when it creates the instance — a later caller
    cannot quietly widen the ceiling by passing a roomier config. Use
    :func:`set_shared_limiter` to replace it deliberately.
    """

    global _SHARED
    if _SHARED is None:
        _SHARED = SubagentLimiter(limits or SubagentLimits.load())
    return _SHARED


def set_shared_limiter(limiter: Optional[SubagentLimiter]) -> Optional[SubagentLimiter]:
    """Install (or clear) the shared limiter. Returns the previous one."""

    global _SHARED
    previous = _SHARED
    _SHARED = limiter
    return previous


def reset_shared_limiter() -> None:
    """Drop the shared limiter so the next :func:`shared_limiter` rebuilds it."""

    set_shared_limiter(None)
