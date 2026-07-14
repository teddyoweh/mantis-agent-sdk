"""Background jobs: the JobManager engine, task(run_in_background=true),
job_output, and result injection into the conversation."""

from __future__ import annotations

import asyncio

import anyio
import pytest

from mantis_agent.jobs import JobManager


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


# -- engine ---------------------------------------------------------------------


def test_job_lifecycle_done() -> None:
    events = []

    async def work():
        await asyncio.sleep(0.05)
        return "the answer"

    async def go():
        jm = JobManager(on_event=events.append)
        job = jm.spawn(work(), desc="compute")
        assert job.status == "running" and job.id == 1
        got = await jm.wait(job.id, timeout_s=5)
        assert got.status == "done" and got.result == "the answer"
    anyio.run(go)
    assert len(events) == 1 and events[0].status == "done"


def test_job_error_and_cancel() -> None:
    async def boom():
        raise RuntimeError("kaput")

    async def forever():
        await asyncio.sleep(999)

    async def go():
        jm = JobManager()
        j1 = jm.spawn(boom(), desc="explodes")
        await jm.wait(j1.id, timeout_s=5)
        assert j1.status == "error" and "kaput" in j1.result
        j2 = jm.spawn(forever(), desc="hangs")
        assert jm.cancel(j2.id)
        await jm.wait(j2.id, timeout_s=5)
        assert j2.status == "cancelled"
        assert not jm.cancel(j2.id)         # already terminal
        assert jm.cancel(999) is False      # unknown id
    anyio.run(go)


def test_job_runtime_backstop() -> None:
    async def slow():
        await asyncio.sleep(999)

    async def go():
        jm = JobManager()
        j = jm.spawn(slow(), desc="too slow", max_runtime_s=0.1)
        await jm.wait(j.id, timeout_s=5)
        assert j.status == "timeout"
    anyio.run(go)


def test_broken_on_event_never_kills_result() -> None:
    def bad_hook(job):
        raise RuntimeError("ui died")

    async def work():
        return "ok"

    async def go():
        jm = JobManager(on_event=bad_hook)
        j = jm.spawn(work(), desc="x")
        await jm.wait(j.id, timeout_s=5)
        assert j.status == "done" and j.result == "ok"
    anyio.run(go)


# -- task(run_in_background) + job_output ----------------------------------------


def test_task_background_returns_job_id_then_completes() -> None:
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.subagent import make_job_output_tool, make_task_tool

    async def go():
        jm = JobManager()
        t = make_task_tool(model="mock", provider=MockProvider(default_text="bg result"),
                           tools=[], jobs=jm)
        out = await t.fn(prompt="long investigation", description="dig deep",
                         run_in_background=True)
        assert "background job #1" in out and "job_output" in out
        jo = make_job_output_tool(jm)
        # wait=true blocks until done and returns the subagent's final text
        res = await jo.fn(job_id=1, wait=True)
        assert "done" in res and "bg result" in res
    anyio.run(go)


def test_task_foreground_unchanged_without_flag() -> None:
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.subagent import make_task_tool

    async def go():
        jm = JobManager()
        t = make_task_tool(model="mock", provider=MockProvider(default_text="fg"),
                           tools=[], jobs=jm)
        assert await t.fn(prompt="quick") == "fg"       # no job created
        assert jm.all() == []
    anyio.run(go)


def test_background_task_tracks_live_progress() -> None:
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.subagent import make_task_tool

    async def go():
        jm = JobManager()
        t = make_task_tool(model="mock", provider=MockProvider(default_text="working notes"),
                           tools=[], jobs=jm)
        await t.fn(prompt="long investigation", description="dig deep", run_in_background=True)
        job = await jm.wait(1, timeout_s=5)
        assert job.turn_count >= 1
        assert "assistant: working notes" in job.last_event
        assert list(job.events)
    anyio.run(go)


