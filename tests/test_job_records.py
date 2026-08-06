"""Durable job records: round-trip, atomicity, corruption, retention, redaction.

Every test runs against a temp ``MANTIS_AGENT_HOME``: no session, no event
loop, no writes outside tmp_path. Execution is deliberately untouched by this
layer, so nothing here spawns a job — the one test that uses a real
:class:`~mantis_agent.jobs.Job` does so only to prove the record snapshot
matches the live object's fields.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time

import msgspec
import pytest

from mantis_agent.job_records import (
    MAX_RETAINED_RECORDS,
    RECORD_VERSION,
    REDACTED,
    JobRecord,
    job_dir,
    jobs_dir,
    list_job_records,
    load_job_record,
    make_job_id,
    prune_job_records,
    record_from_job,
    record_path,
    redact_spawn_spec,
    resolve_ref,
    save_job_record,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MANTIS_JOBS_DIR", raising=False)
    yield


def _rec(session="s1", seq=1, **kw) -> JobRecord:
    return JobRecord(session_id=session, seq=seq, **kw)


# -- round-trip ------------------------------------------------------------------


def test_round_trip_preserves_every_field() -> None:
    rec = _rec(
        seq=3, kind="workflow", mode="durable", desc="pytest -q", status="done",
        created_at=1000.0, started_at=1001.0, ended_at=1061.5, cwd="/repo",
        pid=4411, pgid=4411, exit_code=0, tool_count=7, turn_count=2,
        stream_count=11, last_event="done", last_tool="Bash",
        result_ref="result.txt", result_bytes=1234, events_ref="events.jsonl",
        workflow_id="wf-9", error="",
    )
    path = save_job_record(rec)

    got = load_job_record(3, session_id="s1")
    assert got is not None
    assert msgspec.structs.asdict(got) == msgspec.structs.asdict(rec)
    assert got.job_id == "job:s1/3"
    assert got.elapsed_s == pytest.approx(60.5)
    assert got.is_terminal is True

    # The path is the namespaced layout, and the file names the path it is at.
    assert path == str(jobs_dir() / "s1" / "3" / "record.json")
    on_disk = json.loads((jobs_dir() / "s1" / "3" / "record.json").read_text())
    assert on_disk["version"] == RECORD_VERSION      # visible, not implied
    assert on_disk["job_id"] == "job:s1/3"


def test_save_normalizes_version_and_job_id() -> None:
    # A struct carrying a stale version and a wrong id still lands correct: the
    # writer owns both fields.
    rec = _rec(seq=5, version=99, job_id="job:someone-else/1")
    save_job_record(rec)
    got = load_job_record("job:s1/5")
    assert got is not None
    assert (got.version, got.job_id) == (RECORD_VERSION, "job:s1/5")


def test_record_survives_the_process_that_wrote_it(tmp_path) -> None:
    # The whole point: a record read back with no live JobManager anywhere.
    save_job_record(_rec(seq=1, desc="40 minute suite", status="running"))
    assert load_job_record(1, session_id="s1").desc == "40 minute suite"


def test_jobs_dir_honors_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MANTIS_JOBS_DIR", str(tmp_path / "elsewhere"))
    save_job_record(_rec(seq=1))
    assert (tmp_path / "elsewhere" / "s1" / "1" / "record.json").is_file()
    assert load_job_record(1, session_id="s1") is not None


# -- identity --------------------------------------------------------------------


def test_job_id_namespacing() -> None:
    assert make_job_id("ses-01J8", 3) == "job:ses-01J8/3"
    # Two sessions both start at #1 and must not collide.
    save_job_record(_rec(session="ses-a", seq=1, desc="A"))
    save_job_record(_rec(session="ses-b", seq=1, desc="B"))
    assert load_job_record("job:ses-a/1").desc == "A"
    assert load_job_record("job:ses-b/1").desc == "B"


@pytest.mark.parametrize(
    ("ref", "session", "expected"),
    [
        (3, "s1", ("s1", 3)),                    # /job 3
        ("3", "s1", ("s1", 3)),
        ("#3", "s1", ("s1", 3)),
        (" #12 ", "s1", ("s1", 12)),
        ("job:s2/7", "s1", ("s2", 7)),           # cross-session wins
        ("job:s2/7", "", ("s2", 7)),
        ("s2/7", "", ("s2", 7)),
        ("JOB:s2/7", "", ("s2", 7)),
        ("3", "", None),                         # ambiguous: no session
        ("", "s1", None),
        ("nope", "s1", None),
        ("job:s2/abc", "s1", None),
    ],
)
def test_resolve_ref(ref, session, expected) -> None:
    assert resolve_ref(ref, session_id=session) == expected


def test_short_ref_resolves_within_the_current_session_only() -> None:
    save_job_record(_rec(session="s1", seq=2, desc="mine"))
    assert load_job_record(2, session_id="s1").desc == "mine"
    assert load_job_record(2, session_id="s2") is None


def test_hostile_session_id_cannot_escape_the_jobs_tree() -> None:
    root = jobs_dir()
    for hostile in ("../../etc", "..", ".", "", "a/../../b", "x" * 400):
        d = job_dir(hostile, 1)
        assert root.resolve() in d.resolve().parents, d
        assert len(d.parent.name) <= 96
    save_job_record(_rec(session="../../etc", seq=1, desc="nice try"))
    assert not (root.parent.parent / "etc").exists()
    # It is still findable — sanitized, not dropped.
    assert list_job_records()[0].desc == "nice try"


# -- permissions and atomicity ---------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_directories_and_files_are_private() -> None:
    save_job_record(_rec(seq=1))
    p = record_path("s1", 1)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    for d in (jobs_dir(), jobs_dir() / "s1", p.parent):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_crash_between_temp_write_and_rename_keeps_the_old_record(monkeypatch) -> None:
    save_job_record(_rec(seq=1, desc="first", status="running"))
    p = record_path("s1", 1)
    before = p.read_bytes()

    def boom(*_a, **_k):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_job_record(_rec(seq=1, desc="second", status="done"))

    assert p.read_bytes() == before                     # never half-written
    assert load_job_record(1, session_id="s1").desc == "first"
    assert list(p.parent.glob("*.tmp-*")) == []         # scratch cleaned up
    assert len(list_job_records()) == 1


def test_temp_files_are_never_mistaken_for_records() -> None:
    save_job_record(_rec(seq=1, desc="real"))
    # Litter a SIGKILL would leave: a scratch file next to the record.
    (record_path("s1", 1).with_name("record.json.tmp-999-0")).write_text("{partial")
    recs = list_job_records()
    assert [r.desc for r in recs] == ["real"]


def test_prune_sweeps_stale_scratch_files() -> None:
    save_job_record(_rec(seq=1))
    litter = record_path("s1", 1).with_name("record.json.tmp-999-0")
    litter.write_text("{partial")
    prune_job_records()
    assert litter.exists()                              # too young to judge

    os.utime(litter, (time.time() - 7200, time.time() - 7200))
    prune_job_records()
    assert not litter.exists()
    assert load_job_record(1, session_id="s1") is not None


# -- corruption and versioning ---------------------------------------------------


def test_missing_record_is_a_miss_not_an_error() -> None:
    assert load_job_record(42, session_id="s1") is None
    assert load_job_record("job:nope/1") is None
    assert load_job_record("garbage") is None
    assert list_job_records() == []


@pytest.mark.parametrize("blob", [
    b"",                                    # zero-length: crashed before write
    b'{"session_id": "s1", "seq":',         # truncated mid-write
    b"not json at all",
    b'{"seq": 1}',                          # missing required session_id
    b'{"session_id": "s1", "seq": "three"}',  # wrong type
    b'[{"session_id": "s1", "seq": 1}]',    # right data, wrong shape
])
def test_corrupt_record_returns_none(blob) -> None:
    p = record_path("s1", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)
    assert load_job_record(1, session_id="s1") is None
    assert list_job_records() == []          # and never breaks the history view


def test_record_from_a_newer_mantis_is_refused() -> None:
    p = record_path("s1", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": "s1", "seq": 1, "version": RECORD_VERSION + 1,
                             "status": "done"}))
    assert load_job_record(1, session_id="s1") is None
    assert list_job_records() == []


def test_record_without_a_version_reads_as_v1() -> None:
    # Defaulted rather than rejected: absence means "the first format".
    p = record_path("s1", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": "s1", "seq": 1, "desc": "old"}))
    got = load_job_record(1, session_id="s1")
    assert got is not None and got.version == 1 and got.desc == "old"


def test_unknown_fields_from_a_future_writer_are_ignored() -> None:
    p = record_path("s1", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": "s1", "seq": 1, "version": 1,
                             "desc": "ok", "nonesuch": {"deep": [1, 2]}}))
    assert load_job_record(1, session_id="s1").desc == "ok"


def test_listing_skips_corrupt_and_keeps_the_rest() -> None:
    for i in (1, 2, 3):
        save_job_record(_rec(seq=i, desc=f"job{i}", status="done"))
    record_path("s1", 2).write_bytes(b"{truncated")
    assert sorted(r.desc for r in list_job_records()) == ["job1", "job3"]


# -- listing ---------------------------------------------------------------------


def test_list_is_newest_first_and_respects_limit() -> None:
    for i in range(1, 6):
        save_job_record(_rec(seq=i, desc=f"job{i}", status="done"))
        os.utime(record_path("s1", i), (1000.0 + i, 1000.0 + i))
    assert [r.seq for r in list_job_records()] == [5, 4, 3, 2, 1]
    assert [r.seq for r in list_job_records(limit=2)] == [5, 4]
    assert [r.seq for r in list_job_records(limit=0)] == [5]   # clamped, not empty


def test_list_can_filter_to_one_session() -> None:
    save_job_record(_rec(session="s1", seq=1, desc="mine"))
    save_job_record(_rec(session="s2", seq=1, desc="theirs"))
    assert len(list_job_records()) == 2
    assert [r.desc for r in list_job_records(session_id="s2")] == ["theirs"]


def test_list_ties_break_on_ordinal() -> None:
    for i in (1, 2, 3):
        save_job_record(_rec(seq=i, status="done"))
        os.utime(record_path("s1", i), (1000.0, 1000.0))   # identical mtimes
    assert [r.seq for r in list_job_records()] == [3, 2, 1]


# -- retention -------------------------------------------------------------------


def test_prune_keeps_the_newest_terminal_records() -> None:
    for i in range(1, 11):
        save_job_record(_rec(seq=i, status="done"), prune=False)
        os.utime(record_path("s1", i), (1000.0 + i, 1000.0 + i))
    assert prune_job_records(keep=4) == 6
    assert [r.seq for r in list_job_records()] == [10, 9, 8, 7]
    assert not job_dir("s1", 6).exists()
    assert prune_job_records(keep=4) == 0            # idempotent


def test_prune_never_evicts_active_work() -> None:
    # Oldest of all, still running: age must not lose it.
    save_job_record(_rec(seq=1, status="running"), prune=False)
    os.utime(record_path("s1", 1), (1.0, 1.0))
    for i in range(2, 8):
        save_job_record(_rec(seq=i, status="done"), prune=False)
        os.utime(record_path("s1", i), (1000.0 + i, 1000.0 + i))

    assert prune_job_records(keep=2) == 4
    kept = {r.seq for r in list_job_records()}
    assert kept == {1, 7, 6}                        # running + newest two done


@pytest.mark.parametrize("status", ["pending", "starting", "running", "blocked",
                                    "orphaned", "adopted"])
def test_active_statuses_are_exempt_from_retention(status) -> None:
    save_job_record(_rec(seq=1, status=status), prune=False)
    assert prune_job_records(keep=0) == 0
    assert load_job_record(1, session_id="s1") is not None


@pytest.mark.parametrize("status", ["done", "error", "cancelled", "timeout",
                                    "who-knows"])
def test_terminal_and_unknown_statuses_age_out(status) -> None:
    # An unrecognized status counts as terminal: one buggy writer must not be
    # able to fill the disk.
    save_job_record(_rec(seq=1, status=status), prune=False)
    assert prune_job_records(keep=0) == 1
    assert load_job_record(1, session_id="s1") is None


def test_prune_removes_the_whole_job_directory() -> None:
    save_job_record(_rec(seq=1, status="done"), prune=False)
    spill = job_dir("s1", 1) / "stdout.log"
    spill.write_text("x" * 1000)                    # a later phase's spill file
    assert prune_job_records(keep=0) == 1
    assert not spill.exists() and not job_dir("s1", 1).exists()
    assert not (jobs_dir() / "s1").exists()         # empty session dir too


def test_prune_drops_undecodable_records() -> None:
    save_job_record(_rec(seq=1, status="done"), prune=False)
    record_path("s1", 1).write_bytes(b"{truncated")
    assert prune_job_records(keep=0) == 1


def test_terminal_save_prunes_automatically(monkeypatch) -> None:
    monkeypatch.setattr("mantis_agent.job_records.MAX_RETAINED_RECORDS", 2)
    for i in range(1, 6):
        save_job_record(_rec(seq=i, status="done"))
        os.utime(record_path("s1", i), (1000.0 + i, 1000.0 + i))
    assert [r.seq for r in list_job_records()] == [5, 4]


def test_running_save_does_not_prune(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("mantis_agent.job_records.prune_job_records",
                        lambda *a, **k: calls.append(a) or 0)
    save_job_record(_rec(seq=1, status="running"))
    assert calls == []
    save_job_record(_rec(seq=1, status="done"))
    assert len(calls) == 1


def test_prune_on_missing_dir_is_a_noop() -> None:
    assert prune_job_records() == 0
    assert MAX_RETAINED_RECORDS == 500


# -- redaction -------------------------------------------------------------------


def test_spawn_spec_is_redacted_on_write() -> None:
    rec = _rec(seq=1, status="done", spawn_spec={
        "argv": ["pytest", "-q"],
        "ANTHROPIC_API_KEY": "sk-ant-real-value",
        "env": {"GH_TOKEN": "ghp_real", "PATH": "/usr/bin",
                "nested": {"aws_secret_access_key": "AKIA", "keep": "me"}},
        "prompt": "summarize the diff",
        "items": [{"password": "hunter2"}, "plain"],
    })
    save_job_record(rec)

    raw = record_path("s1", 1).read_text()
    for leaked in ("sk-ant-real-value", "ghp_real", "AKIA", "hunter2"):
        assert leaked not in raw

    got = load_job_record(1, session_id="s1")
    assert got.spawn_spec["ANTHROPIC_API_KEY"] == REDACTED
    assert got.spawn_spec["env"]["GH_TOKEN"] == REDACTED
    assert got.spawn_spec["env"]["PATH"] == "/usr/bin"
    assert got.spawn_spec["env"]["nested"]["aws_secret_access_key"] == REDACTED
    assert got.spawn_spec["env"]["nested"]["keep"] == "me"
    assert got.spawn_spec["items"][0]["password"] == REDACTED
    assert got.spawn_spec["argv"] == ["pytest", "-q"]
    assert got.spawn_spec["prompt"] == "summarize the diff"
    # The caller's struct is redacted too — the value must not survive in memory
    # under the illusion that it was scrubbed.
    assert rec.spawn_spec["ANTHROPIC_API_KEY"] == REDACTED


def test_redactor_terminates_on_cycles_and_odd_values() -> None:
    cyclic: dict = {"token": "t"}
    cyclic["self"] = cyclic
    out = redact_spawn_spec(cyclic)
    assert out["token"] == REDACTED
    assert "[truncated]" in json.dumps(out)

    assert redact_spawn_spec({"obj": object()})["obj"].startswith("<object")
    assert redact_spawn_spec(["a", 1, None, True]) == ["a", 1, None, True]


def test_non_dict_spawn_spec_is_wrapped_not_dropped() -> None:
    rec = _rec(seq=1)
    rec.spawn_spec = {"": ["a"]}
    save_job_record(rec)
    assert load_job_record(1, session_id="s1").spawn_spec == {"": ["a"]}


# -- snapshotting a live Job -----------------------------------------------------


def test_record_from_job_matches_the_live_job() -> None:
    from mantis_agent.jobs import Job

    job = Job(id=4, desc="pytest -q", kind="workflow", workflow_id="wf-1")
    job.tool_count, job.turn_count, job.stream_count = 3, 1, 9
    job.last_tool, job.last_event = "Bash", "running tests"

    rec = record_from_job(job, session_id="ses-01J8", cwd="/repo")
    assert rec.job_id == "job:ses-01J8/4"
    assert (rec.seq, rec.kind, rec.desc) == (4, "workflow", "pytest -q")
    assert (rec.tool_count, rec.turn_count, rec.stream_count) == (3, 1, 9)
    assert (rec.last_tool, rec.last_event) == ("Bash", "running tests")
    assert rec.workflow_id == "wf-1" and rec.cwd == "/repo"
    assert rec.status == "running" and rec.is_terminal is False
    assert rec.ended_at is None
    # started_at is wall clock derived from the job's monotonic clock.
    assert abs(rec.started_at - time.time()) < 5.0

    job.status, job.result = "error", "RuntimeError: kaput"
    done = record_from_job(job, session_id="ses-01J8")
    assert done.status == "error" and done.error == "RuntimeError: kaput"
    assert done.ended_at is not None and done.is_terminal is True
    assert done.result_bytes == len("RuntimeError: kaput")


def test_record_from_job_keeps_the_success_text_out_of_the_record() -> None:
    from mantis_agent.jobs import Job

    job = Job(id=1, desc="big")
    job.status, job.result = "done", "x" * 5000
    rec = record_from_job(job, session_id="s1")
    assert rec.error == "" and rec.result_bytes == 5000
    assert rec.result_ref == ""                     # spill file lands later
    save_job_record(rec)
    assert len(record_path("s1", 1).read_bytes()) < 2000


def test_record_from_job_tolerates_a_job_shaped_stand_in() -> None:
    class Stub:
        id = 2
        desc = "worker"
        status = "done"

    rec = record_from_job(Stub(), session_id="s1", mode="worker")
    assert (rec.seq, rec.mode, rec.kind) == (2, "worker", "task")
    assert rec.job_id == "job:s1/2"


def test_summary_reads_like_job_summary() -> None:
    rec = _rec(seq=3, kind="watch", status="running", desc="src/**/*.py",
               workflow_id="wf-2", started_at=time.time() - 12)
    rec.job_id = make_job_id(rec.session_id, rec.seq)
    line = rec.summary()
    assert line.startswith("job:s1/3 · watch · running · 12s · run wf-2 · src/**/*.py")
