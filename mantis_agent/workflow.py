"""Deterministic multi-agent workflow engine.

Mirrors Claude Code's coordinator: a :class:`Workflow` groups child-agent runs
into **phases**, fans them out with :meth:`Workflow.parallel` (barrier) or
:meth:`Workflow.pipeline` (per-item chains, no barrier), caps concurrency at
``min(16, cpu-2)``, and streams every state change through a single
``on_event(run)`` callback so a TUI can redraw at 1 Hz.

Design mirrors ``swarm.py``: **pure orchestration with an injectable
``agent_runner``**. The default runner builds a real child :class:`Agent` (via
the same ``make_task_tool``/:class:`AgentType` path the ``task`` tool uses) and
drives ``run_iter``; tests inject a stub async-iterator so nothing hits a model.

Determinism
-----------
No wall-clock reads leak into the engine — a single injectable ``clock`` (default
``time.monotonic`` in **milliseconds**) supplies every timestamp, so tests fix
elapsed durations exactly. Durations follow the Claude rule
``elapsed = now - started - total_paused_ms``.

Token accounting follows :func:`workflow_view.accumulate_usage`: input is
cumulative-per-turn (keep latest), output is per-turn (sum).

This module imports NOTHING from ``prompt_toolkit``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from itertools import count
from typing import Any, AsyncIterator, Awaitable, Callable

import anyio

from .budget import Budget, BudgetTracker
from .types import AssistantMessage, ModelUsage, ToolUseBlock, UserMessage
from .workflow_view import accumulate_usage, total_tokens

__all__ = [
    "AgentRun",
    "Phase",
    "WorkflowRun",
    "Workflow",
    "WorkflowError",
    "default_clock",
    "make_agent_runner",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkflowError(Exception):
    """Typed control-plane error. ``code`` is one of ``not_found`` /
    ``not_running`` — mirrors ``stopTask.ts``'s typed raises."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Clock + id helpers
# ---------------------------------------------------------------------------


def default_clock() -> float:
    """Monotonic clock in **milliseconds** (the unit every timestamp uses)."""

    return time.monotonic() * 1000.0


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n: int) -> str:
    """Base-36 encode a non-negative int (``0`` → ``"0"``)."""

    if n == 0:
        return "0"
    out: list[str] = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


_RECENT_CAP = 5


@dataclass
class AgentRun:
    """One child-agent execution inside a phase."""

    id: str
    label: str
    phase: str = ""
    model: str = ""
    agent_type: str = "general-purpose"
    status: str = "queued"  # queued | running | done | error | cancelled
    started: float | None = None
    ended: float | None = None
    total_paused_ms: float = 0.0
    usage: ModelUsage = field(default_factory=ModelUsage)
    tool_count: int = 0
    recent_activities: list[str] = field(default_factory=list)
    summary: str = ""
    result: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    turns: int = 0
    # Not serialized — private live state.
    _paused: bool = field(default=False, repr=False, compare=False)
    _prompt: str = field(default="", repr=False, compare=False)
    _schema: dict | None = field(default=None, repr=False, compare=False)

    def elapsed_ms(self, now: float) -> float:
        """``now - started - total_paused_ms`` (clamped ≥ 0)."""

        if self.started is None:
            return 0.0
        end = self.ended if self.ended is not None else now
        return max(0.0, end - self.started - self.total_paused_ms)

    def push_activity(self, text: str) -> None:
        """Append to ``recent_activities`` keeping the newest ``_RECENT_CAP``."""

        self.recent_activities.append(text)
        if len(self.recent_activities) > _RECENT_CAP:
            del self.recent_activities[:-_RECENT_CAP]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "phase": self.phase,
            "model": self.model,
            "agent_type": self.agent_type,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "total_paused_ms": self.total_paused_ms,
            "usage": {
                "inputTokens": self.usage.inputTokens,
                "outputTokens": self.usage.outputTokens,
                "cacheReadInputTokens": self.usage.cacheReadInputTokens,
                "cacheCreationInputTokens": self.usage.cacheCreationInputTokens,
                "webSearchRequests": self.usage.webSearchRequests,
                "costUSD": self.usage.costUSD,
            },
            "tool_count": self.tool_count,
            "recent_activities": list(self.recent_activities),
            "summary": self.summary,
            "result": self.result,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "turns": self.turns,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentRun":
        u = d.get("usage") or {}
        ar = cls(
            id=d["id"],
            label=d.get("label", ""),
            phase=d.get("phase", ""),
            model=d.get("model", ""),
            agent_type=d.get("agent_type", "general-purpose"),
            status=d.get("status", "queued"),
            started=d.get("started"),
            ended=d.get("ended"),
            total_paused_ms=d.get("total_paused_ms", 0.0),
            usage=ModelUsage(
                inputTokens=u.get("inputTokens", 0),
                outputTokens=u.get("outputTokens", 0),
                cacheReadInputTokens=u.get("cacheReadInputTokens", 0),
                cacheCreationInputTokens=u.get("cacheCreationInputTokens", 0),
                webSearchRequests=u.get("webSearchRequests", 0),
                costUSD=u.get("costUSD", 0.0),
            ),
            tool_count=d.get("tool_count", 0),
            recent_activities=list(d.get("recent_activities", [])),
            summary=d.get("summary", ""),
            result=d.get("result", ""),
            error=d.get("error"),
            cost_usd=d.get("cost_usd", 0.0),
            turns=d.get("turns", 0),
        )
        return ar


