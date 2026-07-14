"""End-to-end tests for the sub-agent / multi-agent flow.

Covers the two registration shapes exposed by :func:`as_subagent_tool`:

  * ``SubAgentSpec`` form — fresh child Agent minted per invocation.
  * ``Agent`` form — wrap an existing Agent (``WrappedAgentTool``).

Plus:
  * Argument-validation errors raised by the factory.
  * Final-text extraction edge cases (no text in last turn → marker string).
  * Multi-turn orchestration via ``MockProvider`` — parent calls sub-agent,
    sub-agent calls a real tool, sub-agent answers, parent quotes it back.
  * End-to-end smoke test of the ``multi_agent_research`` example in
    mock mode (verifies the example wiring stays runnable).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from mantis_agent import (
    Agent,
    AssistantMessage,
    SubAgentSpec,
    SubAgentTool,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    WrappedAgentTool,
    as_subagent_tool,
    tool,
)
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.subagent import _extract_final_text


# ---------------------------------------------------------------------------
# Helpers — build scripted SSE-style event lists
# ---------------------------------------------------------------------------


def _hdr(model: str = "mock-7b") -> list:
    return [MessageStart(message_id=f"mock-{model}", model=model)]


def _text(idx: int, txt: str) -> list:
    return [
        ContentBlockStart(index=idx, block=TextBlock(text="")),
        ContentBlockDelta(index=idx, delta=TextDelta(text=txt)),
        ContentBlockStop(index=idx),
    ]


def _tool_call(idx: int, cid: str, name: str, json_args: str) -> list:
    return [
        ContentBlockStart(
            index=idx, block=ToolUseBlock(id=cid, name=name, input={})
        ),
        ContentBlockDelta(index=idx, delta=InputJsonDelta(partial_json=json_args)),
        ContentBlockStop(index=idx),
    ]


def _stop(reason: str = "end_turn", usage: Usage | None = None) -> list:
    return [
        MessageDelta(
            stop_reason=reason,
            usage=usage or Usage(input_tokens=5, output_tokens=10),
        ),
        MessageStop(),
    ]


class ScriptedMock(MockProvider):
    """Replay a sequence of scripts on successive ``stream()`` calls.

    Each call pops the next script from the queue. Once exhausted, the
    final script is replayed (so accidental extra calls don't crash —
    they just look like a quiet end_turn).
    """

    def __init__(self, scripts: list[list]) -> None:
        super().__init__()
        self._scripts = list(scripts)
        self._i = 0
        self.call_count = 0
        self.received_tools: list[list[dict]] = []

    async def stream(self, **kw):
        self.call_count += 1
        # Track what tool definitions the parent / child were sent — used
        # below to assert the sub-agent surfaces in the parent's tool list.
        tools = kw.get("tools") or []
        self.received_tools.append(tools)
        script = self._scripts[self._i] if self._i < len(self._scripts) else self._scripts[-1]
        if self._i < len(self._scripts):
            self._i += 1
        for ev in script:
            yield ev


# ---------------------------------------------------------------------------
# Tools used by sub-agents in the tests
# ---------------------------------------------------------------------------


@tool
async def fetch_url(url: str) -> str:
    """Stub fetcher used inside sub-agents."""
    return f"<fetched {url}: Spawn Labs builds AI agents.>"


# ===========================================================================
# Factory-shape tests
# ===========================================================================


def test_as_subagent_tool_with_spec_returns_subagent_tool():
    spec = SubAgentSpec(
        name="research",
        system_prompt="You research.",
        model="mock-7b",
        tools=[fetch_url],
    )
    t = as_subagent_tool(spec)
    assert isinstance(t, SubAgentTool)
    assert t.name == "research"
    assert "research" in t.description
    # Sub-agents are not parallel-safe — same-name dispatch must serialize.
    assert t.parallel_safe is False
    # Single-prompt input schema.
    assert t.input_schema["properties"].keys() == {"prompt"}
    assert t.input_schema["required"] == ["prompt"]


def test_as_subagent_tool_with_agent_returns_wrapped_agent_tool():
    agent = Agent(
        model="mock-7b",
        provider=MockProvider(),  # trivial OK stream
        tools=[fetch_url],
        system="You are research.",
        include_memory=False,
    )
    try:
        t = as_subagent_tool(
            agent, name="research", description="Custom description."
        )
        assert isinstance(t, WrappedAgentTool)
        assert t.name == "research"
        assert t.description == "Custom description."
        assert t.parallel_safe is False
    finally:
        anyio.run(agent.aclose)


def test_as_subagent_tool_with_agent_default_description():
    agent = Agent(
        model="mock-7b",
        provider=MockProvider(),
        include_memory=False,
    )
    try:
        t = as_subagent_tool(agent, name="research")
        assert t.description == "Delegate to the research sub-agent."
    finally:
        anyio.run(agent.aclose)


def test_as_subagent_tool_rejects_bare_agent_without_name():
    agent = Agent(model="mock-7b", provider=MockProvider(), include_memory=False)
    try:
        with pytest.raises(TypeError, match="requires an explicit"):
            as_subagent_tool(agent)  # type: ignore[call-overload]
    finally:
        anyio.run(agent.aclose)


def test_as_subagent_tool_rejects_spec_with_kwargs():
    spec = SubAgentSpec(
        name="research", system_prompt="x", model="mock-7b"
    )
    with pytest.raises(TypeError, match="pass name/description via the spec"):
        # spec form with override kwargs is ambiguous — reject loudly.
        as_subagent_tool(spec, name="something_else")  # type: ignore[call-overload]


def test_as_subagent_tool_rejects_agent_with_parent_provider():
    agent = Agent(model="mock-7b", provider=MockProvider(), include_memory=False)
    try:
        with pytest.raises(TypeError, match="does not take parent_provider"):
            as_subagent_tool(
                agent, name="research", parent_provider=MockProvider()
            )  # type: ignore[call-overload]
    finally:
        anyio.run(agent.aclose)


def test_as_subagent_tool_rejects_unknown_type():
    with pytest.raises(TypeError, match="SubAgentSpec or Agent"):
        as_subagent_tool("not-an-agent")  # type: ignore[arg-type]


# ===========================================================================
# Text extraction edge cases
# ===========================================================================


def test_extract_final_text_picks_last_assistant():
    msgs = [
        UserMessage(content="hi"),
        AssistantMessage(content=[TextBlock(text="first")], stop_reason="end_turn"),
        UserMessage(content="more"),
        AssistantMessage(
            content=[TextBlock(text="hello"), TextBlock(text=" world")],
            stop_reason="end_turn",
        ),
    ]
    assert _extract_final_text(msgs) == "hello world"


def test_extract_final_text_falls_back_to_stop_reason_marker():
    msgs = [
        UserMessage(content="hi"),
        AssistantMessage(
            content=[ToolUseBlock(id="c1", name="x", input={})],
            stop_reason="max_tokens",
        ),
    ]
    out = _extract_final_text(msgs)
    assert "max_tokens" in out
    assert "no text" in out


def test_extract_final_text_empty_messages():
    assert _extract_final_text([]) == "<sub-agent produced no assistant message>"


# ===========================================================================
# Sub-agent execution via the spec form
# ===========================================================================


def test_subagent_spec_runs_child_with_its_own_tool():
    """SubAgentSpec form: tool body spawns a fresh Agent that uses
    fetch_url, then returns the child's final text to the parent."""

    # Child (sub-agent) script: tool call → tool result → final text.
    child_t1 = (
        _hdr()
        + _tool_call(0, "c-1", "fetch_url", '{"url": "https://spawnlabs.ai"}')
        + _stop("tool_use")
    )
    child_t2 = _hdr() + _text(0, "Spawn Labs builds AI agents.") + _stop()

    mock = ScriptedMock([child_t1, child_t2])

    spec = SubAgentSpec(
        name="research_fresh",
        system_prompt="You research.",
        model="mock-7b",
        tools=[fetch_url],
        max_turns=4,
    )
    # Share the same MockProvider with the child so we can script its
    # behavior deterministically.
    t = as_subagent_tool(spec, parent_provider=mock)

    async def go() -> str:
        return await t.fn(prompt="What does Spawn Labs build?")

    out = anyio.run(go)
    assert out == "Spawn Labs builds AI agents."
    # 2 child stream calls: one tool-call turn + one final-text turn.
    assert mock.call_count == 2


