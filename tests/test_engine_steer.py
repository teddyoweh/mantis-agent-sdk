"""Steering a running agent: ``Agent.steer`` and the surfaces built on it.

A steer is not an interrupt. The model call in flight finishes, the tool calls
it asked for finish, and the NEXT request carries the message — folded into
that turn's tool-result message (after every ``tool_result``, so the pairing
invariant still holds) or, after a final answer, as a new user message that
keeps the run going. A ``task`` subagent blocking the turn moves to the
background and keeps working; its result arrives through the job machinery.

Everything runs on scripted mock providers — no network.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from typing import Any

import anyio
import pytest

from mantis_agent import Agent, tool
from mantis_agent.agent import _assert_message_invariants
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    SteerEvent,
    TextDelta,
)
from mantis_agent.hooks import HookResult, Hooks
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

STEER = "No new dependencies, please."


class _Script(MockProvider):
    """One scripted turn per call: a str is a final text answer, a list of
    ``(tool, args)`` is a tool-calling turn. ``during[n]`` runs inside call
    ``n``'s stream, after MessageStart — i.e. while the model is "talking"."""

    name = "mock"

    def __init__(self, turns: list[Any], during: dict[int, Any] | None = None) -> None:
        super().__init__()
        self.turns = list(turns)
        self.during = during or {}

    async def stream(self, **kw: Any):
        n = len(self.calls)
        self.calls.append({**kw, "messages": list(kw["messages"])})
        turn = self.turns[n] if n < len(self.turns) else "done"
        yield MessageStart(message_id=f"m{n}", model="mock")
        cb = self.during.get(n)
        if cb is not None:
            out = cb()
            if inspect.isawaitable(out):
                await out
        if isinstance(turn, str):
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text=turn))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=5))
        else:
            for i, (name, args) in enumerate(turn):
                yield ContentBlockStart(index=i, block=ToolUseBlock(id=f"c{n}_{i}", name=name, input={}))
                yield ContentBlockDelta(index=i, delta=InputJsonDelta(partial_json=json.dumps(args)))
                yield ContentBlockStop(index=i)
            yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=5, output_tokens=5))
        yield MessageStop()


def _agent(provider: MockProvider, tools: list | None = None, **kw: Any) -> Agent:
    agent = Agent(model="mock-7b", provider=provider, tools=tools or [],
                  include_memory=False, include_recall=False, auto_compact=False, **kw)
    events: list[Any] = []
    agent.on_event = events.append
    agent._test_events = events  # type: ignore[attr-defined]
    return agent


def _steers(agent: Agent) -> list[tuple[str, str, str]]:
    return [(e.text, e.status, e.placement)
            for e in agent._test_events if isinstance(e, SteerEvent)]  # type: ignore[attr-defined]


def _texts(msg: Any) -> list[str]:
    if isinstance(msg.content, str):
        return [msg.content]
    return [b.text for b in msg.content if isinstance(b, TextBlock)]


# ---------------------------------------------------------------------------
# Turn boundary
# ---------------------------------------------------------------------------


def test_steer_during_a_tool_lands_in_the_very_next_request() -> None:
    box: dict[str, Agent] = {}

    @tool
    async def install() -> str:
        """Install a package."""
        assert box["agent"].steer(STEER) is True
        return "installing styled-components…"

    prov = _Script([[("install", {})], "Got it. Using the existing CSS variables instead."])
    agent = box["agent"] = _agent(prov, [install])
    msgs: list[Any] = [UserMessage(content="Add dark mode")]
    anyio.run(agent.run, msgs)

    assert len(prov.calls) == 2
    sent = prov.calls[1]["messages"]
    _assert_message_invariants(sent)
    carrier = sent[-1]
    # The steer rides the tool-result message, AFTER the result, so the
    # tool_use is still answered by the immediately following message.
    assert [type(b).__name__ for b in carrier.content] == ["ToolResultBlock", "TextBlock", "TextBlock"]
    assert carrier.content[0].tool_use_id == "c0_0"
    assert carrier.content[1].text.startswith("<system-reminder>")
    assert carrier.content[2].text == STEER
    # Persisted (append-only), and announced exactly once.
    assert msgs[2] is carrier or msgs[2] == carrier
    assert _steers(agent) == [(STEER, "delivered", "tool_results")]
    assert agent.take_undelivered_steers() == []


