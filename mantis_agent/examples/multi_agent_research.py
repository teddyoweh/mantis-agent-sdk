"""Multi-agent example — parent uses ``query()``; a research sub-agent is
exposed as a regular tool.

Two patterns are demonstrated, both via :func:`as_subagent_tool`:

* **Spec-form** — the sub-agent is described by a :class:`SubAgentSpec`;
  a fresh child :class:`Agent` is minted per invocation.
* **Wrap-form** — an already-built :class:`Agent` is wrapped directly,
  reusing its provider, tools, system prompt, budget, and hooks across
  invocations.

The parent (driven by ``query()``) decides when to delegate.

Run it::

    # Against a real backend (Ollama by default):
    python -m mantis_agent.examples.multi_agent_research

    # Pick a different backend / model:
    MANTIS_AGENT_BASE_URL=https://api.together.xyz/v1 \
    MANTIS_AGENT_API_KEY=... \
    MANTIS_AGENT_MODEL=Qwen/Qwen2.5-72B-Instruct-Turbo \
        python -m mantis_agent.examples.multi_agent_research

    # Offline smoke test — no network, scripted MockProvider, sub-agent
    # call is exercised end-to-end. Use this in CI / on a plane:
    MANTIS_AGENT_MOCK=1 python -m mantis_agent.examples.multi_agent_research

The parent's session transcript persists to
``~/.mantis-agent/sessions/<session_id>.jsonl`` via ``persist=True``.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mantis_agent import (
    Agent,
    SubAgentSpec,
    as_subagent_tool,
    query,
    tool,
)


# ---------------------------------------------------------------------------
# A trivial "web" tool the sub-agent will use
# ---------------------------------------------------------------------------


@tool
async def fetch_url(url: str) -> str:
    """Stub fetch — returns a short summary instead of a real GET.

    In a real deployment you'd swap this for ``WebFetch`` from
    ``mantis_agent.builtin_tools`` (Exa-backed) or your own HTTP client.
    """

    return f"<fetched {url}: 200 OK, 1.2KB stub body — Spawn Labs builds AI agents.>"


# ---------------------------------------------------------------------------
# Sub-agent construction — two flavors
# ---------------------------------------------------------------------------


def _build_research_subagent_spec(model: str) -> SubAgentSpec:
    """Spec-form: pure declaration. The parent provider gets shared via
    ``parent_provider=`` at registration time so the child reuses the open
    HTTP pool — the dominant perf win for in-process sub-agents.
    """

    return SubAgentSpec(
        name="research_fresh",
        description=(
            "Delegate a research question to a fresh research sub-agent. "
            "Use for one-off lookups that should start from a clean slate."
        ),
        system_prompt=(
            "You are a research assistant. Fetch URLs with the fetch_url "
            "tool and summarize what you find in 1-2 sentences."
        ),
        model=model,
        tools=[fetch_url],
        max_turns=4,
    )


def _build_research_agent(model: str, backend: str | None) -> Agent:
    """Wrap-form: build an Agent up-front. Reuses its open HTTP pool,
    hook dispatcher, and budget tracker across every parent invocation.
    """

    return Agent(
        model=model,
        backend=backend,
        tools=[fetch_url],
        system=(
            "You are a long-lived research assistant. Fetch URLs with the "
            "fetch_url tool and summarize what you find in 1-2 sentences."
        ),
        max_tokens=512,
        max_turns=4,
        include_memory=False,  # keep example reproducible in CI
    )


# ---------------------------------------------------------------------------
# Mock-mode wiring — lets the example run with zero network in CI
# ---------------------------------------------------------------------------


def _build_mock_provider():
    """Build a deterministic MockProvider that scripts a two-turn parent:

    1. First parent turn: call the ``research_wrapped`` sub-agent.
    2. Second parent turn: emit a final answer that quotes the result.

    The sub-agent itself also runs against the same MockProvider, which is
    given a separate script via a small dispatch wrapper below.
    """

    # Imported lazily so the example doesn't pay the import cost in real-
    # backend mode.
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
    from mantis_agent.types import TextBlock, ToolUseBlock, Usage

    def _hdr(model: str = "mock-7b") -> list:
        return [MessageStart(message_id="mock-1", model=model)]

    def _text(idx: int, txt: str) -> list:
        return [
            ContentBlockStart(index=idx, block=TextBlock(text="")),
            ContentBlockDelta(index=idx, delta=TextDelta(text=txt)),
            ContentBlockStop(index=idx),
        ]

    def _tool_call(idx: int, cid: str, name: str, json_args: str) -> list:
        return [
            ContentBlockStart(
                index=idx,
                block=ToolUseBlock(id=cid, name=name, input={}),
            ),
            ContentBlockDelta(index=idx, delta=InputJsonDelta(partial_json=json_args)),
            ContentBlockStop(index=idx),
        ]

    def _stop(reason: str = "end_turn") -> list:
        return [
            MessageDelta(
                stop_reason=reason,
                usage=Usage(input_tokens=10, output_tokens=20),
            ),
            MessageStop(),
        ]

    # Parent: turn 1 calls research_wrapped; turn 2 emits final text.
    parent_t1 = (
        _hdr()
        + _tool_call(
            0,
            "p-call-1",
            "research_wrapped",
            '{"prompt": "What does Spawn Labs build?"}',
        )
        + _stop("tool_use")
    )
    parent_t2 = (
        _hdr()
        + _text(0, "Based on the research sub-agent: Spawn Labs builds AI agents.")
        + _stop()
    )

    # Sub-agent: turn 1 calls fetch_url; turn 2 emits summary.
    sub_t1 = (
        _hdr()
        + _tool_call(
            0,
            "s-call-1",
            "fetch_url",
            '{"url": "https://spawnlabs.ai"}',
        )
        + _stop("tool_use")
    )
    sub_t2 = (
        _hdr()
        + _text(0, "Spawn Labs builds AI agents.")
        + _stop()
    )

    scripts = [parent_t1, sub_t1, sub_t2, parent_t2]

    class _ScriptedMock(MockProvider):
        """Walks the scripts list in order on each stream() call."""

        def __init__(self) -> None:
            super().__init__()
            self._i = 0

        async def stream(self, **_kw):
            script = scripts[self._i] if self._i < len(scripts) else scripts[-1]
            self._i += 1
            for ev in script:
                yield ev

    return _ScriptedMock()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    mock_mode = os.environ.get("MANTIS_AGENT_MOCK") == "1"
    backend = os.environ.get("MANTIS_AGENT_BASE_URL", "http://localhost:11434")
    model = os.environ.get("MANTIS_AGENT_MODEL", "qwen2.5-7b-instruct")

    if mock_mode:
        provider = _build_mock_provider()
        # In mock mode both parent and child share the same provider —
        # the scripts above know the call order.
        research_agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=[fetch_url],
            system="You are a research assistant.",
            max_tokens=256,
            max_turns=4,
            include_memory=False,
        )
    else:
        provider = None  # query() will build one from model+backend
        research_agent = _build_research_agent(model, backend)

    research_tool_fresh = as_subagent_tool(_build_research_subagent_spec(model))
    research_tool_wrapped = as_subagent_tool(
        research_agent,
        name="research_wrapped",
        description=(
            "Delegate a research question to the long-lived research sub-agent. "
            "Reuses its HTTP pool and tool kit across calls."
        ),
    )

    options: dict = {
        "model": "mock-7b" if mock_mode else model,
        "tools": [research_tool_fresh, research_tool_wrapped],
        "system": (
            "You are an orchestrator. For research-heavy questions, call one "
            "of the 'research_*' tools. For simple questions, answer directly."
        ),
        "max_tokens": 512,
        "max_turns": 5,
        "persist": False if mock_mode else True,
        "include_memory": False,
    }
    if mock_mode:
        options["provider"] = provider
    else:
        options["backend"] = backend

    final_text: list[str] = []
    delegated: list[str] = []

    async for msg in query(
        prompt="What does Spawn Labs build? Use the research_wrapped tool.",
        options=options,
    ):
        if msg.type == "assistant":
            for block in msg.message.content:
                if hasattr(block, "text") and block.text:
                    print(f"[parent] {block.text}")
                    final_text.append(block.text)
                elif hasattr(block, "name") and getattr(block, "name", None) in (
                    "research_fresh",
                    "research_wrapped",
                ):
                    delegated.append(block.name)
                    print(f"[parent → {block.name}] delegating")
        elif msg.type == "result":
            print(
                f"\n[result] {msg.subtype} · {msg.num_turns} parent turns · "
                f"{msg.duration_ms} ms · ${msg.total_cost_usd:.4f}"
            )

    if mock_mode:
        # Sanity-check the demo actually exercised the sub-agent path.
        assert delegated, "expected the parent to delegate to a sub-agent in mock mode"
        assert any("Spawn Labs" in t for t in final_text), (
            "expected the final parent text to incorporate the sub-agent's answer"
        )
        print("\n[ok] mock-mode smoke test passed.")

    await research_agent.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
