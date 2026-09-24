"""Turn-driver safety in the terminal UIs.

* A mid-turn error keeps the tool rounds that already ran (their edits are on
  disk) instead of wiping the turn — like an interrupt does.
* A renderer exception falls back to plain text and never reaches the turn.
* Ctrl+C at a permission / question prompt cancels the turn rather than
  answering "deny" and letting the model carry on.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from mantis_agent.tui import plain_message_text, render_or_plain, settle_failed_turn
from mantis_agent.tui_fullscreen import await_prompt
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def _assert_all_tool_uses_answered(messages: list) -> None:
    for i, m in enumerate(messages):
        if isinstance(m, AssistantMessage):
            ids = {b.id for b in m.content if isinstance(b, ToolUseBlock)}
            if ids:
                nxt = messages[i + 1]
                got = {b.tool_use_id for b in nxt.content if isinstance(b, ToolResultBlock)}
                assert ids <= got


# -- ITEM 3: a failed turn keeps its partial work ------------------------------


def test_error_after_tool_rounds_keeps_partial_turn_and_heals_it() -> None:
    prior = [UserMessage(content="earlier"), AssistantMessage(content=[TextBlock(text="ok")])]
    msgs = list(prior)
    base = len(msgs)
    msgs += [
        UserMessage(content="fix the bug"),
        AssistantMessage(content=[ToolUseBlock(id="t1", name="edit", input={})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="edited")]),
        # the provider died after announcing this call; it never ran
        AssistantMessage(content=[ToolUseBlock(id="t2", name="bash", input={})]),
    ]
    assert settle_failed_turn(msgs, base) is True
    assert msgs[:base] == prior
    assert msgs[base].content == "fix the bug"          # the prompt survives
    assert msgs[base + 2].content[0].content == "edited"  # the done round survives
    _assert_all_tool_uses_answered(msgs)
    healed = msgs[-1].content[0]
    assert healed.tool_use_id == "t2" and healed.is_error


def test_error_before_any_reply_rolls_the_turn_back() -> None:
    msgs = [UserMessage(content="earlier")]
    base = len(msgs)
    msgs += [UserMessage(content="hi"), UserMessage(content="<mentions>", isMeta=True)]
    assert settle_failed_turn(msgs, base) is False
    assert len(msgs) == base  # a retry won't stack duplicate prompts


def test_render_failure_falls_back_to_plain_text() -> None:
    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=80)
    msg = AssistantMessage(content=[TextBlock(text="[bold]hello"),
                                   ToolUseBlock(id="t", name="read_file", input={})])

    def _boom() -> None:
        raise ValueError("renderer bug")

    render_or_plain(_boom, msg, console)  # must not raise
    out = buf.getvalue()
    assert "[bold]hello" in out  # printed literally (markup off)
    assert "read_file" in out


def test_plain_text_covers_tool_results() -> None:
    m = UserMessage(content=[ToolResultBlock(tool_use_id="t", content="42 passed")])
    assert "42 passed" in plain_message_text(m)


# -- ITEM 4: Ctrl+C at a prompt cancels the turn -------------------------------


def test_cancel_at_permission_prompt_reraises_and_dismisses_overlay() -> None:
    async def main() -> None:
        state: dict = {"pending_perm": None}
        fut = asyncio.get_running_loop().create_future()
        payload = {"future": fut, "prompt": "bash rm -rf", "sel": 0}
        task = asyncio.ensure_future(
            await_prompt(state, "pending_perm", payload, fut, cancelled="deny"))
        await asyncio.sleep(0)
        assert state["pending_perm"] is payload  # overlay is up
        task.cancel()                            # Ctrl+C
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["pending_perm"] is None     # no stuck overlay
        assert fut.done()                        # nothing left waiting on it

    asyncio.run(main())


def test_explicit_deny_is_an_answer_not_a_cancel() -> None:
    async def main() -> None:
        state: dict = {"pending_perm": None}
        fut = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(
            await_prompt(state, "pending_perm", {"future": fut}, fut, cancelled="deny"))
        await asyncio.sleep(0)
        fut.set_result("deny")  # Esc / "n"
        assert await task == "deny"
        assert state["pending_perm"] is None

    asyncio.run(main())


def test_cancel_at_question_prompt_cancels_the_turn() -> None:
    async def main() -> None:
        state: dict = {"pending_question": None}
        fut = asyncio.get_running_loop().create_future()

        async def turn() -> str:
            await await_prompt(state, "pending_question", {"future": fut}, fut, cancelled=[])
            return "model kept going"  # must never be reached

        task = asyncio.ensure_future(turn())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["pending_question"] is None

    asyncio.run(main())


def test_rollback_still_heals_when_compaction_left_base_stale() -> None:
    # A mid-turn compaction rewrites the list in place (``messages[:] = ...``),
    # so it can end up SHORTER than ``base`` with an unanswered call in it.
    msgs = [
        UserMessage(content="[summary of earlier work]", isMeta=True),
        UserMessage(content="fix the bug"),
        AssistantMessage(content=[ToolUseBlock(id="t9", name="bash", input={})]),
    ]
    base = 7  # captured before the compaction
    settle_failed_turn(msgs, base)
    _assert_all_tool_uses_answered(msgs)


def test_ctrl_c_at_permission_prompt_cancels_a_real_agent_run() -> None:
    """End to end through the engine: the permission resolver, ``_preflight_call``
    and the streaming executor must let the cancel through — never record it as a
    fail-closed denial and let the model carry on."""

    from mantis_agent import Agent, tool
    from mantis_agent.agent import _assert_message_invariants, aclose_stream
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
    from mantis_agent.permissions import PermissionContext
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import Usage

    ran: list[str] = []

    @tool(name="write_file", is_read_only=False, is_concurrency_safe=False)
    async def _write(path: str) -> str:
        """Stub mutating write."""
        ran.append(path)
        return "wrote"

    tool_turn = [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(index=0, block=ToolUseBlock(id="c1", name="write_file", input={})),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json='{"path": "/a"}')),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=1, output_tokens=1)),
        MessageStop(),
    ]
    text_turn = [
        MessageStart(message_id="m2", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="carried on")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1)),
        MessageStop(),
    ]

    class _Scripted(MockProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def stream(self, **kw):
            script = (tool_turn, text_turn)[min(self.calls, 1)]
            self.calls += 1
            for ev in script:
                yield ev

    async def main() -> None:
        state: dict = {"pending_perm": None}
        shown = asyncio.Event()
        futs: list = []

        async def asker(t, inp, prompt):  # the full-screen asker's shape
            fut = asyncio.get_running_loop().create_future()
            futs.append(fut)
            shown.set()
            return await await_prompt(state, "pending_perm", {"future": fut}, fut,
                                      cancelled="deny")

        provider = _Scripted()
        agent = Agent(model="mock-7b", provider=provider, tools=[_write], max_turns=4,
                      permissions=PermissionContext(mode="default", asker=asker),
                      include_memory=False)
        msgs: list = [UserMessage(content="go")]

        async def handle() -> None:
            stream = agent.run_iter(msgs)
            try:
                async for _ in stream:
                    pass
            finally:
                await aclose_stream(stream)

        task = asyncio.ensure_future(handle())
        await shown.wait()
        task.cancel()  # Ctrl+C while the Allow/Deny overlay is up
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            assert ran == []                     # the tool never ran
            assert provider.calls == 1           # the model was NOT re-prompted
            assert agent._permission_denials == []  # a cancel is not a denial
            assert state["pending_perm"] is None and futs[0].done()
            from mantis_agent.agent import close_open_tool_calls
            close_open_tool_calls(msgs)          # what the turn driver does next
            _assert_message_invariants(msgs)
        finally:
            await agent.aclose()

    asyncio.run(main())
