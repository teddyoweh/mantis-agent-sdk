"""Built-in tracing — record a full span tree of an agent run.

Run::

    python -m mantis_agent.examples.with_tracing

The example uses ``MockProvider`` so it requires no API keys and no network.
Drop your own backend in (``Agent(model="claude-sonnet-4.5", ...)`` etc.) and
the same tracing wiring kicks in unchanged.

Three things this demonstrates:

1. **Zero-dep tracer** — ``InMemoryTracer()`` records every span the agent
   loop emits, with parent/child links, attributes, and timings. No
   external service required.
2. **Span hierarchy** — one ``agent.run`` span wraps one or more
   ``agent.turn`` spans, each containing one ``llm.call`` and zero-or-more
   ``tool.call`` children. Aggregate token / cost totals land on the
   root run span.
3. **Privacy by default** — tool spans record input KEYS only, never
   input values. Traces are safe to ship to third-party observability
   without leaking PII or secrets in tool arguments.

To send the same spans to your existing OpenTelemetry pipeline (Datadog,
Honeycomb, Tempo, Jaeger, ...) swap ``InMemoryTracer()`` for
``OTelTracer()`` and configure your OTel SDK in the usual way. The OTel
adapter ships zero exporter glue — it plugs into whatever provider you
already have. Install ``opentelemetry-api`` + your exporter to use it.
"""

from __future__ import annotations

import json
import os

import anyio

from mantis_agent import Agent, InMemoryTracer, TextBlock, UserMessage, tool
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
from mantis_agent.types import ToolUseBlock, Usage


@tool
async def get_weather(city: str) -> str:
    """Look up the weather (mocked here, real API in your version)."""

    return f"sunny in {city}, 72F"


def _scripted_turns():
    """Two-turn script: turn 1 calls get_weather, turn 2 answers in text."""

    turn1 = [
        MessageStart(message_id="m1", model="mock"),
        ContentBlockStart(
            index=0, block=TextBlock(text="")
        ),
        ContentBlockDelta(index=0, delta=TextDelta(text="Let me check.")),
        ContentBlockStop(index=0),
        ContentBlockStart(
            index=1,
            block=ToolUseBlock(id="tu1", name="get_weather", input={}),
        ),
        ContentBlockDelta(
            index=1, delta=InputJsonDelta(partial_json='{"city": "Paris"}')
        ),
        ContentBlockStop(index=1),
        MessageDelta(
            stop_reason="tool_use",
            usage=Usage(input_tokens=120, output_tokens=18),
        ),
        MessageStop(),
    ]
    turn2 = [
        MessageStart(message_id="m2", model="mock"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(
            index=0, delta=TextDelta(text="It's sunny in Paris, 72F.")
        ),
        ContentBlockStop(index=0),
        MessageDelta(
            stop_reason="end_turn",
            usage=Usage(input_tokens=160, output_tokens=12),
        ),
        MessageStop(),
    ]
    return [turn1, turn2]


class _ScriptedProvider(MockProvider):
    """Mock provider that walks through a list of pre-baked turns."""

    def __init__(self, turns: list[list]):
        super().__init__(scripted_events=turns[0])
        self._turns = turns
        self._i = 0

    async def stream(self, **kw):
        events = self._turns[self._i]
        self._i = min(self._i + 1, len(self._turns) - 1)
        for ev in events:
            yield ev


async def main() -> None:
    tracer = InMemoryTracer()
    provider = _ScriptedProvider(_scripted_turns())
    agent = Agent(
        model="mock",
        provider=provider,
        include_memory=False,
        tools=[get_weather],
        tracer=tracer,
    )

    await agent.run(
        [UserMessage(content=[TextBlock(text="What's the weather in Paris?")])]
    )

    # --- Pretty-print the tree --------------------------------------------
    def walk(nodes, indent: int = 0) -> None:
        for n in nodes:
            attrs = {
                k: v
                for k, v in n["attributes"].items()
                if any(
                    k.startswith(p)
                    for p in (
                        "agent.model",
                        "agent.turns",
                        "agent.total_",
                        "turn.stop_reason",
                        "turn.tool_uses",
                        "llm.input_tokens",
                        "llm.output_tokens",
                        "llm.first_token_ms",
                        "tool.name",
                        "tool.is_error",
                    )
                )
            }
            dur = n["duration_ms"]
            dur_s = f"{dur:6.2f}ms" if dur is not None else "      ?"
            print(f"{'  ' * indent}{n['name']:14s} {dur_s}  {attrs}")
            walk(n["children"], indent + 1)

    print("\nSpan tree (newest at top):\n")
    walk(tracer.tree())

    # --- Summary ----------------------------------------------------------
    print("\nSummary:")
    print(json.dumps(tracer.summary(), indent=2))

    # --- Optional: dump JSONL to disk -------------------------------------
    out = os.environ.get("TRACE_OUT", "")
    if out:
        tracer.write_jsonl(out)
        print(f"\nWrote {len(tracer.spans)} spans → {out}")


if __name__ == "__main__":
    anyio.run(main)