def test_parallel_tool_calls_stay_answered_first_when_steered() -> None:
    box: dict[str, Agent] = {}
    gate = {"n": 0}

    @tool(is_concurrency_safe=True)
    async def probe(i: int) -> str:
        """Probe one thing."""
        gate["n"] += 1
        if gate["n"] == 2:
            box["agent"].steer("stop probing, summarize")
        await anyio.sleep(0)
        return f"probe {i}"

    prov = _Script([[("probe", {"i": 1}), ("probe", {"i": 2}), ("probe", {"i": 3})], "Summary."])
    agent = box["agent"] = _agent(prov, [probe])
    anyio.run(agent.run, [UserMessage(content="probe all")])

    sent = prov.calls[1]["messages"]
    _assert_message_invariants(sent)
    carrier = sent[-1]
    blocks = carrier.content
    results = [b for b in blocks if isinstance(b, ToolResultBlock)]
    assert [r.tool_use_id for r in results] == ["c0_0", "c0_1", "c0_2"]
    # Every result comes before any text.
    first_text = next(i for i, b in enumerate(blocks) if isinstance(b, TextBlock))
    assert all(isinstance(b, ToolResultBlock) for b in blocks[:first_text])
    assert blocks[-1].text == "stop probing, summarize"


def test_steered_tool_results_encode_results_first_on_every_wire() -> None:
    """The carrier shape is valid for each provider path: OpenAI native
    (``tool`` messages, then one user message), the text channel (results
    folded, then the steer), Ollama native, Anthropic (tool_result blocks
    before text) and the Responses API."""
    from mantis_agent.providers import ollama as ol
    from mantis_agent.providers import openai_compat as oc
    from mantis_agent.providers.anthropic_passthrough import _encode_content

    carrier = UserMessage(content=[
        ToolResultBlock(tool_use_id="a", content="r1"),
        ToolResultBlock(tool_use_id="b", content="r2"),
        TextBlock(text="<system-reminder>\nframing\n</system-reminder>"),
        TextBlock(text=STEER),
    ])

    wire_a = oc._encode_message(carrier, path="A")
    assert [m["role"] for m in wire_a] == ["tool", "tool", "user"]
    assert [m.get("tool_call_id") for m in wire_a[:2]] == ["a", "b"]
    assert wire_a[-1]["content"].endswith(STEER)

    wire_b = oc._encode_message(carrier, path="B")
    assert len(wire_b) == 1 and wire_b[0]["role"] == "user"
    body = wire_b[0]["content"]
    assert body.index("</tool_result>") < body.index(STEER)

    wire_ol = ol._encode_user_native(carrier.content, {"a": ("x", 0), "b": ("x", 1)})
    assert [m["role"] for m in wire_ol] == ["tool", "tool", "user"]
    assert wire_ol[-1]["content"].endswith(STEER)

    enc = _encode_content(carrier.content)
    assert [b["type"] for b in enc] == ["tool_result", "tool_result", "text", "text"]

    assistant = AssistantMessage(content=[
        ToolUseBlock(id="a", name="x", input={}), ToolUseBlock(id="b", name="x", input={}),
    ])
    payload = oc._build_responses_payload(
        messages=[UserMessage(content="hi"), assistant, carrier], system=None,
        model="gpt-5.4", tools=[], max_tokens=64, extra=None, thinking=None,
        model_capability=None,
    )
    kinds = [i.get("type") or i.get("role") for i in payload["input"]]
    assert kinds[-3:] == ["function_call_output", "function_call_output", "user"]
    assert payload["input"][-1]["content"][0]["text"].endswith(STEER)


