"""Kimi capability + routing tests — offline, no live model required.

Kimi K2 is a 1T-parameter MoE — its self-host requires a real GPU cluster
and the Ollama library only exposes ``:cloud`` tags. So a *local* Kimi test
isn't possible in CI. These tests verify the pieces that DON'T need a live
model: capability table entries are correct, provider auto-detection picks
the right path, and a Kimi-shaped scripted stream flows through the agent
loop end-to-end.

The companion ``test_real_kimi.py`` runs against Moonshot's hosted API
when ``MOONSHOT_API_KEY`` is set.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import Agent, AssistantMessage, TextBlock, ToolUseBlock, UserMessage, tool
from mantis_agent.capabilities import (
    HOSTED_PROFILES,
    hosted_profile_from_url,
    lookup_model,
    resolve_tool_use_path,
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
from mantis_agent.types import Usage


# ---------------------------------------------------------------------------
# Capability table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id, expected_family, expects_native_tools, expects_inline_thinking",
    [
        # Explicit entries
        ("kimi-k2-instruct", "kimi", True, False),
        ("kimi-k1.5-instruct", "kimi", True, False),
        ("moonshot-v1-128k", "kimi", True, False),
        ("moonshot-v1-32k", "kimi", True, False),
        ("moonshot-v1-8k", "kimi", True, False),
        # Family fallback heuristic
        ("Kimi-K2-Thinking", "kimi", True, False),
        ("moonshot-future-128k", "kimi", True, False),
        ("kimi-k2.6-cloud", "kimi", True, False),
    ],
)
def test_kimi_capability_resolution(
    model_id: str,
    expected_family: str,
    expects_native_tools: bool,
    expects_inline_thinking: bool,
) -> None:
    cap = lookup_model(model_id)
    assert cap.family == expected_family, f"{model_id} → family {cap.family}"
    assert cap.supports_native_tools == expects_native_tools
    assert cap.emits_inline_thinking == expects_inline_thinking
    # Every Kimi entry should claim a 32K+ context window — they're long-ctx
    # models by design.
    assert cap.context_window >= 8192


def test_moonshot_url_detection() -> None:
    """Pointing the SDK at api.moonshot.ai routes to the Moonshot profile."""

    cap = hosted_profile_from_url("https://api.moonshot.ai/v1")
    assert cap is not None
    assert cap.kind == "openai_compat"
    assert cap.supports_native_tools is True
    assert cap.provider_hint == "moonshot"

    # Same for the China endpoint and the kimi alias.
    for url in [
        "https://api.moonshot.cn/v1",
        "https://kimi.moonshot.ai/v1",
        "http://localhost:8000/v1/kimi",
    ]:
        cap = hosted_profile_from_url(url)
        assert cap is not None and cap.provider_hint == "moonshot", f"failed for {url}"


def test_kimi_picks_path_a_on_native_backend() -> None:
    """On Moonshot (native tools) and vLLM (native tools + grammar), Kimi
    should take Path A (native OpenAI-compatible tool calling)."""

    kimi = lookup_model("kimi-k2-instruct")
    assert resolve_tool_use_path(kimi, HOSTED_PROFILES["moonshot"]) == "A"
    assert resolve_tool_use_path(kimi, HOSTED_PROFILES["vllm"]) == "A"


# ---------------------------------------------------------------------------
# End-to-end scripted Kimi stream through the agent loop
# ---------------------------------------------------------------------------


@tool
async def calc(expression: str) -> str:
    """Evaluate an arithmetic expression."""
    # Minimal demo — real impl would parse + sandbox.
    return str(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307


def _kimi_two_turn_script():
    """Two Kimi-shaped turns: tool call → final answer."""

    turn1 = [
        MessageStart(message_id="kimi-1", model="kimi-k2-instruct"),
        ContentBlockStart(
            index=0,
            block=ToolUseBlock(id="tc_kimi_1", name="calc", input={}),
        ),
        ContentBlockDelta(
            index=0,
            delta=InputJsonDelta(partial_json='{"expression": "21 * 2"}'),
        ),
        ContentBlockStop(index=0),
        MessageDelta(
            stop_reason="tool_use",
            usage=Usage(input_tokens=80, output_tokens=15),
        ),
        MessageStop(),
    ]
    turn2 = [
        MessageStart(message_id="kimi-2", model="kimi-k2-instruct"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="The answer is 42.")),
        ContentBlockStop(index=0),
        MessageDelta(
            stop_reason="end_turn",
            usage=Usage(input_tokens=120, output_tokens=10),
        ),
        MessageStop(),
    ]
    return turn1, turn2


def test_kimi_scripted_end_to_end() -> None:
    """The full agent run loop driven by a Kimi-shaped scripted stream."""

    turn1, turn2 = _kimi_two_turn_script()

    class TwoTurnKimi(MockProvider):
        def __init__(self):
            super().__init__()
            self._turn = 0

        async def stream(self, **kw):
            script = turn1 if self._turn == 0 else turn2
            self._turn += 1
            for ev in script:
                yield ev

    async def main():
        agent = Agent(
            model="kimi-k2-instruct",
            provider=TwoTurnKimi(),
            tools=[calc],
            max_turns=5,
        )
        try:
            msgs = await agent.run([UserMessage(content="What is 21 doubled?")])
        finally:
            await agent.aclose()

        # The capability should have been resolved.
        assert agent.model_capability is not None
        assert agent.model_capability.family == "kimi"
        assert agent.model_capability.supports_native_tools is True

        # Tool ran, result was 42.
        tool_results = msgs[2].content
        assert tool_results[0].content == "42"
        assert tool_results[0].is_error is False

        # Final assistant text mentions 42.
        final = msgs[-1]
        assert isinstance(final, AssistantMessage)
        text = "".join(b.text for b in final.content if isinstance(b, TextBlock))
        assert "42" in text

    anyio.run(main)
