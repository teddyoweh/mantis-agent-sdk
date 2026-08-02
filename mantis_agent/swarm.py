"""Swarm mode — N parallel attempts at one task, a judge picks the winner.

The play: create N detached git worktrees (cheap copies of HEAD), run an
independent general-purpose agent in each, capture every attempt's diff,
have a judge rank them, and apply ONLY the winning patch to the real tree.
Parallel exploration beats one-attempt-iterated when the solution space is
wide — different attempts take different approaches, and you keep the best.

Engine design: pure orchestration with injectable ``agent_runner`` and
``judge`` callables, so tests drive it with stubs (no model, no tokens) and
the TUI drives it with real child agents. Worktrees are always cleaned up
(``keep_worktrees=True`` to inspect the losers).

Isolation is prompt-level: each runner is told to work ONLY inside its
worktree with absolute paths. Worktrees share the object store but have
independent checkouts, so parallel file edits can't collide.
"""

from __future__ import annotations

import functools
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

__all__ = ["SwarmCandidate", "SwarmResult", "run_swarm"]


def _emit(cb: Any, ev: dict) -> None:
    """Best-effort progress ping — a bad callback never sinks the swarm."""
    if cb is None:
        return
    try:
        cb(ev)
    except Exception:  # noqa: BLE001 — progress is garnish
        pass


@dataclass
class SwarmCandidate:
    index: int
    worktree: str
    report: str = ""          # the runner's final text
    diff: str = ""            # full patch vs HEAD (includes new files)
    files_changed: int = 0    # distinct files touched — cheap signal for the judge
    error: str | None = None


@dataclass
class SwarmResult:
    winner: int | None            # index into candidates, None = nothing usable
    reason: str = ""
    applied: bool = False
    candidates: list[SwarmCandidate] = field(default_factory=list)


def _git(repo: str | Path, *args: str, input_text: str | None = None,
         timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True, text=True, timeout=timeout, input=input_text,
    )


