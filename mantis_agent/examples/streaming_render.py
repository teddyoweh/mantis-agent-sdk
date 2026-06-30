"""Streaming render example — token-by-token output.

This is the one example that intentionally uses the lower-level ``Agent``
API rather than ``query()``. ``query()`` yields whole ``SDKMessage``
objects (per Claude SDK parity); ``Agent.stream()`` yields the raw
normalized event stream — ``MessageStart`` / ``ContentBlockStart`` /
``ContentBlockDelta(TextDelta(text))`` / ``ContentBlockStop`` /
``MessageStop`` — which is what TUIs / WebSocket renderers want.

If you don't need token-level rendering, use ``query()`` instead (see
quickstart.py).

Run::

    python -m mantis_agent.examples.streaming_render
"""

from __future__ import annotations

import asyncio
import os
import sys

from mantis_agent import Agent, UserMessage
from mantis_agent.events import ContentBlockDelta, TextDelta


async def main() -> None:
    agent = Agent(
        model=os.environ.get("MANTIS_AGENT_MODEL", "qwen2.5-7b-instruct"),
        backend=os.environ.get("MANTIS_AGENT_BASE_URL", "http://localhost:11434"),
        system="Reply in one short paragraph.",
        max_tokens=200,
    )
    try:
        messages = [UserMessage(content="Tell me one interesting fact about the moon.")]
        async for ev in agent.stream(messages):
            if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, TextDelta):
                sys.stdout.write(ev.delta.text)
                sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
