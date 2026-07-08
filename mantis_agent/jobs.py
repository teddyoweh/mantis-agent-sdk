"""Background jobs — long-running agent work that outlives the turn.

A *job* is any coroutine (usually a subagent run) detached from the
conversation: the tool call that started it returns immediately with a job id,
the work keeps going, and completion flows back through ``on_event`` — the
TUI announces it and injects the result as context so the model learns the
outcome on its next turn. This is Claude Code's background-task pattern:
"stuff that would normally time out" runs here instead.

Pure asyncio, no threads: jobs share the TUI's event loop (and the parent's
HTTP pool via the subagent machinery). One manager per session; the TUI owns
it and cancels leftovers on exit.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Job", "JobManager"]

_MAX_RUNTIME_S = 60 * 60  # absolute backstop — a job may not run forever


@dataclass(slots=True)
class Job:
    id: int
    desc: str
    kind: str = "task"
    started: float = field(default_factory=time.monotonic)
    status: str = "running"          # running | done | error | cancelled | timeout
    result: str = ""                 # final text (or error string)
    task: Any = None                 # the asyncio.Task

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def summary(self) -> str:
        el = int(self.elapsed_s)
        return f"#{self.id} · {self.kind} · {self.status} · {el}s · {self.desc[:50]}"


class JobManager:
    """Owns the session's background jobs.

    ``on_event(job)`` fires exactly once per job, on any terminal state —
    the UI hook for announcements + context injection. Callback errors are
    swallowed; a broken notifier must never kill the job result."""

    def __init__(self, on_event: Any = None) -> None:
        self.on_event = on_event
        self.jobs: dict[int, Job] = {}
        self._counter = itertools.count(1)

    def spawn(self, coro: Any, *, desc: str, kind: str = "task",
              max_runtime_s: float = _MAX_RUNTIME_S) -> Job:
        """Detach ``coro`` as a job. Returns the Job (id assigned) immediately."""
        job = Job(id=next(self._counter), desc=desc, kind=kind)
        self.jobs[job.id] = job

        async def _run() -> None:
            try:
                async with asyncio.timeout(max_runtime_s):
                    out = await coro
                job.status, job.result = "done", str(out or "")
            except TimeoutError:
                job.status = "timeout"
                job.result = f"(job exceeded {max_runtime_s / 60:.0f} min and was stopped)"
            except asyncio.CancelledError:
                job.status, job.result = "cancelled", "(cancelled)"
                # swallow: cancellation of a background job is an outcome, not
                # an exception to propagate into the event loop's void
            except Exception as e:  # noqa: BLE001
                job.status = "error"
                job.result = f"{type(e).__name__}: {e}"
            if self.on_event is not None:
                try:
                    self.on_event(job)
                except Exception:  # noqa: BLE001
                    pass

        job.task = asyncio.ensure_future(_run())

        def _finalize(_t: Any) -> None:
            # A task cancelled BEFORE its first step never runs the runner's
            # except block — finish the bookkeeping here (exactly-once: the
            # runner leaves status terminal, so this only fires for that case).
            if job.status == "running":
                job.status, job.result = "cancelled", "(cancelled)"
                if self.on_event is not None:
                    try:
                        self.on_event(job)
                    except Exception:  # noqa: BLE001
                        pass

        job.task.add_done_callback(_finalize)
        return job

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
                # exception for us. Re-raise only when WE (the waiter) were
                # cancelled — i.e. the job's task itself didn't end cancelled.
                if not (job.task is not None and job.task.cancelled()) \
                        and job.status == "running":
                    raise
        return job

    def cancel(self, job_id: int) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.task is None or job.task.done():
            return False
        job.task.cancel()
        return True

    def cancel_all(self) -> int:
        n = 0
        for j in list(self.jobs.values()):
            if self.cancel(j.id):
                n += 1
        return n
