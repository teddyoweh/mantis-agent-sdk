"""End-to-end: a provider that enforces a smaller window than we assumed.

This is the reported Cerebras `zai-glm-4.7` failure driven through a real Agent.
The first call is refused with the provider's actual wording; the agent must
learn the stated ceiling, re-plan against it, and complete the turn — instead of
retrying against its own 128k guess and wedging the session.
"""

from __future__ import annotations

import pytest

from mantis_agent import (
    Agent,
    AssistantMessage,
    SystemMessage,
    TextBlock,
    UserMessage,
    tool,
)
from mantis_agent import context_limits as cl
from mantis_agent.capabilities import ModelCapability
from mantis_agent.compact import SimpleCompactor
from mantis_agent.events import (
    ContentBlockStart,
    ContentBlockStop,
    ContentBlockDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers.mock import MockProvider

pytestmark = pytest.mark.anyio

CEREBRAS = "https://api.cerebras.ai/v1"
REAL_ERROR = ("Please reduce the length of the messages or completion. "
              "Current length is 14789 while limit is 8192")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    cl._reset_cache_for_tests()
    yield
    cl._reset_cache_for_tests()


async def _summarize(prompt: str) -> str:
    return "Summary of prior conversation: earlier turns set up the task."


@tool
async def lookup(q: str) -> str:
    """Look something up."""
    return "ok"


def _ok_stream() -> list:
    return [
        MessageStart(message_id="m1", model="zai-glm-4.7"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="hi back")),
        ContentBlockStop(index=0),
        MessageStop(),
    ]


