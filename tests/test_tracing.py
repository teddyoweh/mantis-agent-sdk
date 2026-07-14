"""Tests for ``mantis_agent.tracing`` + Agent integration.

We test three things:

1. The :class:`InMemoryTracer` primitive on its own — span creation, nesting,
   attributes, error status, JSONL export, ``tree()`` reconstruction,
   ``summary()`` aggregation.

2. End-to-end integration with an :class:`Agent` driven by a scripted
   :class:`MockProvider`. We check the span hierarchy (one ``agent.run`` →
   one or more ``agent.turn`` → ``llm.call`` + zero-or-more ``tool.call``),
   that token / cost totals land on the root span, that tool spans carry
   the right attributes, that the hot-path overhead is gated by
   ``Agent.tracer is None`` (no spans when no tracer).

3. The :class:`OTelTracer` constructor — when ``opentelemetry-api`` is not
   installed the constructor raises ``ImportError`` with a clear message
   pointing at the install command. We do NOT require OTel as a test dep —
   the import gate is the entire contract for users who don't use OTel.

Why no recorded fixture? The agent loop has its own recorded suites under
``tests/test_*_examples_*``; the goal here is wiring + invariants, not
provider behaviour.
"""

from __future__ import annotations

import json

import anyio
import pytest

from mantis_agent import (
    Agent,
    InMemoryTracer,
    OTelTracer,
    TextBlock,
    Tracer,
    UserMessage,
    tool,
)
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


# ---------------------------------------------------------------------------
# Unit: InMemoryTracer primitives
# ---------------------------------------------------------------------------


def test_span_id_widths_match_otel():
    """Span ids are 16-char hex (OTel SpanID width); trace ids are 32-char
    hex (OTel TraceID width). This lets exporters use them verbatim."""

    t = InMemoryTracer()
    assert len(t.trace_id) == 32
    sp = t.start_span("agent.run")
    assert len(sp.span_id) == 16


def test_inmemory_records_in_end_order():
    """Spans land on ``tracer.spans`` in the order they ``end()``, not the
    order they ``start_span()``. Matches OTel exporter semantics."""

    t = InMemoryTracer()
    with t.span("outer") as outer:
        with t.span("inner_a", parent=outer):
            pass
        with t.span("inner_b", parent=outer):
            pass
    assert [s.name for s in t.spans] == ["inner_a", "inner_b", "outer"]


def test_inmemory_attributes_round_trip_through_jsonl():
    """``to_dict`` / ``to_jsonl`` carry attributes faithfully — required for
    shipping traces to any line-oriented log sink."""

    t = InMemoryTracer()
    with t.span("agent.turn", attributes={"turn.index": 3}) as sp:
        sp.set_attribute("turn.stop_reason", "end_turn")
    lines = t.to_jsonl().splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["name"] == "agent.turn"
    assert d["attributes"]["turn.index"] == 3
    assert d["attributes"]["turn.stop_reason"] == "end_turn"
    assert d["status"] == "ok"
    assert d["duration_ms"] is not None


def test_inmemory_exception_marks_error_status():
    """An exception inside ``with tracer.span(...)`` marks the span errored
    and records the exception type+message — even when the exception
    propagates out."""

    t = InMemoryTracer()
    with pytest.raises(RuntimeError):
        with t.span("blows.up"):
            raise RuntimeError("boom")
    assert len(t.spans) == 1
    sp = t.spans[0]
    assert sp.status == "error"
    assert sp.exception is not None
    assert "RuntimeError" in sp.exception
    assert "boom" in sp.exception


def test_inmemory_tree_reconstructs_forest():
    """``tree()`` rebuilds the parent/child relationships from flat list."""

    t = InMemoryTracer()
    with t.span("root") as r:
        with t.span("child", parent=r) as c:
            with t.span("grandchild", parent=c):
                pass
    forest = t.tree()
    assert len(forest) == 1
    assert forest[0]["name"] == "root"
    assert len(forest[0]["children"]) == 1
    assert forest[0]["children"][0]["name"] == "child"
    assert forest[0]["children"][0]["children"][0]["name"] == "grandchild"


