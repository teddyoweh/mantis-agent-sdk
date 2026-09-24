"""Prefix-cache stability (item 15).

Local servers (vLLM prefix caching, llama.cpp ``cache_prompt`` / slot KV reuse,
Ollama KV reuse) only reuse the longest byte-identical prompt prefix: a change
at position k re-prefills everything after k. So every provider request must
start with the previous request's messages — only the per-request tail
projections (todo list, task evidence) may differ — and the tool list may only
grow at the end.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import msgspec

from mantis_agent import Agent, tool
from mantis_agent.agent import _is_tail_projection
from mantis_agent.builtin_tools.tool_search import make_tool_search
from mantis_agent.compact import SimpleCompactor, _message_token_estimate
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
from mantis_agent.task_state import TaskState
from mantis_agent.tools import ToolRegistry
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

_ENC = msgspec.json.Encoder()


def _wire(messages: list[Any]) -> list[bytes]:
    return [_ENC.encode(m) for m in messages]


def assert_prefix_stable(prev: dict[str, Any], nxt: dict[str, Any]) -> None:
    """Debug helper: request ``nxt`` must extend request ``prev`` byte-for-byte.

    ``prev``'s trailing tail projections are excluded (they are rebuilt per
    request by design); everything before them must reappear unchanged at the
    head of ``nxt``. The system prompt must be identical and the tool list may
    only append."""
    prev_msgs = list(prev["messages"])
    while prev_msgs and _is_tail_projection(prev_msgs[-1]):
        prev_msgs.pop()
    head, nxt_wire = _wire(prev_msgs), _wire(nxt["messages"])
    assert nxt_wire[: len(head)] == head, next(
        f"request diverged at message {i}: {a[:120]!r} -> {b[:120]!r}"
        for i, (a, b) in enumerate(zip(head, nxt_wire)) if a != b
    ) if len(nxt_wire) >= len(head) else "request history shrank"
    assert nxt["system"] == prev["system"]
    prev_tools = [t["name"] for t in prev["tools"] or []]
    nxt_tools = [t["name"] for t in nxt["tools"] or []]
    assert nxt_tools[: len(prev_tools)] == prev_tools, (prev_tools, nxt_tools)


class _Scripted(MockProvider):
    name = "mock"

    def __init__(self, turns: list[tuple[str, dict] | str]) -> None:
        super().__init__()
        self._turns = iter(turns)
        self.n = 0

    async def stream(self, **kw: Any):
        self.n += 1
        self.calls.append({**kw, "messages": list(kw["messages"])})
        step = next(self._turns)
        yield MessageStart(message_id=f"m{self.n}", model="mock")
        if isinstance(step, tuple):
            name, args = step
            yield ContentBlockStart(index=0, block=ToolUseBlock(id=f"c{self.n}", name=name, input={}))
            yield ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=json.dumps(args)))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=5, output_tokens=5))
        else:
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text=step))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=5, output_tokens=5))
        yield MessageStop()


def test_multi_turn_requests_extend_the_previous_prefix() -> None:
    todos: list[dict] = [{"content": "a", "status": "pending"},
                         {"content": "b", "status": "pending"}]

    @tool
    async def step() -> str:
        """Complete the next todo."""
        next(t for t in todos if t["status"] == "pending")["status"] = "completed"
        return "done"

    @tool
    async def zeta() -> str:
        """A deferred tool."""
        return "z"

    @tool
    async def omega() -> str:
        """An always-live tool registered after the deferred one."""
        return "o"

    registry = ToolRegistry()
    registry.add(step, zeta, omega)
    registry.add(make_tool_search(registry))
    registry.defer("zeta")

    provider = _Scripted([
        ("step", {}), ("tool_search", {"query": "select:zeta"}), ("zeta", {}),
        ("step", {}), "ok", "done",
    ])
    agent = Agent(model="mock", provider=provider, tools=registry, todos=todos,
                  task_state=TaskState(), include_env=False, include_memory=False,
                  include_recall=False, max_steps=8)
    messages: list = [UserMessage(content="work")]

    async def main() -> None:
        await agent.run(messages)
        messages.append(UserMessage(content="more"))
        await agent.run(messages)
        await agent.aclose()

    anyio.run(main)

    calls = provider.calls
    assert len(calls) == 6
    for prev, nxt in zip(calls, calls[1:]):
        assert_prefix_stable(prev, nxt)
    # The surfaced tool joined at the END of the list, not its registration slot.
    assert [t["name"] for t in calls[-1]["tools"]][-1] == "zeta"
    # Every request still carries fresh reminders at the tail…
    assert "[x] a" in str(calls[1]["messages"][-1].content)
    assert all(_is_tail_projection(c["messages"][-1]) for c in calls)
    # …but persisted history carries none of them.
    assert not any(_is_tail_projection(m) for m in messages)


def _history(n: int, size: int = 4_000) -> list:
    msgs: list = [UserMessage(content="go")]
    for i in range(n):
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"c{i}", name="read", input={})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"c{i}", content="x" * size)]))
    return msgs


def test_microcompact_hysteresis_rewrites_history_rarely() -> None:
    async def _fake(_p: str) -> str:
        return "S"

    window = 40_000
    comp = SimpleCompactor(_fake, context_window=window)
    msgs = _history(30)          # ~30k tokens: over the 60% micro threshold
    rewrites = 0
    for turn in range(12):
        used = sum(_message_token_estimate(m) for m in msgs)
        before = _wire(msgs)
        if comp.should_microcompact(msgs, Usage(input_tokens=used), window):
            assert comp.microcompact(msgs, used_tokens=used, ctx_window=window)
            rewrites += 1
            after = sum(_message_token_estimate(m) for m in msgs)
            assert after <= 0.4 * window    # cleared down to the low-water mark
        else:
            assert _wire(msgs) == before
        i = 100 + turn
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"c{i}", name="read", input={})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"c{i}", content="x" * 4_000)]))
    # The old per-turn behaviour rewrote history on every one of the 12 turns.
    assert 1 <= rewrites <= 3


def test_microcompact_never_clears_below_min_keep() -> None:
    async def _fake(_p: str) -> str:
        return "S"

    comp = SimpleCompactor(_fake, context_window=1_000, micro_min_keep=2)
    msgs = _history(10)
    comp.microcompact(msgs)
    kept = [b for m in msgs if isinstance(m.content, list) for b in m.content
            if isinstance(b, ToolResultBlock) and b.content == "x" * 4_000]
    assert [b.tool_use_id for b in kept] == ["c8", "c9"]


# --- review fixes -----------------------------------------------------------


def _strip_cache(obj: Any) -> Any:
    """Wire message with the cache marker removed and string content in its
    equivalent single-text-block form (Anthropic treats them the same)."""
    content = obj["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return {**obj, "content": [{k: v for k, v in b.items() if k != "cache_control"}
                               for b in content]}


def _breakpoint(payload: dict[str, Any]) -> int:
    marked = [i for i, m in enumerate(payload["messages"])
              if isinstance(m["content"], list)
              and any("cache_control" in b for b in m["content"])]
    assert len(marked) == 1, marked
    return marked[0]


def test_anthropic_breakpoint_skips_the_tail_and_prefix_is_reused() -> None:
    from mantis_agent.providers.anthropic_passthrough import build_messages_payload

    todos: list[dict] = [{"content": "a", "status": "pending"},
                         {"content": "b", "status": "pending"}]

    @tool
    async def step() -> str:
        """Complete the next todo."""
        next(t for t in todos if t["status"] == "pending")["status"] = "completed"
        return "done"

    provider = _Scripted([("step", {}), ("step", {}), "ok"])
    agent = Agent(model="mock", provider=provider, tools=[step], todos=todos,
                  task_state=TaskState(), include_env=False, include_memory=False,
                  include_recall=False, max_steps=5)
    anyio.run(agent.run, [UserMessage(content="work")])

    payloads = [build_messages_payload(model="claude-x", messages=c["messages"])
                for c in provider.calls]
    assert len(payloads) == 3
    for call, payload in zip(provider.calls, payloads):
        n_tail = 0
        while n_tail < len(call["messages"]) and _is_tail_projection(
                call["messages"][-1 - n_tail]):
            n_tail += 1
        assert n_tail >= 1                      # todos + evidence ride the tail…
        # …and the breakpoint lands on the last STABLE message before them.
        assert _breakpoint(payload) == len(payload["messages"]) - 1 - n_tail
    for prev, nxt in zip(payloads, payloads[1:]):
        bp = _breakpoint(prev)
        assert [_strip_cache(m) for m in nxt["messages"][: bp + 1]] == \
            [_strip_cache(m) for m in prev["messages"][: bp + 1]]


def test_mention_containing_the_sentinel_text_is_not_dropped() -> None:
    mention = UserMessage(
        content='Contents of @mantis_agent/agent.py:\n_TODO_SENTINEL = "[Current todo list]"\n'
                "[Current task evidence]\nmore",
        isMeta=True,
    )
    evidence_mention = UserMessage(content="[Current task evidence] is a heading", isMeta=True)
    assert not _is_tail_projection(mention)
    assert not _is_tail_projection(evidence_mention)
    from mantis_agent.agent import _render_todo_reminder
    legacy = UserMessage(content=_render_todo_reminder([{"content": "a"}]), isMeta=True)
    assert _is_tail_projection(legacy)
    assert _is_tail_projection(UserMessage(content="[Current task evidence]\n- x", isMeta=True))

    provider = _Scripted(["ok"])
    agent = Agent(model="mock", provider=provider, include_env=False,
                  include_memory=False, include_recall=False)
    anyio.run(agent.run, [UserMessage(content="hi"), mention, legacy, evidence_mention])
    sent = provider.calls[0]["messages"]
    assert mention in sent and evidence_mention in sent
    assert legacy not in sent


def _big_todos() -> list[dict]:
    return [{"content": "y" * 400, "status": "pending"} for _ in range(20)]


def test_still_over_limit_counts_the_tail(monkeypatch) -> None:
    agent = Agent(model="mock", provider=_Scripted([]), todos=_big_todos(),
                  include_env=False, include_memory=False, include_recall=False)
    msgs: list = [UserMessage(content="hi")]
    tail = sum(_message_token_estimate(m) for m in agent._tail_projections(msgs))
    assert tail > 1_000
    body = sum(_message_token_estimate(m) for m in msgs)
    window = int((body + tail) / 0.9)       # fits without the tail, not with it
    monkeypatch.setattr(agent, "_effective_context_window", lambda: window)
    monkeypatch.setattr(agent, "_prompt_overhead_tokens", lambda: 0)
    assert agent._still_over_limit(msgs)


def test_compaction_decisions_see_the_tail() -> None:
    seen: list[int] = []

    class _Recording(SimpleCompactor):
        async def should_compact(self, messages, usage, ctx_window):
            seen.append(len(messages))
            return False

    async def _fake(_p: str) -> str:
        return "S"

    provider = _Scripted(["ok"] * 5)
    agent = Agent(model="mock", provider=provider, todos=_big_todos(), max_steps=1,
                  compactor=_Recording(_fake), include_env=False,
                  include_memory=False, include_recall=False)
    msgs: list = [UserMessage(content="hi")]
    anyio.run(agent.run, msgs)
    # The request is [hi, todo tail]; the persisted history is [hi, reply].
    assert seen and seen[0] == 2


def test_deep_microcompact_keeps_current_prompt_images() -> None:
    from mantis_agent.types import ImageBlock

    async def _fake(_p: str) -> str:
        return "S"

    old_img = ImageBlock(source={"type": "base64", "data": "o" * 40_000})
    new_img = ImageBlock(source={"type": "base64", "data": "n" * 40_000})
    msgs: list = [UserMessage(content=[TextBlock(text="first"), old_img])]
    for i in range(10):          # small results: clearing them frees nothing
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"a{i}", name="read", input={})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"a{i}", content="ok")]))
    msgs.append(UserMessage(content=[TextBlock(text="look at this"), new_img]))
    for i in range(3):
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"b{i}", name="read", input={})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"b{i}", content="ok")]))

    comp = SimpleCompactor(_fake, context_window=2_000)
    used = sum(_message_token_estimate(m) for m in msgs)
    comp.microcompact(msgs, used_tokens=used, ctx_window=2_000)
    current = next(m for m in msgs if isinstance(m.content, list)
                   and any(isinstance(b, TextBlock) and b.text == "look at this"
                           for b in m.content))
    assert new_img in current.content          # pasted this turn: untouched
    assert old_img not in msgs[0].content      # old one outside the window: cleared
