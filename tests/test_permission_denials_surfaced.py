"""``SDKResultMessage.permission_denials`` carries every denied call.

Production observability: a UI / audit pipeline reading the final
result message should see every (tool_name, tool_use_id, tool_input)
the permission layer rejected — without having to walk the message
stream looking for is_error tool_result blocks.
"""

from __future__ import annotations

import anyio

from mantis_agent import (
    MantisAgentOptions,
    PermissionResultDeny,
    ResultMessage,
    Tool,
    ToolUseBlock,
    Usage,
    query,
    tool,
)
from mantis_agent import TextBlock
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


@tool
async def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


def _two_turn_script(args_json: str, final_text: str = "ok") -> tuple[list, list]:
    turn1 = [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(index=0, block=ToolUseBlock(id="c1", name="add", input={})),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=args_json)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
        MessageStop(),
    ]
    turn2 = [
        MessageStart(message_id="m2", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=final_text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=8, output_tokens=3)),
        MessageStop(),
    ]
    return turn1, turn2


class _TwoTurnMock(MockProvider):
    def __init__(self, t1, t2):
        super().__init__()
        self._t1, self._t2 = t1, t2
        self._turn = 0

    async def stream(self, **kw):
        script = self._t1 if self._turn == 0 else self._t2
        self._turn += 1
        for ev in script:
            yield ev


def test_denial_surfaces_in_result_permission_denials() -> None:
    """can_use_tool denies → ResultMessage.permission_denials has one entry
    with the right tool_name, tool_use_id, and tool_input."""

    turn1, turn2 = _two_turn_script('{"a": 2, "b": 3}')
    provider = _TwoTurnMock(turn1, turn2)

    async def can_use(t: Tool, inp, ctx) -> PermissionResultDeny:
        return PermissionResultDeny(message="not today")

    async def main() -> ResultMessage:
        result = None
        async for msg in query(
            prompt="add 2 and 3",
            options=MantisAgentOptions(
                model="mock-7b",
                backend="mock",
                tools=[add],
                max_turns=5,
                can_use_tool=can_use,
                include_memory=False,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result = msg
        return result

    # Register the mock under the "mock" provider name so the agent uses it.
    from mantis_agent.providers.base import register
    register("mock", lambda: provider)

    result = anyio.run(main)
    assert result is not None
    assert isinstance(result.permission_denials, list)
    assert len(result.permission_denials) == 1
    d = result.permission_denials[0]
    assert d["tool_name"] == "add"
    assert d["tool_use_id"] == "c1"
    assert d["tool_input"] == {"a": 2, "b": 3}


def test_clean_run_has_empty_denials_list() -> None:
    """No can_use_tool → ResultMessage.permission_denials is empty list."""

    turn1, turn2 = _two_turn_script('{"a": 1, "b": 1}', final_text="done")
    provider = _TwoTurnMock(turn1, turn2)

    async def main() -> ResultMessage:
        result = None
        async for msg in query(
            prompt="add 1 and 1",
            options=MantisAgentOptions(
                model="mock-7b",
                backend="mock",
                tools=[add],
                max_turns=5,
                include_memory=False,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result = msg
        return result

    from mantis_agent.providers.base import register
    register("mock", lambda: provider)

    result = anyio.run(main)
    assert result.permission_denials == []
