"""Minimal quickstart — uses ``query()`` (Claude SDK-compatible entry point).

Run with a local Ollama:

    ollama pull qwen2.5:7b
    python -m mantis_agent.examples.quickstart

Or any hosted OpenAI-compatible backend:

    MANTIS_AGENT_BASE_URL=https://api.together.xyz/v1 \\
    MANTIS_AGENT_API_KEY=$TOGETHER_API_KEY \\
    MANTIS_AGENT_MODEL=qwen2.5-72b-instruct \\
    python -m mantis_agent.examples.quickstart

This is the byte-for-byte equivalent of the Claude Agent SDK pattern:

    from claude_agent_sdk import query
    async for msg in query(prompt="...", options={...}):
        ...

Swap ``claude_agent_sdk`` for ``mantis_agent`` and the rest of your
code is unchanged. The yielded SDKMessage shapes are identical
(``message.type`` ∈ {assistant, user, system, result}, etc.).
"""

from __future__ import annotations

import asyncio
import os

from mantis_agent import query, tool


@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city. Returns a one-line summary."""

    # Real impl would hit an API. Stubbed here so the quickstart runs
    # without external deps.
    return f"{city}: 67°F, partly cloudy, wind 8 mph NW"


async def main() -> None:
    model = os.environ.get("MANTIS_AGENT_MODEL", "qwen2.5-7b-instruct")
    backend = os.environ.get("MANTIS_AGENT_BASE_URL", "http://localhost:11434")

    final_text = ""
    async for msg in query(
        prompt="What's the weather in San Francisco?",
        options={
            "model": model,
            "backend": backend,
            "system": (
                "You are a concise weather assistant. "
                "Use the get_weather tool when asked about weather."
            ),
            "tools": [get_weather],
            "max_tokens": 512,
            "max_turns": 5,
        },
    ):
        # The streaming output is a sequence of SDKMessages — system init,
        # user echo, one or more assistant turns, possibly tool-result user
        # turns, and finally a result message with cost + usage.
        kind = msg.type if hasattr(msg, "type") else type(msg).__name__
        if kind == "assistant":
            for block in msg.message.content:
                if hasattr(block, "text"):
                    final_text = block.text
        elif kind == "result":
            print(f"\n[{msg.subtype}] {msg.num_turns} turns, "
                  f"{msg.duration_ms} ms, ${msg.total_cost_usd:.4f}")

    print("\n--- final ---")
    print(final_text)


if __name__ == "__main__":
    asyncio.run(main())