def test_job_output_unknown_and_running() -> None:
    from mantis_agent.subagent import make_job_output_tool

    async def slow():
        await asyncio.sleep(5)
        return "late"

    async def go():
        jm = JobManager()
        jo = make_job_output_tool(jm)
        assert "no job #7" in await jo.fn(job_id=7)
        j = jm.spawn(slow(), desc="slowpoke")
        out = await jo.fn(job_id=j.id)                   # no wait → status now
        assert "still running" in out
        jm.cancel(j.id)
    anyio.run(go)


# -- TUI integration ----------------------------------------------------------------


def test_completion_injects_meta_message() -> None:
    from mantis_agent.tui import MantisTUI

    async def work():
        return "job findings here"

    async def go():
        t = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                      system=None, max_tokens=1, temperature=None, max_turns=1)
        notes = []
        t._job_notify = notes.append
        j = t._jobs.spawn(work(), desc="research X")
        await t._jobs.wait(j.id, timeout_s=5)
        metas = [m for m in t.messages if getattr(m, "isMeta", False)]
        assert len(metas) == 1
        assert "background-job id=1 status=done" in metas[0].content
        assert "job findings here" in metas[0].content
        assert notes and notes[0].id == 1                # UI hook fired too
    anyio.run(go)


def test_completion_queues_meta_message_during_active_turn() -> None:
    from mantis_agent.tui import MantisTUI

    async def go():
        t = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                      system=None, max_tokens=1, temperature=None, max_turns=1)
        t._turn_active = True
        j = t._jobs.spawn(asyncio.sleep(0, result="job findings here"), desc="research X")
        await t._jobs.wait(j.id, timeout_s=5)
        assert t.messages == []
        assert len(t._job_context_backlog) == 1
        t._turn_active = False
        t._flush_job_context_backlog()
        assert t._job_context_backlog == []
        assert "background-job id=1 status=done" in t.messages[0].content
    anyio.run(go)


def test_job_tools_registered_for_big_models() -> None:
    from mantis_agent.tui import MantisTUI
    t = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    names = {x.name for x in t._build_agent().tools}
    assert "job_output" in names
    task = t._build_agent().tools.get("task")
    assert "run_in_background" in task.input_schema["properties"]


def test_job_records_start_and_terminal_events() -> None:
    async def work():
        return "ok"

    async def go():
        jm = JobManager()
        j = jm.spawn(work(), desc="records")
        assert list(j.events)[0][1] == "starting"
        await jm.wait(j.id, timeout_s=5)
        assert j.status == "done"
        assert list(j.events)[-1][1] == "done"
    anyio.run(go)


def test_job_detail_command_shows_recent_events_and_result() -> None:
    import io

    from rich.console import Console

    from mantis_agent.tui import MantisTUI, SLASH_COMMANDS, build_help_lines

    async def work():
        return "final report"

    async def go():
        t = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                      system=None, max_tokens=1, temperature=None, max_turns=1)
        buf = io.StringIO()
        t.console = Console(file=buf, force_terminal=False, width=120)
        j = t._jobs.spawn(work(), desc="deep research", kind="task:research")
        j.record_event("assistant: investigating")
        await t._jobs.wait(j.id, timeout_s=5)

        t._cmd_job("1")
        out = buf.getvalue()
        assert "Background job #1" in out
        assert "task:research" in out
        assert "deep research" in out
        assert "assistant: investigating" in out
        assert "final report" in out
        assert "/job" in SLASH_COMMANDS
        assert any(command == "/job" for _cat, command, _desc in build_help_lines(SLASH_COMMANDS))
    anyio.run(go)


def test_job_detail_command_can_cancel_running_job() -> None:
    import io

    from rich.console import Console

    from mantis_agent.tui import MantisTUI

    async def slow():
        await asyncio.sleep(999)

    async def go():
        t = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                      system=None, max_tokens=1, temperature=None, max_turns=1)
        buf = io.StringIO()
        t.console = Console(file=buf, force_terminal=False, width=120)
        j = t._jobs.spawn(slow(), desc="slow research")
        t._cmd_job("1 kill")
        await t._jobs.wait(j.id, timeout_s=5)
        assert j.status == "cancelled"
        assert "cancelling job #1" in buf.getvalue()
        assert list(j.events)[-1][1] == "cancelled"
    anyio.run(go)
