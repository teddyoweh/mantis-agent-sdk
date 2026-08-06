"""Durable job records — background work that survives the session as *data*.

:mod:`mantis_agent.jobs` is an excellent in-process manager and every byte of
its state is ephemeral: ``Job.task`` is an ``asyncio.Task`` in this event loop,
``Job.events`` is a 40-entry deque, ``JobManager.jobs`` is a plain dict, and the
TUI calls ``cancel_all()`` on exit. Close the terminal and a 40-minute test run
is not just killed — there is no evidence it ever happened.

This module is the first, smallest fix for that: **a durable record, with
execution completely unchanged**. Nothing here spawns, supervises, cancels or
reattaches anything. A job runs exactly as it does today; alongside it we write
one small JSON file per job that outlives the process, which alone buys history,
post-hoc inspection and crash forensics.

The design is deliberately *not* new. :mod:`mantis_agent.workflow_store` already
solved this exact problem for workflow runs — versioned record, sanitized
filenames, redact-before-write, atomic replace, bounded listing, retention
pruning — so the conventions are lifted from it rather than reinvented:

* **Atomic** — the payload goes to a scratch file in the *same* directory,
  is fsynced, then :func:`os.replace`\\ s the target. A reader sees the old
  record or the new one, never half of either, and a crash mid-write leaves
  the previous record intact.
* **Tolerant** — a truncated, hand-edited or future-versioned record reads as
  a miss (``None`` + one debug line), never an exception. History is
  observability; it must not be able to break the thing it observes.
* **Bounded** — :func:`prune_job_records` keeps the newest ``keep`` *terminal*
  records, mirroring ``JobManager._prune``: work still in flight is never
  evicted just because it is old.
* **Redacted** — the retry spec is filtered through
  :func:`mantis_agent.redaction.is_secret_name`, the one shared answer to "does
  this name look like a credential?". No fifth secret matcher is defined here.

Layout, under ``$MANTIS_AGENT_HOME/jobs`` (or ``$MANTIS_JOBS_DIR``)::

    jobs/
      <session-id>/
        <seq>/
          record.json          # this module
          events.jsonl         # later: the activity journal
          stdout.log …         # later: output spill

Each job gets a *directory*, not a file, precisely so the later phases have
somewhere to put a journal and spill files without a migration.

Identity is namespaced: ``Job.id`` is a per-process counter restarting at 1 in
every session, so ``#3`` is ambiguous the moment two sessions' records sit in
one directory. The durable key is ``job:<session-id>/<seq>``, while
:func:`resolve_ref` still resolves a bare ``3`` against the current session so
the user-visible ``/job 3`` keeps working.
"""

from __future__ import annotations

import itertools
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import msgspec

from .paths import get_mantis_agent_dir, sanitize_session_id
from .redaction import is_secret_name

__all__ = [
    "ACTIVE_STATUSES",
    "MAX_RETAINED_RECORDS",
    "RECORD_VERSION",
    "REDACTED",
    "JobRecord",
    "job_dir",
    "jobs_dir",
    "list_job_records",
    "load_job_record",
    "make_job_id",
    "prune_job_records",
    "record_from_job",
    "record_path",
    "redact_spawn_spec",
    "resolve_ref",
    "save_job_record",
]

_log = logging.getLogger("mantis_agent.job_records")

RECORD_VERSION = 1

#: How many *terminal* records survive a prune. Non-terminal ones are exempt.
MAX_RETAINED_RECORDS = 500

# Statuses that mean "this job is still someone's problem". Everything else —
# done/error/cancelled/timeout, and any status a future writer invents — counts
# as terminal and is therefore prunable. Erring this way bounds disk: an unknown
# status can cost a record, never unbounded growth.
ACTIVE_STATUSES = frozenset({
    "pending", "starting", "running", "blocked", "orphaned", "adopted",
})

#: What a credential-shaped value becomes on disk. Same spelling as
#: ``workflow_store.redact_inputs`` so the two artifacts read alike.
REDACTED = "[redacted]"