class _TooLongThenFine(MockProvider):
    """Refuses the first request the way Cerebras does, then succeeds."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def stream(self, **kw):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError(REAL_ERROR)
        for ev in _ok_stream():
            yield ev


def _long_history() -> list:
    """A conversation with something to compact — the reported failure was a
    14789-token transcript, and emergency compaction can only help when there
    are older turns to summarize away."""
    msgs: list = [SystemMessage(content="you are a helpful assistant")]
    for i in range(30):
        msgs.append(UserMessage(content=f"question {i}: " + "detail " * 60))
        msgs.append(AssistantMessage(content=[TextBlock(text=f"answer {i}: " + "words " * 60)]))
    msgs.append(UserMessage(content="hi"))
    return msgs


def _agent(provider) -> Agent:
    return Agent(
        model="zai-glm-4.7",
        backend=CEREBRAS,
        provider=provider,
        tools=[lookup],
        compactor=SimpleCompactor(_summarize, keep_recent_turns=2),
        # What we believe — and what the endpoint will refuse.
        model_capability=ModelCapability(name="zai-glm-4.7", family="glm",
                                        context_window=128_000),
        include_memory=False,
        max_turns=3,
    )


async def test_the_turn_completes_instead_of_wedging() -> None:
    provider = _TooLongThenFine()
    agent = _agent(provider)
    try:
        await agent.run(_long_history())
    finally:
        await agent.aclose()
    assert provider.calls == 2, "should have retried after learning the real ceiling"


async def test_the_enforced_ceiling_is_learned_from_the_refusal() -> None:
    agent = _agent(_TooLongThenFine())
    try:
        await agent.run(_long_history())
    finally:
        await agent.aclose()
    assert cl.learned_limit("zai-glm-4.7", CEREBRAS) == 8192


async def test_planning_switches_to_the_real_window() -> None:
    agent = _agent(_TooLongThenFine())
    try:
        assert agent._effective_context_window() == 128_000  # our guess, pre-failure
        await agent.run(_long_history())
        # Post-failure every later turn plans against what the endpoint enforces,
        # so compaction actually fires instead of sitting idle at 6% of 128k.
        assert agent._effective_context_window() == 8192
    finally:
        await agent.aclose()


async def test_a_later_session_starts_out_correct() -> None:
    """The whole point: never learn the same lesson by failing twice."""
    agent = _agent(_TooLongThenFine())
    try:
        await agent.run(_long_history())
    finally:
        await agent.aclose()

    cl._reset_cache_for_tests()          # simulate a fresh process
    fresh = _agent(MockProvider())
    try:
        assert fresh._effective_context_window() == 8192
    finally:
        await fresh.aclose()


async def test_an_unrelated_error_teaches_nothing() -> None:
    """Only a context refusal may lower the window. A rate limit, an auth
    failure or a 500 must leave planning untouched — clamping the window on a
    number scraped from an unrelated error would cripple the model."""

    class _RateLimited(MockProvider):
        async def stream(self, **kw):
            raise RuntimeError("Anthropic API error (429): Error")
            yield  # pragma: no cover — makes this an async generator

    agent = _agent(_RateLimited())
    try:
        with pytest.raises(RuntimeError, match="429"):
            await agent.run(_long_history())
        assert cl.learned_limit("zai-glm-4.7", CEREBRAS) is None
        assert agent._effective_context_window() == 128_000
    finally:
        await agent.aclose()


async def test_tool_schemas_count_against_the_budget() -> None:
    """The estimator counted messages only.

    A session reporting "7k used" was sending 13140 tokens, because the system
    prompt and every tool schema ride on the request too. On a 128k model that
    slack is invisible; against an 8192 ceiling it IS the failure — compaction
    reports success at a target the request can never meet.
    """
    agent = _agent(MockProvider())
    try:
        agent.system = "you are a helpful assistant " * 100
        overhead = agent._prompt_overhead_tokens()
        assert overhead > 0, "system prompt and tools must cost something"
        # The conversation may only use what is left after that fixed cost.
        assert agent._message_budget() == agent._effective_context_window() - overhead
    finally:
        await agent.aclose()


async def test_the_budget_shrinks_as_tools_are_added() -> None:
    from mantis_agent import tool as _tool

    @_tool
    async def a_second_tool(query: str, depth: int = 1) -> str:
        """A tool with a longer schema than the first one."""
        return ""

    lean = _agent(MockProvider())
    fat = Agent(
        model="zai-glm-4.7", backend=CEREBRAS, provider=MockProvider(),
        tools=[lookup, a_second_tool],
        compactor=SimpleCompactor(_summarize),
        model_capability=ModelCapability(name="zai-glm-4.7", family="glm",
                                         context_window=128_000),
        include_memory=False,
    )
    try:
        assert fat._prompt_overhead_tokens() > lean._prompt_overhead_tokens()
        assert fat._message_budget() < lean._message_budget()
    finally:
        await lean.aclose()
        await fat.aclose()


async def test_a_window_too_small_for_the_tools_yields_no_budget() -> None:
    """Not a compaction problem: if the fixed overhead alone does not fit,
    summarizing the conversation to nothing still overflows."""
    agent = Agent(
        model="tiny", backend=CEREBRAS, provider=MockProvider(), tools=[lookup],
        compactor=SimpleCompactor(_summarize),
        model_capability=ModelCapability(name="tiny", family="x", context_window=2048),
        include_memory=False,
    )
    try:
        agent.system = "x" * 100_000       # overhead alone dwarfs the window
        assert agent._message_budget() == 0
    finally:
        await agent.aclose()


async def test_the_hint_stops_recommending_compact_on_a_tiny_window() -> None:
    """`/compact or /clear` is a dead end when the window is this small — an
    empty conversation overflows too, so the advice loops."""
    from mantis_agent.tui import error_hint

    cl.record_limit("zai-glm-4.7", 8192, CEREBRAS)
    hint = error_hint(RuntimeError("Please reduce the length of the messages"),
                      CEREBRAS, "zai-glm-4.7")
    assert hint is not None
    assert "8k" in hint and "larger model" in hint
    assert "/compact" not in hint


async def test_a_learned_limit_recorded_without_an_endpoint_is_still_honoured() -> None:
    """The record and lookup keys diverged once, and it silently undid the fix.

    The TUI hands the agent a constructed provider and leaves ``backend`` unset,
    so the ceiling was written under a bare model name — then looked up under an
    endpoint-scoped one and never found. The limit was learned and ignored.
    """
    cl.record_limit("zai-glm-4.7", 8192)          # bare, as the TUI path wrote it
    assert cl.learned_limit("zai-glm-4.7", CEREBRAS) == 8192

    agent = _agent(MockProvider())
    try:
        assert agent._effective_context_window() == 8192
    finally:
        await agent.aclose()


async def test_the_endpoint_is_taken_from_the_provider_when_backend_is_unset() -> None:
    class _WithBase(MockProvider):
        base_url = "https://api.cerebras.ai/v1"

    agent = Agent(
        model="zai-glm-4.7", provider=_WithBase(), tools=[lookup],
        compactor=SimpleCompactor(_summarize),
        model_capability=ModelCapability(name="zai-glm-4.7", family="glm",
                                         context_window=128_000),
        include_memory=False,
    )
    try:
        assert agent.backend is None            # the TUI's situation exactly
        assert agent._endpoint() == "https://api.cerebras.ai/v1"
    finally:
        await agent.aclose()
