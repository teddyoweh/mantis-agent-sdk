"""Local Ollama example — uses ``query()`` for Claude SDK parity.

Prereqs::

    ollama pull qwen2.5:7b-instruct
    ollama serve   # default: http://localhost:11434

Run::

    python -m mantis_agent.examples.ollama_local

Demonstrates: ``query()`` against a fully-local Ollama backend, one
tool the model invokes, persisted to ``~/.mantis-agent/sessions/`` via the
``persist=True`` option.
"""

from __future__ import annotations

import asyncio

from mantis_agent import query, tool


@tool
async def get_weather(city: str) -> str:
    """Return a one-line weather summary for the given city."""

    # Stubbed — a real implementation would call a weather API.
    return f"{city}: 67°F, partly cloudy."


async def main() -> None:
    async for msg in query(
        prompt="Weather in SF?",
        options={
            "model": "qwen2.5-7b-instruct",
            "backend": "http://localhost:11434",
            "tools": [get_weather],
            "system": "Reply in one sentence.",
            "max_tokens": 256,
            "max_turns": 3,
            "persist": True,  # writes JSONL transcript to ~/.mantis-agent/sessions/
        },
    ):
        if msg.type == "assistant":
            for block in msg.message.content:
                if hasattr(block, "text") and block.text:
                    print(f"[assistant] {block.text}")
        elif msg.type == "result":
            print(
                f"\n[result] {msg.subtype} · {msg.num_turns} turns · "
                f"{msg.duration_ms} ms · ${msg.total_cost_usd:.4f}"
            )


if __name__ == "__main__":
    asyncio.run(main())