def test_steer_after_the_final_answer_starts_another_turn() -> None:
    box: dict[str, Agent] = {}
    prov = _Script(
        ["Next, I'll install styled-components for dark mode.",
         "Got it. Using the existing CSS variables instead."],
        during={0: lambda: box["agent"].steer(STEER)},
    )
    agent = box["agent"] = _agent(prov)
    msgs: list[Any] = [UserMessage(content="Add dark mode")]
    anyio.run(agent.run, msgs)

    assert len(prov.calls) == 2, "the steer must keep the run going"
    sent = prov.calls[1]["messages"]
    _assert_message_invariants(sent)
    assert isinstance(sent[-1], UserMessage) and sent[-1].content == STEER
    assert not sent[-1].isMeta
    assert isinstance(msgs[-1], AssistantMessage)
    assert _texts(msgs[-1]) == ["Got it. Using the existing CSS variables instead."]
    assert _steers(agent) == [(STEER, "delivered", "user_turn")]
    assert agent._stop_cause == "natural"


def test_steer_from_the_consumer_after_the_final_answer_is_yielded() -> None:
    """The Cursor demo flow: the caller sees the answer, steers, keeps
    iterating — and gets a reply to the steer from the same run."""
    prov = _Script(["Next, I'll install styled-components.", "Got it."])
    agent = _agent(prov)

    async def go() -> list[Any]:
        seen: list[Any] = []
        msgs: list[Any] = [UserMessage(content="Add dark mode")]
        async for m in agent.run_iter(msgs):
            seen.append(m)
            if isinstance(m, AssistantMessage) and len(seen) == 1:
                assert agent.steer(STEER) is True
        return seen

    seen = anyio.run(go)
    assert [type(m).__name__ for m in seen] == ["AssistantMessage", "UserMessage", "AssistantMessage"]
    assert seen[1].content == STEER


def test_steer_outside_a_run_is_refused_and_not_kept() -> None:
    prov = _Script(["first answer", "second answer"])
    agent = _agent(prov)

    async def go() -> None:
        assert agent.steer("too early") is False
        await agent.run([UserMessage(content="hi")])
        assert agent.steer("too late") is False
        await agent.run([UserMessage(content="again")])

    anyio.run(go)
    assert len(prov.calls) == 2
    flat = [t for m in prov.calls[-1]["messages"] for t in _texts(m)]
    assert "too early" not in flat and "too late" not in flat
    with pytest.raises(ValueError):
        agent.steer("   ")
    with pytest.raises(TypeError):
        agent.steer(None)  # type: ignore[arg-type]


def test_steers_keep_their_order_across_tasks_threads_and_boundaries() -> None:
    box: dict[str, Agent] = {}

    @tool
    async def slow() -> str:
        """A slow tool."""
        a = box["agent"]
        assert a.steer("one")
        # A burst from other threads and tasks, each strictly after the last.
        t = threading.Thread(target=lambda: a.steer("two"))
        t.start()
        t.join()

        async def later() -> None:
            a.steer("three")

        async with anyio.create_task_group() as tg:
            tg.start_soon(later)
        return "ok"

    prov = _Script(
        [[("slow", {})], "answer"],
        during={1: lambda: box["agent"].steer("four")},
    )
    agent = box["agent"] = _agent(prov, [slow])

    async def go() -> list[Any]:
        msgs: list[Any] = [UserMessage(content="go")]
        await agent.run(msgs)
        return msgs

    msgs = anyio.run(go)
    assert len(prov.calls) == 3
    folded = prov.calls[1]["messages"][-1]
    assert _texts(folded)[1:] == ["one", "two", "three"]   # [0] is the framing
    assert prov.calls[2]["messages"][-1].content == "four"
    assert [t for t, _s, _p in _steers(agent)] == ["one", "two", "three", "four"]
    _assert_message_invariants(msgs)


def test_steer_arriving_between_turns_rides_the_next_request() -> None:
    """A steer sent while the caller is still holding the tool-result message
    (after the fold point) goes out as its own user message on the very next
    request — it does not wait a whole extra turn."""

    @tool
    async def noop() -> str:
        """Nothing."""
        return "ok"

    prov = _Script([[("noop", {})], "done"])
    agent = _agent(prov, [noop])

    async def go() -> None:
        msgs: list[Any] = [UserMessage(content="go")]
        async for m in agent.run_iter(msgs):
            if isinstance(m, UserMessage) and isinstance(m.content, list):
                assert agent.steer("late but welcome")

    anyio.run(go)
    sent = prov.calls[1]["messages"]
    _assert_message_invariants(sent)
    assert sent[-1].content == "late but welcome"
    assert isinstance(sent[-2].content, list)   # the untouched tool results


