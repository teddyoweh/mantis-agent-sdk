"""close_open_tool_calls — after an interrupt, unanswered tool_use blocks get
synthetic tool_results so the next request stays well-formed (work is kept)."""

from __future__ import annotations

from mantis_agent.agent import close_open_tool_calls
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def _ids_answered(messages: list) -> tuple[set[str], set[str]]:
    uses, results = set(), set()
    for m in messages:
        c = getattr(m, "content", None)
        if isinstance(c, list):
            for b in c:
                if isinstance(b, ToolUseBlock):
                    uses.add(b.id)
                elif isinstance(b, ToolResultBlock):
                    results.add(b.tool_use_id)
    return uses, results


def test_closes_unanswered_tool_use() -> None:
    msgs = [
        UserMessage(content="run it"),
        AssistantMessage(content=[ToolUseBlock(id="c1", name="bash", input={"command": "ls"})]),
    ]
    n = close_open_tool_calls(msgs)
    assert n == 1
    uses, answered = _ids_answered(msgs)
    assert uses == answered            # every tool_use now answered
    # the synthetic result is flagged as an error with the interrupt note
    res = msgs[-1].content[0]
    assert isinstance(res, ToolResultBlock) and res.is_error
    assert "interrupted" in res.content


def test_noop_when_all_answered() -> None:
    msgs = [
        AssistantMessage(content=[ToolUseBlock(id="c1", name="bash", input={})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="c1", content="done")]),
    ]
    before = len(msgs)
    assert close_open_tool_calls(msgs) == 0
    assert len(msgs) == before         # nothing appended


def test_closes_multiple_in_one_message() -> None:
    msgs = [AssistantMessage(content=[
        ToolUseBlock(id="a", name="x", input={}),
        ToolUseBlock(id="b", name="y", input={}),
    ])]
    assert close_open_tool_calls(msgs) == 2
    _uses, answered = _ids_answered(msgs)
    assert answered == {"a", "b"}


def test_partial_answered_closes_only_the_gap() -> None:
    msgs = [
        AssistantMessage(content=[
            ToolUseBlock(id="a", name="x", input={}),
            ToolUseBlock(id="b", name="y", input={}),
        ]),
        UserMessage(content=[ToolResultBlock(tool_use_id="a", content="ok")]),  # only 'a' ran
    ]
    assert close_open_tool_calls(msgs) == 1     # only 'b'
    _uses, answered = _ids_answered(msgs)
    assert answered == {"a", "b"}


def test_no_tool_use_is_noop() -> None:
    msgs = [UserMessage(content="hi"), AssistantMessage(content=[TextBlock(text="hello")])]
    assert close_open_tool_calls(msgs) == 0
    assert len(msgs) == 2


def test_idempotent() -> None:
    msgs = [AssistantMessage(content=[ToolUseBlock(id="c1", name="bash", input={})])]
    assert close_open_tool_calls(msgs) == 1
    assert close_open_tool_calls(msgs) == 0     # second call finds nothing open


def test_result_slotted_before_following_user_message() -> None:
    # open tool_use with a NEW user message after it → result goes BETWEEN them,
    # not tacked on the end (order must stay tool_use → tool_result → user text).
    msgs = [
        AssistantMessage(content=[ToolUseBlock(id="c1", name="bash", input={})]),
        UserMessage(content="actually, do something else"),
    ]
    assert close_open_tool_calls(msgs) == 1
    assert [type(m).__name__ for m in msgs] == ["AssistantMessage", "UserMessage", "UserMessage"]
    assert isinstance(msgs[1].content[0], ToolResultBlock)     # the synthetic result
    assert msgs[2].content == "actually, do something else"    # the real message, after


def test_run_iter_self_heals_malformed_history() -> None:
    # A history ending in an unanswered tool_use must not break the next run —
    # run_iter closes it before the first provider call.
    import anyio

    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider

    async def go():
        agent = Agent(model="mock", provider=MockProvider(default_text="ok"),
                      auto_compact=False, include_recall=False, include_env=False,
                      include_memory=False)
        msgs = [
            UserMessage(content="run ls"),
            AssistantMessage(content=[ToolUseBlock(id="c1", name="bash", input={"command": "ls"})]),
        ]
        async for _ in agent.run_iter(msgs):
            pass
        return msgs

    msgs = anyio.run(go)
    uses, answered = _ids_answered(msgs)
    assert uses <= answered      # the dangling tool_use was closed, run completed
