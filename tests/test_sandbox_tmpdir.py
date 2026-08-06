"""One shared ``/tmp`` for every sandboxed process is a hole, not a temp dir.

``SandboxPolicy.roots_for`` appends ``tempfile.gettempdir()`` and bubblewrap
binds it read-write, so today every parallel subagent and every swarm candidate
writes into the same directory and can read everything the others left there.
These tests pin the replacement: a private directory per session, a private
subdirectory per child, and a reclaim path for the sessions that die without
getting to clean up.

The sweep tests use *real* processes — a genuinely dead pid from a subprocess
that has exited, and this test process itself as the live one — because the
interesting failure of an orphan sweep is that it believes a mock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from mantis_agent.redaction import build_child_env
from mantis_agent.sandbox import SandboxPolicy, SandboxUnavailable, seatbelt_profile
from mantis_agent.sandbox_tmpdir import (
    GONE,
    LIVE,
    TMP_ENV_NAMES,
    UNKNOWN,
    OwnerRecord,
    PrivateTmpError,
    cleanup,
    owner_state,
    private_tmpdir,
    process_start_time,
    sweep_orphans,
    tmp_env,
    tmp_root,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own ``~/.mantis-agent``; nothing touches the real one."""
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "state"))
    return tmp_path


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _owner_file(session: str) -> Path:
    return tmp_root() / ".owners" / f"{session}.json"


def _write_owner(session: str, record: OwnerRecord) -> None:
    path = _owner_file(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.to_json(), encoding="utf-8")