async def _agit(repo: str | Path, *args: str, input_text: str | None = None,
                timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Async wrapper around :func:`_git` — offloads the blocking
    ``subprocess.run`` to a worker thread so it never freezes the event loop
    (these run under the concurrent swarm task group)."""
    return await anyio.to_thread.run_sync(
        functools.partial(_git, repo, *args, input_text=input_text, timeout=timeout)
    )


async def _capture_diff(worktree: str) -> str:
    """Full patch of everything the runner did in the worktree, INCLUDING new
    files — stage all, diff the index against HEAD."""
    await _agit(worktree, "add", "-A")
    r = await _agit(worktree, "diff", "--cached", "--binary")
    return r.stdout or ""


def _count_files_changed(diff: str) -> int:
    """How many distinct files a patch touches — one ``diff --git`` header per
    file. A cheap correctness/scope signal the judge can weigh (a one-file diff
    for a cross-cutting task, or a sprawling diff for a one-liner, is suspect)."""
    return sum(1 for line in diff.splitlines() if line.startswith("diff --git "))


async def run_swarm(
    task: str,
    n: int,
    repo_root: str | Path,
    *,
    agent_runner: Any,          # async (worktree_path: str, task: str, index: int) -> str
    judge: Any,                 # async (candidates: list[SwarmCandidate]) -> tuple[int, str]
    apply_winner: bool = True,
    keep_worktrees: bool = False,
    concurrency: int | None = None,
    on_progress: Any = None,    # optional (event: dict) -> None progress sink
) -> SwarmResult:
    """Run the swarm. Never raises for per-candidate failures — a crashed
    runner becomes a candidate with ``error`` set and an empty diff; the judge
    only sees candidates that produced a diff. Raises only when ``repo_root``
    isn't a git repo or no worktree can be created at all.

    ``concurrency`` caps how many candidate runners execute at once (default
    ``min(8, cpu_count)``) so a wide swarm doesn't thrash the shared model/HTTP
    pool. ``on_progress``, if given, receives ``{"index", "phase"(start|end),
    ...}`` events per candidate — the same live channel the /workflows viewer
    reads."""
    repo_root = str(Path(repo_root).resolve())
    if (await _agit(repo_root, "rev-parse", "--git-dir")).returncode != 0:
        raise RuntimeError(f"{repo_root} is not a git repository — swarm needs one")
    n = max(2, min(int(n), 8))

    # Cap concurrent runners so a wide swarm doesn't thrash the shared model/
    # HTTP pool. Default min(8, cpu); never exceed n, never below 1.
    cap = concurrency if concurrency is not None else min(8, os.cpu_count() or 1)
    cap = max(1, min(int(cap), n))
    limiter = anyio.CapacityLimiter(cap)

    base = Path(tempfile.mkdtemp(prefix="mantis-swarm-"))
    candidates: list[SwarmCandidate] = []
    winner: int | None = None
    applied = False

    async def _run_one(c: SwarmCandidate) -> None:
        async with limiter:
            _emit(on_progress, {"index": c.index, "phase": "start",
                                "worktree": c.worktree})
            try:
                c.report = str(await agent_runner(c.worktree, task, c.index)) or ""
            except Exception as e:  # noqa: BLE001 — one crashed attempt ≠ dead swarm
                c.error = f"{type(e).__name__}: {e}"
            try:
                c.diff = await _capture_diff(c.worktree)
                c.files_changed = _count_files_changed(c.diff)
            except Exception as e:  # noqa: BLE001
                c.error = c.error or f"diff failed: {e}"
            _emit(on_progress, {"index": c.index, "phase": "end",
                                "error": c.error, "diff_bytes": len(c.diff),
                                "files_changed": c.files_changed})

    try:
        # Worktree creation lives inside the try so a failure on the FIRST
        # ``worktree add`` still hits the finally and removes ``base`` — no
        # leaked temp directory.
        for i in range(n):
            wt = base / f"attempt-{i + 1}"
            r = await _agit(repo_root, "worktree", "add", "--detach", str(wt), "HEAD")
            if r.returncode != 0:
                if i == 0:
                    raise RuntimeError(f"could not create worktree: {r.stderr.strip()}")
                break
            candidates.append(SwarmCandidate(index=i, worktree=str(wt)))

        async with anyio.create_task_group() as tg:
            for c in candidates:
                tg.start_soon(_run_one, c)

        viable = [c for c in candidates if c.diff.strip() and not c.error]
        if not viable:
            return SwarmResult(winner=None, reason="no attempt produced a usable diff",
                               candidates=candidates)
        if len(viable) == 1:
            winner, reason = viable[0].index, "only viable attempt"
        else:
            winner, reason = await judge(viable)
            if not any(c.index == winner for c in viable):
                winner, reason = viable[0].index, f"judge picked invalid index; {reason}"

        if apply_winner:
            patch = next(c.diff for c in candidates if c.index == winner)
            r = await _agit(repo_root, "apply", input_text=patch)  # patch via stdin
            applied = r.returncode == 0
            if not applied:
                wt = next(c.worktree for c in candidates if c.index == winner)
                reason += (f" (apply failed: {r.stderr.strip()[:200]}); "
                           f"winning worktree kept at {wt}")
        return SwarmResult(winner=winner, reason=reason, applied=applied,
                           candidates=candidates)
    finally:
        # Preserve the winner's worktree when we were asked to apply it but the
        # apply failed — otherwise the user loses the work and is left with only
        # the diff string. Everything else is always cleaned up.
        keep_winner = apply_winner and winner is not None and not applied
        if not keep_worktrees:
            for c in candidates:
                if keep_winner and c.index == winner:
                    continue
                await _agit(repo_root, "worktree", "remove", "--force", c.worktree)
            if not keep_winner:
                try:
                    base.rmdir()
                except OSError:
                    pass
            await _agit(repo_root, "worktree", "prune")