def test_a_cancelled_run_hands_back_its_undelivered_steers() -> None:
    box: dict[str, Agent] = {}

    @tool
    async def hang() -> str:
        """Hang until cancelled."""
        box["agent"].steer("never sent")
        box["agent"].cancel()
        await anyio.sleep(10)
        return "unreachable"

    prov = _Script([[("hang", {})], "unreachable"])
    agent = box["agent"] = _agent(prov, [hang])
    anyio.run(agent.run, [UserMessage(content="go")])
    assert len(prov.calls) == 1
    assert agent.take_undelivered_steers() == ["never sent"]
    assert agent.take_undelivered_steers() == []
    assert agent.steer("after") is False


def test_a_steer_at_the_step_cap_is_handed_back_not_swallowed() -> None:
    box: dict[str, Agent] = {}

    @tool
    async def step() -> str:
        """One step."""
        box["agent"].steer("too late for this run")
        return "ok"

    prov = _Script([[("step", {})], [("step", {})]])
    agent = box["agent"] = _agent(prov, [step], max_steps=1, persist=False)
    msgs: list[Any] = [UserMessage(content="go")]
    anyio.run(agent.run, msgs)
    assert agent._stop_cause == "max_steps"
    flat = [t for m in msgs for t in _texts(m)]
    assert "too late for this run" not in flat
    assert agent.take_undelivered_steers() == ["too late for this run"]


def test_user_prompt_submit_hook_gates_steers_too() -> None:
    box: dict[str, Agent] = {}
    seen: list[dict] = []

    async def gate(ctx: Any) -> HookResult:
        seen.append(dict(ctx.arbitrary))
        if "rm -rf" in (ctx.arbitrary.get("prompt") or ""):
            return HookResult(block=True, note="destructive request")
        return HookResult()

    prov = _Script(
        ["working on it", "fine"],
        during={0: lambda: (box["agent"].steer("run rm -rf /"), box["agent"].steer("use tabs"))},
    )
    agent = box["agent"] = _agent(prov, hooks=Hooks(user_prompt_submit=gate))
    anyio.run(agent.run, [UserMessage(content="tidy up")])
    assert prov.calls[1]["messages"][-1].content == "use tabs"
    assert ("run rm -rf /", "blocked", "user_turn") in _steers(agent)
    assert ("use tabs", "delivered", "user_turn") in _steers(agent)
    assert any(s.get("steer") for s in seen)


# ---------------------------------------------------------------------------
# task subagents move to the background
# ---------------------------------------------------------------------------