def _dead_pid() -> int:
    """A pid that is definitely not running: spawn it, reap it, return it.

    A real exited process rather than a large made-up number, so the liveness
    check is exercised the way it will be in production — including the reaping,
    since an unwaited zombie still answers to ``kill(pid, 0)``.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait(timeout=60)
    return proc.pid


# -- separation --------------------------------------------------------------


def test_two_sessions_get_distinct_directories() -> None:
    a = private_tmpdir("session-a")
    b = private_tmpdir("session-b")

    assert a != b
    # Neither is inside the other: containment would make "private" a lie the
    # moment one session was handed its directory as a writable root.
    assert a not in b.parents and b not in a.parents
    assert a.parent == b.parent == tmp_root()


def test_sessions_cannot_see_each_others_files() -> None:
    a = private_tmpdir("session-a")
    b = private_tmpdir("session-b")

    (a / "notes.txt").write_text("secret-from-a", encoding="utf-8")
    (b / "notes.txt").write_text("secret-from-b", encoding="utf-8")

    # Same filename, no collision, and neither directory contains the other's
    # contents — which is exactly what a shared /tmp fails at.
    assert (a / "notes.txt").read_text(encoding="utf-8") == "secret-from-a"
    assert (b / "notes.txt").read_text(encoding="utf-8") == "secret-from-b"
    assert [p.name for p in a.iterdir()] == ["notes.txt"]
    assert "secret-from-b" not in (a / "notes.txt").read_text(encoding="utf-8")


def test_children_are_isolated_from_their_siblings() -> None:
    """The case that actually happens: two subagents of the SAME session."""
    one = private_tmpdir("ses", child_id="sub-1")
    two = private_tmpdir("ses", child_id="sub-2")
    parent = private_tmpdir("ses")

    assert one != two
    assert one.parent == two.parent == parent
    (one / "scratch").write_text("one", encoding="utf-8")
    assert not (two / "scratch").exists()
    assert [p.name for p in two.iterdir()] == []


def test_child_ids_cannot_traverse_out_of_the_session() -> None:
    box = private_tmpdir("ses", child_id="../../escape")
    assert tmp_root() / "ses" == box.parent
    assert not (tmp_root().parent / "escape").exists()


def test_the_directory_is_not_the_shared_system_temp_dir() -> None:
    box = private_tmpdir("ses")
    assert box != Path(tempfile.gettempdir())
    assert box.parent == tmp_root()
    assert tmp_root().parent.name == "state"        # under the mantis state dir


def test_creation_is_idempotent_and_keeps_contents() -> None:
    first = private_tmpdir("ses", child_id="c")
    (first / "keep.txt").write_text("x", encoding="utf-8")
    second = private_tmpdir("ses", child_id="c")

    assert first == second
    assert (second / "keep.txt").read_text(encoding="utf-8") == "x"


# -- modes -------------------------------------------------------------------


def test_directories_are_0o700_despite_a_permissive_umask() -> None:
    """``Path.mkdir(mode=…)`` is masked by the umask, so the chmod is the load
    bearing part — a 0o022 umask would otherwise yield a world-readable 0o755."""
    old = os.umask(0o022)
    try:
        child = private_tmpdir("ses", child_id="sub")
    finally:
        os.umask(old)

    assert _mode(tmp_root()) == 0o700
    assert _mode(tmp_root() / "ses") == 0o700
    assert _mode(child) == 0o700


def test_owner_records_are_not_world_readable() -> None:
    private_tmpdir("ses")
    assert _mode(_owner_file("ses")) == 0o600
    # ...and they live outside the box handed to the child, so a compromised
    # child cannot forge itself an immortal owner.
    assert not (tmp_root() / "ses" / ".owners").exists()


def test_a_symlinked_target_is_refused(tmp_path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    tmp_root().mkdir(parents=True, exist_ok=True)
    (tmp_root() / "ses").symlink_to(victim, target_is_directory=True)

    with pytest.raises(PrivateTmpError):
        private_tmpdir("ses")
    # PrivateTmpError is a SandboxUnavailable, so callers already handling a
    # missing backend handle this too.
    assert issubclass(PrivateTmpError, SandboxUnavailable)


# -- the environment ---------------------------------------------------------


def test_tmp_env_sets_all_three_spellings() -> None:
    box = private_tmpdir("ses")
    env = tmp_env(box)
    assert set(env) == set(TMP_ENV_NAMES)
    assert env == {"TMPDIR": str(box), "TMP": str(box), "TEMP": str(box)}
    assert all(isinstance(v, str) for v in env.values())


def test_tmp_env_agrees_with_the_child_env_builder() -> None:
    """``build_child_env(tmpdir=…)`` rewrites the same trio; if the two ever
    disagreed, one spelling would point back at the shared temp dir."""
    box = private_tmpdir("ses")
    built = build_child_env({"PATH": "/bin"}, tmpdir=str(box))
    for name, value in tmp_env(box).items():
        assert built[name] == value


def test_a_real_subprocess_writes_into_the_private_directory() -> None:
    box = private_tmpdir("ses")
    env = {**build_child_env(), **tmp_env(box)}
    proc = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        env=env, capture_output=True, text=True, timeout=60, check=False)
    assert proc.stdout.strip() == str(box), proc.stderr


def test_two_children_write_the_same_filename_without_colliding() -> None:
    """The acceptance criterion, run for real: same name, two processes, no
    collision and no visibility."""
    boxes = [private_tmpdir("ses", child_id=f"sub-{i}") for i in (1, 2)]
    for i, box in enumerate(boxes):
        subprocess.run(
            [sys.executable, "-c",
             "import os, tempfile; "
             f"open(os.path.join(tempfile.gettempdir(), 'note.txt'), 'w').write('{i}')"],
            env={**build_child_env(), **tmp_env(box)},
            capture_output=True, text=True, timeout=60, check=False)

    assert (boxes[0] / "note.txt").read_text(encoding="utf-8") == "0"
    assert (boxes[1] / "note.txt").read_text(encoding="utf-8") == "1"


# -- cleanup -----------------------------------------------------------------


def test_cleanup_is_idempotent() -> None:
    box = private_tmpdir("ses", child_id="c")
    (box / "f.txt").write_text("x", encoding="utf-8")

    assert cleanup("ses") is True
    assert not (tmp_root() / "ses").exists()
    # Again, and again: teardown, an atexit hook and the next sweep may all
    # reach the same directory.
    assert cleanup("ses") is True
    assert cleanup("ses") is True
    assert cleanup("never-existed") is True


def test_cleanup_removes_the_owner_record_too() -> None:
    private_tmpdir("ses")
    assert _owner_file("ses").exists()
    cleanup("ses")
    assert not _owner_file("ses").exists()


def test_cleanup_touches_only_the_named_session() -> None:
    keep = private_tmpdir("keep")
    private_tmpdir("drop")
    (keep / "f").write_text("x", encoding="utf-8")

    cleanup("drop")

    assert not (tmp_root() / "drop").exists()
    assert (keep / "f").read_text(encoding="utf-8") == "x"


def test_cleanup_of_a_child_leaves_the_session_alone() -> None:
    child = private_tmpdir("ses", child_id="sub-1")
    sibling = private_tmpdir("ses", child_id="sub-2")

    assert cleanup("ses", child_id="sub-1") is True

    assert not child.exists()
    assert sibling.exists()
    assert _owner_file("ses").exists()       # the session still owns its box


def test_cleanup_survives_read_only_leftovers() -> None:
    """pip caches and git object stores leave read-only files behind; that is a
    normal thing to find in a temp dir, not a reason for teardown to fail."""
    box = private_tmpdir("ses")
    sub = box / "cache"
    sub.mkdir()
    (sub / "wheel").write_text("x", encoding="utf-8")
    os.chmod(sub / "wheel", 0o400)
    os.chmod(sub, 0o500)
    try:
        assert cleanup("ses") is True
    finally:
        if sub.exists():
            os.chmod(sub, 0o700)
    assert not box.exists()


def test_cleanup_never_raises_on_a_hostile_path(monkeypatch) -> None:
    """Cancellation-safety in practice: teardown must not grow a new failure
    mode. Whatever the filesystem does, cleanup returns a bool."""
    private_tmpdir("ses")

    def boom(*_a, **_k):
        raise OSError("device is on fire")

    monkeypatch.setattr("shutil.rmtree", boom)
    assert cleanup("ses", retries=2) is False    # honest about the outcome
    assert (tmp_root() / "ses").exists()


# -- process identity --------------------------------------------------------


def test_process_start_time_is_stable_for_a_live_process() -> None:
    first = process_start_time(os.getpid())
    assert first, "this platform must be able to identify its own process"
    time.sleep(0.01)
    assert process_start_time(os.getpid()) == first


def test_process_start_time_is_none_for_a_dead_or_absurd_pid() -> None:
    assert process_start_time(_dead_pid()) is None
    assert process_start_time(0) is None
    assert process_start_time(-1) is None


def test_owner_state_reads_live_gone_and_unknown() -> None:
    me = OwnerRecord(session="s", pid=os.getpid(),
                     start=process_start_time(os.getpid()))
    assert owner_state(me) == LIVE
    assert owner_state(OwnerRecord(session="s", pid=_dead_pid(), start="x")) == GONE
    # Same pid, different incarnation — the pid-reuse case.
    assert owner_state(OwnerRecord(session="s", pid=os.getpid(),
                                   start="ps:not-this-boot")) == UNKNOWN
    # No start token recorded at all: unverifiable, therefore not deletable.
    assert owner_state(OwnerRecord(session="s", pid=os.getpid())) == UNKNOWN
    assert owner_state(None) == UNKNOWN


def test_proc_stat_parsing_survives_a_hostile_process_name() -> None:
    """Runs on every platform even though only Linux reads /proc: the parser is
    the part with the classic bug, and a mac-only suite would never see it. A
    process can name itself ``evil ) 9 9 9``, so anything tokenising the line
    from the left reads a forged starttime out of the comm field."""
    from mantis_agent.sandbox_tmpdir import _linux_start_from_stat

    tail = " ".join(f"f{i}" for i in range(3, 45))     # fields 3..44
    assert _linux_start_from_stat(f"1234 (python3) {tail}") == "linux:f22"
    assert _linux_start_from_stat(f"1234 (evil ) 9 9 9) {tail}") == "linux:f22"
    assert _linux_start_from_stat("1234 (short) S 1 2") is None
    assert _linux_start_from_stat("no parens here") is None


def test_owner_record_round_trips_and_survives_corruption() -> None:
    rec = OwnerRecord(session="s", pid=42, start="linux:99", created=1.5)
    assert OwnerRecord.parse(rec.to_json()) == rec
    assert OwnerRecord.parse("{not json") is None
    assert OwnerRecord.parse("[]") is None
    assert OwnerRecord.parse(json.dumps({"pid": "nope"})) is None


# -- the orphan sweep --------------------------------------------------------


def test_sweep_removes_a_dead_owners_directory() -> None:
    private_tmpdir("dead")
    _write_owner("dead", OwnerRecord(session="dead", pid=_dead_pid(),
                                     start="whatever", created=time.time()))

    assert sweep_orphans() == ["dead"]
    assert not (tmp_root() / "dead").exists()
    assert not _owner_file("dead").exists()


def test_sweep_spares_a_live_owners_directory() -> None:
    """The one that must never be deleted: this very process's session."""
    live = private_tmpdir("live")
    (live / "work.txt").write_text("in progress", encoding="utf-8")

    assert sweep_orphans() == []
    assert (live / "work.txt").read_text(encoding="utf-8") == "in progress"


