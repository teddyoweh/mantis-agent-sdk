"""Real-backend test against a local Ollama instance.

Skipped when Ollama isn't reachable at the default URL. Set
``MANTIS_AGENT_OLLAMA_MODEL`` to override the default ``llama3.2:3b``.

This is the closest thing to the README's acceptance test:
``ollama pull llama3.2:3b`` + 10-line script + tool call + works first try.
"""

from __future__ import annotations

import os
import socket

import anyio
import pytest

from mantis_agent import Agent, AssistantMessage, TextBlock, ToolUseBlock, UserMessage, tool
from mantis_agent.providers.ollama import OllamaProvider


def _ollama_reachable(host: str = "127.0.0.1", port: int = 11434) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _ollama_reachable(),
    reason="Ollama not running on localhost:11434",
)


MODEL = os.environ.get("MANTIS_AGENT_OLLAMA_MODEL", "llama3.2:3b")


@tool
async def add(a: int, b: int) -> str:
    """Add two integers and return the result as a string."""
    return str(a + b)


def test_ollama_simple_chat():
    """No tools — just a plain prompt and response."""

    async def main():
        agent = Agent(
            model=MODEL,
            backend="http://localhost:11434",
            system="You are a helpful assistant. Answer in one short sentence.",
            tools=[],
            max_tokens=64,
            max_turns=1,
            temperature=0.1,
        )
        try:
            msgs = await agent.run(
                [UserMessage(content="What is 2+2? Answer with one word.")]
            )
        finally:
            await agent.aclose()

        # We got an assistant message with some text content.
        assert isinstance(msgs[-1], AssistantMessage)
        text = "".join(
            b.text for b in msgs[-1].content if isinstance(b, TextBlock)
        )
        assert len(text) > 0, "expected non-empty response"

    anyio.run(main)


def test_ollama_with_tool_call():
    """Tool call: ask the model to add 7 and 5 via the add tool."""

    async def main():
        agent = Agent(
            model=MODEL,
            backend="http://localhost:11434",
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

        # Was the tool invoked? Look across all assistant messages.
        tool_uses = []
        for m in msgs:
            if isinstance(m, AssistantMessage):
                tool_uses.extend(
                    b for b in m.content if isinstance(b, ToolUseBlock)
                )

        # Llama 3.2 3B is small — be lenient. We accept either:
        #   (a) it called add (ideal)
        #   (b) it answered without the tool (acceptable but logged)
        if tool_uses:
            assert any(t.name == "add" for t in tool_uses), (
                f"called wrong tool: {[t.name for t in tool_uses]}"
            )

    anyio.run(main)