# Marker in a scratch file's name. Two guards depend on it: the writer builds
# temp names around it, and every reader skips anything carrying it — so a save
# that dies between "temp written" and "renamed" leaves litter that no listing
# can mistake for a record.
_TMP_MARK = ".tmp-"

# pid keeps two processes apart; the counter keeps two threads in one process
# apart (a status update can race a terminal write).
_TMP_SEQ = itertools.count()

# Scratch files this old cannot belong to a live write — a record is a few
# hundred bytes and never takes an hour — so they are a crashed writer's litter
# and pruning sweeps them.
_TEMP_TTL_S = 3600.0

_DIR_MODE = 0o700
_FILE_MODE = 0o600

# A hostile or merely long session id must not become a hostile path. Names are
# capped as well as sanitized: some filesystems cap a component at 255 bytes and
# a rejected mkdir would turn a logging concern into a failed job.
_MAX_COMPONENT = 96

# Depth cap for the recursive spawn-spec redactor. Bounds both pathological
# nesting and a self-referential dict, which would otherwise recurse forever.
_MAX_SPEC_DEPTH = 6

_ENCODER = msgspec.json.Encoder()


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


class JobRecord(msgspec.Struct):
    """One job, as it survives the process that ran it.

    ``session_id`` and ``seq`` are required because a record without them
    cannot be placed, named or resolved — a file missing either is corrupt, and
    decoding fails loudly into a ``None`` from :func:`load_job_record` rather
    than quietly inventing an identity.

    Every other field is defaulted so a partially-populated record is legal:
    the interesting moments to write one are *before* the job has a pid, an
    exit code or a result.

    Unlike ``workflow_store``'s record this struct does **not** set
    ``omit_defaults``. A record is read by humans doing crash forensics, and
    ``version`` (whose value equals its default for the whole life of v1) must
    be visible in the file rather than implied by its absence.

    Note what is *not* here: the job's result text. Bounded, redacted output
    belongs in a spill file next to this record, which is the next phase's job;
    ``result_ref``/``result_bytes`` are the seam it will fill in, and ``error``
    carries the short failure string in the meantime.
    """

    session_id: str
    seq: int                                  # per-session ordinal == Job.id
    version: int = RECORD_VERSION
    job_id: str = ""                          # namespaced: "job:<session>/<seq>"
    kind: str = "task"                        # task | workflow | watch | shell | agent
    mode: str = "inproc"                      # inproc | durable | worker | detached
    desc: str = ""
    status: str = "running"
    created_at: float = msgspec.field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    cwd: str = ""
    pid: int | None = None                    # worker only
    pgid: int | None = None
    exit_code: int | None = None
    tool_count: int = 0
    turn_count: int = 0
    stream_count: int = 0
    last_event: str = ""
    last_tool: str = ""
    result_ref: str = ""                      # spill file path (later phase)
    result_bytes: int = 0
    events_ref: str = ""                      # journal path (later phase)
    workflow_id: str = ""                     # preserved for /workflows linkage
    error: str = ""
    spawn_spec: dict[str, Any] | None = None  # for retry; redacted on write

    @property
    def is_terminal(self) -> bool:
        return self.status not in ACTIVE_STATUSES

    @property
    def elapsed_s(self) -> float:
        """Wall-clock seconds the job has been (or was) running.

        ``Job.elapsed_s`` uses ``time.monotonic()``, which is meaningless across
        processes; a durable record has to use wall clock, with all the caveats
        that implies about clock changes. Never negative."""

        start = self.started_at if self.started_at is not None else self.created_at
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - start)

    def summary(self) -> str:
        """One line, shaped like ``Job.summary`` so the two read the same."""

        wf = f" · run {self.workflow_id}" if self.workflow_id else ""
        return (f"{self.job_id or make_job_id(self.session_id, self.seq)} · "
                f"{self.kind} · {self.status} · {int(self.elapsed_s)}s{wf} · "
                f"{self.desc[:50]}")


_DECODER = msgspec.json.Decoder(JobRecord)


# ---------------------------------------------------------------------------
# Identity and paths
# ---------------------------------------------------------------------------


