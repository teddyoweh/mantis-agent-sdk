"""Hosted Fireworks example — DeepSeek-V3 via ``query()``.

Demonstrates a two-turn flow against Fireworks:

  1. parent emits a tool call to ``lookup_company``;
  2. parent receives the tool result and emits a one-line answer.

Run modes
---------

**Real Fireworks** (default — requires ``FIREWORKS_API_KEY``)::

    export FIREWORKS_API_KEY=fw_...
    python -m mantis_agent.examples.fireworks_hosted

**Override model / route to a different OpenAI-compat backend**::

    MANTIS_AGENT_BASE_URL=https://api.fireworks.ai/inference/v1 \
    MANTIS_AGENT_MODEL=accounts/fireworks/models/deepseek-v3 \
        python -m mantis_agent.examples.fireworks_hosted

**Offline smoke test** — no network, scripted MockProvider, the
tool-call → tool-result → final-answer path is exercised end-to-end.
Use this in CI / on a plane::

    MANTIS_AGENT_MOCK=1 python -m mantis_agent.examples.fireworks_hosted

Fireworks speaks OpenAI-compat, so the OpenAI-compat adapter is the right
call. DeepSeek-V3 has native tool calling, so a real run hits Path A —
the prompt-engineered fallback parser never fires. The mock mode skips
the network entirely but goes through the exact same agent loop, so
regressions in tool dispatch / result threading / message extraction
show up immediately.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mantis_agent import query, tool


# ---------------------------------------------------------------------------
# Tool the example exercises
# ---------------------------------------------------------------------------


@tool
async def lookup_company(name: str) -> str:
    """Return a one-line description of a company by name."""

    canned = {
        "spawn labs": "Spawn Labs builds AI agents.",
        "fireworks": "Fireworks AI runs hosted open-weight inference.",
    }
    return canned.get(name.strip().lower(), f"{name}: no record on file.")


# ---------------------------------------------------------------------------
# Mock-mode wiring — lets the example run with zero network in CI
# ---------------------------------------------------------------------------


def _build_mock_provider():
    """Build a deterministic MockProvider that scripts two parent turns:

    1. First turn: emit a ``lookup_company`` tool call (Path A native).
    2. Second turn: emit a final text answer that quotes the tool result.

    The MockProvider replays each script on successive ``stream()`` calls;
    the Agent loop dispatches the tool between turns the same way a real
    Fireworks response would trigger.
    """

    # Imported lazily so the example doesn't pay the import cost in
    # real-backend mode (where it isn't needed).
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

    def _hdr(model: str = "mock-fireworks") -> list:
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
                usage=Usage(input_tokens=12, output_tokens=24),
            ),
            MessageStop(),
        ]

    turn_1 = (
        _hdr()
        + _tool_call(
            0,
            "fw-call-1",
            "lookup_company",
            '{"name": "Spawn Labs"}',
        )
        + _stop("tool_use")
    )
    turn_2 = (
        _hdr()
        + _text(
            0,
            "Based on the lookup: Spawn Labs builds AI agents.",
        )
        + _stop()
    )

    scripts = [turn_1, turn_2]

    class _ScriptedMock(MockProvider):
        """Walks ``scripts`` in order on each ``stream()`` call.

        Any call beyond the script list replays the last one — defensive
        in case the model emits an extra empty turn under retries.
        """

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
    backend = os.environ.get(
        "MANTIS_AGENT_BASE_URL", "https://api.fireworks.ai/inference/v1"
    )
    model = os.environ.get(
        "MANTIS_AGENT_MODEL", "accounts/fireworks/models/deepseek-v3"
    )

    if not mock_mode and not os.environ.get("FIREWORKS_API_KEY"):
        raise SystemExit(
            "Set FIREWORKS_API_KEY before running this example, or use "
            "MANTIS_AGENT_MOCK=1 for the offline smoke test."
        )

    options: dict = {
        "model": "mock-fireworks" if mock_mode else model,
        "tools": [lookup_company],
        "system": (
            "You are a research assistant. Use the lookup_company tool "
            "when asked about companies, then quote the result."
        ),
        "max_tokens": 512,
        "max_turns": 3,
        "persist": False if mock_mode else True,
        "include_memory": False,
    }
    if mock_mode:
        options["provider"] = _build_mock_provider()
    else:
        options["backend"] = backend

    final_text: list[str] = []
    tool_calls: list[str] = []

    async for msg in query(
        prompt="Tell me about Spawn Labs in one sentence.",
        options=options,
    ):
        if msg.type == "assistant":
            for block in msg.message.content:
                if hasattr(block, "text") and block.text:
                    print(f"[assistant] {block.text}")
                    final_text.append(block.text)
                elif getattr(block, "name", None) == "lookup_company":
                    tool_calls.append(block.name)
                    print(f"[assistant → lookup_company] dispatching")
        elif msg.type == "result":
            cost = getattr(msg, "total_cost_usd", 0.0) or 0.0
            print(
                f"\n[result] {msg.subtype} · {msg.num_turns} turns · "
                f"${cost:.4f} (in={msg.usage.input_tokens}, "
                f"out={msg.usage.output_tokens})"
            )

    if mock_mode:
        # Sanity-check that the demo actually exercised the tool path.
        assert tool_calls, "expected the parent to call lookup_company in mock mode"
        assert any("Spawn Labs" in t for t in final_text), (
            "expected the final parent text to incorporate the tool result"
        )
        print("\n[ok] mock-mode smoke test passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
