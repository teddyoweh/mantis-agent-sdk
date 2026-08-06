"""The shared task DAG — atomic claims, leases, dependency gating.

Everything else a team does is coordination; *this* is the correctness
property. Two peers must never do the same work, and the only way to guarantee
that between processes is to let the database arbitrate. So a claim is a
compare-and-set inside a single ``BEGIN IMMEDIATE`` transaction:

.. code-block:: text

    BEGIN IMMEDIATE                 -- write lock taken here, before any read
      SELECT ... FROM tasks WHERE id = ?
      (release the claim if its lease has expired)
      (refuse unless status = 'open' AND assignee IS NULL)
      (refuse unless every dependency is done)
      UPDATE tasks SET assignee = ? WHERE id = ? AND status = 'open' AND assignee IS NULL
    COMMIT

``IMMEDIATE`` is what makes it sound. A plain ``BEGIN`` is deferred: the
transaction starts in read mode, so twenty peers would all read
``assignee IS NULL``, and nineteen would then fail to upgrade to a write lock —
``SQLITE_BUSY`` on *commit*, at which point the loser has already told its agent
it won. Taking the write lock up front turns the race into a queue: the losers
block, then read the winner's committed row and lose cleanly.

The rest of the module is bookkeeping around that one guarantee:

* **Leases.** A claim expires (default :data:`~mantis_agent.teams.types.
  DEFAULT_LEASE_SECONDS`) unless the peer heartbeats. Without this, a peer that
  is ``kill -9``'d converts its task into a permanently blocked one — the
  failure mode a shared todo list has and a team must not.
* **Partial work.** Expiry is not free: the dead peer may have edited half the
  files. The row remembers that (``partial_work``) and the next claimant is told
  so in :attr:`~mantis_agent.teams.types.ClaimResult.warning` rather than being
  handed what looks like clean, untouched work.
* **Dependencies.** A task is claimable only when every ``depends_on`` task is
  ``done``. Completion re-evaluates dependents and reports which ones woke up;
  that list is what the runtime turns into peer wake-ups.
* **Cycles.** Rejected when the edge is created, with the path in the error.
  Rejecting later — at claim time — would mean a team that deadlocks instead of
  a task creation that fails.
* **Failure.** A failed task blocks its dependents *transitively*, each pointing
  at the root cause with a reason. Leaving them ``open`` but permanently
  ungated would be a silent stall, which is the hardest kind to debug.

Storage follows :class:`~mantis_agent.session.SqliteSessionStore`: one file, a
connection per call (``check_same_thread=False``), WAL, schema created eagerly
at construction. Two deliberate differences — a ``busy_timeout``, because this
store is *designed* to be contended, and a refusal of ``":memory:"``, because
every connection to an in-memory database gets its own private database, which
would make claims non-atomic in exactly the way nothing would ever notice.

The sync API is primary: peers claim from threads, and a claim that has to
bounce through an event loop is harder to reason about, not easier.
:class:`AsyncTaskStore` wraps it for the async runtime, dispatching each call to
a worker thread via ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

from .types import (
    ACTIVE_CLAIM_STATUSES,
    DEFAULT_LEASE_SECONDS,
    TERMINAL_STATUSES,
    Blockers,
    ClaimResult,
    Completion,
    Failure,
    Task,
    TaskClaimConflictError,
    TaskCycleError,
    TaskDependencyBlockedError,
    TaskLeaseExpiredError,
    TaskNotFoundError,
    TaskStateError,
    TeamStoreCorruptError,
    new_task_id,
    validate_actor,
    validate_id,
    validate_title,
)

__all__ = ["SCHEMA_VERSION", "AsyncTaskStore", "TaskStore"]

_log = logging.getLogger("mantis_agent.teams.tasks")

#: Bumped, not silently extended — a store written by a newer mantis is refused
#: rather than half-understood, the same rule ``workflow_store`` applies to run
#: records. ``schema_meta`` exists from v1 so there is somewhere to read it from.
SCHEMA_VERSION = 1

# How long a claim waits for the write lock before giving up. Contention here is
# expected and short (a claim is three statements against a handful of rows), so
# a generous timeout costs nothing and turns a transient SQLITE_BUSY under a
# twenty-peer stampede into a queue rather than an error.
_BUSY_TIMEOUT_MS = 15_000

_DIR_MODE = 0o700
_FILE_MODE = 0o600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  assignee TEXT,
  claimed_at REAL,
  lease_expires REAL,
  heartbeat_at REAL,
  reclaim_count INTEGER NOT NULL DEFAULT 0,
  last_assignee TEXT NOT NULL DEFAULT '',
  partial_work INTEGER NOT NULL DEFAULT 0,
  stale_claim INTEGER NOT NULL DEFAULT 0,
  blocked_reason TEXT NOT NULL DEFAULT '',
  blocked_by TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  artifacts TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_deps (
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (task_id, depends_on)
);
CREATE INDEX IF NOT EXISTS idx_tasks_team_status ON tasks(team_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(lease_expires);
CREATE INDEX IF NOT EXISTS idx_deps_upstream ON task_deps(depends_on);
"""