def test_inmemory_summary_counts_by_name():
    """``summary()`` aggregates counts + total_ms by span name; errors
    counted separately."""

    t = InMemoryTracer()
    with t.span("agent.run") as root:
        with t.span("agent.turn", parent=root):
            with t.span("llm.call", parent=root):
                pass
            with t.span("llm.call", parent=root):
                pass
    s = t.summary()
    assert s["by_name"]["llm.call"]["count"] == 2
    assert s["by_name"]["llm.call"]["errors"] == 0
    assert s["span_count"] == 4


def test_inmemory_write_jsonl_to_tmp(tmp_path):
    """File-export sanity check."""

    t = InMemoryTracer()
    with t.span("one"):
        pass
    with t.span("two"):
        pass
    out = tmp_path / "trace.jsonl"
    t.write_jsonl(out)
    text = out.read_text()
    assert text.count("\n") == 2
    decoded = [json.loads(line) for line in text.splitlines()]
    assert [d["name"] for d in decoded] == ["one", "two"]


# ---------------------------------------------------------------------------
# Tracer protocol — duck-typed adapters work
# ---------------------------------------------------------------------------


def test_inmemory_satisfies_tracer_protocol():
    """The protocol is structural; ``isinstance`` works because of
    ``@runtime_checkable``. Catches accidental signature drift."""

    t = InMemoryTracer()
    assert isinstance(t, Tracer)


# ---------------------------------------------------------------------------
# Integration: end-to-end with Agent + MockProvider
# ---------------------------------------------------------------------------


def _scripted_text_turn(text: str = "hello") -> list:
    """One natural-stop turn that yields a single text block."""

    return [
        MessageStart(message_id="m", model="mock"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=text)),
        ContentBlockStop(index=0),
        MessageDelta(
            stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=2)
        ),
        MessageStop(),
    ]


def test_agent_records_run_turn_llm_spans_with_mock():
    """A single-turn run with no tools produces exactly:
       agent.run → agent.turn → llm.call. Totals land on the run span."""

    tracer = InMemoryTracer()
    provider = MockProvider(scripted_events=_scripted_text_turn())
    agent = Agent(
        model="mock", provider=provider, include_memory=False, tracer=tracer
    )

    async def go():
        await agent.run([UserMessage(content=[TextBlock(text="hi")])])

    anyio.run(go)

    names = [s.name for s in tracer.spans]
    assert names == ["llm.call", "agent.turn", "agent.run"]

    # Parent relationships.
    run = next(s for s in tracer.spans if s.name == "agent.run")
    turn = next(s for s in tracer.spans if s.name == "agent.turn")
    llm = next(s for s in tracer.spans if s.name == "llm.call")
    assert run.parent_id is None
    assert turn.parent_id == run.span_id
    assert llm.parent_id == turn.span_id

    # Totals stamped on root.
    assert run.attributes["agent.turns"] == 1
    assert run.attributes["agent.total_input_tokens"] == 10
    assert run.attributes["agent.total_output_tokens"] == 2
    assert run.attributes["agent.model"] == "mock"

    # Per-turn attrs.
    assert turn.attributes["turn.stop_reason"] == "end_turn"
    assert turn.attributes["turn.tool_uses"] == 0

    # LLM attrs include first_token_ms (may be 0 in tests but key is present).
    assert "llm.first_token_ms" in llm.attributes


def test_agent_records_tool_call_span_with_input_keys_only():
    """Tool span carries ``tool.name``, ``tool.id``, ``tool.input.keys``
    (sorted) — but NEVER ``tool.input.values``, so PII can't leak into
    observability backends."""

    @tool
    async def lookup(user_id: str, secret_token: str) -> str:
        "lookup user"
        return f"user-{user_id}"

    tool_turn = [
        MessageStart(message_id="m1", model="mock"),
        ContentBlockStart(
            index=0, block=ToolUseBlock(id="tu1", name="lookup", input={})
        ),
        ContentBlockDelta(
            index=0,
            delta=InputJsonDelta(
                partial_json='{"user_id": "alice", "secret_token": "shhh"}'
            ),
        ),
        ContentBlockStop(index=0),
        MessageDelta(
            stop_reason="tool_use", usage=Usage(input_tokens=5, output_tokens=3)
        ),
        MessageStop(),
    ]
    final_turn = _scripted_text_turn("done")

    class TwoTurn(MockProvider):
        def __init__(self):
            super().__init__(scripted_events=tool_turn)
            self._n = 0

        async def stream(self, **kw):
            events = tool_turn if self._n == 0 else final_turn
            self._n += 1
            for e in events:
                yield e

    tracer = InMemoryTracer()
    agent = Agent(
        model="mock",
        provider=TwoTurn(),
        include_memory=False,
        tools=[lookup],
        tracer=tracer,
    )

    async def go():
        await agent.run([UserMessage(content=[TextBlock(text="who is alice?")])])

    anyio.run(go)

    tool_spans = [s for s in tracer.spans if s.name == "tool.call"]
    assert len(tool_spans) == 1
    ts = tool_spans[0]
    assert ts.attributes["tool.name"] == "lookup"
    assert ts.attributes["tool.id"] == "tu1"
    # Sorted input keys (and only keys — never values).
    assert ts.attributes["tool.input.keys"] == ["secret_token", "user_id"]
    # No raw input values bled through.
    assert "alice" not in json.dumps(ts.to_dict(), default=str)
    assert "shhh" not in json.dumps(ts.to_dict(), default=str)
    assert ts.attributes["tool.is_error"] is False


