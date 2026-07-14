"""Tracing — span tree for every agent run, with optional OpenTelemetry export.

The Anthropic ``claude-agent-sdk`` ships rich event taxonomies but no first-class
observability primitive. In production you usually want to answer questions like
"how long did turn 3 take?", "which tool blew the latency budget?", "what was the
prompt cache hit rate this turn?", or just "give me a trace I can ship to
Datadog/Honeycomb/Tempo." That's what this module is for.

Three layers — pick the one you need and ignore the rest:

1. ``Tracer`` — the protocol. The agent loop calls ``tracer.start_span(...)``
   and the returned span is a context manager. Implementations decide what to
   do (record in memory, ship to OTel, throw away).
2. ``InMemoryTracer`` — zero-dep default. Keeps an ordered list of finished
   spans on the tracer; spans carry parent/child references for tree
   reconstruction. ``tracer.spans`` is the public surface; ``tracer.tree()``
   builds a nested view. Cheap enough to leave on in production.
3. ``OTelTracer`` — lazy-imports ``opentelemetry.trace``. If the package isn't
   installed we raise a clear ImportError at construction (not at first span).
   Everything plays nicely with an existing OTel context — if the user already
   has a tracer provider configured (Datadog, Honeycomb, Tempo, Jaeger,
   whatever), spans nest under it.

The agent loop wraps four scopes:

* ``agent.run`` — the outermost span. One per ``Agent.run()`` / ``run_iter()``
  / ``stream()`` call. Attributes: ``agent.model``, ``agent.max_steps``,
  ``agent.system.len``, ``agent.tools.count``, end-state ``agent.turns``,
  ``agent.total_input_tokens``, ``agent.total_output_tokens``,
  ``agent.total_cost_usd``.
* ``agent.turn`` — per turn. Attributes: ``turn.index``, ``turn.stop_reason``,
  ``turn.input_tokens``, ``turn.output_tokens``, ``turn.cost_usd``.
* ``llm.call`` — one per provider stream invocation. Attributes:
  ``llm.model``, ``llm.provider``, ``llm.input_tokens``, ``llm.output_tokens``,
  ``llm.cache_read_tokens``, ``llm.cache_creation_tokens``,
  ``llm.stop_reason``, ``llm.first_token_ms``.
* ``tool.call`` — one per tool execution (NOT short-circuited). Attributes:
  ``tool.name``, ``tool.id``, ``tool.input.keys`` (sorted list of input keys —
  never the values, to avoid leaking PII), ``tool.is_error``,
  ``tool.result.len``.

The semantic conventions for ``llm.*`` and ``tool.*`` align with the
OpenTelemetry GenAI working-group draft (https://opentelemetry.io/docs/specs/semconv/gen-ai/)
so existing OTel dashboards drop in.

Design notes
------------
* Tracing is *strictly additive* — when ``Agent.tracer`` is ``None`` the agent
  loop does ZERO extra work (no allocation, no method calls, no branching past
  the initial ``if self.tracer is None``). The hot path stays clean.
* Spans hold attribute dicts and timing; they do NOT hold the conversation,
  the prompt, or the tool result content. That stays out of traces because
  traces leak — they get shipped to third-party SaaS, captured in screenshots,
  shared in tickets. PII is on the *messages*, not the spans.
* ``span_id`` is a 16-char hex string (NOT a uuid) so OTel exporters can use
  it verbatim as the OTel ``trace_id`` low-64-bits. Costs us no compatibility.
* Implementations MUST be safe to construct without an event loop running.
  ``InMemoryTracer`` is just dicts + lists; ``OTelTracer`` only imports
  ``opentelemetry.trace`` (cheap, no I/O).
* The tracer is shared across sub-agents — the parent agent passes its tracer
  to spawned sub-agents so the sub-agent's spans nest under the parent's
  ``agent.run`` span. See ``subagent.py`` for the wiring (added in the same
  patch as this module).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Span data
# ---------------------------------------------------------------------------


def _new_span_id() -> str:
    """16-char hex span id. Matches the OTel SpanContext span_id width."""

    return secrets.token_hex(8)


@dataclass(slots=True)
class Span:
    """One unit of work, with timing and attributes.

    Spans are mutable while open (attributes can be added mid-flight) and
    frozen-by-convention after ``end()``. The agent loop never mutates a
    span after closing it — callers shouldn't either.
    """

    name: str
    span_id: str = field(default_factory=_new_span_id)
    parent_id: str | None = None
    trace_id: str = ""
    start_ns: int = field(default_factory=time.monotonic_ns)
    end_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # "ok" | "error" | "cancelled"
    exception: str | None = None
    # Callbacks fired exactly once when the span ends (idempotent with end()).
    # Tracers register here so that a span opened via ``start_span`` and closed
    # manually via ``sp.end()`` still gets exported/recorded — the agent loop
    # uses that manual pattern (not the context manager) for run/turn/llm spans.
    # Excluded from repr/compare/serialization; runtime wiring only.
    _end_callbacks: list[Callable[[Span], None]] = field(
        default_factory=list, repr=False, compare=False
    )

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration in milliseconds, or ``None`` if still open."""

        if self.end_ns is None:
            return None
        return (self.end_ns - self.start_ns) / 1_000_000.0

    def set_attribute(self, key: str, value: Any) -> None:
        """Record an attribute. Values must be JSON-serializable for export."""

        self.attributes[key] = value

    def set_attributes(self, attrs: Mapping[str, Any]) -> None:
        for k, v in attrs.items():
            self.attributes[k] = v

    def end(
        self,
        *,
        status: str = "ok",
        exception: BaseException | None = None,
    ) -> None:
        """Mark the span complete. Idempotent — subsequent calls are no-ops."""

        if self.end_ns is not None:
            return
        self.end_ns = time.monotonic_ns()
        self.status = status
        if exception is not None:
            self.exception = f"{type(exception).__name__}: {exception}"
            if self.status == "ok":
                self.status = "error"
        # Fire end callbacks exactly once (guarded by the end_ns check above).
        # A misbehaving callback must never prevent the span from closing.
        for cb in self._end_callbacks:
            try:
                cb(self)
            except Exception:  # noqa: BLE001 — never let export break span end
                logger.debug("span end callback failed", exc_info=True)

    # Convenience: dict for JSON / OTel translation.
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "trace_id": self.trace_id,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "exception": self.exception,
            "attributes": dict(self.attributes),
        }


