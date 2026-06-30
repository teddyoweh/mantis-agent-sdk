"""Reasoning-model example — render thinking and final answer differently.

DeepSeek-R1, QwQ, R1-Distill, Marco-o1 etc. emit reasoning either inline
(``<think>...</think>`` tags) or via Ollama's separate ``thinking``
field. The streaming pipeline consolidates both into ``ThinkingBlock``
deltas — consumers can render thinking dimmed/collapsed while keeping
the final answer loud.

Uses the lower-level ``Agent.stream()`` API for token-by-token output
(``query()`` yields whole SDKMessages, not raw event deltas).

Run with local Ollama::

    ollama pull deepseek-r1:1.5b
    python -m mantis_agent.examples.with_thinking

Or any backend serving a reasoning model::

    MANTIS_AGENT_MODEL=qwq-32b-preview \\
    MANTIS_AGENT_BASE_URL=http://localhost:8000/v1 \\
    python -m mantis_agent.examples.with_thinking
"""

from __future__ import annotations

import asyncio
import os
import sys

from mantis_agent import Agent, UserMessage
from mantis_agent.events import ContentBlockDelta, TextDelta, ThinkingDelta


# ANSI dim/normal — keep the thinking visually distinct without depending on
# a TUI library. Falls back gracefully on terminals that don't support ANSI.
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


async def main() -> None:
    agent = Agent(
        model=os.environ.get("MANTIS_AGENT_MODEL", "deepseek-r1:1.5b"),
        backend=os.environ.get("MANTIS_AGENT_BASE_URL", "http://localhost:11434"),
        system="Think carefully, then answer.",
        max_tokens=1024,
        temperature=0.6,
    )
    try:
        messages = [
            UserMessage(content="A train leaves at 9 going 60 mph. Where is it at noon?")
        ]
        in_thinking = False
        async for ev in agent.stream(messages):
            if not isinstance(ev, ContentBlockDelta):
                continue
            d = ev.delta
            if isinstance(d, ThinkingDelta):
                if not in_thinking:
                    sys.stdout.write(_DIM + "[thinking] ")
                    in_thinking = True
                sys.stdout.write(d.thinking)
            elif isinstance(d, TextDelta):
                if in_thinking:
                    sys.stdout.write(_RESET + "\n")
                    in_thinking = False
                sys.stdout.write(d.text)
            sys.stdout.flush()
        if in_thinking:
            sys.stdout.write(_RESET)
        sys.stdout.write("\n")
        sys.stdout.flush()
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