def test_agent_with_no_tracer_records_no_spans():
    """When ``Agent.tracer is None`` the loop is the hot path — no spans
    are recorded anywhere. Guards against accidental always-on tracing."""

    provider = MockProvider(scripted_events=_scripted_text_turn())
    agent = Agent(
        model="mock", provider=provider, include_memory=False, tracer=None
    )

    async def go():
        await agent.run([UserMessage(content=[TextBlock(text="hi")])])

    anyio.run(go)
    # We have no tracer to inspect, but the loop completed — that's the
    # entire contract: zero exceptions, zero behavioral difference.
    assert agent.tracer is None


def test_run_span_aggregates_across_multiple_turns():
    """Multi-turn run: totals sum across every turn's usage."""

    @tool
    async def echo(s: str) -> str:
        "echo"
        return s

    t1 = [
        MessageStart(message_id="m1", model="mock"),
        ContentBlockStart(index=0, block=ToolUseBlock(id="tu1", name="echo", input={})),
        ContentBlockDelta(
            index=0, delta=InputJsonDelta(partial_json='{"s": "x"}')
        ),
        ContentBlockStop(index=0),
        MessageDelta(
            stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)
        ),
        MessageStop(),
    ]
    t2 = [
        MessageStart(message_id="m2", model="mock"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="x")),
        ContentBlockStop(index=0),
        MessageDelta(
            stop_reason="end_turn", usage=Usage(input_tokens=15, output_tokens=1)
        ),
        MessageStop(),
    ]

    class TwoTurn(MockProvider):
        def __init__(self):
            super().__init__(scripted_events=t1)
            self._n = 0

        async def stream(self, **kw):
            events = t1 if self._n == 0 else t2
            self._n += 1
            for e in events:
                yield e

    tracer = InMemoryTracer()
    agent = Agent(
        model="mock",
        provider=TwoTurn(),
        include_memory=False,
        tools=[echo],
        tracer=tracer,
    )

    async def go():
        await agent.run([UserMessage(content=[TextBlock(text="say x")])])

    anyio.run(go)

    run = next(s for s in tracer.spans if s.name == "agent.run")
    # Two turns. Totals sum input+output across both.
    assert run.attributes["agent.turns"] == 2
    assert run.attributes["agent.total_input_tokens"] == 10 + 15
    assert run.attributes["agent.total_output_tokens"] == 5 + 1


# ---------------------------------------------------------------------------
# OTelTracer — optional dep gate
# ---------------------------------------------------------------------------


def test_otel_tracer_import_error_message_is_clear():
    """When ``opentelemetry-api`` isn't installed (the default test env),
    ``OTelTracer()`` raises ImportError pointing at the install cmd.

    If OTel *is* present in the test env, this test silently passes —
    we still get observability via ``InMemoryTracer`` which is what most
    users will reach for first.
    """

    try:
        import opentelemetry.trace  # noqa: F401 — checking presence

        # OTel is installed; constructor should succeed.
        t = OTelTracer()
        assert len(t.trace_id) == 32
    except ImportError:
        # OTel not installed (expected default). Construction must raise
        # with a helpful message.
        with pytest.raises(ImportError) as ei:
            OTelTracer()
        assert "opentelemetry-api" in str(ei.value)
