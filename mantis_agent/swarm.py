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

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

__all__ = ["SwarmCandidate", "SwarmResult", "run_swarm"]


@dataclass(slots=True)
class SwarmCandidate:
    index: int
    worktree: str
    report: str = ""          # the runner's final text
    diff: str = ""            # full patch vs HEAD (includes new files)
    error: str | None = None


@dataclass(slots=True)
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


def _capture_diff(worktree: str) -> str:
    """Full patch of everything the runner did in the worktree, INCLUDING new
    files — stage all, diff the index against HEAD."""
    _git(worktree, "add", "-A")
    r = _git(worktree, "diff", "--cached", "--binary")
    return r.stdout or ""


async def run_swarm(
    task: str,
    n: int,
    repo_root: str | Path,
    *,
    agent_runner: Any,          # async (worktree_path: str, task: str, index: int) -> str
    judge: Any,                 # async (candidates: list[SwarmCandidate]) -> tuple[int, str]
    apply_winner: bool = True,
    keep_worktrees: bool = False,
) -> SwarmResult:
    """Run the swarm. Never raises for per-candidate failures — a crashed
    runner becomes a candidate with ``error`` set and an empty diff; the judge
    only sees candidates that produced a diff. Raises only when ``repo_root``
    isn't a git repo or no worktree can be created at all."""
    repo_root = str(Path(repo_root).resolve())
    if _git(repo_root, "rev-parse", "--git-dir").returncode != 0:
        raise RuntimeError(f"{repo_root} is not a git repository — swarm needs one")
    n = max(2, min(int(n), 8))

    base = Path(tempfile.mkdtemp(prefix="mantis-swarm-"))
    candidates: list[SwarmCandidate] = []
    for i in range(n):
        wt = base / f"attempt-{i + 1}"
        r = _git(repo_root, "worktree", "add", "--detach", str(wt), "HEAD")
        if r.returncode != 0:
            if i == 0:
                raise RuntimeError(f"could not create worktree: {r.stderr.strip()}")
            break
        candidates.append(SwarmCandidate(index=i, worktree=str(wt)))

    async def _run_one(c: SwarmCandidate) -> None:
        try:
            c.report = str(await agent_runner(c.worktree, task, c.index)) or ""
        except Exception as e:  # noqa: BLE001 — one crashed attempt ≠ dead swarm
            c.error = f"{type(e).__name__}: {e}"
        try:
            c.diff = _capture_diff(c.worktree)
        except Exception as e:  # noqa: BLE001
            c.error = c.error or f"diff failed: {e}"

    try:
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

        applied = False
        if apply_winner:
            patch = next(c.diff for c in candidates if c.index == winner)
            r = _git(repo_root, "apply", input_text=patch)  # reads patch from stdin
            applied = r.returncode == 0
            if not applied:
                reason += f" (apply failed: {r.stderr.strip()[:200]})"
        return SwarmResult(winner=winner, reason=reason, applied=applied,
                           candidates=candidates)
    finally:
        if not keep_worktrees:
            for c in candidates:
                _git(repo_root, "worktree", "remove", "--force", c.worktree)
            try:
                base.rmdir()
            except OSError:
                pass
            _git(repo_root, "worktree", "prune")
