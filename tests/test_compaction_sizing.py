"""Compaction is sized against the right numbers.

* Reported ``input_tokens`` already include the system prompt + tool schemas,
  while thresholds are fractions of ``_message_budget()`` (window minus that
  overhead). Comparing the raw figure double-counted the overhead: 13k of tool
  schemas on a 32k window compacted at ~3k tokens of real conversation.
* A microcompaction that frees enough must avert the summarize — the re-check
  uses the fresh estimate, not the stale pre-clear usage.
* The summarizer prompt scales with the window and the summarize call asks
  for a bounded ``max_tokens``, so it fits the small window it is rescuing.
* A failing summarizer is logged, not silent.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from mantis_agent import Agent, tool
from mantis_agent.compact import (
    SimpleCompactor,
    _summarizer_prompt_cap,
    run_manual_compaction,
    summary_reply_tokens,
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
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@tool
async def bump() -> str:
    """Bump."""
    return "bumped"


class _Provider(MockProvider):
    """Turn 1: a tool call reporting ``input_tokens``. Turn 2: a plain answer."""

    name = "mock"

    def __init__(self, reported_input: int) -> None:
        super().__init__()
        self.reported_input = reported_input
        self.n = 0
        self.kwargs: list[dict[str, Any]] = []

    async def stream(self, **kw: Any):
        self.n += 1
        self.kwargs.append(kw)
        yield MessageStart(message_id=f"m{self.n}", model="mock")
        if self.n == 1:
            yield ContentBlockStart(index=0, block=ToolUseBlock(id="c-live", name="bump", input={}))
            yield ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json="{}"))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="tool_use",
                               usage=Usage(input_tokens=self.reported_input, output_tokens=10))
        else:
            yield ContentBlockStart(index=0, block=TextBlock(text=""))
            yield ContentBlockDelta(index=0, delta=TextDelta(text="done"))
            yield ContentBlockStop(index=0)
            yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


class _Summarizer:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "Summary of prior conversation: stuff happened."


def _agent(provider: Any, summarizer: _Summarizer, monkeypatch, *, window: int,
           overhead: int) -> Agent:
    agent = Agent(
        model="mock", provider=provider, tools=[bump], max_steps=4,
        compactor=SimpleCompactor(summarizer, keep_recent_turns=2),
        include_recall=False, include_env=False, include_memory=False,
    )
    monkeypatch.setattr(agent, "_effective_context_window", lambda: window)
    monkeypatch.setattr(agent, "_prompt_overhead_tokens", lambda: overhead)
    return agent


def _chat_history(n: int = 10) -> list:
    msgs: list = []
    for i in range(n):
        msgs.append(UserMessage(content=f"question {i}"))
        msgs.append(AssistantMessage(content=[TextBlock(text=f"answer {i}")]))
    msgs.append(UserMessage(content="go"))
    return msgs


# --- (a) overhead is not double-counted ------------------------------------


@pytest.mark.parametrize(
    ("message_tokens", "expect_summarize"),
    [(5_000, False), (17_000, True)],
)
async def test_tool_overhead_is_not_double_counted(monkeypatch, message_tokens, expect_summarize):
    # 32k window, 13k of system prompt + tool schemas → 19k message budget;
    # full compaction at 85% of that (~16.1k). The provider's reported input
    # includes the overhead, so 13k + 5k must NOT compact, 13k + 17k must.
    summ = _Summarizer()
    agent = _agent(_Provider(13_000 + message_tokens), summ, monkeypatch,
                   window=32_000, overhead=13_000)
    try:
        await agent.run(_chat_history())
    finally:
        await agent.aclose()
    assert bool(summ.prompts) is expect_summarize


def test_message_usage_floors_at_zero(monkeypatch) -> None:
    agent = Agent(model="mock", provider=MockProvider(), include_memory=False)
    monkeypatch.setattr(agent, "_prompt_overhead_tokens", lambda: 5_000)
    assert agent._message_usage(Usage(input_tokens=3_000)).input_tokens == 0
    assert agent._message_usage(Usage(input_tokens=8_000, output_tokens=7)) == Usage(
        input_tokens=3_000, output_tokens=7)
    assert agent._message_usage(None) == Usage()


# --- (b) micro that frees enough prevents summarize ---------------------------


def _tool_heavy_history(rounds: int = 12, size: int = 4_000) -> list:
    msgs: list = [UserMessage(content="read all the files")]
    for i in range(rounds):
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"old{i}", name="bump", input={})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"old{i}",
                                                         content="x" * size)]))
    msgs.append(AssistantMessage(content=[TextBlock(text="read them")]))
    msgs.append(UserMessage(content="now continue"))
    return msgs


async def test_microcompaction_averts_summarize(monkeypatch) -> None:
    # Stale reported usage (28k) is over the 85% full threshold of a 32k
    # window; the transcript itself is ~12k tokens, most of it old tool
    # results micro can clear. After the clear the fresh estimate is well
    # under threshold, so no summarizer call may happen.
    summ = _Summarizer()
    agent = _agent(_Provider(28_000), summ, monkeypatch, window=32_000, overhead=0)
    history = _tool_heavy_history()
    try:
        await agent.run(history)
    finally:
        await agent.aclose()
    cleared = [b for m in history if isinstance(m, UserMessage) and isinstance(m.content, list)
               for b in m.content if isinstance(b, ToolResultBlock)
               and "x" * 4_000 != b.content]
    assert cleared, "microcompaction should have cleared old tool results"
    assert summ.prompts == []


# --- (c) summarizer fits a small window ---------------------------------------


async def test_summarizer_prompt_respects_scaled_cap_on_8k_window() -> None:
    summ = _Summarizer()
    comp = SimpleCompactor(summ, keep_recent_turns=2, context_window=8_192)
    history = [UserMessage(content="goal")]
    for i in range(40):
        history.append(UserMessage(content=f"q{i} " + "detail " * 500))
        history.append(AssistantMessage(content=[TextBlock(text=f"a{i} " + "words " * 500)]))
    out = await comp.compact(history)
    assert out is not history
    assert len(summ.prompts) == 1
    # Window minus the reply reservation (8192 // 4) and the system line, at a
    # conservative ~3 chars/token.
    assert len(summ.prompts[0]) <= (8_192 - 2_048 - 256) * 3


async def test_summarizer_prompt_unscaled_when_window_unknown() -> None:
    summ = _Summarizer()
    comp = SimpleCompactor(summ, keep_recent_turns=2)
    history = [UserMessage(content="goal")]
    for i in range(20):
        history.append(UserMessage(content=f"q{i} " + "detail " * 400))
        history.append(AssistantMessage(content=[TextBlock(text=f"a{i}")]))
    await comp.compact(history)
    assert len(summ.prompts[0]) > 8_192 * 2


async def test_summarize_call_bounds_max_tokens(monkeypatch) -> None:
    provider = _Provider(1)
    provider.n = 1  # skip the tool turn: answer with text
    agent = Agent(model="mock", provider=provider, include_memory=False, max_tokens=32_000,
                  compactor=SimpleCompactor(_Summarizer(), summary_token_budget=2048))
    # A large window, so the quarter-of-window cap doesn't bind.
    monkeypatch.setattr(agent, "_effective_context_window", lambda: 128_000)
    try:
        await agent._summarize("summarize this")
    finally:
        await agent.aclose()
    assert provider.kwargs[-1]["max_tokens"] == int(2048 * 1.25)


async def test_small_agent_max_tokens_is_kept() -> None:
    provider = _Provider(1)
    provider.n = 1
    agent = Agent(model="mock", provider=provider, include_memory=False, max_tokens=1_000,
                  compactor=SimpleCompactor(_Summarizer()))
    try:
        await agent._summarize("summarize this")
    finally:
        await agent.aclose()
    assert provider.kwargs[-1]["max_tokens"] == 1_000


async def test_auto_compaction_passes_the_window_to_the_compactor(monkeypatch) -> None:
    summ = _Summarizer()
    agent = _agent(_Provider(1), summ, monkeypatch, window=8_192, overhead=0)
    try:
        await agent.run(_chat_history(2))
    finally:
        await agent.aclose()
    assert agent._compactor.context_window == 8_192


async def test_summarizer_failure_is_logged(caplog) -> None:
    async def boom(prompt: str) -> str:
        raise RuntimeError("context length exceeded")

    comp = SimpleCompactor(boom, keep_recent_turns=2)
    history = _chat_history()
    with caplog.at_level(logging.WARNING, logger="mantis_agent.compact"):
        out = await comp.compact(history)
    assert out is history
    assert any("summarizer failed" in r.getMessage() and "context length exceeded" in r.getMessage()
               for r in caplog.records)


# --- (d) /compact is sized to the window ---------------------------------------


def _long_history(n: int = 40) -> list:
    history: list = [UserMessage(content="goal")]
    for i in range(n):
        history.append(UserMessage(content=f"q{i} " + "detail " * 500))
        history.append(AssistantMessage(content=[TextBlock(text=f"a{i} " + "words " * 500)]))
    return history


async def test_manual_compaction_respects_the_window() -> None:
    small, unknown = _Summarizer(), _Summarizer()
    await run_manual_compaction(_long_history(), small, context_window=8_192)
    await run_manual_compaction(_long_history(), unknown)
    assert len(small.prompts[0]) <= (8_192 - 2_048 - 256) * 3
    assert len(unknown.prompts[0]) > len(small.prompts[0])


# --- (e) prompt + reply fit small / learned windows -----------------------------


@pytest.mark.parametrize("window", [4_096, 8_192, 16_384, 32_768])
def test_prompt_and_reply_fit_the_window_at_3_chars_per_token(window) -> None:
    reply = summary_reply_tokens(2048, window)
    assert reply <= window // 4
    cap = _summarizer_prompt_cap(window, reply)
    assert cap // 3 + reply + 256 <= window


def test_prompt_cap_is_clamped() -> None:
    assert _summarizer_prompt_cap(0) == 240_000
    assert _summarizer_prompt_cap(1_000_000) == 240_000
    assert _summarizer_prompt_cap(1_000) == 4_000


async def test_summarize_max_tokens_follows_the_learned_window(monkeypatch) -> None:
    # max_tokens was derived from a large declared window; the learned window is
    # 4k, so the summary reply must shrink to a quarter of it.
    provider = _Provider(1)
    provider.n = 1
    agent = Agent(model="mock", provider=provider, include_memory=False, max_tokens=8_000,
                  compactor=SimpleCompactor(_Summarizer()))
    monkeypatch.setattr(agent, "_effective_context_window", lambda: 4_096)
    try:
        await agent._summarize("summarize this")
    finally:
        await agent.aclose()
    assert provider.kwargs[-1]["max_tokens"] == 1_024
    agent._compactor.context_window = 4_096
    assert agent._compactor.summary_reply_tokens == 1_024


# --- (f) micro keeps the real count, minus what it freed ------------------------


async def test_microcompaction_subtracts_freed_tokens_from_real_usage(monkeypatch) -> None:
    # Reported usage (40k) is far above the 85% threshold of a 32k window. Micro
    # frees ~10k tokens of old tool results — not enough. Falling back to the
    # bare (undercounting) estimate would skip the summarize; subtracting what
    # was freed from the real count must still summarize.
    summ = _Summarizer()
    agent = _agent(_Provider(40_000), summ, monkeypatch, window=32_000, overhead=0)
    try:
        await agent.run(_tool_heavy_history())
    finally:
        await agent.aclose()
    assert summ.prompts, "micro did not free enough; the summarize must still run"


# --- (g) read-only compactor window ---------------------------------------------


async def test_read_only_compactor_window_does_not_crash(monkeypatch) -> None:
    class _RO:
        context_window = property(lambda self: 0)

        async def should_compact(self, messages, usage, ctx_window) -> bool:
            return False

        async def compact(self, messages):
            return messages

    agent = Agent(model="mock", provider=_Provider(1), tools=[bump], max_steps=4,
                  compactor=_RO(), include_recall=False, include_env=False,
                  include_memory=False)
    monkeypatch.setattr(agent, "_effective_context_window", lambda: 8_192)
    try:
        await agent.run(_chat_history(2))
        await agent._emergency_compact(_chat_history(2))
    finally:
        await agent.aclose()