# ---------------------------------------------------------------------------
# Tracer protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Tracer(Protocol):
    """Anything the agent loop calls to record spans.

    Implementations:

      * :class:`InMemoryTracer` — record to a list, no deps.
      * :class:`OTelTracer` — proxy to ``opentelemetry.trace``.
      * Your own — three methods. Just match the signatures.

    Reentrant: ``start_span`` may be called from any task; implementations
    are responsible for thread/task-safety of their own state.
    """

    def start_span(
        self,
        name: str,
        *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        """Open a new span. Returns it; caller must ``end()`` it."""

        ...

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Context-managed span. Auto-ends on exit; sets error status on exc."""

        ...

    @property
    def trace_id(self) -> str:
        """The id grouping all spans from one root. 32-char hex (OTel width)."""

        ...


# ---------------------------------------------------------------------------
# InMemoryTracer — the default
# ---------------------------------------------------------------------------


class InMemoryTracer:
    """Zero-dep tracer that keeps every finished span on ``self.spans``.

    Suitable for tests, local debugging, dashboards that read the list
    after a run, and "just give me a JSONL trace" exports. The list is
    bounded by ``max_spans`` (default 50k) with oldest-first rolling
    eviction, so a long-running agent can't grow it without limit — for
    very long-running agents that need every span you may still want to
    swap in an :class:`OTelTracer` (or a custom impl that flushes
    periodically). For 99% of agent runs, in-memory is fine.

    Span order in ``self.spans`` is *end-order* — a span enters the list
    when it ``end()``s, not when it starts. This matches OTel exporter
    semantics and means the list is naturally in finish-time order.

    Thread/task safety: appends to ``self.spans`` are atomic in CPython
    (list.append is GIL-protected). Reading the list while a run is in
    flight is safe but the snapshot may grow under you.
    """

    def __init__(
        self, *, trace_id: str | None = None, max_spans: int | None = 50_000
    ) -> None:
        # 32-char hex matches OTel TraceId width (128 bits).
        self._trace_id = trace_id or secrets.token_hex(16)
        self.spans: list[Span] = []
        self._open: dict[str, Span] = {}
        # Bound the retained history so a long-running agent can't grow the
        # list without limit. Oldest (earliest-finished) spans are evicted
        # first. ``None`` disables the cap for callers that truly want it all.
        self._max_spans = max_spans

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def start_span(
        self,
        name: str,
        *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        sp = Span(
            name=name,
            parent_id=parent.span_id if parent is not None else None,
            trace_id=self._trace_id,
            attributes=dict(attributes) if attributes else {},
        )
        self._open[sp.span_id] = sp
        # Move the span into ``self.spans`` when it ends — even when the caller
        # ends it manually via ``sp.end()`` instead of the ``span()`` CM. end()
        # is idempotent so ``_close`` runs at most once.
        sp._end_callbacks.append(self._close)
        return sp

    def _close(self, sp: Span) -> None:
        # Caller already called sp.end(); we only move it to spans + drop from open.
        # Idempotent: only the first close (while the span is still open) records
        # it. Guards against double-recording when a span is closed via BOTH its
        # end callback and an explicit ``_close`` call — e.g. the streaming
        # executor ends the span then calls ``_close`` as belt-and-suspenders.
        if self._open.pop(sp.span_id, None) is None:
            return
        self.spans.append(sp)
        if self._max_spans is not None and len(self.spans) > self._max_spans:
            # Rolling eviction — drop the oldest to keep memory bounded.
            del self.spans[: len(self.spans) - self._max_spans]

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        sp = self.start_span(name, parent=parent, attributes=attributes)
        try:
            yield sp
        except BaseException as exc:
            sp.end(status="error", exception=exc)  # _close runs via end callback
            raise
        else:
            sp.end()  # _close runs via end callback

    # --- export helpers --------------------------------------------------

    def to_jsonl(self) -> str:
        """Serialize finished spans, one JSON object per line."""

        return "\n".join(json.dumps(s.to_dict(), default=str) for s in self.spans)

    def write_jsonl(self, path: str | os.PathLike[str]) -> None:
        """Write JSONL spans to disk. Overwrites the file."""

        with open(path, "w", encoding="utf-8") as f:
            for s in self.spans:
                f.write(json.dumps(s.to_dict(), default=str))
                f.write("\n")

    def tree(self) -> list[dict[str, Any]]:
        """Reconstruct the span forest. Roots are spans with no parent.

        Each node has the span's ``to_dict()`` shape plus a ``children``
        key. Order within each level is end-time-ascending. Useful for
        pretty-printing or piping into a UI.
        """

        by_id: dict[str, dict[str, Any]] = {}
        roots: list[dict[str, Any]] = []
        for sp in self.spans:
            node = sp.to_dict()
            node["children"] = []
            by_id[sp.span_id] = node
        for sp in self.spans:
            node = by_id[sp.span_id]
            parent = by_id.get(sp.parent_id) if sp.parent_id else None
            if parent is None:
                roots.append(node)
            else:
                parent["children"].append(node)
        return roots

    def summary(self) -> dict[str, Any]:
        """Aggregate stats useful for tests + dashboards.

        Returns counts and totals by span name. Cost / token totals are
        computed from ``agent.total_*`` attributes on the root agent.run
        span when present.
        """

        by_name: dict[str, dict[str, Any]] = {}
        for sp in self.spans:
            slot = by_name.setdefault(
                sp.name, {"count": 0, "total_ms": 0.0, "errors": 0}
            )
            slot["count"] += 1
            if sp.duration_ms is not None:
                slot["total_ms"] += sp.duration_ms
            if sp.status == "error":
                slot["errors"] += 1
        # Pull totals off any root agent.run if present.
        root = next(
            (
                s for s in self.spans
                if s.name == "agent.run" and s.parent_id is None
            ),
            None,
        )
        totals: dict[str, Any] = {}
        if root is not None:
            for key in (
                "agent.total_input_tokens",
                "agent.total_output_tokens",
                "agent.total_cost_usd",
                "agent.turns",
            ):
                if key in root.attributes:
                    totals[key.removeprefix("agent.")] = root.attributes[key]
        return {
            "trace_id": self._trace_id,
            "by_name": by_name,
            "totals": totals,
            "span_count": len(self.spans),
        }


# ---------------------------------------------------------------------------
# OTelTracer — optional, lazy-imports opentelemetry
# ---------------------------------------------------------------------------


class OTelTracer:
    """Adapter that proxies to ``opentelemetry.trace``.

    Constructing this requires the ``opentelemetry-api`` package. If it isn't
    installed we raise ImportError at construction (loud + immediate) so users
    aren't surprised at first span. The user is responsible for *configuring*
    OTel — we don't ship an exporter, a provider, or any SDK glue. Plug into
    your existing OTel setup; we just emit spans.

    Each span we open is also kept as a lightweight :class:`Span` (so the
    in-memory inspection API works in parallel) AND propagated as an
    ``opentelemetry.trace.Span``. The OTel span context carries the
    parent/child relationship correctly across asyncio tasks via the
    OTel context API.
    """

    def __init__(self, *, service_name: str = "mantis-agent-sdk") -> None:
        try:
            from opentelemetry import trace as _otel_trace  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "OTelTracer requires the 'opentelemetry-api' package. "
                "Install it with: pip install opentelemetry-api opentelemetry-sdk"
            ) from exc
        self._otel = _otel_trace
        self._tracer = _otel_trace.get_tracer(service_name)
        self._trace_id = secrets.token_hex(16)
        self._open_otel: dict[str, Any] = {}
        # Run an InMemoryTracer alongside so the same inspection API works.
        self._mirror = InMemoryTracer(trace_id=self._trace_id)
        self.spans = self._mirror.spans  # alias for parity with InMemoryTracer

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def start_span(
        self,
        name: str,
        *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        sp = self._mirror.start_span(name, parent=parent, attributes=attributes)
        # Open the OTel span as a peer; it will be ended when we close ours.
        # We DON'T use OTel's context propagation here because the agent
        # passes Span objects explicitly — that's the contract the agent
        # loop relies on for task-safe nesting.
        try:
            otel_span = self._tracer.start_span(name, attributes=dict(sp.attributes))
        except Exception:  # noqa: BLE001 — never let OTel break the agent
            otel_span = None
            logger.debug("OTel start_span failed", exc_info=True)
        if otel_span is not None:
            self._open_otel[sp.span_id] = otel_span
            # Ensure the OTel span is ended (and thus exported) even when the
            # caller closes ``sp`` manually via ``sp.end()`` rather than the
            # ``span()`` context manager — the agent loop uses the manual path.
            # The mirror's own _close callback (registered by
            # ``InMemoryTracer.start_span``) already fires alongside this one.
            sp._end_callbacks.append(self._end_otel_from_span)
        return sp

    def _end_otel_from_span(self, sp: Span) -> None:
        """End-callback: close the OTel span using the mirror span's status.

        Used for the manual ``start_span``/``sp.end()`` path where we don't have
        the original exception object — status ("ok"/"error"/"cancelled") is
        read off ``sp``. The context-managed path calls ``_finish_otel``
        directly first (with the real exception) so this becomes a no-op there,
        since ``_finish_otel`` pops the OTel span from ``_open_otel``.
        """

        self._finish_otel(sp, status=sp.status, exception=None)

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        sp = self.start_span(name, parent=parent, attributes=attributes)
        try:
            yield sp
        except BaseException as exc:
            # Finish OTel first with the real exception (for record_exception);
            # sp.end() then runs the mirror's _close callback (and the OTel
            # end-callback, now a no-op since the span was already popped).
            self._finish_otel(sp, status="error", exception=exc)
            sp.end(status="error", exception=exc)
            raise
        else:
            self._finish_otel(sp, status="ok", exception=None)
            sp.end()

    def _finish_otel(
        self,
        sp: Span,
        *,
        status: str,
        exception: BaseException | None,
    ) -> None:
        otel_span = self._open_otel.pop(sp.span_id, None)
        if otel_span is None:
            return
        try:
            # Re-set attributes captured after start_span.
            for k, v in sp.attributes.items():
                try:
                    otel_span.set_attribute(k, v)
                except Exception:  # noqa: BLE001
                    pass
            if exception is not None:
                try:
                    otel_span.record_exception(exception)
                except Exception:  # noqa: BLE001
                    pass
            # Mark the span errored when we either caught an exception or the
            # mirror span itself recorded an error status (manual end() path,
            # where the exception object isn't threaded through the callback).
            if exception is not None or sp.status == "error":
                try:
                    otel_span.set_status(self._otel.Status(self._otel.StatusCode.ERROR))
                except Exception:  # noqa: BLE001
                    pass
            otel_span.end()
        except Exception:  # noqa: BLE001
            logger.debug("OTel end_span failed", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers used by the agent loop
# ---------------------------------------------------------------------------


def maybe_start_span(
    tracer: Tracer | None,
    name: str,
    *,
    parent: Span | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Span | None:
    """``tracer.start_span(...)`` if a tracer is configured, else ``None``.

    Centralizes the ``if tracer is None`` branch so call sites stay tight.
    Returns a span the caller must close manually; for context-managed usage
    use :func:`maybe_span` instead.
    """

    if tracer is None:
        return None
    return tracer.start_span(name, parent=parent, attributes=attributes)


@contextlib.contextmanager
def maybe_span(
    tracer: Tracer | None,
    name: str,
    *,
    parent: Span | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span | None]:
    """Context-managed span that is a no-op when ``tracer is None``.

    Lets the agent loop write::

        with maybe_span(self.tracer, "agent.turn", parent=run_span,
                        attributes={"turn.index": i}) as turn_span:
            ...

    without an ``if tracer is None`` branch around every span.
    """

    if tracer is None:
        yield None
        return
    with tracer.span(name, parent=parent, attributes=attributes) as sp:
        yield sp


__all__ = [
    "Span",
    "Tracer",
    "InMemoryTracer",
    "OTelTracer",
    "maybe_start_span",
    "maybe_span",
]
