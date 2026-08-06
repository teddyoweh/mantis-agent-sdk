"""Private per-session temporary directories for sandboxed subprocesses.

``SandboxPolicy.roots_for`` ends its list with ``tempfile.gettempdir()``, and
``bubblewrap_argv`` binds that directory read-**write**. So every sandboxed
process on the machine — every parallel subagent, every swarm worktree
candidate, every background shell — shares one writable ``/tmp``. Two things
follow, and both are bad:

* one agent can read another's intermediate files, which is a confidentiality
  hole between siblings that the writable-roots policy claims to have closed;
* a compromised agent gets a *stable, predictable* staging location that
  survives the death of the process that created it.

The fix is boring on purpose: give each session its own directory under the
mantis state dir, mode ``0o700``, and hand the child ``TMPDIR``/``TMP``/``TEMP``
pointing at it. Per-*child* subdirectories go one level further, so parallel
siblings are isolated from **each other** and not merely from other sessions —
which is the case that actually shows up, since siblings are the processes that
run at the same time on the same box.

    from mantis_agent.sandbox_tmpdir import private_tmpdir, tmp_env

    box = private_tmpdir(session_id, child_id="sub-3")
    subprocess.run(argv, env={**build_child_env(), **tmp_env(box)})

This module is deliberately standalone: it creates directories and reports
paths, and it is the caller's job to feed those paths to ``writable_roots`` (or
to bubblewrap's ``--bind <private> /tmp``). ``/dev/null`` is untouched by any of
this — :mod:`.sandbox` special-cases it, and a private ``TMPDIR`` neither needs
nor is allowed to change that.

Lifetime
--------

Sessions end in two ways. The clean way is :func:`cleanup`, which is idempotent
and never raises, because it runs on the teardown path where an exception would
mask the *real* reason the session is ending. The other way is a crash, a
``SIGKILL``, or a laptop lid — and for those :func:`sweep_orphans` runs at the
next startup and reclaims directories whose owning process is gone.

"Is that process gone?" is where this kind of code is usually wrong. A bare
``os.kill(pid, 0)`` answers "does *a* process have that pid", not "is *my*
process still alive": pids are recycled, and on a busy box recycling takes
minutes, not months. So each session records its owner as ``(pid, start-time)``
and liveness is only ever asserted when **both** match. Everything else is
:data:`UNKNOWN`, and this sweep deletes only on certainty — see
:func:`owner_state` for exactly which way each ambiguous case falls and why.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .paths import get_mantis_agent_dir, sanitize_session_id
from .sandbox import SandboxUnavailable

__all__ = [
    "GONE",
    "LIVE",
    "ORPHAN_GRACE_SECONDS",
    "OwnerRecord",
    "PrivateTmpError",
    "TMP_ENV_NAMES",
    "UNKNOWN",
    "cleanup",
    "owner_state",
    "private_tmpdir",
    "process_start_time",
    "sweep_orphans",
    "tmp_env",
    "tmp_root",
]


class PrivateTmpError(SandboxUnavailable):
    """A private temp directory could not be created safely.

    Derives from :class:`~mantis_agent.sandbox.SandboxUnavailable` because that
    is what it means to the caller: confinement was requested and cannot be
    provided, so the same ``fail_if_unavailable`` reasoning applies.
    """


#: The three spellings of "where scratch files go". They travel together —
#: mirrors ``redaction._TMP_NAMES``, which rewrites the same trio when it builds
#: a child environment.
TMP_ENV_NAMES = ("TMPDIR", "TMP", "TEMP")

#: Directories with no owner record younger than this are left alone. The record
#: is written immediately after the directory, so a missing record means either
#: (a) we caught another process mid-creation — a race we must not win — or
#: (b) something removed the record. Five minutes settles (a) and costs nothing.
ORPHAN_GRACE_SECONDS = 300.0

#: Results of :func:`owner_state`.
LIVE = "live"
GONE = "gone"
UNKNOWN = "unknown"

_DIR_MODE = 0o700


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
#
# <state>/tmp/<session>/<child>          the boxes themselves
# <state>/tmp/.owners/<session>.json     who owns each box
#
# The owner records live *outside* the session directories on purpose. A
# sandboxed child is handed its box as TMPDIR and can write anything it likes
# inside it; if the record it is judged by were in there too, the first thing a
# compromised child would do is forge itself an immortal owner. Dot-prefixed
# names are skipped by the sweep, so `.owners` is not mistaken for a session.


def tmp_root() -> Path:
    """The parent of every private temp directory: ``<state>/tmp``.

    Under the mantis state dir rather than the system temp dir, which is the
    whole point — the system temp dir is the shared thing being replaced.
    """

    return get_mantis_agent_dir() / "tmp"


def _owners_dir() -> Path:
    return tmp_root() / ".owners"


def _owner_path(session: str) -> Path:
    return _owners_dir() / f"{session}.json"


def _mkdir_private(path: Path) -> Path:
    """``mkdir -p`` with mode ``0o700`` actually enforced.

    ``Path.mkdir(mode=…)`` is masked by the process umask, so a 0o022 umask
    silently yields 0o755 and the "private" directory is world-readable. The
    chmod is not belt-and-braces; it is the only part that works. A symlink at
    the target is refused outright: following one would place the session's
    scratch — and its writable-root grant — wherever the link points.
    """

    if path.is_symlink():
        raise PrivateTmpError(f"refusing to use a symlinked temp directory: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        os.chmod(path, _DIR_MODE)
    except OSError as exc:
        raise PrivateTmpError(f"cannot create private temp directory {path}: {exc}") from exc
    return path


# ---------------------------------------------------------------------------
# Owner records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerRecord:
    """Which process a private temp directory belongs to.

    ``start`` is an opaque token from :func:`process_start_time` — not a
    timestamp to do arithmetic on. Compare it for equality and nothing else;
    its format differs per platform and is allowed to change.
    """

    session: str
    pid: int
    start: Optional[str] = None
    created: float = 0.0

    def to_json(self) -> str:
        return json.dumps({"session": self.session, "pid": self.pid,
                           "start": self.start, "created": self.created})

    @classmethod
    def parse(cls, text: str) -> Optional[OwnerRecord]:
        """A record from disk, or ``None`` if it is not one.

        Corrupt records read as *absent* rather than as an error: a truncated
        JSON file is exactly what a crash mid-write leaves behind, and the
        grace period already handles "no record".
        """

        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            pid = int(raw.get("pid", 0))
        except (TypeError, ValueError):
            return None
        start = raw.get("start")
        try:
            created = float(raw.get("created", 0.0))
        except (TypeError, ValueError):
            created = 0.0
        return cls(session=str(raw.get("session", "")), pid=pid,
                   start=str(start) if isinstance(start, str) else None,
                   created=created)


def _linux_start_from_stat(raw: str) -> Optional[str]:
    """Field 22 (``starttime``) of a ``/proc/<pid>/stat`` line.

    Split after the **last** ``)``, never on whitespace from the left: field 2
    is the executable name in parentheses, it is attacker-controlled (a process
    can name itself ``foo ) 1 2 3``), and every parser that tokenises the line
    from the front reads a forged starttime out of a hostile comm field.
    """

    close = raw.rfind(")")
    if close < 0:
        return None
    # fields[0] is field 3 (state), so field 22 sits at index 19.
    fields = raw[close + 1:].split()
    if len(fields) < 20:
        return None
    return f"linux:{fields[19]}"


def process_start_time(pid: int) -> Optional[str]:
    """An opaque token identifying *this incarnation* of ``pid``.

    The pair ``(pid, start-time)`` is unique over the lifetime of a boot in a
    way that ``pid`` alone is not, which is the entire reason the sweep can tell
    "my session is still running" from "something else got that pid".

    Returns ``None`` when the process does not exist **or** when this platform
    cannot answer. Callers must treat those two identically — as *unverifiable*,
    never as *dead*.
    """

    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return _linux_start_from_stat(raw)
    if os.name == "posix":
        # macOS and the BSDs: `ps -o lstart=` prints the start wall-clock time
        # to the second. One-second resolution is coarse, but a pid that is
        # recycled *and* lands in the same second as its predecessor is not a
        # case worth engineering for — and it fails toward UNKNOWN anyway.
        try:
            proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=5,
                                  check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        token = " ".join(proc.stdout.split())
        return f"ps:{token}" if token else None
    return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, therefore *a* process. Not ours to judge —
        # and definitely not ours to declare dead.
        return True
    except OSError:
        return True
    return True


def owner_state(record: Optional[OwnerRecord]) -> str:
    """:data:`LIVE`, :data:`GONE`, or :data:`UNKNOWN` for a recorded owner.

    The three cases, and why each falls the way it does:

    * **no process with that pid** → :data:`GONE`. Certain; nothing can be
      hiding behind a pid that does not exist.
    * **pid exists and its start token matches the record** → :data:`LIVE`.
    * **pid exists, tokens differ or cannot be read** → :data:`UNKNOWN`. The pid
      has been recycled (or the platform cannot say), so the owner is very
      probably gone — but "very probably" is not the standard this sweep
      deletes on. Somebody else's live process now answers to that pid, and
      acting on that identity is exactly the mistake the start token exists to
      prevent. The cost of being wrong here is a leaked directory, reclaimed on
      a later sweep once the recycling process itself exits; the cost of being
      wrong the other way is deleting scratch out from under a running build.
    """

    if record is None or record.pid <= 0:
        return UNKNOWN
    if not _pid_exists(record.pid):
        return GONE
    current = process_start_time(record.pid)
    if current is not None and record.start is not None:
        return LIVE if current == record.start else UNKNOWN
    return UNKNOWN


def _read_owner(session: str) -> Optional[OwnerRecord]:
    try:
        text = _owner_path(session).read_text(encoding="utf-8")
    except OSError:
        return None
    return OwnerRecord.parse(text)


def _write_owner(session: str) -> None:
    """Claim ``session`` for this process, unless a live process holds it.

    Re-claiming on every call would let a short-lived helper steal ownership
    from the long-lived session process and take its scratch directory to the
    grave with it; leaving a stale record forever would make a resumed session
    look orphaned. Claim when the incumbent is not :data:`LIVE`, otherwise leave
    the live owner alone.
    """

    existing = _read_owner(session)
    if existing is not None and existing.pid == os.getpid():
        return
    if owner_state(existing) == LIVE:
        return
    record = OwnerRecord(session=session, pid=os.getpid(),
                         start=process_start_time(os.getpid()),
                         created=time.time())
    path = _owner_path(session)
    try:
        _mkdir_private(path.parent)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(record.to_json(), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)          # atomic: a reader never sees half a record
    except OSError:
        # A missing owner record costs a delayed reclaim, not a broken session.
        # Losing the session's scratch directory over it would be the worse bug.
        pass


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------


def private_tmpdir(session_id: str, child_id: Optional[str] = None) -> Path:
    """Create (or reuse) this session's private temp directory and return it.

    ``<state>/tmp/<session>/`` for the session, ``<state>/tmp/<session>/<child>``
    when ``child_id`` is given. Both are mode ``0o700``.

    Idempotent: calling it again for the same ids returns the same path with its
    contents intact, so a caller does not have to remember whether it already
    ran. Ids are sanitised the same way session filenames are, so a hostile or
    merely careless ``child_id`` cannot traverse out of the session directory.

    Note what the ``0o700`` does and does not buy. Against *other users* on the
    box it is the whole protection. Against a sibling agent — same uid, same
    machine — permissions are no protection at all; what isolates siblings is
    that they are handed different paths and the sandbox grants each of them
    only its own as a writable root. This module supplies the paths that make
    that possible; it cannot enforce it alone.
    """

    session = sanitize_session_id(str(session_id))
    _mkdir_private(tmp_root())
    box = _mkdir_private(tmp_root() / session)
    _write_owner(session)
    if child_id is not None and str(child_id).strip():
        box = _mkdir_private(box / sanitize_session_id(str(child_id)))
    return box


def tmp_env(path: str | Path) -> dict[str, str]:
    """The ``TMPDIR``/``TMP``/``TEMP`` mapping to hand a child.

    All three, always. Setting only ``TMPDIR`` leaves a Windows-flavoured
    toolchain — or anything reading ``TMP`` — writing into the shared system
    temp dir, which is the exact hole this module exists to close, reopened by
    a variable spelling.
    """

    value = str(path)
    return {name: value for name in TMP_ENV_NAMES}


def _force_writable(root: Path) -> None:
    """Restore the write bits under ``root`` so ``rmtree`` can finish.

    A build that leaves a read-only directory behind (pip's wheel cache does,
    git's object store does) is a normal thing to find in a temp directory, not
    an error. ``shutil.rmtree``'s error callback is spelled two different ways
    across the supported Pythons — ``onerror`` is deprecated in 3.12 and
    ``onexc`` does not exist before it — so fix the modes up front instead and
    stay off that fence entirely.
    """

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in list(dirnames) + list(filenames):
            target = os.path.join(dirpath, name)
            try:
                if not os.path.islink(target):
                    os.chmod(target, stat.S_IRWXU)
            except OSError:
                continue
    try:
        os.chmod(root, stat.S_IRWXU)
    except OSError:
        pass


def cleanup(session_id: str, child_id: Optional[str] = None, *,
            retries: int = 3) -> bool:
    """Remove a session's (or one child's) private temp directory.

    Returns whether the directory is gone afterwards — ``True`` also when it was
    never there, because "gone" is the postcondition callers care about.

    Two properties this has to hold, both of which are about *where it runs*
    rather than what it does:

    * **idempotent** — session teardown, an atexit hook and the next startup's
      sweep may all reach the same directory; the second and third calls are
      no-ops, not errors.
    * **cancellation-safe** — it never raises and never awaits. This runs while
      a session is being torn down, frequently because something already went
      wrong; an exception here would replace the real failure with a temp-file
      complaint, and a cancellation point would let teardown abandon the
      directory half-deleted.
    """

    session = sanitize_session_id(str(session_id))
    target = tmp_root() / session
    whole_session = child_id is None or not str(child_id).strip()
    if not whole_session:
        target = target / sanitize_session_id(str(child_id))

    for attempt in range(max(1, retries)):
        if not target.exists() and not target.is_symlink():
            break
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
            break
        except OSError:
            # Second and later attempts: a file we cannot delete is usually a
            # mode problem, not a busy one. Fix the modes, then back off a
            # little for the genuinely-busy case (an editor still flushing, a
            # child that has not quite exited).
            _force_writable(target)
            if attempt + 1 < max(1, retries):
                time.sleep(0.05 * (attempt + 1))

    gone = not target.exists() and not target.is_symlink()
    if whole_session and gone:
        try:
            _owner_path(session).unlink()
        except OSError:
            pass
    return gone


def sweep_orphans(*, grace_seconds: float = ORPHAN_GRACE_SECONDS) -> list[str]:
    """Reclaim temp directories whose owning process is gone. Returns the ids.

    Call this once at startup. A session that was ``SIGKILL``ed, or that went
    down with the machine, never ran :func:`cleanup`, and without a sweep those
    directories accumulate for the life of the install.

    Only :data:`GONE` owners are reclaimed — see :func:`owner_state` for why
    :data:`UNKNOWN` is spared. A directory with no owner record at all is given
    ``grace_seconds`` before it counts as unowned, so a sweep racing another
    process's :func:`private_tmpdir` cannot delete a directory that was created
    a millisecond ago and is about to be used.

    Never raises: a startup path that can be broken by one unreadable directory
    is worse than a leaked temp directory.
    """

    root = tmp_root()
    removed: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return removed

    now = time.time()
    seen: set[str] = set()
    for entry in entries:
        name = entry.name
        # `.owners` and any other bookkeeping dir is not a session.
        if name.startswith("."):
            continue
        seen.add(name)
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
            record = _read_owner(name)
            if record is None:
                age = now - _mtime(entry)
                if age < grace_seconds:
                    continue
            elif owner_state(record) != GONE:
                continue
            if cleanup(name):
                removed.append(name)
        except OSError:
            continue

    # Records whose directory is already gone are pure litter; drop them so the
    # owners dir does not grow without bound across a long-lived install.
    try:
        for rec_path in sorted(_owners_dir().glob("*.json")):
            if rec_path.stem not in seen:
                try:
                    rec_path.unlink()
                except OSError:
                    continue
    except OSError:
        pass
    return removed


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
