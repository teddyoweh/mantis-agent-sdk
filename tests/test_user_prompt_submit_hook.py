"""UserPromptSubmit hook — fires per user turn; can inject context or block."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.agent import Agent
from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.hooks import HookResult, Hooks
from mantis_agent.types import TextBlock, UserMessage, Usage


class _Txt:
    name = "mock"

    def __init__(self) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]
        self.called = 0

    async def stream(self, **_kw: Any):
        self.called += 1
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text="answer"))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _run(hook) -> tuple[list, _Txt]:
    prov = _Txt()
    agent = Agent(model="mock", provider=prov, hooks=Hooks(user_prompt_submit=hook),
                  auto_compact=False, include_recall=False, include_env=False,
                  include_memory=False)
    msgs: list = [UserMessage(content="do the thing")]

    async def go():
        async for _ in agent.run_iter(msgs):
            pass
    anyio.run(go)
    return msgs, prov


def test_injects_context() -> None:
    async def hook(_ctx):
        return HookResult(note="Remember: the deploy target is staging.")
    msgs, prov = _run(hook)
    assert any(getattr(m, "isMeta", False) and "staging" in str(m.content) for m in msgs)
    assert prov.called == 1                       # prompt still ran


def test_blocks_prompt() -> None:
    async def hook(_ctx):
        return HookResult(block=True, note="Blocked: needs approval.")
    msgs, prov = _run(hook)
    assert prov.called == 0                        # model never called
    assert any("Blocked" in str(getattr(m, "content", "")) or
               any(getattr(b, "text", "") == "Blocked: needs approval."
                   for b in (m.content if isinstance(m.content, list) else []))
               for m in msgs)


def test_no_hook_is_noop() -> None:
    _msgs, prov = _run(None)
    assert prov.called == 1                        # runs normally, no injection


def test_hook_exception_does_not_crash() -> None:
    async def boom(_ctx):
        raise RuntimeError("hook bug")
    _msgs, prov = _run(boom)
    assert prov.called == 1                        # hook error swallowed; run continues