class _ChildGate:
    """A fake child Agent whose run blocks until released, so the test controls
    exactly when the subagent finishes."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.cancelled = False
        self.prompts: list[str] = []

    def factory(self) -> Any:
        gate = self

        class _FakeChild:
            def __init__(self, **kw: Any) -> None:
                pass

            async def run(self, messages: list[Any]) -> list[Any]:
                gate.prompts.append(messages[-1].content)
                gate.started.set()
                try:
                    await gate.release.wait()
                except asyncio.CancelledError:
                    gate.cancelled = True
                    raise
                messages.append(AssistantMessage(content=[TextBlock(text="child findings: 3 call sites")]))
                gate.finished = True
                return messages

        return _FakeChild


def _task_parent(monkeypatch: pytest.MonkeyPatch, gate: _ChildGate, turns: list[Any],
                 during: dict[int, Any] | None = None, **task_kw: Any) -> tuple[Agent, _Script, Any, Any]:
    import mantis_agent.subagent as sub
    from mantis_agent.jobs import JobManager

    monkeypatch.setattr(sub, "Agent", gate.factory())
    done: list[Any] = []
    jobs = JobManager(on_event=done.append)
    progress: list[dict] = []
    task_tool = sub.make_task_tool(model="mock-7b", tools=[], jobs=jobs,
                                   on_progress=progress.append, **task_kw)
    prov = _Script(turns, during=during)
    agent = _agent(prov, [task_tool])
    agent._test_jobs_done = done  # type: ignore[attr-defined]
    agent._test_progress = progress  # type: ignore[attr-defined]
    return agent, prov, jobs, task_tool


def test_a_foreground_subagent_moves_to_the_background_and_reports_later(monkeypatch) -> None:
    async def go() -> None:
        gate = _ChildGate()   # its asyncio.Events belong to this loop
        agent, prov, jobs, _ = _task_parent(
            monkeypatch, gate,
            [[("task", {"prompt": "find the call sites", "description": "call sites"})],
             "Understood — no new dependencies."],
            backend="https://api.openai.com/v1",
        )

        async def steer_when_child_runs() -> None:
            await gate.started.wait()
            assert agent.steer(STEER)

        msgs: list[Any] = [UserMessage(content="refactor")]
        async with anyio.create_task_group() as tg:
            tg.start_soon(steer_when_child_runs)
            await asyncio.wait_for(agent.run(msgs), 5)

        # The parent finished its turn while the child is STILL running.
        assert not gate.finished and not gate.cancelled
        sent = prov.calls[1]["messages"]
        _assert_message_invariants(sent)
        carrier = sent[-1]
        result = carrier.content[0]
        assert isinstance(result, ToolResultBlock) and not result.is_error
        assert "background job #1" in result.content
        assert "job_output(job_id=1)" in result.content
        assert carrier.content[-1].text == STEER
        assert ("background" in [p.get("phase") for p in agent._test_progress])
        job = jobs.get(1)
        assert job is not None and job.status == "running" and job.kind == "task:explore"

        # Later: the child finishes and its result arrives as a job.
        gate.release.set()
        await jobs.wait(1, timeout_s=5)
        assert job.status == "done"
        assert "child findings: 3 call sites" in job.result
        assert agent._test_jobs_done == [job]
        from mantis_agent.subagent import make_job_output_tool
        out = await make_job_output_tool(jobs).fn(job_id=1)
        assert "child findings" in out

    asyncio.run(go())


def test_a_steer_already_waiting_starts_the_subagent_in_the_background(monkeypatch) -> None:
    async def go() -> None:
        gate = _ChildGate()   # its asyncio.Events belong to this loop
        box: dict[str, Agent] = {}
        agent, prov, jobs, _ = _task_parent(
            monkeypatch, gate,
            [[("task", {"prompt": "audit", "description": "audit"})], "ok"],
            during={0: lambda: box["agent"].steer("also check the docs")},
            backend="https://api.openai.com/v1",
        )
        box["agent"] = agent
        await asyncio.wait_for(agent.run([UserMessage(content="go")]), 5)
        result = prov.calls[1]["messages"][-1].content[0]
        assert "Started this subagent as background job #1" in result.content
        assert prov.calls[1]["messages"][-1].content[-1].text == "also check the docs"
        gate.release.set()
        await jobs.wait(1, timeout_s=5)
        assert jobs.get(1).status == "done"

    asyncio.run(go())


def test_without_a_steer_the_foreground_subagent_returns_inline(monkeypatch) -> None:
    async def go() -> None:
        gate = _ChildGate()   # its asyncio.Events belong to this loop
        agent, prov, jobs, _ = _task_parent(
            monkeypatch, gate, [[("task", {"prompt": "scan"})], "ok"],
            backend="https://api.openai.com/v1",
        )

        async def finish_child() -> None:
            await gate.started.wait()
            gate.release.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(finish_child)
            await asyncio.wait_for(agent.run([UserMessage(content="go")]), 5)
        result = prov.calls[1]["messages"][-1].content[0]
        assert "child findings" in result.content
        assert jobs.all() == []

    asyncio.run(go())


def test_backgrounding_respects_the_background_slot_gate(monkeypatch) -> None:
    """Local cap 2 → background gate 1. With one background job already
    holding the gate, a steered foreground child can't detach: it finishes in
    the foreground and the steer lands at the next boundary as usual."""
    monkeypatch.delenv("MANTIS_SUBAGENT_MAX_CONCURRENT", raising=False)

    async def go() -> None:
        gate = _ChildGate()   # its asyncio.Events belong to this loop
        agent, prov, jobs, task_tool = _task_parent(
            monkeypatch, gate,
            [[("task", {"prompt": "fg work"})], "ok"],
            backend="http://localhost:11434",
        )
        # Occupy the single background token with an unrelated bg job.
        hold = asyncio.Event()
        bg_lim = task_tool.background_limiter()
        await bg_lim.acquire_on_behalf_of(hold)

        async def steer_then_finish() -> None:
            await gate.started.wait()
            assert agent.steer("keep going")
            await asyncio.sleep(0.05)
            assert not gate.finished      # still foreground, still blocking
            gate.release.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(steer_then_finish)
            await asyncio.wait_for(agent.run([UserMessage(content="go")]), 5)
        carrier = prov.calls[1]["messages"][-1]
        assert "child findings" in carrier.content[0].content
        assert carrier.content[-1].text == "keep going"
        assert jobs.all() == []
        assert task_tool.concurrency_limiter().borrowed_tokens == 0
        bg_lim.release_on_behalf_of(hold)
        assert bg_lim.borrowed_tokens == 0

    asyncio.run(go())


def test_a_detached_child_keeps_its_slot_until_it_ends(monkeypatch) -> None:
    async def go() -> None:
        gate = _ChildGate()   # its asyncio.Events belong to this loop
        agent, prov, jobs, task_tool = _task_parent(
            monkeypatch, gate,
            [[("task", {"prompt": "long"})], "ok"],
            backend="http://localhost:11434",
        )

        async def steer_when_child_runs() -> None:
            await gate.started.wait()
            agent.steer("go on")

        async with anyio.create_task_group() as tg:
            tg.start_soon(steer_when_child_runs)
            await asyncio.wait_for(agent.run([UserMessage(content="go")]), 5)
        lim, bg = task_tool.concurrency_limiter(), task_tool.background_limiter()
        assert (lim.borrowed_tokens, bg.borrowed_tokens) == (1, 1)
        gate.release.set()
        await jobs.wait(1, timeout_s=5)
        await asyncio.sleep(0)
        assert (lim.borrowed_tokens, bg.borrowed_tokens) == (0, 0)

    asyncio.run(go())


def test_cancelling_the_parent_cancels_a_foreground_child(monkeypatch) -> None:
    async def go() -> None:
        gate = _ChildGate()   # its asyncio.Events belong to this loop
        agent, prov, jobs, task_tool = _task_parent(
            monkeypatch, gate, [[("task", {"prompt": "x"})], "never"],
            backend="https://api.openai.com/v1",
        )

        async def cancel_when_child_runs() -> None:
            await gate.started.wait()
            agent.cancel()

        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel_when_child_runs)
            await asyncio.wait_for(agent.run([UserMessage(content="go")]), 5)
        await asyncio.sleep(0)
        assert gate.cancelled and not gate.finished
        assert jobs.all() == []
        assert task_tool.concurrency_limiter().borrowed_tokens == 0

    asyncio.run(go())


# ---------------------------------------------------------------------------
# SDK surfaces: query() run handle and ClaudeSDKClient
# ---------------------------------------------------------------------------


def test_query_run_handle_steers_the_live_run() -> None:
    from mantis_agent import QueryRun, query
    from mantis_agent.query import SDKAssistantMessage, SDKResultMessage, SDKUserMessage

    prov = _Script(["Next, I'll install styled-components.", "Got it."])

    async def go() -> list[Any]:
        run = query(prompt="Add dark mode",
                    options={"model": "mock-7b", "provider": prov, "include_memory": False})
        assert isinstance(run, QueryRun)
        assert await run.steer("before the run") is False
        out: list[Any] = []
        async for msg in run:
            out.append(msg)
            if isinstance(msg, SDKAssistantMessage) and len(out) == 3:
                assert await run.steer(STEER) is True
        assert await run.steer("after the run") is False
        return out

    out = anyio.run(go)
    users = [m for m in out if isinstance(m, SDKUserMessage)]
    assert any(m.message.content == STEER for m in users)
    result = out[-1]
    assert isinstance(result, SDKResultMessage) and result.result == "Got it."
    assert len(prov.calls) == 2


def test_claude_sdk_client_steer(monkeypatch) -> None:
    import mantis_agent.compat_query as cq
    from mantis_agent import AssistantMessage as CompatAssistant
    from mantis_agent import ClaudeSDKClient, MantisAgentOptions, ResultMessage

    prov = _Script(["Next, I'll install styled-components.", "Got it."])
    real_build = cq._build_agent

    def _build(opts: dict[str, Any]) -> Agent:
        agent = real_build(opts)
        agent.provider = prov
        return agent

    monkeypatch.setattr(cq, "_build_agent", _build)

    async def go() -> list[Any]:
        out: list[Any] = []
        async with ClaudeSDKClient(options=MantisAgentOptions(model="mock-7b", include_memory=False)) as client:
            assert await client.steer("nothing running") is False
            await client.query("Add dark mode")
            async for msg in client.receive_response():
                out.append(msg)
                if isinstance(msg, CompatAssistant) and not any(
                        isinstance(m, CompatAssistant) for m in out[:-1]):
                    assert await client.steer(STEER) is True
            assert await client.steer("after") is False
        return out

    out = anyio.run(go)
    assert isinstance(out[-1], ResultMessage) and out[-1].result == "Got it."
    assert len(prov.calls) == 2
    assert prov.calls[1]["messages"][-1].content == STEER


def test_steering_keeps_every_request_a_prefix_of_the_next() -> None:
    """Steers are persisted, append-only user input: each request still
    extends the previous one byte for byte (local KV / prefix caches)."""
    from tests.test_engine_prefix_cache import assert_prefix_stable

    box: dict[str, Agent] = {}

    @tool
    async def look() -> str:
        """Look."""
        box["agent"].steer("mid-tool steer")
        return "seen"

    prov = _Script(
        [[("look", {})], "an answer", "the final answer"],
        during={1: lambda: box["agent"].steer("after-answer steer")},
    )
    agent = box["agent"] = _agent(prov, [look])
    anyio.run(agent.run, [UserMessage(content="go")])
    assert len(prov.calls) == 3
    for prev, nxt in zip(prov.calls, prov.calls[1:]):
        assert_prefix_stable(prev, nxt)


def test_job_manager_adopt_follows_and_cancels_the_task() -> None:
    from mantis_agent.jobs import JobManager

    async def go() -> None:
        done: list[Any] = []
        jm = JobManager(on_event=done.append)
        gate = asyncio.Event()

        async def work() -> str:
            await gate.wait()
            return "finished"

        t1 = asyncio.ensure_future(work())
        job = jm.adopt(t1, desc="adopted", kind="task:explore", started=0.0)
        assert job.id == 1 and job.status == "running" and job.elapsed_s > 1
        gate.set()
        await jm.wait(1, timeout_s=2)
        assert (job.status, job.result, done) == ("done", "finished", [job])

        t2 = asyncio.ensure_future(asyncio.sleep(30))
        job2 = jm.adopt(t2, desc="to cancel")
        await asyncio.sleep(0)
        assert jm.cancel(job2.id)
        await jm.wait(job2.id, timeout_s=2)
        await asyncio.sleep(0)
        assert job2.status == "cancelled" and t2.cancelled()

    asyncio.run(go())


def test_ts_shape_query_marks_a_steered_turn_as_typed() -> None:
    from mantis_agent import query
    from mantis_agent.query import SDKAssistantMessage, SDKUserMessage

    prov = _Script(["first", "second"])

    async def go() -> list[Any]:
        run = query(prompt="hi", options={"model": "mock-7b", "provider": prov,
                                          "include_memory": False})
        out: list[Any] = []
        async for msg in run:
            out.append(msg)
            if isinstance(msg, SDKAssistantMessage) and len(prov.calls) == 1:
                await run.steer("typed mid-run")
        return out

    out = anyio.run(go)
    typed = [m.message.content for m in out
             if isinstance(m, SDKUserMessage) and not m.isSynthetic]
    assert typed == ["hi", "typed mid-run"]
