"""Parallel ``task`` children are capped by backend: 2 on a single local
inference server (Ollama / llama.cpp / LM Studio / localhost vLLM), 8 on a
hosted API. An explicit cap wins, then ``MANTIS_SUBAGENT_MAX_CONCURRENT``,
then settings ``subagents.maxConcurrentAgents``."""

from __future__ import annotations

import functools

import anyio
import pytest

from mantis_agent.subagent_limits import (
    default_subagent_concurrency,
    is_local_backend,
    subagent_concurrency_cap,
)


@pytest.mark.parametrize("backend", [
    "http://localhost:11434",            # Ollama
    "http://127.0.0.1:8080/v1",          # llama.cpp
    "http://localhost:1234/v1",          # LM Studio
    "http://localhost:8000/v1",          # vLLM on this box
    "http://192.168.1.20:8000/v1",       # the GPU box on the LAN
    "http://[::1]:11434",
])
def test_local_backends_default_to_two(backend) -> None:
    assert is_local_backend(backend=backend)
    assert default_subagent_concurrency(backend=backend) == 2
    assert subagent_concurrency_cap(backend=backend, env={}, settings={}) == 2


@pytest.mark.parametrize("backend", [
    "https://api.openai.com/v1",
    "https://api.together.xyz/v1",
    "https://api.groq.com/openai/v1",
    "anthropic",
    "https://ollama.com",
])
def test_hosted_backends_keep_eight(backend) -> None:
    assert not is_local_backend(backend=backend)
    assert subagent_concurrency_cap(backend=backend, env={}, settings={}) == 8


def test_ollama_cloud_tag_counts_as_hosted() -> None:
    assert not is_local_backend(backend="http://localhost:11434", model="gpt-oss:120b-cloud")


def test_provider_base_url_is_consulted() -> None:
    class _Client:
        base_url = "http://localhost:11434"

    class _Prov:
        client = _Client()

    assert is_local_backend(provider=_Prov())


def test_model_name_routing_when_nothing_else_given(monkeypatch) -> None:
    monkeypatch.delenv("MANTIS_AGENT_BASE_URL", raising=False)
    # A bare Ollama tag routes to the local daemon.
    assert default_subagent_concurrency(model="qwen3:8b") == 2
    assert default_subagent_concurrency(model="gpt-5.4") == 8


def test_precedence_explicit_env_settings_default() -> None:
    local = "http://localhost:11434"
    settings = {"subagents": {"maxConcurrentAgents": 5}}
    env = {"MANTIS_SUBAGENT_MAX_CONCURRENT": "3"}
    assert subagent_concurrency_cap(backend=local, explicit=6, env=env, settings=settings) == 6
    assert subagent_concurrency_cap(backend=local, env=env, settings=settings) == 3
    assert subagent_concurrency_cap(backend=local, env={}, settings=settings) == 5
    assert subagent_concurrency_cap(backend=local, env={}, settings={}) == 2
    # junk falls through; huge values clamp
    assert subagent_concurrency_cap(
        backend=local, env={"MANTIS_SUBAGENT_MAX_CONCURRENT": "lots"}, settings={}) == 2
    assert subagent_concurrency_cap(backend=local, explicit=10_000, env={}, settings={}) == 256


def test_task_tool_gates_parallel_children(monkeypatch) -> None:
    """Four concurrent task calls against a local backend: at most 2 children
    are ever running at once."""
    import mantis_agent.subagent as sub

    monkeypatch.delenv("MANTIS_SUBAGENT_MAX_CONCURRENT", raising=False)
    live = {"now": 0, "peak": 0}

    class _FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, messages):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await anyio.sleep(0.05)
            live["now"] -= 1

    monkeypatch.setattr(sub, "Agent", _FakeAgent)
    task = sub.make_task_tool(model="qwen3:8b", tools=[],
                              backend="http://localhost:11434")

    async def go():
        async with anyio.create_task_group() as tg:
            for i in range(4):
                tg.start_soon(functools.partial(task.fn, prompt=f"p{i}"))

    anyio.run(go)
    assert live["peak"] == 2

    live["peak"] = 0
    hosted = sub.make_task_tool(model="gpt-5.4", tools=[],
                                backend="https://api.openai.com/v1")

    async def go_hosted():
        async with anyio.create_task_group() as tg:
            for i in range(4):
                tg.start_soon(functools.partial(hosted.fn, prompt=f"p{i}"))

    anyio.run(go_hosted)
    assert live["peak"] == 4


def test_background_jobs_leave_a_slot_for_foreground(monkeypatch) -> None:
    """At the local cap of 2, background children may hold at most one slot:
    a foreground task call still runs while two background jobs are live."""
    import asyncio

    import mantis_agent.subagent as sub
    from mantis_agent.jobs import JobManager

    monkeypatch.delenv("MANTIS_SUBAGENT_MAX_CONCURRENT", raising=False)
    release = asyncio.Event()
    running: list[str] = []

    class _FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, messages):
            text = messages[-1].content
            text = text if isinstance(text, str) else str(text)
            running.append(text)
            if "bg" in text:
                await release.wait()

    monkeypatch.setattr(sub, "Agent", _FakeAgent)

    async def go():
        jm = JobManager()
        task = sub.make_task_tool(model="qwen3:8b", tools=[], jobs=jm,
                                  backend="http://localhost:11434")
        await task.fn(prompt="bg one", run_in_background=True)
        await task.fn(prompt="bg two", run_in_background=True)
        await asyncio.sleep(0.05)
        assert sum("bg" in r for r in running) == 1   # second bg queues
        await asyncio.wait_for(task.fn(prompt="fg now"), 2.0)
        assert any("fg now" in r for r in running)
        release.set()
        for j in jm.all():
            await jm.wait(j.id, timeout_s=5)

    asyncio.run(go())


def test_job_runtime_clock_starts_after_the_gate() -> None:
    """Time queued for a slot doesn't count against max_runtime_s."""
    import asyncio

    from mantis_agent.jobs import JobManager

    async def go():
        lim = anyio.CapacityLimiter(1)
        jm = JobManager()
        await lim.acquire()   # hold the only slot for longer than the runtime cap

        async def work():
            await asyncio.sleep(0.05)
            return "ok"

        job = jm.spawn(work(), desc="q", max_runtime_s=0.2, gate=lambda: lim)
        await asyncio.sleep(0.4)
        assert job.status == "running"
        lim.release()
        await jm.wait(job.id, timeout_s=2)
        assert (job.status, job.result) == ("done", "ok")

    asyncio.run(go())


def test_job_cancelled_while_queued_closes_its_coroutine() -> None:
    import asyncio
    import warnings

    from mantis_agent.jobs import JobManager

    async def go():
        lim = anyio.CapacityLimiter(1)
        await lim.acquire()
        jm = JobManager()

        async def work():
            return "never"

        job = jm.spawn(work(), desc="q", gate=lambda: lim)
        await asyncio.sleep(0.05)
        job.task.cancel()
        await asyncio.sleep(0.05)
        assert job.status == "cancelled"

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        asyncio.run(go())
