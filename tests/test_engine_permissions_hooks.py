"""Permission + hook robustness at the engine.

* A crashing ``can_use_tool`` is a DENIAL (fail closed) that is recorded on
  ``ResultMessage.permission_denials`` — never a crash that hides every other
  result, never a silent allow.
* ``_permission_denials`` is per run: reusing one ``Agent`` for several runs
  must not re-report the previous run's denials.
* Hook exceptions (PreToolUse / PostToolUse / Stop) never kill the run.
* The ``Stop`` hook fires exactly once — on a natural stop and on a
  cancellation mid-tool.
"""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent import Agent, PermissionResultDeny, tool
from mantis_agent.agent import _assert_message_invariants
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
from mantis_agent.hooks import HookResult, Hooks
from mantis_agent.permissions import PermissionContext
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, Usage, UserMessage

RAN: list[str] = []


@tool
async def write_file(path: str) -> str:
    """Pretend to write."""
    RAN.append(path)
    return f"wrote {path}"


class _Provider(MockProvider):
    name = "mock"

    def __init__(self, tool_turns: int = 1) -> None:
        super().__init__()
        self._tool_turns = tool_turns
        self.n = 0

    async def stream(self, **kw: Any):
        self.n += 1
        yield MessageStart(message_id=f"m{self.n}", model="mock")
        if self.n <= self._tool_turns:
            yield ContentBlockStart(index=0, block=ToolUseBlock(id=f"c{self.n}", name="write_file", input={}))
            yield ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json='{"path": "/tmp/x"}'))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=1, output_tokens=1))
        else:
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="ok"))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _make(provider: Any, **kw: Any) -> Agent:
    kw.setdefault("tools", [write_file])
    kw.setdefault("max_steps", 4)
    return Agent(model="mock", provider=provider, auto_compact=False,
                 include_recall=False, include_env=False, include_memory=False, **kw)


def _results(msgs: list) -> list[ToolResultBlock]:
    return [b for m in msgs if isinstance(m, UserMessage) and isinstance(m.content, list)
            for b in m.content if isinstance(b, ToolResultBlock)]


# ---------------------------------------------------------------------------
# (h) permissions
# ---------------------------------------------------------------------------


def test_crashing_can_use_tool_is_a_recorded_denial() -> None:
    RAN.clear()

    async def boom(tool_name, tool_input, ctx):  # noqa: ANN001
        raise RuntimeError("policy service down")

    agent = _make(_Provider(), permissions=PermissionContext(mode="default", can_use_tool=boom))
    msgs: list = [UserMessage(content="write")]
    anyio.run(agent.run, msgs)  # must not raise

    assert RAN == [], "tool ran despite the permission check crashing"
    assert len(agent._permission_denials) == 1
    d = agent._permission_denials[0]
    assert d["tool_name"] == "write_file" and d["tool_use_id"] == "c1"
    (res,) = _results(msgs)
    assert res.is_error and "permission" in res.content.lower()
    _assert_message_invariants(msgs)


def test_permission_denials_are_per_run() -> None:
    async def deny(tool_name, tool_input, ctx):  # noqa: ANN001
        return PermissionResultDeny(message="no")

    agent = _make(_Provider(tool_turns=1), permissions=PermissionContext(mode="default", can_use_tool=deny))

    async def go() -> None:
        await agent.run([UserMessage(content="one")])
        assert len(agent._permission_denials) == 1
        agent.provider.n = 0  # replay the script
        await agent.run([UserMessage(content="two")])
        assert len(agent._permission_denials) == 1, "denials from the previous run leaked into this one"

    anyio.run(go)


# ---------------------------------------------------------------------------
# (g) hooks
# ---------------------------------------------------------------------------


def test_raising_pre_post_and_stop_hooks_never_kill_the_run() -> None:
    RAN.clear()
    fired = {"stop": 0}

    async def pre(ctx):  # noqa: ANN001
        raise ValueError("pre boom")

    async def post(ctx):  # noqa: ANN001
        raise ValueError("post boom")

    async def stop(ctx):  # noqa: ANN001
        fired["stop"] += 1
        raise ValueError("stop boom")

    agent = _make(_Provider(), hooks=Hooks(pre_tool_use=pre, post_tool_use=post, stop=stop))
    msgs: list = [UserMessage(content="write")]
    anyio.run(agent.run, msgs)
    assert RAN == ["/tmp/x"]  # fail-open default: the tool still ran
    assert fired["stop"] == 1
    assert isinstance(msgs[-1], AssistantMessage)


def test_stop_hook_fires_exactly_once_on_natural_stop() -> None:
    fired: list[str] = []

    async def stop(ctx):  # noqa: ANN001
        fired.append(ctx.event)
        return HookResult()

    agent = _make(_Provider(tool_turns=2), hooks=Hooks(stop=stop))
    anyio.run(agent.run, [UserMessage(content="write twice")])
    assert fired == ["Stop"]


def test_stop_hook_fires_exactly_once_on_cancel_mid_tool() -> None:
    fired: list[str] = []
    agent_ref: dict[str, Agent] = {}

    @tool
    async def cancel_me() -> str:
        """Cancel the agent from inside a tool body, then block."""
        agent_ref["a"].cancel()
        await anyio.sleep(5)
        return "never"

    async def stop(ctx):  # noqa: ANN001
        fired.append(ctx.event)

    class _P(MockProvider):
        name = "mock"

        async def stream(self, **kw: Any):
            yield MessageStart(message_id="m", model="mock")
            yield ContentBlockStart(index=0, block=ToolUseBlock(id="c1", name="cancel_me", input={}))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=1, output_tokens=1))
            yield MessageStop()

    agent = _make(_P(), tools=[cancel_me], hooks=Hooks(stop=stop))
    agent_ref["a"] = agent
    msgs: list = [UserMessage(content="go")]

    async def go() -> None:
        with anyio.fail_after(3):
            await agent.run(msgs)

    anyio.run(go)
    assert fired == ["Stop"]
    _assert_message_invariants(msgs)
    (res,) = _results(msgs)
    assert res.is_error and "cancel" in res.content
