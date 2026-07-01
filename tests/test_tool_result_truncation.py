"""Tool-result truncation backstop (T0.3) — a huge tool result can't blow the
context window in one turn."""

from __future__ import annotations

import anyio

from mantis_agent import (
    Agent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
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
from mantis_agent.streaming.executor import _truncate_tool_result
from mantis_agent.types import TextBlock


# -- unit ------------------------------------------------------------------


def test_small_result_untouched() -> None:
    assert _truncate_tool_result("hello", "bash") == "hello"


def test_large_result_truncated_head_and_tail() -> None:
    big = "START" + "A" * 100_000 + "END"
    out = _truncate_tool_result(big, "custom_tool")
    assert len(out) < len(big)
    assert "elided" in out
    assert out.startswith("START")   # head kept
    assert out.endswith("END")       # tail kept


def test_tool_aware_caps() -> None:
    big = "A" * 100_000
    # read_file gets a bigger budget than a generic tool.
    assert len(_truncate_tool_result(big, "read_file")) > len(
        _truncate_tool_result(big, "generic")
    )


def test_env_override(monkeypatch) -> None:
    import importlib

    import mantis_agent.streaming.executor as ex
    monkeypatch.setenv("MANTIS_AGENT_MAX_TOOL_RESULT", "5000")
    importlib.reload(ex)
    try:
        out = ex._truncate_tool_result("A" * 100_000, "generic")
        assert len(out) < 6000
    finally:
        monkeypatch.delenv("MANTIS_AGENT_MAX_TOOL_RESULT", raising=False)
        importlib.reload(ex)


# -- end-to-end through the executor ---------------------------------------


@tool(name="dump")
async def _dump(n: int) -> str:
    """Return a big blob."""
    return "X" * n


def _tool_turn(call_id: str, args_json: str) -> list:
    return [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(index=0, block=ToolUseBlock(id=call_id, name="dump", input={})),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=args_json)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=5, output_tokens=2)),
        MessageStop(),
    ]


def _text_turn() -> list:
    return [
        MessageStart(message_id="m2", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="done")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=2)),
        MessageStop(),
    ]


def test_huge_tool_result_capped_in_message() -> None:
    class _Two(MockProvider):
        def __init__(self):
            super().__init__()
            self._i = 0

        async def stream(self, **kw):
            script = _tool_turn("c1", '{"n": 500000}') if self._i == 0 else _text_turn()
            self._i += 1
            for ev in script:
                yield ev

    async def main():
        agent = Agent(model="mock-7b", provider=_Two(), tools=[_dump],
                      include_env=False, include_memory=False, include_recall=False)
        try:
            msgs = await agent.run([UserMessage(content="dump it")])
        finally:
            await agent.aclose()
        results = [
            b for m in msgs if isinstance(getattr(m, "content", None), list)
            for b in m.content if isinstance(b, ToolResultBlock)
        ]
        assert len(results) == 1
        # 500k chars requested, but the stored result is capped well under that.
        assert len(results[0].content) < 60_000
        assert "elided" in results[0].content

    anyio.run(main)