def test_sweep_spares_a_reused_pid() -> None:
    """The classic bug, stated: a pid check alone says "alive" (something does
    hold that pid) or "mine" depending on which way you squint. With the start
    token recorded, the mismatch is visible — and an owner we can no longer
    identify is never deleted on, only reported as unknown."""
    box = private_tmpdir("reused")
    (box / "work.txt").write_text("someone's data", encoding="utf-8")
    _write_owner("reused", OwnerRecord(session="reused", pid=os.getpid(),
                                       start="ps:a-different-boot",
                                       created=time.time()))

    assert sweep_orphans() == []
    assert (box / "work.txt").read_text(encoding="utf-8") == "someone's data"


def test_sweep_separates_the_dead_from_the_living_in_one_pass() -> None:
    private_tmpdir("live")
    private_tmpdir("dead-1")
    private_tmpdir("dead-2")
    dead = _dead_pid()
    for name in ("dead-1", "dead-2"):
        _write_owner(name, OwnerRecord(session=name, pid=dead, start="x",
                                       created=time.time()))

    assert sweep_orphans() == ["dead-1", "dead-2"]
    assert (tmp_root() / "live").exists()


def test_sweep_gives_a_recordless_directory_a_grace_period() -> None:
    """A sweep racing another process's `private_tmpdir` must lose."""
    box = tmp_root() / "just-created"
    box.mkdir(parents=True)

    assert sweep_orphans() == []
    assert box.exists()

    old = time.time() - 10_000
    os.utime(box, (old, old))
    assert sweep_orphans() == ["just-created"]
    assert not box.exists()


