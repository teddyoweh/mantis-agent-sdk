from __future__ import annotations

from mantis_agent.agent import _repair_tool_call_history
from mantis_agent.types import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage


def test_repair_synthesizes_missing_tool_result_before_next_user_message() -> None:
    call = ToolUseBlock(id="call_1", name="bash", input={"command": "echo hi"})
    messages = [
        UserMessage(content="hi"),
        AssistantMessage(content=[TextBlock(text="running"), call]),
        UserMessage(content="next user turn"),
    ]

    repaired = _repair_tool_call_history(messages)

    assert isinstance(repaired[2], UserMessage)
    assert isinstance(repaired[2].content, list)
    result = repaired[2].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.tool_use_id == "call_1"
    assert result.is_error is True
    assert repaired[3] is messages[2]


def test_repair_preserves_existing_tool_results() -> None:
    call = ToolUseBlock(id="call_1", name="bash", input={})
    result = ToolResultBlock(tool_use_id="call_1", content="ok")
    messages = [AssistantMessage(content=[call]), UserMessage(content=[result])]

    assert _repair_tool_call_history(messages) == messages


def test_repair_drops_orphan_tool_result() -> None:
    orphan = UserMessage(content=[ToolResultBlock(tool_use_id="missing", content="ok")])

    assert _repair_tool_call_history([UserMessage(content="hi"), orphan]) == [UserMessage(content="hi")]


def test_repair_backfills_partial_tool_results() -> None:
    a = ToolUseBlock(id="a", name="one", input={})
    b = ToolUseBlock(id="b", name="two", input={})
    existing = ToolResultBlock(tool_use_id="a", content="ok")
    messages = [AssistantMessage(content=[a, b]), UserMessage(content=[existing])]

    repaired = _repair_tool_call_history(messages)

    # The valid result and the synthetic backfill for the orphaned tool_use now
    # live in the SAME user message immediately after the assistant turn, so
    # every result directly answers the call and no real result is stranded in a
    # separate message. The existing result is preserved untouched; only the
    # genuinely missing tool_use gets a synthetic is_error result.
    assert len(repaired) == 2
    assert isinstance(repaired[1].content, list)
    by_id = {blk.tool_use_id: blk for blk in repaired[1].content}
    assert set(by_id) == {"a", "b"}
    assert by_id["a"].content == "ok"
    assert by_id["a"].is_error is False
    assert by_id["b"].is_error is True