def jobs_dir() -> Path:
    """``$MANTIS_JOBS_DIR`` or ``$MANTIS_AGENT_HOME/jobs``.

    Not created here — creation is lazy, per :func:`mantis_agent.paths.ensure_dir`'s
    convention, so importing this module never touches the filesystem."""

    override = os.environ.get("MANTIS_JOBS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return get_mantis_agent_dir() / "jobs"


def _safe_component(value: Any) -> str:
    """One path component derived from untrusted text.

    :func:`~mantis_agent.paths.sanitize_session_id` already strips everything
    outside ``[A-Za-z0-9._-]``, which is *not* sufficient on its own here: it
    leaves ``.`` and ``..`` untouched, and unlike ``workflow_store`` — where the
    id is only ever part of a filename — a session id is a whole directory
    component, so ``..`` would climb out of the jobs tree."""

    s = sanitize_session_id(str(value))[:_MAX_COMPONENT]
    return "_" if s in ("", ".", "..") else s


def _seq_component(seq: Any) -> str:
    try:
        return str(int(seq))
    except (TypeError, ValueError):
        return _safe_component(seq)


def make_job_id(session_id: str, seq: Any) -> str:
    """The durable, cross-session key: ``job:<session-id>/<seq>``.

    Built from the *sanitized* session component so the id and the path it
    resolves to can never disagree."""

    return f"job:{_safe_component(session_id)}/{_seq_component(seq)}"


def job_dir(session_id: str, seq: Any) -> Path:
    return jobs_dir() / _safe_component(session_id) / _seq_component(seq)


def record_path(session_id: str, seq: Any) -> Path:
    return job_dir(session_id, seq) / "record.json"


def resolve_ref(ref: Any, *, session_id: str = "") -> tuple[str, int] | None:
    """Resolve a user- or model-supplied job reference to ``(session, seq)``.

    Accepts every spelling the two id schemes produce::

        resolve_ref(3, session_id="s1")        -> ("s1", 3)   # /job 3
        resolve_ref("#3", session_id="s1")     -> ("s1", 3)
        resolve_ref("job:s2/7")                -> ("s2", 7)   # cross-session
        resolve_ref("s2/7")                    -> ("s2", 7)

    ``None`` for anything else — including a bare ordinal with no session to
    resolve it against, which is genuinely ambiguous rather than an error."""

    text = str(ref).strip()
    if not text:
        return None
    if text.lower().startswith("job:"):
        text = text[4:]
    sid, sep, tail = text.rpartition("/")
    if not sep:
        sid, tail = session_id, text
    tail = tail.lstrip("#").strip()
    if not tail.isdigit():
        return None
    if not str(sid).strip():
        return None
    return _safe_component(sid), int(tail)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_spawn_spec(spec: Any, _depth: int = 0) -> Any:
    """Copy ``spec`` with credential-shaped *names* blanked, recursively.

    A spawn spec exists so a job can be retried, which means it holds prompts,
    argv and environment overrides — and it is written to a file that outlives
    the session. Same instinct as ``workflow_store.redact_inputs``, and the same
    matcher as everything else in the codebase
    (:func:`mantis_agent.redaction.is_secret_name`), because a fifth private
    list of secret words is how one of them ends up missing
    ``AWS_SECRET_ACCESS_KEY``.

    Values that msgspec cannot encode are stringified rather than raising: a
    record that fails to serialize loses the whole job's history to protect a
    field nobody will read."""

    if _depth > _MAX_SPEC_DEPTH:
        return "[truncated]"
    if isinstance(spec, dict):
        out: dict[str, Any] = {}
        for k, v in spec.items():
            key = str(k)
            out[key] = REDACTED if is_secret_name(key) else redact_spawn_spec(v, _depth + 1)
        return out
    if isinstance(spec, (list, tuple)):
        return [redact_spawn_spec(v, _depth + 1) for v in spec]
    if spec is None or isinstance(spec, (str, int, float, bool)):
        return spec
    return str(spec)


# ---------------------------------------------------------------------------
# Building a record from a live job
# ---------------------------------------------------------------------------


def record_from_job(job: Any, *, session_id: str, mode: str = "inproc",
                    cwd: str = "", spawn_spec: Any = None) -> JobRecord:
    """Snapshot a live :class:`~mantis_agent.jobs.Job` as a durable record.

    Duck-typed on purpose: this module must not import ``jobs.py`` (execution
    stays untouched, and the dependency would run the wrong way), and the same
    call has to work for a worker-side stand-in later.

    ``Job.started`` is a ``time.monotonic()`` reading — useless in another
    process — so wall-clock timestamps are re-derived from the job's elapsed
    time. ``Job.result`` holds the failure string for the non-``done`` terminal
    states, which is exactly what ``error`` is for; the success text is left for
    the spill file."""

    now = time.time()
    elapsed = float(getattr(job, "elapsed_s", 0.0) or 0.0)
    status = str(getattr(job, "status", "running") or "running")
    result = str(getattr(job, "result", "") or "")
    terminal = status not in ACTIVE_STATUSES
    rec = JobRecord(
        session_id=str(session_id),
        seq=int(getattr(job, "id", 0) or 0),
        kind=str(getattr(job, "kind", "task") or "task"),
        mode=mode,
        desc=str(getattr(job, "desc", "") or ""),
        status=status,
        created_at=now - elapsed,
        started_at=now - elapsed,
        ended_at=now if terminal else None,
        cwd=str(cwd or ""),
        tool_count=int(getattr(job, "tool_count", 0) or 0),
        turn_count=int(getattr(job, "turn_count", 0) or 0),
        stream_count=int(getattr(job, "stream_count", 0) or 0),
        last_event=str(getattr(job, "last_event", "") or ""),
        last_tool=str(getattr(job, "last_tool", "") or ""),
        result_bytes=len(result.encode("utf-8", "replace")),
        workflow_id=str(getattr(job, "workflow_id", "") or ""),
        error=result if terminal and status != "done" else "",
        spawn_spec=spawn_spec,
    )
    rec.job_id = make_job_id(rec.session_id, rec.seq)
    return rec


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def _mkdir(path: Path) -> Path:
    """Create ``path`` and make it private. ``chmod`` failures are tolerated —
    Windows and some network filesystems don't implement POSIX modes, and a job
    record is worth more than the mode bit."""

    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(_DIR_MODE)
    except OSError:  # pragma: no cover — platform dependent
        pass
    return path


def _open_scratch(target: Path) -> tuple[Path, int]:
    """A freshly created, ``0o600``, exclusively-owned sibling scratch file.

    ``O_EXCL`` rather than a plain truncating open, so a name that somehow
    already exists is never written through — the one file we must not clobber
    is another writer's in-flight temp. pid keeps two processes apart and the
    counter keeps two threads apart, so a collision means stale litter from a
    dead process whose pid was reused: retry under a new name, and let the
    third failure raise, since by then something is genuinely wrong."""

    last: OSError | None = None
    for _ in range(3):
        tmp = target.with_name(f"{target.name}{_TMP_MARK}{os.getpid()}-{next(_TMP_SEQ)}")
        try:
            return tmp, os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        except FileExistsError as e:  # pragma: no cover — needs a pid collision
            last = e
    raise last if last is not None else OSError("cannot create scratch file")


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` all-or-nothing, mode ``0o600``.

    A record is the only surviving artifact of the job, so a partial write is
    worse than no write: a truncated file is unparseable, which loses the entire
    history for that job rather than one stale update. The payload goes to a
    sibling scratch file (same directory, hence same filesystem, hence a rename
    that cannot degrade to a copy), is flushed to the platter, and only then
    replaces the target — :func:`os.replace` is atomic on POSIX and Windows.

    The file is created ``0o600`` by :func:`os.open` rather than chmod'ed after
    the fact: a record can hold a prompt, and there must be no window in which
    it is world-readable."""

    tmp, fd = _open_scratch(target)
    try:
        try:
            fh = os.fdopen(fd, "wb")
        except BaseException:  # pragma: no cover — only a bad fd gets here
            os.close(fd)
            raise
        with fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover — tmpfs/pseudo-fs in containers
                # Unavailable on some filesystems. The replace below is still
                # atomic there; only the power-loss guarantee is lost.
                pass
        os.replace(str(tmp), str(target))
    except BaseException:
        # Never leave litter — including on KeyboardInterrupt, which is exactly
        # the "crash mid-write" case this function exists for.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_job_record(rec: JobRecord, *, path: Any = None, prune: bool | None = None) -> str:
    """Persist one record; returns the written path.

    Best-effort at the call site: a failure here is a logging problem, never a
    failed job — same contract as ``workflow_store.save_run``.

    Two fields are normalized on the way out. ``version`` becomes
    :data:`RECORD_VERSION`, because this writer writes that format whatever the
    struct was carrying, and ``job_id`` is recomputed from
    ``session_id``/``seq`` so the id in the file always names the path the file
    is at. ``spawn_spec`` is redacted here rather than at the call site: the
    guarantee worth having is *nothing unredacted is ever written*, and that
    can only be enforced at the write.

    Pruning runs when the record is terminal (that is the moment history grows)
    unless ``prune`` says otherwise."""

    rec.version = RECORD_VERSION
    rec.job_id = make_job_id(rec.session_id, rec.seq)
    if rec.spawn_spec is not None:
        redacted = redact_spawn_spec(rec.spawn_spec)
        rec.spawn_spec = redacted if isinstance(redacted, dict) else {"spec": redacted}

    if path is not None:
        target = Path(path)
        _mkdir(target.parent)
    else:
        target = record_path(rec.session_id, rec.seq)
        # Walk down rather than mkdir(parents=True): pathlib creates missing
        # parents with the default mode, so each level has to be made private
        # explicitly or ~/.mantis-agent/jobs itself ends up 0o755.
        root = _mkdir(jobs_dir())
        _mkdir(root / _safe_component(rec.session_id))
        _mkdir(target.parent)

    _atomic_write_bytes(target, msgspec.json.format(_ENCODER.encode(rec), indent=2))

    if rec.is_terminal if prune is None else prune:
        try:
            prune_job_records()
        except OSError as e:  # pragma: no cover — retention must not fail a save
            _log.debug("job record prune failed: %r", e)
    return str(target)


# ---------------------------------------------------------------------------
# Load and list
# ---------------------------------------------------------------------------


def _decode(raw: bytes, where: Any) -> JobRecord | None:
    """Decode one record, or ``None`` with a debug line.

    Everything unreadable lands here: a file truncated by a crash, a record
    hand-edited into invalid JSON, a field of the wrong type, or a record from
    a *newer* mantis. The last one is refused deliberately — a v2 record read
    through v1 eyes would be reported with fields that quietly mean something
    else, and a miss is far safer than a confident lie."""

    try:
        rec = _DECODER.decode(raw)
    except (msgspec.DecodeError, msgspec.ValidationError, ValueError) as e:
        _log.debug("unreadable job record %s: %r", where, e)
        return None
    if rec.version < 1 or rec.version > RECORD_VERSION:
        _log.debug("job record %s has unsupported version %d", where, rec.version)
        return None
    return rec


def load_job_record(ref: Any, *, session_id: str = "", path: Any = None) -> JobRecord | None:
    """Load one record by reference. ``None`` if absent, corrupt or too new.

    ``ref`` is anything :func:`resolve_ref` understands, so both ``3`` (with
    ``session_id``) and ``"job:s2/7"`` work."""

    if path is not None:
        p = Path(path)
    else:
        resolved = resolve_ref(ref, session_id=session_id)
        if resolved is None:
            return None
        p = record_path(*resolved)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        _log.debug("unreadable job record %s: %r", p, e)
        return None
    return _decode(raw, p)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _sort_key(p: Path) -> tuple[float, int]:
    # mtime first, then the ordinal, so records written inside one filesystem
    # timestamp tick still list newest-first instead of in glob order.
    try:
        seq = int(p.parent.name)
    except ValueError:
        seq = 0
    return (_mtime(p), seq)


def _record_paths(session_id: str | None = None) -> list[Path]:
    root = jobs_dir()
    if not root.is_dir():
        return []
    pattern = (f"{_safe_component(session_id)}/*/record.json" if session_id
               else "*/*/record.json")
    try:
        found = list(root.glob(pattern))
    except OSError as e:  # pragma: no cover — unreadable jobs dir
        _log.debug("cannot scan job records in %s: %r", root, e)
        return []
    # Scratch files are named ``record.json.tmp-…`` so the glob already excludes
    # them; the explicit guard covers a half-written *directory* too, and costs
    # one substring test per candidate.
    keep = [p for p in found if _TMP_MARK not in p.name and _TMP_MARK not in p.parent.name]
    return sorted(keep, key=_sort_key, reverse=True)


def list_job_records(limit: int = 50, *, session_id: str | None = None) -> list[JobRecord]:
    """Newest-first records, at most ``limit`` of them.

    Corrupt records, half-written scratch files and records from a newer mantis
    are skipped: a single bad file must not break the history view. Reading is
    lazy — it stops at ``limit`` rather than decoding the whole tree, which is
    what keeps this usable until the SQLite discovery index lands."""

    out: list[JobRecord] = []
    cap = max(1, int(limit))
    for p in _record_paths(session_id):
        try:
            raw = p.read_bytes()
        except OSError as e:
            _log.debug("skipping unreadable job record %s: %r", p, e)
            continue
        rec = _decode(raw, p)
        if rec is None:
            continue
        out.append(rec)
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _sweep_temp(paths: list[Path], now: float) -> None:
    """Delete scratch files a crashed writer left behind.

    A record is a few hundred bytes; no live write holds a temp file for an
    hour. Anything older than that is litter, and litter that is never swept is
    a disk leak with extra steps."""

    for d in paths:
        try:
            candidates = list(d.glob(f"*{_TMP_MARK}*"))
        except OSError:  # pragma: no cover
            continue
        for p in candidates:
            if now - _mtime(p) < _TEMP_TTL_S:
                continue
            try:
                p.unlink()
            except OSError:  # pragma: no cover
                pass


def prune_job_records(keep: int | None = None) -> int:
    """Drop the oldest *terminal* job directories past ``keep``. Returns how
    many were removed.

    ``keep`` defaults to :data:`MAX_RETAINED_RECORDS` read at *call* time, not
    bound into the signature, so a retention setting can move without every
    caller having to thread it through.

    Two rules, both borrowed from ``JobManager._prune`` so the durable history
    and the in-memory one age the same way:

    * A job that is still active is never evicted, however old it is, and does
      not count against the budget. Deleting the record of running work is how
      you lose the ability to find it again.
    * A record whose status this version does not recognize counts as terminal.
      The alternative — exempting unknown statuses — turns one buggy writer into
      unbounded disk use.

    The whole job *directory* goes, not just ``record.json``: the directory is
    the unit of retention because the later phases put a journal and spill files
    in it, and a pruned record with an orphaned 60 MB log is the worst of both."""

    root = jobs_dir()
    if not root.is_dir():
        return 0
    budget = max(0, MAX_RETAINED_RECORDS if keep is None else int(keep))
    paths = _record_paths()
    _sweep_temp([p.parent for p in paths], time.time())
    if len(paths) <= budget:
        return 0

    removed = 0
    for p in paths:  # newest-first
        try:
            rec = _decode(p.read_bytes(), p)
        except OSError:  # pragma: no cover — raced with another pruner
            continue
        # An undecodable record has no status to protect it, and keeping bytes
        # nothing can read only fills the disk — it ages out like a terminal one.
        if rec is not None and not rec.is_terminal:
            continue
        if budget > 0:
            budget -= 1
            continue
        # Guaranteed under ``root``: every path came from a glob rooted there.
        try:
            shutil.rmtree(p.parent)
            removed += 1
        except OSError as e:  # pragma: no cover
            _log.debug("cannot prune job dir %s: %r", p.parent, e)

    _prune_empty_sessions(root)
    return removed


def _prune_empty_sessions(root: Path) -> None:
    """Remove session directories left empty by pruning. Best-effort, and
    ``rmdir`` deliberately — it refuses to delete a directory that still holds
    a job, so a race with a concurrent writer fails safely."""

    try:
        children = list(root.iterdir())
    except OSError:  # pragma: no cover
        return
    for d in children:
        if not d.is_dir():
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:  # pragma: no cover — non-empty or racing
            pass