def test_sweep_ignores_the_owners_directory() -> None:
    private_tmpdir("ses")
    owners = tmp_root() / ".owners"
    old = time.time() - 10_000
    os.utime(owners, (old, old))

    assert sweep_orphans() == []
    assert owners.exists()
    assert _owner_file("ses").exists()


def test_sweep_drops_records_whose_directory_is_gone() -> None:
    _write_owner("ghost", OwnerRecord(session="ghost", pid=os.getpid(),
                                      start=process_start_time(os.getpid())))
    assert sweep_orphans() == []
    assert not _owner_file("ghost").exists()


def test_sweep_is_idempotent_and_safe_on_a_missing_root() -> None:
    assert sweep_orphans() == []            # nothing created yet at all
    private_tmpdir("dead")
    _write_owner("dead", OwnerRecord(session="dead", pid=_dead_pid(), start="x"))
    assert sweep_orphans() == ["dead"]
    assert sweep_orphans() == []


def test_a_resumed_session_reclaims_a_dead_owners_directory() -> None:
    """Otherwise a resumed session keeps a dead owner forever and its scratch is
    swept out from under it on the next startup."""
    private_tmpdir("ses")
    _write_owner("ses", OwnerRecord(session="ses", pid=_dead_pid(), start="x"))

    private_tmpdir("ses")                   # resume

    assert owner_state(OwnerRecord.parse(_owner_file("ses").read_text())) == LIVE
    assert sweep_orphans() == []


# -- what must not change ----------------------------------------------------


def test_dev_null_stays_writable() -> None:
    """The sandbox special-cases /dev/null and a private TMPDIR has no business
    disturbing it: a shell that cannot write to /dev/null breaks on the first
    `2>/dev/null` an agent types."""
    assert "/dev/null" in SandboxPolicy().roots_for()
    profile = seatbelt_profile(SandboxPolicy(enabled=True))
    assert '(allow file-write-data (literal "/dev/null"))' in profile
    assert "/dev/(null" in profile