# Column list shared by every SELECT so ``_row_to_task`` can trust the shape.
_COLS = (
    "id, team_id, title, description, created_by, status, assignee, claimed_at, "
    "lease_expires, heartbeat_at, reclaim_count, last_assignee, partial_work, "
    "stale_claim, blocked_reason, blocked_by, result, artifacts, created_at, "
    "updated_at"
)

# Sort order for every listing: creation order, ties broken by id. Stable across
# processes (unlike rowid) and it is what a user means by "the next task".
_ORDER = "ORDER BY created_at ASC, id ASC"


def _row_to_task(row: sqlite3.Row, deps: tuple[str, ...]) -> Task:
    """Build an immutable snapshot from a row plus its dependency edges."""

    try:
        artifacts = tuple(json.loads(row["artifacts"] or "[]"))
    except (ValueError, TypeError):
        # A hand-edited or truncated artifacts column must not take down the
        # task it decorates; the refs are a convenience, the row is the record.
        artifacts = ()
    return Task(
        id=row["id"],
        team_id=row["team_id"],
        title=row["title"],
        description=row["description"] or "",
        created_by=row["created_by"] or "",
        depends_on=deps,
        status=row["status"],
        assignee=row["assignee"],
        claimed_at=row["claimed_at"],
        lease_expires=row["lease_expires"],
        heartbeat_at=row["heartbeat_at"],
        reclaim_count=int(row["reclaim_count"] or 0),
        last_assignee=row["last_assignee"] or "",
        partial_work=bool(row["partial_work"]),
        blocked_reason=row["blocked_reason"] or "",
        blocked_by=row["blocked_by"] or "",
        result=row["result"] or "",
        artifacts=artifacts,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _dedupe(ids: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving dedupe. ``depends_on=("a", "a")`` is one edge."""

    seen: dict[str, None] = {}
    for value in ids:
        seen.setdefault(value, None)
    return tuple(seen)


def _find_cycle(
    edges: dict[str, tuple[str, ...]], start: str, from_start: Sequence[str]
) -> Optional[tuple[str, ...]]:
    """Return the path proving *start* reaches itself, or ``None``.

    Iterative DFS following ``depends_on`` edges, so the returned path reads in
    dependency order (``a -> c -> b -> a`` = "a depends on c depends on b
    depends on a"). Explicit stack rather than recursion: a deep chain is a
    legitimate DAG, and blowing the interpreter's stack on one would be a poor
    way to report a cycle that isn't there.

    ``seen`` memoizes nodes that were fully explored *without* reaching start —
    revisiting them cannot change the answer. Nodes on the current path are
    skipped too: a cycle that does not pass through ``start`` is pre-existing,
    and this call is only adjudicating the edge being added.
    """

    path = [start]
    on_path = {start}
    seen: set[str] = set()
    stack: list[Iterator[str]] = [iter(from_start)]
    while stack:
        try:
            nxt = next(stack[-1])
        except StopIteration:
            stack.pop()
            on_path.discard(path.pop())
            continue
        if nxt == start:
            return tuple(path + [start])
        if nxt in on_path or nxt in seen:
            continue
        seen.add(nxt)
        path.append(nxt)
        on_path.add(nxt)
        stack.append(iter(edges.get(nxt, ())))
    return None


class TaskStore:
    """Durable task DAG for one or more teams, in a single SQLite file.

    Parameters
    ----------
    path:
        Filesystem path to ``tasks.db``. Parent directories are created ``0o700``
        and the file is chmod'ed ``0o600`` (plan §11). ``":memory:"`` is refused —
        see the module docstring.
    lease_seconds:
        Default claim lease. Per-claim overrides are accepted by :meth:`claim`.
    clock:
        Wall-clock source, injectable so tests can expire a lease without
        sleeping. Must be seconds-since-epoch: leases are compared across
        processes, so ``time.monotonic`` would be meaningless.
    """

    def __init__(
        self,
        path: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if path == ":memory:" or path.startswith("file::memory:"):
            raise ValueError(
                "TaskStore refuses an in-memory database: every connection "
                "would get its own copy, and claims would stop being atomic "
                "without anything failing. Use a file path."
            )
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._path = path
        self._lease = float(lease_seconds)
        self._clock = clock
        self._init_schema()

    # ------------------------------------------------------------------
    # connection plumbing
    # ------------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def lease_seconds(self) -> float:
        return self._lease

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        conn.row_factory = sqlite3.Row
        # WAL lets readers (the panel, stall detection) run while a claim holds
        # the write lock. NORMAL sync is the session store's tradeoff too: a
        # power cut can lose the last commit, and a lost claim is re-claimable.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=%d" % _BUSY_TIMEOUT_MS)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        parent = Path(self._path).expanduser().parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                try:
                    found = int(row["value"])
                except (TypeError, ValueError) as exc:
                    raise TeamStoreCorruptError(
                        f"{self._path}: unreadable schema_version {row['value']!r}"
                    ) from exc
                if found > SCHEMA_VERSION:
                    raise TeamStoreCorruptError(
                        f"{self._path}: schema version {found} is newer than this "
                        f"build understands ({SCHEMA_VERSION}); refusing to touch it"
                    )
        finally:
            conn.close()
        self._harden_permissions()

    def _harden_permissions(self) -> None:
        """0o600 on the db and its WAL sidecars. Best effort — a filesystem
        without POSIX modes (or a read-only one) must not stop a team."""

        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self._path + suffix, _FILE_MODE)
            except OSError:
                pass

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """A ``BEGIN IMMEDIATE`` transaction. The write lock is held for the
        whole block, which is what makes the read-then-CAS inside it atomic."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # row helpers (all called with a connection already in hand)
    # ------------------------------------------------------------------

    @staticmethod
    def _row(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT " + _COLS + " FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    @staticmethod
    def _require_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = TaskStore._row(conn, task_id)
        if row is None:
            raise TaskNotFoundError(task_id)
        return row

    @staticmethod
    def _deps(conn: sqlite3.Connection, task_id: str) -> tuple[str, ...]:
        cur = conn.execute(
            "SELECT depends_on FROM task_deps WHERE task_id = ? "
            "ORDER BY position ASC, depends_on ASC",
            (task_id,),
        )
        return tuple(r["depends_on"] for r in cur.fetchall())

    @staticmethod
    def _dependents(conn: sqlite3.Connection, task_id: str) -> tuple[str, ...]:
        cur = conn.execute(
            "SELECT task_id FROM task_deps WHERE depends_on = ? ORDER BY task_id ASC",
            (task_id,),
        )
        return tuple(r["task_id"] for r in cur.fetchall())

    @staticmethod
    def _all_edges(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
        edges: dict[str, list[str]] = {}
        cur = conn.execute(
            "SELECT task_id, depends_on FROM task_deps ORDER BY position ASC"
        )
        for row in cur.fetchall():
            edges.setdefault(row["task_id"], []).append(row["depends_on"])
        return {k: tuple(v) for k, v in edges.items()}

    def _task(self, conn: sqlite3.Connection, task_id: str) -> Task:
        return _row_to_task(self._require_row(conn, task_id), self._deps(conn, task_id))

    def _blockers(self, conn: sqlite3.Connection, task_id: str) -> Blockers:
        """Split this task's unmet dependencies by cause. See :class:`Blockers`."""

        pending: list[str] = []
        failed: list[str] = []
        missing: list[str] = []
        for dep in self._deps(conn, task_id):
            row = self._row(conn, dep)
            if row is None:
                missing.append(dep)
            elif row["status"] == "done":
                continue
            elif row["status"] in ("failed", "blocked"):
                # 'blocked' counts as failed: a blocked dependency is blocked by
                # something that failed, so this task's remedy is the same one.
                failed.append(dep)
            else:
                pending.append(dep)
        return Blockers(tuple(pending), tuple(failed), tuple(missing))

    def _expire(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> bool:
        """Release *row*'s claim if its lease has run out. Returns whether it did.

        Called at the top of every claim-sensitive transaction rather than only
        from a background sweep, so correctness never depends on a sweeper
        running: the first peer to touch an abandoned task is the one that
        frees it.
        """

        if row["status"] not in ACTIVE_CLAIM_STATUSES:
            return False
        expires = row["lease_expires"]
        if expires is None or expires > now:
            return False
        conn.execute(
            "UPDATE tasks SET status='open', assignee=NULL, claimed_at=NULL, "
            "lease_expires=NULL, heartbeat_at=NULL, last_assignee=?, "
            "reclaim_count=reclaim_count+1, partial_work=1, stale_claim=1, "
            "updated_at=? WHERE id=?",
            (row["assignee"] or "", now, row["id"]),
        )
        _log.info(
            "team task %s: lease held by %s expired %.0fs ago; released for reclaim",
            row["id"],
            row["assignee"],
            now - float(expires),
        )
        return True

    def _reap(self, task_id: str, peer: Optional[str], now: float) -> None:
        """Expire a dead claim on *task_id* in a transaction of its own.

        This exists because of an ordering subtlety worth stating plainly: an
        expired lease is a *fact about a dead peer*, not a step in whatever
        operation happened to notice it. If the release rode inside the caller's
        transaction, a refusal (``TaskLeaseExpiredError``) would roll it back and
        the task would keep looking claimed to the panel until someone else
        happened to try. So the release commits first, and only then does the
        refusal get raised — outside the ``with`` block, deliberately.

        Passing ``peer`` asks "and was it *this* peer's claim I just reaped?",
        which is the difference between "you went quiet, re-claim" and "someone
        else holds it". Pass ``None`` (claim does) to reap without raising.
        """

        # Cheap read first. Expiry is the rare case, and taking the write lock
        # on every claim just to discover there was nothing to reap would double
        # the contention the IMMEDIATE transaction is carefully minimizing.
        with self._read() as conn:
            probe = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? AND lease_expires IS NOT NULL "
                "AND lease_expires <= ?",
                (task_id, now),
            ).fetchone()
        if probe is None:
            return
        with self._write() as conn:
            row = self._row(conn, task_id)
            if row is None or not self._expire(conn, row, now):
                return
            expired_from = row["assignee"] or ""
        if peer is not None and expired_from == peer:
            raise TaskLeaseExpiredError(task_id, peer)

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------

    def create_task(
        self,
        team_id: str,
        title: str,
        *,
        task_id: Optional[str] = None,
        description: str = "",
        created_by: str = "",
        depends_on: Sequence[str] = (),
    ) -> Task:
        """Create a task. Rejects a dependency edge that would close a cycle.

        Dependencies may name tasks that do not exist yet — a lead sketching a
        plan top-down would otherwise have to emit it in reverse. An unknown
        dependency simply keeps the task unclaimable and shows up in
        :meth:`blockers` as ``missing``, which is honest about the typo case
        without forbidding the legitimate one.
        """

        validate_id(team_id, what="team_id")
        title = validate_title(title)
        tid = validate_id(task_id, what="task_id") if task_id else new_task_id()
        if created_by:
            validate_actor(created_by, what="created_by")
        deps = _dedupe(validate_id(d, what="dependency") for d in depends_on)
        now = self._clock()

        with self._write() as conn:
            if self._row(conn, tid) is not None:
                raise ValueError(f"task {tid!r} already exists")
            if deps:
                cycle = _find_cycle(self._all_edges(conn), tid, deps)
                if cycle is not None:
                    # Raised before the INSERT, and the transaction rolls back
                    # anyway: a rejected task leaves no trace.
                    raise TaskCycleError(cycle)
            conn.execute(
                "INSERT INTO tasks (id, team_id, title, description, created_by, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                (tid, team_id, title, description, created_by, now, now),
            )
            conn.executemany(
                "INSERT INTO task_deps (task_id, depends_on, position) VALUES (?, ?, ?)",
                [(tid, dep, i) for i, dep in enumerate(deps)],
            )
            return self._task(conn, tid)

    def add_dependency(self, task_id: str, depends_on: str) -> Task:
        """Add one edge to an existing task, with the same cycle check."""

        validate_id(task_id, what="task_id")
        validate_id(depends_on, what="dependency")
        now = self._clock()
        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["status"] in TERMINAL_STATUSES:
                raise TaskStateError(task_id, row["status"], "add a dependency")
            existing = self._deps(conn, task_id)
            if depends_on in existing:
                return self._task(conn, task_id)
            edges = self._all_edges(conn)
            cycle = _find_cycle(edges, task_id, tuple(existing) + (depends_on,))
            if cycle is not None:
                raise TaskCycleError(cycle)
            conn.execute(
                "INSERT INTO task_deps (task_id, depends_on, position) VALUES (?, ?, ?)",
                (task_id, depends_on, len(existing)),
            )
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id))
            return self._task(conn, task_id)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        with self._read() as conn:
            return self._task(conn, task_id)

    def list_tasks(
        self, team_id: Optional[str] = None, *, status: Optional[str] = None
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[Any] = []
        if team_id is not None:
            clauses.append("team_id = ?")
            params.append(team_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT " + _COLS + " FROM tasks" + where + " " + _ORDER, params
            ).fetchall()
            return [_row_to_task(r, self._deps(conn, r["id"])) for r in rows]

    def blockers(self, task_id: str) -> Blockers:
        """Why this task is not claimable — empty when it is."""

        with self._read() as conn:
            self._require_row(conn, task_id)
            return self._blockers(conn, task_id)

    def claimable(self, team_id: str, *, sweep: bool = True) -> list[Task]:
        """Open tasks whose dependencies are all done.

        Sweeps expired leases first by default: a task whose holder died is
        claimable in every sense that matters, and a listing that says otherwise
        would send an idle peer to sleep next to available work. Pass
        ``sweep=False`` for a pure read (the panel refreshing on a timer).
        """

        if sweep:
            self.sweep_expired(team_id)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT " + _COLS + " FROM tasks WHERE team_id = ? AND status = 'open' "
                "AND assignee IS NULL " + _ORDER,
                (team_id,),
            ).fetchall()
            out = []
            for row in rows:
                if self._blockers(conn, row["id"]).clear:
                    out.append(_row_to_task(row, self._deps(conn, row["id"])))
            return out

    def stats(self, team_id: str) -> dict[str, int]:
        """``{status: count}`` for one team, zero-valued statuses omitted."""

        with self._read() as conn:
            cur = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE team_id = ? "
                "GROUP BY status",
                (team_id,),
            )
            return {r["status"]: int(r["n"]) for r in cur.fetchall()}

    # ------------------------------------------------------------------
    # claims
    # ------------------------------------------------------------------

    def claim(
        self, task_id: str, peer: str, *, lease_seconds: Optional[float] = None
    ) -> ClaimResult:
        """Atomically claim *task_id* for *peer*.

        Raises :class:`TaskClaimConflictError` if another peer holds it or it is
        no longer claimable, and :class:`TaskDependencyBlockedError` if its
        dependencies are unmet. Use :meth:`try_claim` when losing a race is a
        normal outcome rather than an error.
        """

        validate_actor(peer)
        lease = float(lease_seconds) if lease_seconds is not None else self._lease
        if lease <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock()
        self._reap(task_id, None, now)
        with self._write() as conn:
            return self._claim_locked(conn, task_id, peer, lease, now)

    def try_claim(
        self, task_id: str, peer: str, *, lease_seconds: Optional[float] = None
    ) -> Optional[ClaimResult]:
        """:meth:`claim`, returning ``None`` instead of raising on a lost race.

        Only the conflict is swallowed. A missing task or an unmet dependency
        still raises — those are the caller's bugs, not the arbitration's
        verdict, and turning them into ``None`` would make a typo look like
        contention.
        """

        try:
            return self.claim(task_id, peer, lease_seconds=lease_seconds)
        except TaskClaimConflictError:
            return None

    def claim_next(
        self, team_id: str, peer: str, *, lease_seconds: Optional[float] = None
    ) -> Optional[ClaimResult]:
        """Claim the oldest claimable task in *team_id*, or ``None`` if there is
        none.

        Selection happens *inside* the claiming transaction, which is why this
        cannot lose a race: a second peer entering while the first holds the
        write lock reads the already-updated row and moves to the next
        candidate, instead of picking the same task and failing.
        """

        validate_actor(peer)
        lease = float(lease_seconds) if lease_seconds is not None else self._lease
        if lease <= 0:
            # Same guard `claim` enforces. Without it a non-positive lease writes
            # `lease_expires <= claimed_at` — a claim that is already expired the
            # instant it is granted, so the next peer reclaims work still in flight.
            raise ValueError("lease_seconds must be positive")
        now = self._clock()
        with self._write() as conn:
            self._sweep_locked(conn, team_id, now)
            rows = conn.execute(
                "SELECT " + _COLS + " FROM tasks WHERE team_id = ? AND status = 'open' "
                "AND assignee IS NULL " + _ORDER,
                (team_id,),
            ).fetchall()
            for row in rows:
                if self._blockers(conn, row["id"]).clear:
                    return self._claim_locked(conn, row["id"], peer, lease, now)
            return None

    def _claim_locked(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        peer: str,
        lease: float,
        now: float,
    ) -> ClaimResult:
        row = self._require_row(conn, task_id)
        self._expire(conn, row, now)
        row = self._require_row(conn, task_id)

        status = row["status"]
        if status == "blocked" and row["assignee"] is None:
            # A blocked task is unheld, so "already claimed" would be a lie and
            # an unhelpful one: the claimant needs the upstream failure, which
            # is what the dependency error carries.
            raise TaskDependencyBlockedError(task_id, self._blockers(conn, task_id))
        if status != "open" or row["assignee"] is not None:
            raise TaskClaimConflictError(
                task_id, holder=row["assignee"] or "", status=status
            )
        blockers = self._blockers(conn, task_id)
        if not blockers.clear:
            raise TaskDependencyBlockedError(task_id, blockers)

        # The WHERE clause is the compare-and-set. Under BEGIN IMMEDIATE we
        # already hold the write lock so it cannot fail here — it is kept
        # because it makes the invariant checkable in the schema rather than
        # only in this function, and because a future caller that forgets the
        # transaction gets a lost update instead of a silent double-claim.
        cur = conn.execute(
            "UPDATE tasks SET status='claimed', assignee=?, claimed_at=?, "
            "lease_expires=?, heartbeat_at=?, stale_claim=0, blocked_reason='', "
            "blocked_by='', updated_at=? "
            "WHERE id=? AND status='open' AND assignee IS NULL",
            (peer, now, now + lease, now, now, task_id),
        )
        if cur.rowcount != 1:  # pragma: no cover - unreachable while transactional
            fresh = self._require_row(conn, task_id)
            raise TaskClaimConflictError(
                task_id, holder=fresh["assignee"] or "", status=fresh["status"]
            )

        reclaimed = bool(row["stale_claim"])
        previous = row["last_assignee"] or "" if reclaimed else ""
        partial = bool(row["partial_work"]) and reclaimed
        warning = ""
        if reclaimed:
            warning = (
                f"task {task_id} was previously held by {previous or 'another peer'} "
                f"and abandoned (reclaim #{int(row['reclaim_count'] or 0)}). "
                "Work may be partially complete — inspect the tree before redoing it."
                if partial
                else f"task {task_id} was previously released by "
                f"{previous or 'another peer'} (reclaim #{int(row['reclaim_count'] or 0)})."
            )
        return ClaimResult(
            task=self._task(conn, task_id),
            reclaimed=reclaimed,
            previous_assignee=previous,
            partial_work=partial,
            warning=warning,
        )

    def heartbeat(
        self, task_id: str, peer: str, *, lease_seconds: Optional[float] = None
    ) -> float:
        """Extend *peer*'s lease on *task_id*. Returns the new expiry.

        Refuses to resurrect an expired claim: by then the task may already
        belong to someone else, and a heartbeat that silently re-acquires it is
        the double-assignment this whole module exists to prevent.
        """

        validate_actor(peer)
        lease = float(lease_seconds) if lease_seconds is not None else self._lease
        if lease <= 0:
            # Same guard `claim` enforces. Without it a non-positive lease writes
            # `lease_expires <= claimed_at` — a claim that is already expired the
            # instant it is granted, so the next peer reclaims work still in flight.
            raise ValueError("lease_seconds must be positive")
        now = self._clock()
        self._reap(task_id, peer, now)
        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["assignee"] != peer:
                raise TaskClaimConflictError(
                    task_id, holder=row["assignee"] or "", status=row["status"]
                )
            expires = now + lease
            conn.execute(
                "UPDATE tasks SET lease_expires=?, heartbeat_at=?, updated_at=? "
                "WHERE id=? AND assignee=?",
                (expires, now, now, task_id, peer),
            )
            return expires

    def sweep_expired(self, team_id: Optional[str] = None) -> tuple[str, ...]:
        """Release every claim whose lease has run out. Returns the task ids.

        Correctness does not depend on this running — :meth:`claim` expires the
        row it touches — but a periodic sweep is what makes an abandoned task
        *visible* (and wakes idle peers) before anyone happens to ask for it.
        """

        now = self._clock()
        with self._write() as conn:
            return self._sweep_locked(conn, team_id, now)

    def _sweep_locked(
        self, conn: sqlite3.Connection, team_id: Optional[str], now: float
    ) -> tuple[str, ...]:
        params: list[Any] = [now]
        where = "lease_expires IS NOT NULL AND lease_expires <= ?"
        if team_id is not None:
            where += " AND team_id = ?"
            params.append(team_id)
        rows = conn.execute(
            "SELECT " + _COLS + " FROM tasks WHERE " + where + " " + _ORDER, params
        ).fetchall()
        released = [row["id"] for row in rows if self._expire(conn, row, now)]
        return tuple(released)

    def release(
        self,
        task_id: str,
        peer: str,
        *,
        partial_work: bool = False,
        reason: str = "",
    ) -> Task:
        """Voluntarily give a claim back.

        Distinct from lease expiry: the peer is alive and says so, which is why
        ``reclaim_count`` does not move and the next claimant is not warned —
        unless ``partial_work=True``, the honest answer when a peer bails out
        halfway through an edit.
        """

        validate_actor(peer)
        now = self._clock()
        self._reap(task_id, peer, now)
        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["assignee"] != peer:
                raise TaskClaimConflictError(
                    task_id, holder=row["assignee"] or "", status=row["status"]
                )
            conn.execute(
                "UPDATE tasks SET status='open', assignee=NULL, claimed_at=NULL, "
                "lease_expires=NULL, heartbeat_at=NULL, last_assignee=?, "
                "partial_work=?, stale_claim=?, blocked_reason=?, updated_at=? "
                "WHERE id=?",
                (
                    peer,
                    1 if partial_work else 0,
                    1 if partial_work else 0,
                    reason,
                    now,
                    task_id,
                ),
            )
            return self._task(conn, task_id)

    def start(self, task_id: str, peer: str) -> Task:
        """``claimed`` → ``in_progress``. The lease is untouched."""

        validate_actor(peer)
        now = self._clock()
        self._reap(task_id, peer, now)
        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["assignee"] != peer:
                raise TaskClaimConflictError(
                    task_id, holder=row["assignee"] or "", status=row["status"]
                )
            if row["status"] != "claimed":
                raise TaskStateError(task_id, row["status"], "start")
            conn.execute(
                "UPDATE tasks SET status='in_progress', updated_at=? WHERE id=?",
                (now, task_id),
            )
            return self._task(conn, task_id)

    # ------------------------------------------------------------------
    # terminal transitions
    # ------------------------------------------------------------------

    def complete(
        self,
        task_id: str,
        peer: str,
        *,
        result: str = "",
        artifacts: Sequence[str] = (),
    ) -> Completion:
        """Finish a held task and re-evaluate everything downstream of it.

        The returned :class:`Completion` carries ``unblocked`` — the dependents
        that just became claimable. That list, not the completion itself, is
        what makes a team more than a shared todo list, so it is a return value
        rather than something a caller has to recompute.
        """

        validate_actor(peer)
        now = self._clock()
        self._reap(task_id, peer, now)
        with self._write() as conn:
            self._require_held(conn, task_id, peer, "complete")
            conn.execute(
                "UPDATE tasks SET status='done', assignee=NULL, lease_expires=NULL, "
                "heartbeat_at=NULL, last_assignee=?, stale_claim=0, partial_work=0, "
                "blocked_reason='', blocked_by='', result=?, artifacts=?, updated_at=? "
                "WHERE id=?",
                (peer, result, json.dumps(list(artifacts)), now, task_id),
            )
            resumed = self._resume_blocked(conn, (task_id,), now)
            unblocked = tuple(
                dep_id
                for dep_id in self._dependents(conn, task_id)
                if self._is_claimable_row(conn, dep_id)
            )
            return Completion(
                task=self._task(conn, task_id),
                unblocked=unblocked,
                resumed=resumed,
            )

    def fail(self, task_id: str, peer: str, *, reason: str) -> Failure:
        """Fail a held task and block everything downstream, with a reason.

        Dependents are marked ``blocked`` transitively and every one of them
        points at the *root* cause, not at its immediate neighbour: a peer three
        hops down needs to know which task actually broke, and the chain is
        reconstructible from the edges anyway.
        """

        validate_actor(peer)
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("fail() requires a non-empty reason")
        now = self._clock()
        self._reap(task_id, peer, now)
        with self._write() as conn:
            self._require_held(conn, task_id, peer, "fail")
            conn.execute(
                "UPDATE tasks SET status='failed', assignee=NULL, lease_expires=NULL, "
                "heartbeat_at=NULL, last_assignee=?, stale_claim=0, blocked_reason=?, "
                "updated_at=? WHERE id=?",
                (peer, reason, now, task_id),
            )
            blocked = self._block_dependents(conn, task_id, reason, now)
            return Failure(task=self._task(conn, task_id), blocked=blocked)

    def reopen(self, task_id: str, *, reason: str = "") -> Completion:
        """Return a failed or blocked task to ``open`` and unblock its subtree.

        The retry path. ``done`` is deliberately not reopenable: work that was
        reported finished should be superseded by a new task, not quietly
        un-finished under peers that already built on it.
        """

        now = self._clock()
        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["status"] not in ("failed", "blocked"):
                raise TaskStateError(task_id, row["status"], "reopen")
            conn.execute(
                "UPDATE tasks SET status='open', assignee=NULL, claimed_at=NULL, "
                "lease_expires=NULL, heartbeat_at=NULL, stale_claim=0, "
                "blocked_reason=?, blocked_by='', updated_at=? WHERE id=?",
                (reason, now, task_id),
            )
            resumed = self._resume_blocked(conn, (task_id,), now)
            unblocked = tuple(
                dep_id
                for dep_id in self._dependents(conn, task_id)
                if self._is_claimable_row(conn, dep_id)
            )
            return Completion(
                task=self._task(conn, task_id), unblocked=unblocked, resumed=resumed
            )

    def _require_held(
        self, conn: sqlite3.Connection, task_id: str, peer: str, verb: str
    ) -> sqlite3.Row:
        """Assert *peer* holds a live claim on *task_id*, or raise.

        Callers :meth:`_reap` first, so an expired lease has already been
        released and reported by the time this runs; what is left to decide is
        terminal-vs-held-by-someone-else. Terminal wins, because a completed
        task is a state error rather than a stolen claim.
        """

        row = self._require_row(conn, task_id)
        if row["status"] in TERMINAL_STATUSES:
            raise TaskStateError(task_id, row["status"], verb)
        if row["assignee"] != peer:
            raise TaskClaimConflictError(
                task_id, holder=row["assignee"] or "", status=row["status"]
            )
        return row

    def _is_claimable_row(self, conn: sqlite3.Connection, task_id: str) -> bool:
        row = self._row(conn, task_id)
        if row is None or row["status"] != "open" or row["assignee"] is not None:
            return False
        return self._blockers(conn, task_id).clear

    def _block_dependents(
        self, conn: sqlite3.Connection, root: str, reason: str, now: float
    ) -> tuple[str, ...]:
        """Breadth-first mark of everything downstream of a failed task."""

        blocked: list[str] = []
        queue = list(self._dependents(conn, root))
        seen = {root}
        text = f"upstream task {root} failed: {reason}"
        while queue:
            tid = queue.pop(0)
            if tid in seen:
                continue
            seen.add(tid)
            row = self._row(conn, tid)
            if row is None or row["status"] not in ("open", "blocked"):
                # Terminal or actively held work is left alone; a peer mid-edit
                # is told through its inbox, not by having its row moved under
                # it. Traversal still continues past it.
                queue.extend(self._dependents(conn, tid))
                continue
            if row["status"] != "blocked":
                conn.execute(
                    "UPDATE tasks SET status='blocked', blocked_reason=?, "
                    "blocked_by=?, updated_at=? WHERE id=?",
                    (text, root, now, tid),
                )
                blocked.append(tid)
            queue.extend(self._dependents(conn, tid))
        return tuple(blocked)

    def _resume_blocked(
        self, conn: sqlite3.Connection, roots: Sequence[str], now: float
    ) -> tuple[str, ...]:
        """Un-block dependents whose upstream failure is gone.

        A task returns to ``open`` when none of its direct dependencies is
        ``failed`` or ``blocked`` — it may still be gated (a dependency merely
        unfinished), and that is the difference between *blocked* (someone must
        decide something) and *not yet claimable* (wait).
        """

        resumed: list[str] = []
        queue = list(roots)
        seen: set[str] = set()
        while queue:
            tid = queue.pop(0)
            for dep_id in self._dependents(conn, tid):
                if dep_id in seen:
                    continue
                seen.add(dep_id)
                row = self._row(conn, dep_id)
                if row is None or row["status"] != "blocked":
                    continue
                if self._blockers(conn, dep_id).failed:
                    continue
                conn.execute(
                    "UPDATE tasks SET status='open', blocked_reason='', "
                    "blocked_by='', updated_at=? WHERE id=?",
                    (now, dep_id),
                )
                resumed.append(dep_id)
                queue.append(dep_id)
        return tuple(resumed)


class AsyncTaskStore:
    """Async facade over :class:`TaskStore`, one worker thread per call.

    Identical method names and semantics; every call is
    ``await``-ed through ``anyio.to_thread.run_sync`` so a claim never blocks
    the event loop the peers' agent loops are running on. This is the same
    split :class:`~mantis_agent.session.SqliteSessionStore` uses, kept as a
    separate class here because the sync store is the primary API — peers claim
    from threads too.
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def sync(self) -> TaskStore:
        """The underlying sync store, for callers already on a worker thread."""

        return self._store

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        import functools  # noqa: PLC0415 - local: keeps import cost off the sync path

        import anyio  # noqa: PLC0415

        fn = getattr(self._store, name)
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    async def create_task(self, *args: Any, **kwargs: Any) -> Task:
        return await self._call("create_task", *args, **kwargs)

    async def add_dependency(self, *args: Any, **kwargs: Any) -> Task:
        return await self._call("add_dependency", *args, **kwargs)

    async def get(self, task_id: str) -> Task:
        return await self._call("get", task_id)

    async def list_tasks(self, *args: Any, **kwargs: Any) -> list[Task]:
        return await self._call("list_tasks", *args, **kwargs)

    async def blockers(self, task_id: str) -> Blockers:
        return await self._call("blockers", task_id)

    async def claimable(self, *args: Any, **kwargs: Any) -> list[Task]:
        return await self._call("claimable", *args, **kwargs)

    async def stats(self, team_id: str) -> dict[str, int]:
        return await self._call("stats", team_id)

    async def claim(self, *args: Any, **kwargs: Any) -> ClaimResult:
        return await self._call("claim", *args, **kwargs)

    async def try_claim(self, *args: Any, **kwargs: Any) -> Optional[ClaimResult]:
        return await self._call("try_claim", *args, **kwargs)

    async def claim_next(self, *args: Any, **kwargs: Any) -> Optional[ClaimResult]:
        return await self._call("claim_next", *args, **kwargs)

    async def heartbeat(self, *args: Any, **kwargs: Any) -> float:
        return await self._call("heartbeat", *args, **kwargs)

    async def sweep_expired(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return await self._call("sweep_expired", *args, **kwargs)

    async def release(self, *args: Any, **kwargs: Any) -> Task:
        return await self._call("release", *args, **kwargs)

    async def start(self, *args: Any, **kwargs: Any) -> Task:
        return await self._call("start", *args, **kwargs)

    async def complete(self, *args: Any, **kwargs: Any) -> Completion:
        return await self._call("complete", *args, **kwargs)

    async def fail(self, *args: Any, **kwargs: Any) -> Failure:
        return await self._call("fail", *args, **kwargs)

    async def reopen(self, *args: Any, **kwargs: Any) -> Completion:
        return await self._call("reopen", *args, **kwargs)