def test_subagent_spec_with_no_tools_just_text():
    """Spec form with empty tool kit — child emits text immediately."""

    child_t1 = _hdr() + _text(0, "I have no tools but here's a guess.") + _stop()
    mock = ScriptedMock([child_t1])

    spec = SubAgentSpec(
        name="guesser", system_prompt="Guess.", model="mock-7b"
    )
    t = as_subagent_tool(spec, parent_provider=mock)

    async def go() -> str:
        return await t.fn(prompt="What's the answer?")

    out = anyio.run(go)
    assert out == "I have no tools but here's a guess."


def test_subagent_spec_unknown_isolation_raises():
    spec = SubAgentSpec(
        name="x",
        system_prompt="y",
        model="mock-7b",
        isolation="asyncio_task",
    )
    # Bypass the dataclass guard — set a bad value directly so we exercise
    # the runtime check inside ``_invoke``.
    spec.isolation = "rocket"  # type: ignore[assignment]
    t = as_subagent_tool(spec)

    async def go():
        await t.fn(prompt="hi")

    with pytest.raises(ValueError, match="unknown isolation mode"):
        anyio.run(go)


def test_subagent_spec_subprocess_isolation_stubbed():
    spec = SubAgentSpec(
        name="x",
        system_prompt="y",
        model="mock-7b",
        isolation="subprocess",
    )
    t = as_subagent_tool(spec)

    async def go():
        await t.fn(prompt="hi")

    with pytest.raises(NotImplementedError, match="subprocess"):
        anyio.run(go)


