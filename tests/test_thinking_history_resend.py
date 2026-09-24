"""Old reasoning is not re-sent every turn on the text-channel tool paths.

Paths B/C re-inline ``<think>`` only for assistant messages inside the current
agentic turn (after the most recent real user message); earlier turns' chain of
thought is dropped, like Qwen3 / DeepSeek-R1 / QwQ chat templates do. Path A
never sends reasoning back. Anthropic passthrough keeps every signed thinking
block (the API requires them on tool-use turns).
"""
from __future__ import annotations

from mantis_agent.providers.anthropic_passthrough import _encode_message as anth_encode
from mantis_agent.providers.base import current_turn_start, is_real_user_message
from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

TOOL = {"name": "bash", "description": "run", "input_schema": {"type": "object"}}


def _turn(n: int) -> list:
    return [
        UserMessage(content=f"task {n}"),
        UserMessage(content="<system-reminder>\nnudge\n</system-reminder>", isMeta=True),
        AssistantMessage(content=[
            ThinkingBlock(thinking=f"plan-{n}a", signature=f"sig-{n}a"),
            ToolUseBlock(id=f"c{n}", name="bash", input={"command": "ls"}),
        ]),
        UserMessage(content=[
            ToolResultBlock(tool_use_id=f"c{n}", content="ok"),
            TextBlock(text="<system-reminder>\nbudget\n</system-reminder>"),
        ]),
        AssistantMessage(content=[
            ThinkingBlock(thinking=f"plan-{n}b", signature=f"sig-{n}b"),
            TextBlock(text=f"done {n}"),
        ]),
    ]


def _history() -> list:
    # The third turn is still in flight: the final message is a tool result.
    return [*_turn(1), *_turn(2), *_turn(3)[:4]]


def _payload(path: str) -> dict:
    return OpenAICompatProvider(base_url="http://x/v1", api_key="x")._build_payload(
        model="qwen3-32b", messages=_history(), system=None, tools=[TOOL],
        max_tokens=100, temperature=None, extra=None, path=path,
    )


def _wire_text(payload: dict) -> str:
    return "\n".join(
        str(m.get("content") or "") for m in payload["messages"] if m["role"] == "assistant"
    )


def test_real_user_message_classification():
    assert is_real_user_message(UserMessage(content="hi"))
    assert is_real_user_message(UserMessage(content=[TextBlock(text="hi")]))
    assert not is_real_user_message(UserMessage(content="x", isMeta=True))
    assert not is_real_user_message(
        UserMessage(content=[ToolResultBlock(tool_use_id="a", content="ok")])
    )
    assert not is_real_user_message(UserMessage(content=[
        ToolResultBlock(tool_use_id="a", content="ok"),
        TextBlock(text="<system-reminder>\nx\n</system-reminder>"),
    ]))
    assert not is_real_user_message(AssistantMessage(content=[TextBlock(text="hi")]))


def test_current_turn_start_is_last_real_user_message():
    h = _history()
    assert current_turn_start(h) == 10
    assert current_turn_start([]) == 0
    # No real user message at all (headless continuation): keep everything.
    assert current_turn_start(h[2:4]) == 0


def test_path_b_only_resends_current_turn_thinking():
    text = _wire_text(_payload("B"))
    assert "<think>plan-3a</think>" in text
    for stale in ("plan-1a", "plan-1b", "plan-2a", "plan-2b"):
        assert stale not in text
    # Old turns keep their visible answers and tool calls.
    assert "done 1" in text and "done 2" in text
    assert text.count("<tool_call>") == 3


def test_path_c_matches_path_b():
    assert _wire_text(_payload("C")) == _wire_text(_payload("B"))


def test_path_a_never_sends_reasoning_back():
    payload = _payload("A")
    for m in payload["messages"]:
        assert "reasoning_content" not in m and "reasoning" not in m
        assert "plan-" not in str(m.get("content") or "")


def test_anthropic_passthrough_keeps_every_signed_thinking_block():
    encoded = [anth_encode(m) for m in _history()]
    thinking = [
        b for m in encoded if isinstance(m["content"], list)
        for b in m["content"] if b.get("type") == "thinking"
    ]
    assert [b["signature"] for b in thinking] == ["sig-1a", "sig-1b", "sig-2a", "sig-2b", "sig-3a"]


def test_old_thinking_only_turn_keeps_an_empty_content_field():
    """Dropping an earlier turn's reasoning must not leave an assistant message
    with neither content nor tool_calls (strict templates reject it)."""
    from mantis_agent.providers.openai_compat import _encode_assistant_blocks
    from mantis_agent.types import ThinkingBlock

    for path in ("A", "B", "C"):
        (msg,) = _encode_assistant_blocks(
            [ThinkingBlock(thinking="hmm", signature="")], path=path, keep_thinking=False,
        )
        assert msg == {"role": "assistant", "content": ""}
