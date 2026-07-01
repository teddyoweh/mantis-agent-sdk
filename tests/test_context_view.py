"""/context breakdown — context_breakdown(messages, system_text)."""

from __future__ import annotations

from mantis_agent import AssistantMessage, TextBlock, UserMessage
from mantis_agent.system_reminder import wrap_system_reminder
from mantis_agent.tui_fullscreen import context_breakdown


def test_categorizes_system_context_and_conversation() -> None:
    head = UserMessage(content=wrap_system_reminder("<env>\ncwd /x\n</env>"), isMeta=True)
    msgs = [
        head,
        UserMessage(content="hello there, please help with the redis cache"),
        AssistantMessage(content=[TextBlock(text="sure, here's what I found")]),
    ]
    bd = context_breakdown(msgs, system_text="You are Mantis. " * 20)

    assert bd["system"] > 0                 # system prompt counted
    assert bd["context"] > 0                # the isMeta head counted as context
    assert bd["conversation"] > 0           # real turns counted as conversation
    assert bd["total"] == bd["system"] + bd["context"] + bd["conversation"]


def test_empty() -> None:
    bd = context_breakdown([], "")
    assert bd == {"system": 0, "context": 0, "conversation": 0, "total": 0}


def test_meta_head_not_counted_as_conversation() -> None:
    head = UserMessage(content="x" * 400, isMeta=True)
    convo = UserMessage(content="y" * 400)
    bd = context_breakdown([head, convo], "")
    assert bd["context"] > 0
    assert bd["conversation"] > 0
    # the two are separated, not lumped together
    assert bd["context"] != bd["total"]