def test_subagent_spec_remote_isolation_stubbed():
    spec = SubAgentSpec(
        name="x",
        system_prompt="y",
        model="mock-7b",
        isolation="remote",
    )
    t = as_subagent_tool(spec)

    async def go():
        await t.fn(prompt="hi")

    with pytest.raises(NotImplementedError, match="remote"):
        anyio.run(go)


# ===========================================================================
# Sub-agent execution via the wrap form
# ===========================================================================


def test_wrapped_agent_tool_runs_existing_agent_per_call():
    """Wrap form: reuses the same Agent instance across invocations.

    We assert that calling the tool twice runs the same provider twice
    (4 child stream calls total — 2 per invocation), and that each
    invocation gets a fresh message list (no state bleed)."""

    # Per-invocation script: tool call → final text. Repeated for the
    # second invocation.
    child_a_t1 = (
        _hdr() + _tool_call(0, "a-1", "fetch_url", '{"url": "u1"}') + _stop("tool_use")
    )
    child_a_t2 = _hdr() + _text(0, "answer for u1") + _stop()
    child_b_t1 = (
        _hdr() + _tool_call(0, "b-1", "fetch_url", '{"url": "u2"}') + _stop("tool_use")
    )
    child_b_t2 = _hdr() + _text(0, "answer for u2") + _stop()

    mock = ScriptedMock([child_a_t1, child_a_t2, child_b_t1, child_b_t2])

    agent = Agent(
        model="mock-7b",
        provider=mock,
        tools=[fetch_url],
        system="You research.",
        max_turns=4,
        include_memory=False,
    )
    try:
        t = as_subagent_tool(agent, name="researcher")

        async def go() -> tuple[str, str]:
            a = await t.fn(prompt="Look up u1")
            b = await t.fn(prompt="Look up u2")
            return a, b

        a, b = anyio.run(go)
        assert a == "answer for u1"
        assert b == "answer for u2"
        # 4 child stream calls total.
        assert mock.call_count == 4
    finally:
        anyio.run(agent.aclose)


def test_wrapped_agent_tool_isolates_per_invocation_messages():
    """A second invocation must not see messages from the first.

    Verified by running once, mutating the hypothetical 'state', then
    calling again — both calls get a fresh single UserMessage seed.
    """

    seen_first_messages: list[int] = []

    # We track what the mock sees per call so we can assert isolation.
    class IsolationMock(ScriptedMock):
        async def stream(self, **kw):
            msgs = list(kw.get("messages") or [])
            # Count user messages from the caller — should always be 1
            # because Agent prepends a user_context meta message but
            # that's still a UserMessage; the *substantive* user prompt
            # should be just one.
            non_meta_users = [
                m for m in msgs
                if isinstance(m, UserMessage) and not getattr(m, "isMeta", False)
            ]
            seen_first_messages.append(len(non_meta_users))
            async for ev in super().stream(**kw):
                yield ev

    child_t1 = _hdr() + _text(0, "ok") + _stop()
    mock = IsolationMock([child_t1, child_t1])

    agent = Agent(
        model="mock-7b",
        provider=mock,
        system="You research.",
        max_turns=2,
        include_memory=False,
    )
    try:
        t = as_subagent_tool(agent, name="r")

        async def go():
            await t.fn(prompt="first")
            await t.fn(prompt="second")

        anyio.run(go)
        # Each invocation should send exactly one substantive user
        # message — no carry-over.
        assert seen_first_messages == [1, 1]
    finally:
        anyio.run(agent.aclose)


# ===========================================================================
# Full multi-agent orchestration via query()
# ===========================================================================


