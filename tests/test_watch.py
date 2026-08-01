"""Watch — a background command whose stdout lines stream in as events.

Covers the engine (batching, rate limiting, timeout, process-group teardown),
the tool surface, and the TUI notification path that turns an event into both a
user-visible line and model context.
"""

from __future__ import annotations

import asyncio
import os
import sys

import anyio
import pytest

from mantis_agent.jobs import JobManager
from mantis_agent.watch import (
    _coerce_timeout_ms,
    make_watch_stop_tool,
    make_watch_tool,
    run_watch,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="watch uses bash + process groups")


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


def _collect() -> tuple[list[str], object]:
    """An ``emit`` that records every event it is handed."""
    seen: list[str] = []

    async def emit(text: str) -> None:
        seen.append(text)

    return seen, emit


# -- engine ---------------------------------------------------------------------


def test_each_line_becomes_an_event() -> None:
    seen, emit = _collect()

    async def go():
        # Sleep between lines so each clears the 200ms batching window.
        return await run_watch(
            "echo one; sleep 0.35; echo two; sleep 0.35; echo three",
            "three lines", emit=emit, timeout_ms=10_000)

    summary = anyio.run(go)
    assert seen == ["one", "two", "three"]
    assert summary == 'Watch "three lines" stream ended (3 events)'


def test_lines_within_the_batch_window_coalesce() -> None:
    """A traceback printed all at once must arrive as ONE notification, not N."""
    seen, emit = _collect()

    async def go():
        return await run_watch(
            "printf 'Traceback:\\n  line a\\n  line b\\n'",
            "burst", emit=emit, timeout_ms=10_000)

    anyio.run(go)
    assert seen == ["Traceback:\n  line a\n  line b"]


def test_stderr_does_not_notify_but_is_logged(tmp_path) -> None:
    """Stdout is the event stream; stderr goes to the log file only."""
    seen, emit = _collect()
    log = tmp_path / "mon.log"

    async def go():
        return await run_watch(
            "echo out; echo boom >&2", "split streams", emit=emit,
            timeout_ms=10_000, log_path=str(log))

    anyio.run(go)
    assert seen == ["out"]
    assert "boom" in log.read_text()


def test_nonzero_exit_is_reported_as_script_failure() -> None:
    seen, emit = _collect()

    async def go():
        return await run_watch("echo hi; exit 3", "failing", emit=emit,
                                 timeout_ms=10_000)

    summary = anyio.run(go)
    assert seen == ["hi"]
    assert summary == 'Watch "failing" script failed (exit 3) (1 event)'


def test_timeout_stops_an_unbounded_watch() -> None:
    """`tail -f`-shaped scripts never exit; the deadline must end the watch."""
    seen, emit = _collect()

    async def go():
        return await run_watch("while true; do sleep 5; done", "idle forever",
                                 emit=emit, timeout_ms=1000)

    summary = anyio.run(go)
    assert seen == []
    assert "timed out" in summary


def test_flood_is_rate_limited_and_stopped() -> None:
    """A raw log pipe is stopped, not allowed to flood the conversation."""
    seen, emit = _collect()

    async def go():
        # No sleep: batching coalesces 40 lines per event but the events
        # themselves still come faster than the budget allows.
        return await run_watch("while true; do echo noise; done", "firehose",
                                 emit=emit, timeout_ms=30_000)

    summary = anyio.run(go)
    assert "stopped" in summary and "too loose" in summary
    # Stopped at the budget rather than running to the 30s timeout.
    assert 0 < len(seen) < 60


def test_steady_stream_is_coalesced_not_rate_limited() -> None:
    """Batching must absorb a chatty-but-reasonable stream. A watch emitting
    50 lines/s is a normal build log, not a firehose — if the 200ms window
    didn't collapse those into ~5 events/s it would trip the limiter and the
    watch would die on legitimate output."""
    seen, emit = _collect()

    async def go():
        return await run_watch(
            "for i in $(seq 1 150); do echo line $i; sleep 0.02; done",
            "steady", emit=emit, timeout_ms=20_000)

    summary = anyio.run(go)
    assert "stream ended" in summary
    assert sum(len(e.splitlines()) for e in seen) == 150
    assert len(seen) < 20  # coalesced well below one event per line


def test_process_group_dies_with_the_watch() -> None:
    """Killing the shell must take the pipeline's children with it, or a
    `tail -f | grep` leaves `tail` running and holding the file."""
    marker = []

    async def emit(text: str) -> None:
        marker.append(text)

    async def go():
        await run_watch(
            "sleep 30 & echo $!; wait", "leaky child", emit=emit,
            timeout_ms=1200)

    anyio.run(go)
    assert marker, "expected the child pid on stdout"
    child_pid = int(marker[0].strip())
    # The grandchild must be gone (or reaped) now that the watch ended.
    with pytest.raises(OSError):
        for _ in range(20):
            os.kill(child_pid, 0)
            import time
            time.sleep(0.05)
        raise AssertionError(f"pid {child_pid} survived the watch")


def test_cancelling_the_job_kills_the_process() -> None:
    async def go():
        jm = JobManager()
        job = jm.spawn(
            run_watch("echo up; sleep 30", "cancel me",
                        emit=lambda _t: asyncio.sleep(0), timeout_ms=30_000),
            desc="cancel me", kind="watch")
        await asyncio.sleep(0.6)
        assert jm.cancel(job.id)
        await jm.wait(job.id, timeout_s=5)
        return job

    job = anyio.run(go)
    assert job.status == "cancelled"


# -- JobManager streaming substrate ---------------------------------------------


