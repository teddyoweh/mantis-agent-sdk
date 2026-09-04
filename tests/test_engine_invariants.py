"""Message-shape invariants at the engine boundary.

Every provider rejects a transcript where a ``tool_use`` is not answered by a
``tool_result`` immediately after it (Anthropic and OpenAI both 400). The
engine must therefore (a) never *produce* such a list — including on the
exception / abandonment paths — and (b) heal or loudly reject one it is
handed, before the provider sees it.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent import Agent, BudgetExceededError, tool
from mantis_agent.agent import (
    MessageInvariantError,
    _assert_message_invariants,
    _repair_tool_call_history,
    aclose_stream,
)
from mantis_agent.budget import Budget
from mantis_agent.capabilities import HOSTED_PROFILES
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
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)


@tool
async def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


def _tool_call(idx: int, call_id: str, name: str, args: str) -> list:
    return [
        ContentBlockStart(index=idx, block=ToolUseBlock(id=call_id, name=name, input={})),
        ContentBlockDelta(index=idx, delta=InputJsonDelta(partial_json=args)),
        ContentBlockStop(index=idx),
    ]


def _text(idx: int, text: str) -> list:
    return [
        ContentBlockStart(index=idx, block=TextBlock(text="")),
        ContentBlockDelta(index=idx, delta=TextDelta(text=text)),
        ContentBlockStop(index=idx),
    ]


def _stop(reason: str = "end_turn") -> list:
    return [
        MessageDelta(stop_reason=reason, usage=Usage(input_tokens=5, output_tokens=5)),
        MessageStop(),
    ]


class _Scripted(MockProvider):
    """Replay a different event list per call; the last one repeats."""

    name = "mock"

    def __init__(self, scripts: list[list]) -> None:
        super().__init__()
        self._scripts = scripts
        self.n = 0

    async def stream(self, **kw: Any):
        script = self._scripts[min(self.n, len(self._scripts) - 1)]
        self.n += 1
        self.calls.append(kw)
        self.last_call_kwargs = kw
        for ev in script:
            yield ev
            await anyio.sleep(0)


def _agent(provider: Any, **kw: Any) -> Agent:
    kw.setdefault("tools", [add])
    kw.setdefault("max_steps", 4)
    return Agent(model="mock", provider=provider, auto_compact=False,
                 include_recall=False, include_env=False, include_memory=False, **kw)


# ---------------------------------------------------------------------------
# (a) tool_use ids that a provider left empty or duplicated
# ---------------------------------------------------------------------------


def test_empty_and_duplicate_tool_ids_are_made_unique_and_pair_correctly() -> None:
    """Gemini's OpenAI-compat endpoint omits tool-call ids; some vLLM builds
    reuse ``call_0`` for every call. Two calls sharing an id collapse into one
    result slot — the second result overwrites the first and BOTH tool_use
    blocks get the same answer. The engine must mint distinct ids."""
    turn1 = (
        [MessageStart(message_id="m1", model="mock")]
        + _tool_call(0, "", "add", '{"a": 1, "b": 2}')
        + _tool_call(1, "", "add", '{"a": 10, "b": 20}')
        + _tool_call(2, "dup", "add", '{"a": 100, "b": 200}')
        + _tool_call(3, "dup", "add", '{"a": 1000, "b": 2000}')
        + _stop("tool_use")
    )
    turn2 = [MessageStart(message_id="m2", model="mock")] + _text(0, "done") + _stop()
    prov = _Scripted([turn1, turn2])
    agent = _agent(prov)
    msgs: list = [UserMessage(content="add things")]

    anyio.run(agent.run, msgs)

    assistant = msgs[1]
    assert isinstance(assistant, AssistantMessage)
    uses = [b for b in assistant.content if isinstance(b, ToolUseBlock)]
    ids = [b.id for b in uses]
    assert len(uses) == 4
    assert all(ids), f"empty tool_use id survived: {ids}"
    assert len(set(ids)) == 4, f"duplicate tool_use ids survived: {ids}"

    results = {b.tool_use_id: b for b in msgs[2].content if isinstance(b, ToolResultBlock)}
    assert set(results) == set(ids)
    # Each result carries ITS call's answer, not a sibling's.
    by_input = {b.id: b.input["a"] + b.input["b"] for b in uses}
    for tid, expected in by_input.items():
        assert results[tid].content == str(expected), (tid, results[tid].content)
    _assert_message_invariants(msgs)


# ---------------------------------------------------------------------------
# validator + repair
# ---------------------------------------------------------------------------


def test_validator_accepts_well_formed_history() -> None:
    msgs = [
        UserMessage(content="hi"),
        AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="a", content="3")]),
        AssistantMessage(content=[TextBlock(text="done")]),
    ]
    _assert_message_invariants(msgs)


@pytest.mark.parametrize(
    "bad",
    [
        # dangling tool_use at the tail
        [AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})])],
        # tool_use followed by a plain user message, not its result
        [
            AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})]),
            UserMessage(content="next question"),
        ],
        # result for the wrong id
        [
            AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})]),
            UserMessage(content=[ToolResultBlock(tool_use_id="zzz", content="3")]),
        ],
        # orphan tool_result with no preceding tool_use
        [
            UserMessage(content="hi"),
            UserMessage(content=[ToolResultBlock(tool_use_id="a", content="3")]),
        ],
        # duplicate tool_use ids inside one assistant turn
        [
            AssistantMessage(content=[
                ToolUseBlock(id="a", name="add", input={}),
                ToolUseBlock(id="a", name="add", input={}),
            ]),
            UserMessage(content=[ToolResultBlock(tool_use_id="a", content="3")]),
        ],
    ],
)
def test_validator_rejects_malformed_history(bad: list) -> None:
    with pytest.raises(MessageInvariantError):
        _assert_message_invariants(bad)


def test_repair_drops_stale_tool_results_that_answer_nothing() -> None:
    """A result whose id matches no pending tool_use (a leftover from a rewound
    or hand-edited transcript) is a hard 400 on Anthropic. Repair must drop
    it while keeping the real result."""
    msgs = [
        UserMessage(content="hi"),
        AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})]),
        UserMessage(content=[
            ToolResultBlock(tool_use_id="a", content="3"),
            ToolResultBlock(tool_use_id="stale", content="???"),
        ]),
    ]
    repaired = _repair_tool_call_history(msgs)
    ids = [b.tool_use_id for b in repaired[2].content if isinstance(b, ToolResultBlock)]
    assert ids == ["a"]
    _assert_message_invariants(repaired)


def test_repaired_history_always_satisfies_invariants() -> None:
    """Every provider call goes through ``_repair_tool_call_history``; whatever
    it emits must pass the validator, for every malformed shape above."""
    shapes = [
        [UserMessage(content="q"), AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})])],
        [
            UserMessage(content="q"),
            AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})]),
            UserMessage(content="next"),
        ],
        [
            UserMessage(content="q"),
            AssistantMessage(content=[ToolUseBlock(id="a", name="add", input={})]),
            UserMessage(content=[ToolResultBlock(tool_use_id="zzz", content="3")]),
        ],
        [UserMessage(content="q"), UserMessage(content=[ToolResultBlock(tool_use_id="a", content="3")])],
    ]
    for shape in shapes:
        _assert_message_invariants(_repair_tool_call_history(shape))


# ---------------------------------------------------------------------------
# (d) the list is valid after the run dies mid-dispatch
# ---------------------------------------------------------------------------


def test_budget_exception_between_assistant_and_results_leaves_list_valid() -> None:
    """``BudgetExceededError`` fires right after the assistant turn materializes
    and BEFORE the tool results are appended. The caller's list must not be
    left ending on an unanswered tool_use."""
    turn = (
        [MessageStart(message_id="m1", model="mock")]
        + _tool_call(0, "c1", "add", '{"a": 1, "b": 2}')
        + _stop("tool_use")
    )
    prov = _Scripted([turn])
    agent = _agent(prov, budget=Budget(max_turns=1))
    msgs: list = [UserMessage(content="go")]

    async def go() -> None:
        with pytest.raises(BudgetExceededError):
            await agent.run(msgs)

    anyio.run(go)
    assert isinstance(msgs[-2], AssistantMessage)
    _assert_message_invariants(msgs)


def test_consumer_abandoning_run_iter_mid_tool_leaves_list_valid() -> None:
    """A UI that breaks out of ``run_iter`` after the assistant turn (Esc) and
    closes the generator must find every tool_use answered."""
    started = anyio.Event()

    @tool
    async def slow() -> str:
        """Sleep a while."""
        started.set()
        await anyio.sleep(5)
        return "late"

    turn = (
        [MessageStart(message_id="m1", model="mock")]
        + _tool_call(0, "c1", "slow", "{}")
        + _stop("tool_use")
    )
    prov = _Scripted([turn])
    agent = _agent(prov, tools=[slow])
    msgs: list = [UserMessage(content="go")]

    async def go() -> None:
        stream = agent.run_iter(msgs)
        async for m in stream:
            if isinstance(m, AssistantMessage):
                break
        await aclose_stream(stream)

    anyio.run(go)
    assert isinstance(msgs[-2], AssistantMessage)
    tail = msgs[-1]
    assert isinstance(tail, UserMessage) and isinstance(tail.content, list)
    assert [b.tool_use_id for b in tail.content] == ["c1"]
    _assert_message_invariants(msgs)


def test_provider_sees_only_valid_histories_across_a_tool_run() -> None:
    """Belt and braces: snapshot what the provider actually received on every
    call of a multi-turn tool run and validate each."""
    turn1 = (
        [MessageStart(message_id="m1", model="mock")]
        + _tool_call(0, "c1", "add", '{"a": 1, "b": 2}')
        + _stop("tool_use")
    )
    turn2 = [MessageStart(message_id="m2", model="mock")] + _text(0, "3") + _stop()
    prov = _Scripted([turn1, turn2])
    agent = _agent(prov)
    anyio.run(agent.run, [UserMessage(content="1+2?")])
    assert prov.n == 2
    for call in prov.calls:
        _assert_message_invariants(list(call["messages"]))
    assert HOSTED_PROFILES["mock"].supports_native_tools
