"""Background jobs — long-running agent work that outlives the turn.

A *job* is any coroutine (usually a subagent run) detached from the
conversation: the tool call that started it returns immediately with a job id,
the work keeps going, and completion flows back through ``on_event`` — the
TUI announces it and injects the result as context so the model learns the
outcome on its next turn. This is Claude Code's background-task pattern:
"stuff that would normally time out" runs here instead.

Jobs come in two shapes, distinguished by ``kind``. Most are *one-shot*: they
run, finish, and fire ``on_event`` exactly once. A ``watch`` job (see
``mantis_agent.watch``) is *streaming*: it stays alive and pushes many events
through ``on_stream`` before its single terminal ``on_event``. The two callbacks
are deliberately separate — Claude Code makes the same split, and for the same
reason: only the terminal notification carries a status, so a mid-stream event
can never be mistaken for the job closing.

Pure asyncio, no threads: jobs share the TUI's event loop (and the parent's
HTTP pool via the subagent machinery). One manager per session; the TUI owns
it and cancels leftovers on exit.

A manager may additionally be given an ``ActivityRegistry``, in which case each
job also appears as a node in the unified activity tree. That is pure
instrumentation: every emission goes through ``activity.emit``, which is
guarded (``registry is None`` costs a null check and nothing else) and swallows
registry errors, on the same principle ``_fire_on_event`` already applies — a
broken observer must never turn a completed job into a failed one.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .activity import emit as activity_emit
from .activity import status as activity_status
from .activity.ids import make_id

__all__ = ["Job", "JobManager"]

_MAX_RUNTIME_S = 60 * 60  # absolute backstop — a job may not run forever
_MAX_RETAINED_JOBS = 100  # cap terminal jobs kept for later job_output/inspection


@dataclass
class Job:
    id: int
    desc: str
    kind: str = "task"
    started: float = field(default_factory=time.monotonic)
    status: str = "running"          # running | done | error | cancelled | timeout
    result: str = ""                 # final text (or error string)
    task: Any = None                 # the asyncio.Task
    tool_count: int = 0
    turn_count: int = 0
    stream_count: int = 0            # notifications pushed mid-run (watch jobs)
    last_event: str = "starting"
    last_tool: str = ""
    events: Any = field(default_factory=lambda: deque(maxlen=40))
    # Set when this job IS a workflow run (kind == "workflow"): the id the
    # /workflows viewer knows it by. One piece of work, two windows onto it —
    # /jobs for lifecycle, /workflows for structure.
    workflow_id: str = ""
    # The command/script this job is actually running, when it has one. A watch
    # is usually a poll loop the MODEL composed on the fly, so "what is this
    # thing running on my machine" is the first question the monitor drill-down
    # has to answer — and ``desc`` is a human label, not the answer. Defaulted so
    # every existing ``Job(...)`` construction keeps working untouched.
    script: str = ""

    def __post_init__(self) -> None:
        self.record_event(self.last_event, ts=self.started)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def record_event(self, text: str, *, ts: float | None = None,
                     update_last: bool = True) -> None:
        if update_last:
            self.last_event = text
        self.events.append((time.monotonic() if ts is None else ts, text))

    def summary(self) -> str:
        el = int(self.elapsed_s)
        wf = f" · run {self.workflow_id}" if self.workflow_id else ""
        return f"#{self.id} · {self.kind} · {self.status} · {el}s{wf} · {self.desc[:50]}"


class JobManager:
    """Owns the session's background jobs.

    ``on_event(job)`` fires exactly once per job, on any terminal state —
    the UI hook for announcements + context injection. ``on_stream(job, text)``
    fires zero-or-more times *before* that, for jobs that push events as they
    run (watches). Callback errors are swallowed; a broken notifier must never
    kill the job result.

    ``registry`` is an optional
    :class:`~mantis_agent.activity.registry.ActivityRegistry`. When given, each
    job is mirrored as a node in the activity tree at four points — spawn, each
    streamed event, the terminal transition, and a cancellation request. When
    omitted (the default, and every existing construction) the manager behaves
    byte-for-byte as before and makes no emission calls at all."""

    def __init__(self, on_event: Any = None, on_stream: Any = None,
                 registry: Any = None) -> None:
        self.on_event = on_event
        self.on_stream = on_stream
        self.jobs: dict[int, Job] = {}
        self._counter = itertools.count(1)
        #: Optional activity registry. Assignable after construction because the
        #: TUI builds its manager before the session (and therefore the session's
        #: registry) exists.
        self.registry = registry
        #: The node new jobs are parented to — the current turn, when a host is
        #: tracking one. An attribute rather than a ``spawn()`` argument: the
        #: parent is a property of *when* a job is spawned, not of the call, and
        #: every existing ``spawn()`` signature stays untouched.
        self.activity_parent_id: str | None = None

    # ------------------------------------------------------------------
    # Activity emission
    # ------------------------------------------------------------------
    # Four call sites, each a single guarded line in the engine below. Every
    # helper starts by reading ``self.registry`` and returning when it is None,
    # so a manager without a registry pays one attribute load per event and
    # nothing else — no id building, no event construction, no try block.
    #
    # None of these may raise. ``activity.emit`` already swallows registry
    # errors, and the id derivation here is wrapped for the same reason: this
    # code runs inside a job's terminal path, including its cancellation path,
    # where an exception would rewrite the job's own outcome.

    def _node_id(self, job: Job) -> str:
        try:
            return make_id("job", job.id)
        except Exception:  # noqa: BLE001 — an unusable id is a lost node, not an error
            return ""

    def _emit_spawned(self, job: Job) -> None:
        reg = self.registry
        if reg is None:
            return
        node_id = self._node_id(job)
        if not node_id:
            return
        activity_emit.node_created(
            reg,
            node_id,
            self.activity_parent_id or None,
            # The job's OWN kind (``task`` / ``watch`` / ``workflow``), not the
            # literal "job": the id namespace already says which engine owns
            # this node, and the kind is what a viewer groups and labels by.
            job.kind,
            job.desc,
            source="model",
            actions=("stop", "open") if job.kind == "watch" else ("stop",),
        )
        activity_emit.node_status(reg, node_id, activity_status.RUNNING)

    def _emit_activity(self, job: Job, text: str) -> None:
        reg = self.registry
        if reg is None:
            return
        node_id = self._node_id(job)
        if node_id:
            activity_emit.node_activity(reg, node_id, text)

    def _emit_terminal(self, job: Job) -> None:
        """Mirror a job's terminal transition. Called once per job, from
        whichever of the two terminal paths got there first."""

        reg = self.registry
        if reg is None:
            return
        node_id = self._node_id(job)
        if not node_id:
            return
        # ``error`` carries the failure text for the outcomes that have one; a
        # cancelled or completed job has a result, not an error.
        error = job.result if job.status in ("error", "timeout") else None
        activity_emit.node_status(
            reg, node_id, activity_status.from_job_status(job.status), error
        )

    def _emit_action(self, job: Job, action: str, actor: str = "user") -> None:
        reg = self.registry
        if reg is None:
            return
        node_id = self._node_id(job)
        if node_id:
            # The resulting status is not emitted here: it arrives from the
            # terminal path once the task actually ends, which is the only place
            # that knows what the outcome turned out to be.
            activity_emit.node_action(reg, node_id, action, actor)

    async def _fire_on_event(self, job: Job) -> None:
        """Deliver a terminal job to ``on_event``, awaiting async callbacks.

        ``on_event`` may be a plain function or a coroutine function; either is
        supported. Errors are swallowed — a broken notifier must never turn a
        completed job into a failure."""
        cb = self.on_event
        if cb is None:
            return
        try:
            res = cb(job)
            if inspect.isawaitable(res):
                await res
        except Exception:  # noqa: BLE001
            pass

    async def emit(self, job: Job, text: str) -> None:
        """Push a mid-run event from ``job`` to the UI.

        Used by streaming jobs (watches) for each batch of output. The event is
        recorded on the job — so ``/job <id>`` shows recent activity even for a
        session that has scrolled past the notification — and handed to
        ``on_stream``. Deliberately carries no status: this is a progress ping,
        not a terminal state, and must never be read as the job finishing."""
        job.stream_count += 1
        job.record_event(text)
        # Before the ``on_stream is None`` early return below: a manager with a
        # registry but no stream callback must still see the activity.
        self._emit_activity(job, text)
        cb = self.on_stream
        if cb is None:
            return
        try:
            res = cb(job, text)
            if inspect.isawaitable(res):
                await res
        except Exception:  # noqa: BLE001 — a broken notifier must not end the stream
            pass

    def spawn(self, coro: Any, *, desc: str, kind: str = "task",
              max_runtime_s: float | None = _MAX_RUNTIME_S,
              workflow_id: str = "") -> Job:
        """Detach ``coro`` as a job. Returns the Job (id assigned) immediately.

        ``max_runtime_s=None`` disables the backstop — only for jobs whose whole
        point is to outlive it (a ``persistent`` watch), which instead end with
        the session via :meth:`cancel_all`. ``workflow_id`` links the job to a
        workflow run so /jobs and /workflows name the same thing."""
        job = Job(id=next(self._counter), desc=desc, kind=kind,
                  workflow_id=workflow_id)
        self.jobs[job.id] = job
        self._prune()
        self._emit_spawned(job)

        async def _run() -> None:
            try:
                out = await asyncio.wait_for(coro, timeout=max_runtime_s)
                job.status, job.result = "done", str(out or "")
                job.record_event("done", update_last=False)
            except (TimeoutError, asyncio.TimeoutError):
                job.status = "timeout"
                mins = (max_runtime_s or _MAX_RUNTIME_S) / 60
                job.result = f"(job exceeded {mins:.0f} min and was stopped)"
                job.record_event("timeout", update_last=False)
            except asyncio.CancelledError:
                job.status, job.result = "cancelled", "(cancelled)"
                job.record_event("cancelled", update_last=False)
                # swallow: cancellation of a background job is an outcome, not
                # an exception to propagate into the event loop's void
            except Exception as e:  # noqa: BLE001
                job.status = "error"
                job.result = f"{type(e).__name__}: {e}"
                job.record_event(f"error: {job.result}", update_last=False)
            # Before the callback: a notifier that reads the tree must not see
            # a node still marked running for a job it was just handed.
            self._emit_terminal(job)
            await self._fire_on_event(job)

        job.task = asyncio.ensure_future(_run())

        def _finalize(_t: Any) -> None:
            # A task cancelled BEFORE its first step never runs the runner's
            # except block — finish the bookkeeping here (exactly-once: the
            # runner leaves status terminal, so this only fires for that case).
            if job.status == "running":
                job.status, job.result = "cancelled", "(cancelled)"
                job.record_event("cancelled", update_last=False)
                close = getattr(coro, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                # The runner never ran, so this is the terminal transition —
                # exactly-once still holds, because reaching here means the
                # runner's own emission did not happen.
                self._emit_terminal(job)
                if self.on_event is not None:
                    # Sync context (a done-callback) — can't await here, so run
                    # the callback via the shared async helper on the loop. This
                    # awaits coroutine callbacks instead of dropping them.
                    try:
                        asyncio.ensure_future(self._fire_on_event(job))
                    except Exception:  # noqa: BLE001
                        pass

        job.task.add_done_callback(_finalize)
        return job

    def _prune(self) -> None:
        # Evict the oldest TERMINAL jobs once we exceed the retention cap so the
        # dict (and each job's retained result string + events deque) can't grow
        # unbounded over a long session that fires many background jobs. Running
        # jobs are never evicted; the most recent terminal results stay fetchable
        # via job_output. ``self.jobs`` preserves id-ascending insertion order,
        # so slicing from the front drops the oldest first.
        terminal = [j for j in self.jobs.values() if j.status != "running"]
        excess = len(terminal) - _MAX_RETAINED_JOBS
        if excess <= 0:
            return
        for j in terminal[:excess]:
            self.jobs.pop(j.id, None)

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    def all(self) -> list[Job]:
        return list(self.jobs.values())

    async def wait(self, job_id: int, timeout_s: float | None = None) -> Job | None:
        """Block until the job reaches a terminal state (or ``timeout_s``)."""
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.task is not None and not job.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=timeout_s)
            except (TimeoutError, asyncio.TimeoutError):
                pass  # still running — caller sees status == "running"
            except asyncio.CancelledError:
                # The JOB ending cancelled is a terminal outcome for it, not an
                # exception for us: in that case its task ends cancelled, so
                # return the job. Otherwise the CancelledError is a genuine
                # cancellation of WE, the waiter, and MUST propagate for
                # structured cancellation. Gate only on the task's own cancelled
                # state — never on job.status, which races with the job reaching
                # a terminal state at the same instant we're cancelled.
                if job.task is not None and job.task.cancelled():
                    return job
                raise
        return job

    def cancel(self, job_id: int) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.task is None or job.task.done():
            return False
        self._emit_action(job, "stop")
        job.task.cancel()
        return True

    def cancel_all(self) -> int:
        n = 0
        for j in list(self.jobs.values()):
            if self.cancel(j.id):
                n += 1
        return n
