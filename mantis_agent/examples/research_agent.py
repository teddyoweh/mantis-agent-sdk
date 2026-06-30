"""Research agent — give a model two built-in tools (WebSearch + WebFetch)
and ask it to research a subject. Uses ``query()`` (Claude SDK parity).

The SDK's built-in tools are 1:1 with Claude Code's named tools. When
``EXA_API_KEY`` is set (recommended), WebSearch routes through Exa
(neural, real URLs, ~$0.005/query). Without a key, falls back to Brave →
Tavily → DuckDuckGo HTML scraping.

Run with local Llama 3.2 on Ollama (the acceptance-test path)::

    ollama pull llama3.2:3b
    export EXA_API_KEY=...      # optional; better quality
    python -m mantis_agent.examples.research_agent "Subject Name"

Or any hosted backend::

    MANTIS_AGENT_BASE_URL=https://api.together.xyz/v1 \\
    MANTIS_AGENT_API_KEY=$TOGETHER_API_KEY \\
    MANTIS_AGENT_MODEL=qwen2.5-72b-instruct \\
    python -m mantis_agent.examples.research_agent "Subject Name"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from mantis_agent import (
    WebFetch,
    WebSearch,
    query,
)
from mantis_agent.hooks import HookContext, HookResult, Hooks

logging.basicConfig(level=logging.WARNING)


# ---------------------------------------------------------------------------
# Live transcript hooks — observable progress as the agent works
# ---------------------------------------------------------------------------


async def trace_pre(ctx: HookContext) -> HookResult:
    print(f"\n  🔧 {ctx.tool.name}({_short(ctx.input)})", flush=True)
    return HookResult()


async def trace_post(ctx: HookContext) -> HookResult:
    body = _short(ctx.output, 220)
    print(f"  ← {len(str(ctx.output))} chars · {body}", flush=True)
    return HookResult()


def _short(obj: Any, n: int = 120) -> str:
    s = str(obj).replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


# ---------------------------------------------------------------------------
# Research loop — query()-driven, with hooks for transparency
# ---------------------------------------------------------------------------


async def research(subject: str, max_turns: int = 8) -> str:
    model = os.environ.get("MANTIS_AGENT_MODEL", "llama3.2:3b")
    backend = os.environ.get("MANTIS_AGENT_BASE_URL", "http://localhost:11434")

    provider = "Exa" if os.environ.get("EXA_API_KEY") else (
        "Brave" if os.environ.get("BRAVE_API_KEY") else (
            "Tavily" if os.environ.get("TAVILY_API_KEY") else "DuckDuckGo (fallback)"
        )
    )
    print(f"\n=== Researching {subject!r} ===")
    print(f"    model:   {model}")
    print(f"    backend: {backend}")
    print(f"    search:  {provider}")

    final_text = ""
    async for msg in query(
        prompt=f"Research {subject!r} and summarize what you find.",
        options={
            "model": model,
            "backend": backend,
            "tools": [WebSearch, WebFetch],
            "system": (
                "You are a research assistant. The user names a subject — a "
                "person, project, or topic. You have two tools:\n"
                "  - WebSearch(query: str) — returns ranked results as "
                "    TITLE — URL — SNIPPET lines.\n"
                "  - WebFetch(url: str) — fetches a URL and returns its text.\n\n"
                "Process:\n"
                "  1. WebSearch the subject name. Read every result line.\n"
                "  2. If results look thin, refine the query and search again.\n"
                "  3. Pick the 1-2 most relevant URLs and WebFetch them.\n"
                "  4. Write a 3-6 sentence factual summary citing the URLs you "
                "     actually fetched. Never invent facts.\n"
            ),
            "max_tokens": 512,
            "max_turns": max_turns,
            "temperature": 0.2,
            "hooks": Hooks(pre_tool_use=trace_pre, post_tool_use=trace_post),
            "persist": True,  # write to ~/.mantis-agent/sessions/
        },
    ):
        if msg.type == "assistant":
            for block in msg.message.content:
                if hasattr(block, "text") and block.text:
                    final_text = block.text
        elif msg.type == "result":
            print(
                f"\n[result] {msg.subtype} · {msg.num_turns} turns · "
                f"{msg.duration_ms} ms · ${msg.total_cost_usd:.4f}"
            )

    return final_text or "(no final assistant text)"


def main() -> None:
    subject = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Teddy Oweh"
    summary = asyncio.run(research(subject))
    print("\n\n=== FINAL SUMMARY ===")
    print(summary)


if __name__ == "__main__":
    main()