@dataclass
class Phase:
    """A named group of agent runs. Status rolls up from its members."""

    title: str
    detail: str = ""
    agents: list[AgentRun] = field(default_factory=list)
    status: str = "queued"  # queued | running | done | error

    def roll_up(self) -> None:
        """Recompute ``status`` from member agents."""

        if not self.agents:
            return
        stats = {a.status for a in self.agents}
        if "running" in stats:
            self.status = "running"
        elif "error" in stats:
            self.status = "error"
        elif stats <= {"done", "cancelled"}:
            self.status = "done"
        else:
            self.status = "running" if stats != {"queued"} else "queued"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
            "agents": [a.to_dict() for a in self.agents],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Phase":
        return cls(
            title=d.get("title", ""),
            detail=d.get("detail", ""),
            status=d.get("status", "queued"),
            agents=[AgentRun.from_dict(a) for a in d.get("agents", [])],
        )


@dataclass
class WorkflowRun:
    """Serializable snapshot of an entire workflow."""

    id: str
    name: str
    phases: list[Phase] = field(default_factory=list)
    status: str = "queued"  # queued | running | done | error | cancelled
    started: float | None = None
    ended: float | None = None
    total_paused_ms: float = 0.0
    log_lines: list[str] = field(default_factory=list)

    def all_agents(self) -> list[AgentRun]:
        return [a for ph in self.phases for a in ph.agents]

    def find_agent(self, agent_id: str) -> AgentRun | None:
        for a in self.all_agents():
            if a.id == agent_id:
                return a
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "total_paused_ms": self.total_paused_ms,
            "log_lines": list(self.log_lines),
            "phases": [p.to_dict() for p in self.phases],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkflowRun":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            status=d.get("status", "queued"),
            started=d.get("started"),
            ended=d.get("ended"),
            total_paused_ms=d.get("total_paused_ms", 0.0),
            log_lines=list(d.get("log_lines", [])),
            phases=[Phase.from_dict(p) for p in d.get("phases", [])],
        )


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------
#
# An ``AgentRunner`` is an async callable returning an async iterator of
# ``Message`` objects — exactly the shape of ``Agent.run_iter``. The engine
# drives it, folding each AssistantMessage's usage + tool_use blocks into the
# ``AgentRun``. Tests inject a stub; production uses :func:`make_agent_runner`.

AgentRunner = Callable[..., AsyncIterator[Any]]


