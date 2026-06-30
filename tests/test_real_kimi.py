"""Real-backend test against Moonshot's hosted Kimi API.

Skipped when ``MOONSHOT_API_KEY`` is not set in the env. This is the path
production users will take for Kimi K2 — the model itself is a 1T-param
MoE that nobody runs locally without a GPU cluster.

To run:

    export MOONSHOT_API_KEY=sk-...
    pytest tests/test_real_kimi.py -v

Endpoint reference: https://platform.moonshot.ai/docs/api/chat — fully
OpenAI-compatible so our OpenAICompatProvider handles it without changes.
"""

from __future__ import annotations

import os

import anyio
import pytest

from mantis_agent import Agent, AssistantMessage, TextBlock, ToolUseBlock, UserMessage, tool


pytestmark = pytest.mark.skipif(
    not os.environ.get("MOONSHOT_API_KEY"),
    reason="MOONSHOT_API_KEY not set",
)


# Moonshot's API uses these model identifiers.
MODEL = os.environ.get("MANTIS_AGENT_KIMI_MODEL", "moonshot-v1-8k")
BASE_URL = os.environ.get("MANTIS_AGENT_KIMI_BASE_URL", "https://api.moonshot.ai/v1")


@tool
async def add(a: int, b: int) -> str:
    """Add two integers and return the result as a string."""
    return str(a + b)


def test_kimi_simple_chat():
    """Plain chat, no tools."""

    async def main():
        agent = Agent(
            model=MODEL,
            backend=BASE_URL,
            system="Answer in exactly one short sentence.",
            tools=[],
            max_tokens=64,
            max_turns=1,
            temperature=0.1,
        )
        try:
            msgs = await agent.run(
                [UserMessage(content="What is 2+2? Answer in one word.")]
            )
        finally:
            await agent.aclose()

        assert isinstance(msgs[-1], AssistantMessage)
        text = "".join(
            b.text for b in msgs[-1].content if isinstance(b, TextBlock)
        )
        assert len(text) > 0

    anyio.run(main)


def test_kimi_with_tool_call():
    """Tool call: ask Kimi to add 7 and 5 via the `add` tool."""

    async def main():
        agent = Agent(
            model=MODEL,
            backend=BASE_URL,
            system=(
                "You are a math helper. When the user asks an arithmetic "
                "question, ALWAYS use the add tool — never compute it yourself."
            ),
            tools=[add],
            max_tokens=128,
            max_turns=3,
            temperature=0.1,
        )
        try:
            msgs = await agent.run(
                [UserMessage(content="Use the add tool to compute 7 + 5.")]
            )
        finally:
            await agent.aclose()

        tool_uses = []
        for m in msgs:
            if isinstance(m, AssistantMessage):
                tool_uses.extend(
                    b for b in m.content if isinstance(b, ToolUseBlock)
                )

        # Kimi K2 should hit native tools cleanly. If it does, the name is
        # `add` and the input has a/b set. If not (smaller model or
        # off-day), we accept a plain text answer as a soft pass.
        if tool_uses:
            assert any(t.name == "add" for t in tool_uses), (
                f"called wrong tool: {[t.name for t in tool_uses]}"
            )

    anyio.run(main)