def test_multi_agent_orchestration_parent_delegates_to_subagent():
    """Parent calls a wrapped sub-agent; sub-agent calls fetch_url;
    sub-agent answers; parent quotes the answer in its final turn.

    Asserts:
      * The parent's tool list contains the sub-agent's name.
      * The sub-agent gets invoked exactly once.
      * The parent's final message includes the sub-agent's text.
    """

    # Sub-agent script: tool call → final text.
    sub_t1 = (
        _hdr()
        + _tool_call(0, "s-1", "fetch_url", '{"url": "https://spawnlabs.ai"}')
        + _stop("tool_use")
    )
    sub_t2 = _hdr() + _text(0, "Spawn Labs builds AI agents.") + _stop()

    # Parent script: delegate to sub-agent → final text quoting it.
    parent_t1 = (
        _hdr()
        + _tool_call(
            0,
            "p-1",
            "researcher",
            '{"prompt": "What does Spawn Labs build?"}',
        )
        + _stop("tool_use")
    )
    parent_t2 = (
        _hdr() + _text(0, "Per research: Spawn Labs builds AI agents.") + _stop()
    )

    # The scripts run in the order: parent_t1, sub_t1, sub_t2, parent_t2.
    mock = ScriptedMock([parent_t1, sub_t1, sub_t2, parent_t2])

    sub_agent = Agent(
        model="mock-7b",
        provider=mock,
        tools=[fetch_url],
        system="You research.",
        max_turns=4,
        include_memory=False,
    )
    parent = Agent(
        model="mock-7b",
        provider=mock,
        tools=[as_subagent_tool(sub_agent, name="researcher")],
        system="You delegate.",
        max_turns=5,
        include_memory=False,
    )

    try:

        async def go():
            return await parent.run(
                [UserMessage(content="What does Spawn Labs build?")]
            )

        msgs = anyio.run(go)
    finally:
        anyio.run(sub_agent.aclose)
        anyio.run(parent.aclose)

    # Parent's first stream call should have surfaced the "researcher"
    # tool to the provider.
    first_tools = mock.received_tools[0]
    names = [t.get("name") for t in first_tools]
    assert "researcher" in names

    # 4 total stream calls: 2 parent + 2 sub-agent.
    assert mock.call_count == 4

    # The parent's transcript shape:
    #   [user, assistant(tool_use:researcher), user(tool_result), assistant(text)]
    assert isinstance(msgs[1], AssistantMessage)
    tool_uses = [b for b in msgs[1].content if isinstance(b, ToolUseBlock)]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "researcher"

    # The tool-result block on the parent should carry the sub-agent's
    # final text verbatim — that's the whole contract.
    result_block = msgs[2].content[0]
    assert isinstance(result_block, ToolResultBlock)
    assert result_block.is_error is False
    assert result_block.content == "Spawn Labs builds AI agents."

    # The parent's final text must quote / include the sub-agent's answer.
    final = "".join(
        b.text for b in msgs[-1].content if isinstance(b, TextBlock)
    )
    assert "Spawn Labs builds AI agents" in final


# ===========================================================================
# Example smoke test — keep multi_agent_research runnable
# ===========================================================================


def test_multi_agent_research_example_runs_in_mock_mode():
    """Run ``python -m mantis_agent.examples.multi_agent_research`` in
    mock mode as a subprocess. Verifies the example's wiring stays
    runnable end-to-end without touching the network.

    The example's main() asserts internally that the sub-agent path was
    exercised; if anything regresses we'll see a non-zero exit code.
    """

    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["MANTIS_AGENT_MOCK"] = "1"
    # Don't let the example try to read or write user memory in CI.
    env.setdefault("MANTIS_AGENT_HOME", str(repo_root / ".pytest_mantis_agent_home"))
    Path(env["MANTIS_AGENT_HOME"]).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mantis_agent.examples.multi_agent_research",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Print on failure so debugging is one scroll away.
    if result.returncode != 0:
        print("---- stdout ----")
        print(result.stdout)
        print("---- stderr ----")
        print(result.stderr)

    assert result.returncode == 0, "example exited non-zero in mock mode"
    assert "mock-mode smoke test passed" in result.stdout, (
        "example didn't reach its end-of-main success line"
    )
    # And the sub-agent's text should have flowed through to the parent.
    assert "Spawn Labs" in result.stdout


# ===========================================================================
# Public exports
# ===========================================================================


def test_public_exports_present():
    """Sub-agent helpers should be importable from the top-level package."""
    import mantis_agent as pkg

    for name in (
        "SubAgentSpec",
        "SubAgentTool",
        "WrappedAgentTool",
        "as_subagent_tool",
    ):
        assert hasattr(pkg, name), f"missing public export: {name}"
        assert name in pkg.__all__, f"{name} not in __all__"