def make_agent_runner(
    *,
    model: str,
    tools: Any = None,
    provider: Any = None,
    backend: str | None = None,
    permissions: Any = None,
    budget: Budget | None = None,
    agent_types: Any = None,
) -> AgentRunner:
    """Build the default runner: constructs a real child :class:`Agent` per call
    and returns its ``run_iter`` stream.

    Uses the same :class:`AgentType` personas the ``task`` tool exposes to pick a
    system prompt + tool policy. Kept thin and lazy-importing so importing the
    engine never drags in the whole agent stack (and tests can skip it).
    """

    from .agent import Agent  # noqa: PLC0415
    from .subagent import (  # noqa: PLC0415
        discover_agent_types,
        resolve_agent_tools,
    )
    from .tools import ToolRegistry  # noqa: PLC0415

    types = list(agent_types) if agent_types is not None else discover_agent_types()
    by_name = {t.name: t for t in types}

    def runner(
        prompt: str,
        *,
        model: str = model,
        agent_type: str = "general-purpose",
        schema: dict | None = None,
    ) -> AsyncIterator[Any]:
        at = by_name.get(agent_type) or by_name.get("general-purpose")
        registry = ToolRegistry()
        if at is not None and tools:
            kit = resolve_agent_tools(at, list(tools))
            if kit:
                registry.add(*kit)
        child = Agent(
            model=(at.model if at and at.model else model),
            provider=provider,
            backend=backend,
            system=(at.system_prompt if at else None),
            tools=registry,
            max_steps=(at.max_steps if at else 20),
            permissions=permissions,
            budget=budget,
            response_format=schema,
            include_recall=False,
            include_env=False,
            include_memory=False,
        )
        return child.run_iter([UserMessage(content=prompt)])

    return runner


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _default_cap() -> int:
    """``min(16, cpu-2)`` with a floor of 1."""

    cpu = os.cpu_count() or 2
    return max(1, min(16, cpu - 2))


def _extract_text(msg: Any) -> str:
    """Concatenated text of an AssistantMessage's TextBlocks."""

    parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