def test_emit_records_events_and_fires_on_stream() -> None:
    streamed: list[tuple[int, str]] = []

    async def go():
        jm = JobManager(on_stream=lambda job, text: streamed.append((job.id, text)))
        job = jm.spawn(asyncio.sleep(0.01), desc="d", kind="watch")
        await jm.emit(job, "first")
        await jm.emit(job, "second")
        return job

    job = anyio.run(go)
    assert streamed == [(job.id, "first"), (job.id, "second")]
    assert job.stream_count == 2
    # Recorded on the job too, so /job <id> shows recent activity.
    texts = [text for _ts, text in job.events]
    assert "first" in texts and "second" in texts


def test_broken_stream_callback_does_not_break_the_watch() -> None:
    def boom(job, text):
        raise RuntimeError("notifier exploded")

    async def go():
        jm = JobManager(on_stream=boom)
        job = jm.spawn(asyncio.sleep(0.01), desc="d", kind="watch")
        await jm.emit(job, "still counted")
        return job

    job = anyio.run(go)
    assert job.stream_count == 1


def test_persistent_job_has_no_runtime_backstop() -> None:
    """max_runtime_s=None must mean *no* deadline, not a crash."""

    async def go():
        jm = JobManager()
        job = jm.spawn(asyncio.sleep(0.05), desc="p", kind="watch",
                       max_runtime_s=None)
        return await jm.wait(job.id, timeout_s=5)

    assert anyio.run(go).status == "done"


# -- tool surface ---------------------------------------------------------------


def test_watch_tool_spawns_a_job_and_streams() -> None:
    async def go():
        jm = JobManager()
        tool = make_watch_tool(jm)
        out = await tool.fn(command="echo alpha", description="greet")
        assert "job #1" in out and "greet" in out
        job = jm.get(1)
        assert job.kind == "watch"
        await jm.wait(1, timeout_s=10)
        return job

    job = anyio.run(go)
    assert job.stream_count == 1
    assert "stream ended" in job.result


def test_watch_tool_rejects_missing_description() -> None:
    async def go():
        tool = make_watch_tool(JobManager())
        return await tool.fn(command="echo x")

    assert "description" in anyio.run(go)


def test_watch_stop_cancels_a_running_watch() -> None:
    async def go():
        jm = JobManager()
        start, stop = make_watch_tool(jm), make_watch_stop_tool(jm)
        await start.fn(command="sleep 30", description="long watch",
                       persistent=True)
        await asyncio.sleep(0.3)
        out = await stop.fn(job_id=1)
        await jm.wait(1, timeout_s=5)
        return out, jm.get(1)

    out, job = anyio.run(go)
    assert "Stopping watch #1" in out
    assert job.status == "cancelled"


def test_watch_stop_on_unknown_job() -> None:
    async def go():
        return await make_watch_stop_tool(JobManager()).fn(job_id=99)

    assert "no job #99" in anyio.run(go)


@pytest.mark.parametrize(("given", "expected"), [
    (None, 300_000),
    ("bogus", 300_000),
    (0, 1000),            # clamped up to the floor
    (9_999_999, 3_600_000),  # clamped down to the ceiling
    (45_000, 45_000),
])
def test_timeout_coercion(given, expected) -> None:
    assert _coerce_timeout_ms(given) == expected


# -- TUI notification path ------------------------------------------------------


def test_watch_event_injects_context_and_announces() -> None:
    """A stream event must reach the model as meta context AND the user as a
    line — and must NOT look like a terminal <background-job> status."""
    from mantis_agent.tui import MantisTUI

    tui = MantisTUI.__new__(MantisTUI)
    tui.messages = []
    tui._job_context_backlog = []
    tui._turn_active = False
    announced: list[str] = []
    tui._watch_notify = lambda job, text: announced.append(text)

    class _Job:
        id, desc, stream_count = 7, "errors in deploy.log", 1

    tui._on_job_stream(_Job(), "ERROR: boom")
    assert len(tui.messages) == 1
    content = tui.messages[0].content
    assert "<watch-event job=7>" in content
    assert "ERROR: boom" in content
    assert "status=" not in content  # a ping, not a completion
    assert announced == ["ERROR: boom"]


def test_watch_event_mid_turn_waits_in_the_backlog() -> None:
    from mantis_agent.tui import MantisTUI

    tui = MantisTUI.__new__(MantisTUI)
    tui.messages = []
    tui._job_context_backlog = []
    tui._turn_active = True
    tui._watch_notify = None

    class _Job:
        id, desc, stream_count = 3, "watch", 1

    tui._on_job_stream(_Job(), "event")
    assert tui.messages == [] and len(tui._job_context_backlog) == 1


def test_format_watch_event_line_summarises_multiline() -> None:
    from mantis_agent.tui import format_watch_event_line

    class _Job:
        id, desc, stream_count = 2, "ci", 5

    line = format_watch_event_line(_Job(), "Traceback:\n  a\n  b", width=100)
    assert "watch" in line and "#2" in line and "Traceback:" in line
    assert "+2 more" in line


# -- permissions ----------------------------------------------------------------


def test_watch_is_treated_as_a_shell_surface() -> None:
    """`watch` runs `bash -lc`, so it must hit the same danger classifier as
    `bash` — otherwise it is a way to run `rm -rf /` without the prompt."""
    from mantis_agent.permissions import _is_dangerous_bash, _is_shell_tool

    tool = make_watch_tool(JobManager())
    assert _is_shell_tool(tool)
    assert _is_dangerous_bash(tool, {"command": "rm -rf /", "description": "d"})
    assert not _is_dangerous_bash(
        tool, {"command": "tail -f app.log", "description": "d"})