class Workflow:
    """Live workflow context object.

    Construct with a ``name`` and (optionally) an ``agent_runner``. Call
    :meth:`agent` to run one child, :meth:`parallel` / :meth:`pipeline` to fan
    out, :meth:`phase` to group. Control the run with :meth:`stop`,
    :meth:`pause` / :meth:`resume`, :meth:`cancel`, :meth:`skip_agent`,
    :meth:`retry_agent`, and persist with :meth:`save`.
    """

    def __init__(
        self,
        name: str,
        *,
        agent_runner: AgentRunner | None = None,
        on_event: Callable[[WorkflowRun], None] | None = None,
        clock: Callable[[], float] | None = None,
        budget: Budget | None = None,
        concurrency: int | None = None,
        model: str = "",
    ) -> None:
        self.name = name
        self.model = model
        self._runner = agent_runner
        self.on_event = on_event
        self._clock = clock or default_clock
        self.budget = budget
        self.cap = concurrency if concurrency is not None else _default_cap()
        self._limiter = anyio.CapacityLimiter(self.cap)
        # Workflow-level budget rollup (folds up every child's usage).
        self.budget_tracker = BudgetTracker(budget=budget or Budget())

        self.run = WorkflowRun(id="w" + _b36(int(self._clock()) & 0xFFFFFF), name=name)
        self.run.started = self._clock()
        self.run.status = "running"

        self._agent_ids = count()
        self._current_phase: str | None = None
        self._agents_by_id: dict[str, AgentRun] = {}
        self._scopes: dict[str, anyio.CancelScope] = {}
        self._stopped = False
        self._skip: set[str] = set()

        # Pause gate: set == running, cleared == paused.
        self._gate = anyio.Event()
        self._gate.set()
        self._paused = False
        self._paused_at: float | None = None

        self._emit()

    # ------------------------------------------------------------------
    # Event fan-out
    # ------------------------------------------------------------------

    def _emit(self) -> None:
        for ph in self.run.phases:
            ph.roll_up()
        if self.on_event is not None:
            try:
                self.on_event(self.run)
            except Exception:  # noqa: BLE001 — a bad observer never breaks the run
                pass

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    def phase(self, title: str, detail: str = ""):
        """Context manager that scopes subsequent :meth:`agent` calls to a phase.

        ::

            with wf.phase("Research"):
                await wf.agent("...")
        """

        ph = self._get_phase(title, detail)
        wf = self

        class _PhaseCtx:
            def __enter__(self_inner) -> Phase:
                self_inner._prev = wf._current_phase
                wf._current_phase = title
                if ph.status == "queued":
                    ph.status = "running"
                wf._emit()
                return ph

            def __exit__(self_inner, *exc: Any) -> None:
                wf._current_phase = self_inner._prev
                ph.roll_up()
                wf._emit()

        return _PhaseCtx()

    def _get_phase(self, title: str, detail: str = "") -> Phase:
        for ph in self.run.phases:
            if ph.title == title:
                if detail and not ph.detail:
                    ph.detail = detail
                return ph
        ph = Phase(title=title, detail=detail)
        self.run.phases.append(ph)
        return ph

    def log(self, msg: str) -> None:
        """Append a line to the workflow log and notify observers."""

        self.run.log_lines.append(str(msg))
        self._emit()

    # ------------------------------------------------------------------
    # Running agents
    # ------------------------------------------------------------------

    async def agent(
        self,
        prompt: str,
        *,
        label: str | None = None,
        phase: str | None = None,
        model: str | None = None,
        schema: dict | None = None,
        agent_type: str = "general-purpose",
    ) -> str:
        """Run one child agent to completion and return its final text.

        Registers an :class:`AgentRun` under the given (or current) phase, drives
        the injected runner, folds usage/tools into the run, and honors the pause
        gate + concurrency cap. On runner error the run is marked ``error`` and
        the exception re-raised (so ``parallel`` fails fast).
        """

        phase_title = phase or self._current_phase or "main"
        ph = self._get_phase(phase_title)
        ar = AgentRun(
            id="a" + _b36(next(self._agent_ids)),
            label=label or f"agent-{len(self._agents_by_id) + 1}",
            phase=phase_title,
            model=model or self.model,
            agent_type=agent_type,
        )
        ar._prompt = prompt
        ar._schema = schema
        ph.agents.append(ar)
        self._agents_by_id[ar.id] = ar
        self._emit()
        return await self._drive(ar)

    async def _drive(self, ar: AgentRun) -> str:
        # Skip requested before it ever started.
        if ar.id in self._skip or self._stopped:
            ar.status = "cancelled"
            ar.ended = self._clock()
            self._emit()
            return ""

        # Pause gate — agent boundary (REF: gate checked at phase/agent bounds).
        await self._gate.wait()

        runner = self._runner
        if runner is None:
            raise WorkflowError(
                "not_running",
                "no agent_runner configured — pass agent_runner=... to Workflow()",
            )

        ar.status = "running"
        ar.started = self._clock()
        self._emit()

        tracker = BudgetTracker(budget=self.budget or Budget())
        scope = anyio.CancelScope()
        self._scopes[ar.id] = scope
        try:
            with scope:
                async for msg in _aiter(runner, ar):
                    self._ingest(ar, msg, tracker)
                    self._emit()
        except Exception as exc:  # noqa: BLE001 — record then re-raise
            if scope.cancel_called:
                ar.status = "cancelled"
            else:
                ar.status = "error"
                ar.error = f"{type(exc).__name__}: {exc}"
            ar.ended = self._clock()
            self._finalize(ar, tracker)
            self._emit()
            if ar.status == "error":
                raise
            return ""
        finally:
            self._scopes.pop(ar.id, None)

        if scope.cancel_called:
            ar.status = "cancelled"
        else:
            ar.status = "done"
        ar.ended = self._clock()
        ar.result = ar.summary
        self._finalize(ar, tracker)
        self._emit()
        return ar.result

    def _ingest(self, ar: AgentRun, msg: Any, tracker: BudgetTracker) -> None:
        """Fold one streamed Message into the AgentRun + trackers."""

        if isinstance(msg, AssistantMessage):
            usage = getattr(msg, "usage", None)
            if usage is not None:
                ar.usage = accumulate_usage(ar.usage, usage)
                tracker.add_usage(usage, ar.model or self.model or "")
                tracker.add_turn()
                self.budget_tracker.add_usage(usage, ar.model or self.model or "")
                self.budget_tracker.add_turn()
                ar.turns += 1
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, ToolUseBlock):
                    ar.tool_count += 1
                    ar.push_activity(block.name)
            text = _extract_text(msg)
            if text:
                ar.summary = text if len(text) <= 200 else text[:197] + "…"

    def _finalize(self, ar: AgentRun, tracker: BudgetTracker) -> None:
        ar.cost_usd = tracker.total_usd

    # ------------------------------------------------------------------
    # Fan-out primitives
    # ------------------------------------------------------------------

    async def parallel(
        self, thunks: list[Callable[[], Awaitable[Any]]]
    ) -> list[Any]:
        """Run ``thunks`` concurrently and return results **in input order**.

        Barrier semantics: returns only once every thunk has finished (task-group
        exit). Concurrency is still bounded by the workflow cap because each
        thunk's :meth:`agent` acquires the limiter.
        """

        results: list[Any] = [None] * len(thunks)

        async def _one(i: int, thunk: Callable[[], Awaitable[Any]]) -> None:
            async with self._limiter:
                results[i] = await thunk()

        async with anyio.create_task_group() as tg:
            for i, thunk in enumerate(thunks):
                tg.start_soon(_one, i, thunk)
        return results

    async def pipeline(
        self,
        items: list[Any],
        *stages: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        """Run each item through the chain ``stage0 → stage1 → …`` independently.

        No barrier between items: item B's stage-0 does not wait on item A's
        stage-0. Each stage receives the previous stage's output (the raw item
        for stage 0) and returns the next value. Returns the final value per
        item, in input order. Concurrency stays capped per stage via the limiter.
        """

        results: list[Any] = [None] * len(items)

        async def _chain(i: int, item: Any) -> None:
            value = item
            for stage in stages:
                async with self._limiter:
                    value = await stage(value)
            results[i] = value

        async with anyio.create_task_group() as tg:
            for i, item in enumerate(items):
                tg.start_soon(_chain, i, item)
        return results

    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Abort the whole run: cancel every in-flight agent scope.

        Raises ``WorkflowError(not_running)`` if the run already ended.
        """

        if self.run.status not in ("running", "queued"):
            raise WorkflowError("not_running", f"workflow {self.run.id} is not running")
        self._stopped = True
        self.run.status = "cancelled"
        self.run.ended = self._clock()
        for scope in list(self._scopes.values()):
            scope.cancel()
        # Release the gate so any paused waiter unblocks and observes _stopped.
        if not self._gate.is_set():
            self._gate.set()
        self._emit()

    def cancel(self, agent_id: str) -> None:
        """Cancel a single agent's current turn (per-agent abort).

        Raises ``not_found`` if unknown, ``not_running`` if it isn't running.
        """

        ar = self.run.find_agent(agent_id)
        if ar is None:
            raise WorkflowError("not_found", f"no agent {agent_id!r}")
        if ar.status != "running":
            raise WorkflowError("not_running", f"agent {agent_id} is {ar.status}")
        scope = self._scopes.get(agent_id)
        if scope is not None:
            scope.cancel()

    def pause(self) -> None:
        """Pause the run: clears the gate so new agents wait at their boundary.

        In-flight turns keep running (REF: gate checked at boundaries). Paused
        time accrues to ``total_paused_ms`` on resume.
        """

        if self._paused:
            return
        self._paused = True
        self._paused_at = self._clock()
        self._gate = anyio.Event()  # a fresh, un-set gate blocks waiters
        for ar in self.run.all_agents():
            if ar.status == "running":
                ar._paused = True
        self._emit()

    def resume(self) -> None:
        """Resume a paused run and fold the paused span into ``total_paused_ms``
        on the workflow and every agent that was running when paused."""

        if not self._paused:
            return
        dur = self._clock() - (self._paused_at or self._clock())
        self._paused = False
        self._paused_at = None
        self.run.total_paused_ms += dur
        for ar in self.run.all_agents():
            if ar._paused:
                ar.total_paused_ms += dur
                ar._paused = False
        self._gate.set()
        self._emit()

    def skip_agent(self, agent_id: str) -> None:
        """Skip an agent: cancel it if running, else mark it so it never starts.

        Raises ``not_found`` for an unknown id.
        """

        ar = self.run.find_agent(agent_id)
        if ar is None:
            raise WorkflowError("not_found", f"no agent {agent_id!r}")
        self._skip.add(agent_id)
        if ar.status == "running":
            scope = self._scopes.get(agent_id)
            if scope is not None:
                scope.cancel()
        elif ar.status == "queued":
            ar.status = "cancelled"
            ar.ended = self._clock()
            self._emit()

    async def retry_agent(self, agent_id: str) -> str:
        """Re-run a finished (``done`` / ``error`` / ``cancelled``) agent from a
        clean slate, reusing its stored prompt/model/type. Returns the new
        result. Raises ``not_found`` / ``not_running`` (if still running)."""

        ar = self.run.find_agent(agent_id)
        if ar is None:
            raise WorkflowError("not_found", f"no agent {agent_id!r}")
        if ar.status == "running":
            raise WorkflowError("not_running", f"agent {agent_id} is still running")
        # Reset live counters; keep id/label/phase/model for continuity.
        ar.status = "queued"
        ar.started = None
        ar.ended = None
        ar.total_paused_ms = 0.0
        ar.usage = ModelUsage()
        ar.tool_count = 0
        ar.recent_activities.clear()
        ar.summary = ""
        ar.result = ""
        ar.error = None
        ar.cost_usd = 0.0
        ar.turns = 0
        ar._paused = False
        self._skip.discard(agent_id)
        self._emit()
        return await self._drive(ar)

    # ------------------------------------------------------------------
    # High-level shape: Research → Synthesis → Verification
    # ------------------------------------------------------------------

    async def coordinate(
        self,
        objective: str,
        workers: list[dict[str, Any]],
        *,
        verify: bool = True,
        verify_agent_type: str = "verify",
        research_phase: str = "Research",
        verify_phase: str = "Verification",
    ) -> dict[str, Any]:
        """Run the canonical coordinator DAG and return a structured synthesis.

        Shape (mirrors Claude Code's coordinator): a **Research** phase fans out
        every ``worker`` in parallel (:meth:`parallel`, barrier), their reports
        are synthesized into ``findings``, then — unless the budget is exhausted
        or the run was stopped — a **Verification** phase runs one
        ``verify_agent_type`` agent adversarially over the findings and its
        ``VERDICT`` is parsed out.

        ``workers`` is a list of ``{"label", "prompt", "agent_type"}`` dicts.
        Concurrency cap, pause gate, budget rollup, and :meth:`stop` all apply
        (each worker acquires the limiter; :meth:`stop` cancels them and skips
        verification). Returns a plain-dict synthesis the caller can format."""

        reports: list[Any] = []
        if workers:
            with self.phase(research_phase, detail=(objective or "")[:120]):
                thunks = [
                    (
                        lambda w=w: self.agent(
                            w.get("prompt") or objective,
                            label=w.get("label"),
                            phase=research_phase,
                            agent_type=w.get("agent_type") or "explore",
                        )
                    )
                    for w in workers
                ]
                reports = await self.parallel(thunks)

        findings = [
            {
                "label": w.get("label") or f"worker-{i + 1}",
                "agent_type": w.get("agent_type") or "explore",
                "report": reports[i] if i < len(reports) else "",
            }
            for i, w in enumerate(workers)
        ]

        verification: str | None = None
        verdict: str | None = None
        over_budget = self._budget_exhausted()
        if verify and findings and not over_budget and not self._stopped:
            with self.phase(verify_phase):
                verification = await self.agent(
                    _default_verify_prompt(objective, findings),
                    label="verify",
                    phase=verify_phase,
                    agent_type=verify_agent_type,
                )
            verdict = _parse_verdict(verification)

        self.finish()
        return {
            "objective": objective,
            "workflow_id": self.run.id,
            "status": self.run.status,
            "over_budget": over_budget,
            "findings": findings,
            "verification": verification,
            "verdict": verdict,
            "agents": [
                {
                    "id": a.id,
                    "label": a.label,
                    "agent_type": a.agent_type,
                    "phase": a.phase,
                    "status": a.status,
                    "tokens": total_tokens(a.usage),
                    "turns": a.turns,
                }
                for a in self.run.all_agents()
            ],
            "cost_usd": self.budget_tracker.total_usd,
        }

    def _budget_exhausted(self) -> bool:
        """True when the workflow-level budget rollup has breached a cap.

        Probed at a phase boundary (not mid-parallel) so an over-budget run
        skips the costly verification pass instead of raising, mirroring the
        loop's after-turn ``check()`` discipline."""

        from .errors import BudgetExceededError  # noqa: PLC0415

        try:
            self.budget_tracker.check()
        except BudgetExceededError:
            return True
        return False

    # ------------------------------------------------------------------
    # Completion + persistence
    # ------------------------------------------------------------------

    def finish(self) -> None:
        """Mark the run complete (``done`` unless an agent errored/cancelled)."""

        if self.run.status == "cancelled":
            pass
        elif any(a.status == "error" for a in self.run.all_agents()):
            self.run.status = "error"
        else:
            self.run.status = "done"
        if self.run.ended is None:
            self.run.ended = self._clock()
        self._emit()

    def save(self, path: Any = None) -> str:
        """Persist the run to ``~/.mantis-agent/workflows/<id>.json`` (or ``path``).

        Returns the written path. Round-trips via :meth:`WorkflowRun.from_dict`.
        """

        from pathlib import Path  # noqa: PLC0415

        if path is None:
            from .paths import get_mantis_agent_dir  # noqa: PLC0415

            base = get_mantis_agent_dir() / "workflows"
            base.mkdir(parents=True, exist_ok=True)
            path = base / f"{self.run.id}.json"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.run.to_dict(), indent=2))
        return str(path)

    @staticmethod
    def load(path: Any) -> WorkflowRun:
        """Load a persisted :class:`WorkflowRun` from disk."""

        from pathlib import Path  # noqa: PLC0415

        data = json.loads(Path(path).read_text())
        return WorkflowRun.from_dict(data)


_VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL|PARTIAL)", re.IGNORECASE)


def _default_verify_prompt(objective: str, findings: list[dict[str, Any]]) -> str:
    """Build the verifier's prompt: the objective plus every research agent's
    report, ending with the exact VERDICT contract the ``verify`` persona emits."""

    parts = [
        "Adversarially verify the work below against the stated objective. Hunt "
        "for gaps, unsupported claims, and missing edge cases — do NOT just "
        "confirm it looks right.",
        "",
        f"OBJECTIVE: {objective}",
        "",
        "SYNTHESIZED FINDINGS FROM THE RESEARCH AGENTS:",
    ]
    for f in findings:
        label = f.get("label") or "worker"
        at = f.get("agent_type") or ""
        report = f.get("report") or "(no report)"
        parts.append(f"\n--- {label} ({at}) ---\n{report}")
    parts.append(
        "\nEnd your report with a single final line that is literally "
        "'VERDICT: PASS', 'VERDICT: FAIL', or 'VERDICT: PARTIAL'."
    )
    return "\n".join(parts)


def _parse_verdict(text: str | None) -> str | None:
    """Extract a PASS/FAIL/PARTIAL verdict from a verifier's report.

    Prefers the explicit ``VERDICT: X`` contract line (last one wins), falling
    back to the first bare token found. Returns ``None`` when absent."""

    if not text:
        return None
    hits = _VERDICT_RE.findall(text)
    if hits:
        return hits[-1].upper()
    up = text.upper()
    for v in ("FAIL", "PARTIAL", "PASS"):
        if v in up:
            return v
    return None


async def _aiter(runner: AgentRunner, ar: AgentRun) -> AsyncIterator[Any]:
    """Invoke ``runner`` and yield from whatever it returns.

    The runner may be an async generator function (return the iterator) or an
    async function returning an async iterator/awaitable. This normalizes both.
    """

    out = runner(ar._prompt, model=ar.model, agent_type=ar.agent_type, schema=ar._schema)
    if hasattr(out, "__aiter__"):
        async for msg in out:
            yield msg
        return
    if hasattr(out, "__await__"):
        res = await out
        if hasattr(res, "__aiter__"):
            async for msg in res:
                yield msg
        return
