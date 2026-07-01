"""``StreamingToolExecutor`` — kick off tool calls as soon as their JSON
finalizes mid-stream, run them with the right concurrency rules, and produce
``ToolResultBlock``s as they complete (or in insertion order via
``wait_all`` when the turn is done).

This is the *speed unlock* described in ``docs/plan.md`` §5. The naive
"wait until message_stop, then dispatch" loop pays a per-tool serialization
tax even when tools could overlap. By starting each tool the moment its
``</tool_call>`` / native ``tool_use.stop`` arrives, multi-tool turns finish
in roughly ``max(tool_durations)`` rather than ``sum(tool_durations)``.

Two ways to observe results
---------------------------
* ``await ex.wait_all()`` — block until every dispatched tool finishes
  and return a list of ``ToolResultBlock``s in **insertion order**
  (same order as ``add_tool_call`` calls). Use this when downstream
  needs the canonical result list for the assistant turn.
* ``async for idx, result in ex.iter_completions(): ...`` — yield
  ``(idx, ToolResultBlock)`` pairs in **completion order** as each tool
  finishes. ``idx`` is the insertion index so callers can correlate. Use
  this for live UIs that want to render "tool #2 done (120ms)" the
  moment it completes, without waiting on slower siblings.
* ``await ex.wait_one()`` — single-shot variant of ``iter_completions``.
  Returns the next completion as ``(idx, ToolResultBlock)`` or ``None``
  if the executor has closed with no further work pending.

Behavior summary
----------------
* **Concurrency-safe tools** run in parallel, bounded by
  ``MANTIS_AGENT_MAX_TOOL_CONCURRENCY`` (env, default 10) or the
  ``max_concurrency`` constructor arg.
* **Non-concurrency-safe tools** run serially — one at a time, never
  overlapping with another non-safe tool (they *can* overlap with safe
  tools, mirroring upstream's behavior).
* ``Tool.is_concurrency_safe`` may be a bool *or* a callable
  ``(input) -> bool``. Callable form lets ``bash`` say "safe iff paths
  don't conflict" etc.
* ``Tool.abort_siblings_on_error`` — when a tool with this flag errors,
  every in-flight sibling under the executor is cancelled via a shared
  ``CancelScope`` and produces a ``ToolResultBlock(is_error=True)``.
* ``Tool.timeout_s`` — wraps the call in ``anyio.fail_after``; on timeout
  the result is ``is_error=True``.
* User-tool exceptions never propagate. They become ``is_error=True``
  result blocks. The executor is bulletproof against bad tool code.
* Missing tools become ``is_error=True`` immediately.
* ``can_use_tool(tool, input, ctx)`` — if supplied, gates every call.
  Denials become ``is_error=True`` with the reason as the body.

Anyio, not asyncio
------------------
We use ``anyio`` primitives throughout: ``create_task_group``,
``CancelScope``, ``Semaphore``, ``Lock``, ``fail_after``,
``create_memory_object_stream``. The agent loop must be driven by an
anyio backend (default trio or asyncio under ``anyio.run``). Never
reach for ``asyncio.*`` here.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Optional

import anyio
import anyio.abc
import msgspec

from ..errors import ToolExecutionError
from ..tools import Tool, ToolRegistry
from ..types import ImageBlock, TextBlock, ToolResultBlock, ToolUseBlock

__all__ = ["CanUseToolFn", "StreamingToolExecutor"]

_LOG = logging.getLogger("mantis_agent.streaming.executor")
_ENC = msgspec.json.Encoder()
_DEFAULT_MAX_CONCURRENCY = 10
_ENV_VAR = "MANTIS_AGENT_MAX_TOOL_CONCURRENCY"


# Signature: (tool, input, ctx) -> (allowed, reason_if_denied)
CanUseToolFn = Callable[
    [Tool, dict[str, Any], dict[str, Any]],
    Awaitable[tuple[bool, Optional[str]]],
]


def _resolve_max_concurrency(override: int | None) -> int:
    if override is not None and override > 0:
        return override
    raw = os.environ.get(_ENV_VAR)
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            _LOG.warning("ignoring invalid %s=%r", _ENV_VAR, raw)
    return _DEFAULT_MAX_CONCURRENCY


def _is_safe(tool: Tool, tool_input: dict[str, Any]) -> bool:
    """Resolve ``Tool.is_concurrency_safe`` against this input.

    Accepts:
      * a static ``bool`` (the common case)
      * a callable ``(input) -> bool``
      * the legacy ``parallel_safe: bool`` attribute (current tools.py)

    The callable path lets ``bash``-class tools decide per-input —
    e.g. two writes to disjoint paths can parallelize."""
    flag = getattr(tool, "is_concurrency_safe", None)
    if flag is None:
        # Fall back to the older static attribute on Tool. v0 of tools.py uses
        # ``parallel_safe: bool``; the M0.1 refactor renames + extends it.
        flag = getattr(tool, "parallel_safe", True)
    if callable(flag):
        try:
            return bool(flag(tool_input))
        except Exception:  # noqa: BLE001 — predicate must never crash dispatch
            _LOG.exception("is_concurrency_safe predicate raised; assuming UNSAFE")
            return False
    return bool(flag)


def _stringify(out: Any) -> str:
    if isinstance(out, str):
        return out
    try:
        return _ENC.encode(out).decode()
    except (TypeError, msgspec.EncodeError):
        return str(out)


# Tool-result truncation backstop. A single huge tool result (a `cat bigfile`,
# a noisy build log, an MCP tool dumping JSON) can blow the whole context window
# in one turn — most builtin tools self-cap, but this guards custom / MCP tools
# and full-file reads. Head+tail are kept (the ends usually carry the signal);
# the middle is elided with a note. Caps are tool-aware (reads/shell get more
# room than a grep). Override the default via MANTIS_AGENT_MAX_TOOL_RESULT.
try:
    _DEFAULT_TOOL_RESULT_CAP = max(2000, int(os.environ.get("MANTIS_AGENT_MAX_TOOL_RESULT", "30000")))
except ValueError:
    _DEFAULT_TOOL_RESULT_CAP = 30000

_TOOL_RESULT_CAPS = {
    "read_file": 60_000,
    "bash": 40_000,
    "web_fetch": 40_000,
}


_ACCEPTED_PARAMS_CACHE: dict[int, frozenset[str] | None] = {}


def _accepted_params(fn: Any) -> frozenset[str] | None:
    """Parameter names ``fn`` accepts, or ``None`` if it takes ``**kwargs`` (=
    accept anything). Cached by function id."""
    import inspect  # noqa: PLC0415

    key = id(fn)
    if key not in _ACCEPTED_PARAMS_CACHE:
        try:
            params = inspect.signature(fn).parameters
        except (ValueError, TypeError):
            _ACCEPTED_PARAMS_CACHE[key] = None
        else:
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                _ACCEPTED_PARAMS_CACHE[key] = None
            else:
                _ACCEPTED_PARAMS_CACHE[key] = frozenset(params)
    return _ACCEPTED_PARAMS_CACHE[key]


def _filter_tool_input(fn: Any, input: dict[str, Any]) -> dict[str, Any]:
    """Drop keys ``fn`` won't accept so a model's hallucinated extra arg doesn't
    TypeError the call. Pass-through when ``fn`` takes ``**kwargs`` or ``input``
    is already clean."""
    accepted = _accepted_params(fn)
    if accepted is None or not input:
        return input
    if all(k in accepted for k in input):
        return input
    return {k: v for k, v in input.items() if k in accepted}


def _as_block_content(out: Any) -> list[Any] | None:
    """If a tool returned rich content (an ImageBlock / TextBlock, or a list of
    them — e.g. a multimodal ``read_file`` handing back an image), pass it
    through as the tool_result content instead of stringifying it. Otherwise
    return None so the caller falls back to the text path."""
    if isinstance(out, (ImageBlock, TextBlock)):
        return [out]
    if isinstance(out, list) and out and all(isinstance(b, (ImageBlock, TextBlock)) for b in out):
        return list(out)
    return None


def _truncate_tool_result(text: str, tool_name: str) -> str:
    cap = _TOOL_RESULT_CAPS.get(tool_name, _DEFAULT_TOOL_RESULT_CAP)
    if len(text) <= cap:
        return text
    head = cap * 2 // 3
    tail = cap - head
    dropped = len(text) - head - tail
    note = (
        f"\n\n… [{dropped:,} characters elided — {len(text):,} total. "
        f"Re-run with a narrower query, a line range, or a filter to see the "
        f"middle.] …\n\n"
    )
    return text[:head] + note + text[-tail:]


def _flatten_exception_group(eg: BaseExceptionGroup) -> list[BaseException]:
    """Walk a (possibly nested) ``BaseExceptionGroup`` and return the leaf
    exceptions. anyio 4.x can nest groups arbitrarily — a single-leaf
    group nested two deep should still unwrap cleanly."""
    out: list[BaseException] = []
    for sub in eg.exceptions:
        if isinstance(sub, BaseExceptionGroup):
            out.extend(_flatten_exception_group(sub))
        else:
            out.append(sub)
    return out


class StreamingToolExecutor:
    """Dispatch tool calls as they arrive on the stream.

    Lifecycle::

        async with StreamingToolExecutor(registry) as ex:
            # in the stream consumer, for every ToolUseBlock that finalizes:
            ex.add_tool_call(block)
            # ...after message_stop:
            results = await ex.wait_all()

    The context-manager exit awaits every still-in-flight task (so leaving
    the ``async with`` block is a safe join point even if you forget to
    call ``wait_all``)."""

    __slots__ = (
        "_registry",
        "_can_use_tool",
        "_max_concurrency",
        "_sem",
        "_serial_lock",
        "_calls",
        "_results",
        "_pending",
        "_idle_event",
        "_tg",
        "_scopes",
        "_aborted",
        "_signal_cancelled",
        "_cancellation_signal",
        "_watcher_scope",
        "_closed",
        # Completion-streaming channel — every ``_results[idx] = result``
        # assignment goes through ``_record_result`` which pushes
        # ``(idx, result)`` here. Consumers iterate via ``iter_completions``
        # (completion order) or ``wait_all`` (insertion order).
        "_completion_send",
        "_completion_recv",
        "_completion_closed",
        "_tracer",
        "_trace_parent",
    )

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_concurrency: int | None = None,
        can_use_tool: CanUseToolFn | None = None,
        cancellation_signal: "anyio.Event | None" = None,
        tracer: "Any | None" = None,
        trace_parent: "Any | None" = None,
    ) -> None:
        self._registry = registry
        self._can_use_tool = can_use_tool
        # Tracing — set by the agent loop when ``Agent.tracer`` is on. When
        # both are ``None`` the executor pays zero overhead. Each tool
        # dispatch opens a ``tool.call`` span nested under ``trace_parent``
        # (the per-turn span) — see ``_run_one`` for the wiring.
        self._tracer = tracer
        self._trace_parent = trace_parent
        self._max_concurrency = _resolve_max_concurrency(max_concurrency)
        # Parallel slots for concurrency-safe tools.
        self._sem = anyio.Semaphore(self._max_concurrency)
        # One-at-a-time lock for non-concurrency-safe tools.
        self._serial_lock = anyio.Lock()
        # Track calls in insertion order.
        self._calls: list[ToolUseBlock] = []
        self._results: list[ToolResultBlock | None] = []
        # Number of dispatched-but-not-yet-finished tasks. ``wait_all`` blocks
        # on ``_idle_event`` until this reaches zero. Reset whenever a new
        # call is added so callers can interleave dispatch and waiting.
        self._pending = 0
        self._idle_event = anyio.Event()
        self._idle_event.set()  # start "idle" — no pending work
        # Set in __aenter__.
        self._tg: anyio.abc.TaskGroup | None = None
        # Per-task cancel scopes, kept so sibling-abort can cancel every
        # in-flight task at once. Indexed in insertion order.
        self._scopes: list[anyio.CancelScope] = []
        self._aborted = False
        # Distinct from ``_aborted``: that flag covers
        # ``abort_siblings_on_error`` (one tool crashed, kill the rest).
        # ``_signal_cancelled`` covers external cancellation via the
        # cancellation_signal Event (``Agent.cancel()``, budget overrun,
        # any future abort path). They produce different
        # ToolResultBlock messages so callers can distinguish.
        self._signal_cancelled = False
        # Shared event the agent fires to cancel the whole run. We
        # observe it via a watcher task started in ``__aenter__``; when
        # it fires we mark ``_signal_cancelled`` and cancel every
        # per-task CancelScope so in-flight tools die fast.
        self._cancellation_signal = cancellation_signal
        # Scope wrapping the watcher task so ``__aexit__`` can wake it
        # up when the executor closes cleanly (signal never fired).
        self._watcher_scope: anyio.CancelScope | None = None
        self._closed = False
        # Completion-stream wiring. Unbounded buffer so result-recording
        # paths never block; the buffer holds tiny ``(int, ToolResultBlock)``
        # tuples. The send side is closed in ``__aexit__`` so consumers of
        # ``iter_completions`` terminate cleanly. We must allocate the
        # streams here (cheap) so callers can grab a receive handle BEFORE
        # entering the ``async with`` block — useful for spinning up a
        # consumer task in the same task group as the executor.
        send, recv = anyio.create_memory_object_stream[
            tuple[int, ToolResultBlock]
        ](max_buffer_size=math.inf)
        self._completion_send: anyio.abc.ObjectSendStream[
            tuple[int, ToolResultBlock]
        ] = send
        self._completion_recv: anyio.abc.ObjectReceiveStream[
            tuple[int, ToolResultBlock]
        ] = recv
        self._completion_closed = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "StreamingToolExecutor":
        # We do NOT enter the task group as ``async with`` here because that
        # would block on every task at exit. Instead we manage it manually so
        # ``add_tool_call`` can ``start_soon`` into it across the lifetime.
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        # Wire cancellation_signal handling. Two cases:
        #   1. Signal is already set (caller fired ``cancel()`` BEFORE
        #      entering this executor). Mark ourselves cancelled now so
        #      ``add_tool_call`` short-circuits every call — no watcher
        #      needed.
        #   2. Signal is not set. Spawn a watcher task in the executor's
        #      task group that awaits the signal. When it fires, we
        #      cancel every in-flight per-task scope and mark
        #      ``_signal_cancelled`` so future ``add_tool_call``s
        #      short-circuit too. The watcher lives in its own
        #      ``CancelScope`` so ``__aexit__`` can wake it on clean
        #      shutdown (signal never fired).
        if self._cancellation_signal is not None:
            if self._cancellation_signal.is_set():
                self._signal_cancelled = True
            else:
                self._watcher_scope = anyio.CancelScope()
                self._tg.start_soon(self._watch_cancellation_signal)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        # Closing the task group joins all in-flight tasks.
        assert self._tg is not None
        # Wake the watcher if it's still parked on the Event. Doing this
        # before joining the task group prevents the task group's
        # ``__aexit__`` from blocking forever on a watcher that never
        # observed the signal during this run.
        if self._watcher_scope is not None:
            self._watcher_scope.cancel()
        unwrap_exc: BaseException | None = None
        try:
            await self._tg.__aexit__(exc_type, exc, tb)
        except BaseExceptionGroup as eg:
            # anyio 4.x ALWAYS wraps body-raised exceptions in a
            # ``BaseExceptionGroup`` at task-group exit, even when no child
            # task raised. That makes plain exception flow (e.g. a
            # ``BudgetExceededError`` raised in the body of
            # ``async with StreamingToolExecutor(...)``) bubble up as a
            # group to our caller — which is brittle and not what callers
            # expect from a "dispatch tools and run them" helper. Unwrap
            # singleton groups so callers see the original exception.
            flat = _flatten_exception_group(eg)
            if len(flat) == 1:
                unwrap_exc = flat[0]
            else:
                # Multiple distinct exceptions — keep the group so the
                # caller can inspect every failure.
                raise
        finally:
            # Fill any leftover None slots — shouldn't happen if the TG joined
            # cleanly, but a stray cancellation could leave a hole. Route
            # through ``_record_result`` so any active ``iter_completions``
            # consumer sees these backfills too before the stream closes.
            fallback_reason = (
                "cancelled by signal"
                if self._signal_cancelled
                else "tool execution cancelled"
            )
            for i, r in enumerate(self._results):
                if r is None:
                    call = self._calls[i]
                    self._record_result(
                        i,
                        ToolResultBlock(
                            tool_use_id=call.id,
                            content=fallback_reason,
                            is_error=True,
                        ),
                    )
            # Close the completion channel so iter_completions / wait_one
            # consumers see EndOfStream and break their loops. Done AFTER
            # the backfill so they observe every result first.
            self._close_completion_stream()
            # Releases any wait_all() blocked on us.
            self._pending = 0
            if not self._idle_event.is_set():
                self._idle_event.set()
        if unwrap_exc is not None:
            raise unwrap_exc

    async def _watch_cancellation_signal(self) -> None:
        """Background task: wait for ``self._cancellation_signal`` to fire,
        then yank every in-flight tool task via its CancelScope.

        Lives in the executor's task group. On clean executor shutdown
        (signal never fired) ``__aexit__`` calls ``self._watcher_scope.cancel()``
        to wake us out of the indefinite ``await``. The CancelledError
        is caught here and swallowed — clean exit, no propagation.
        """
        assert self._cancellation_signal is not None
        assert self._watcher_scope is not None
        try:
            with self._watcher_scope:
                await self._cancellation_signal.wait()
                # Signal fired. Flip the flag so any subsequent
                # ``add_tool_call`` short-circuits, and cancel every
                # in-flight per-task scope so the running bodies bail.
                self._signal_cancelled = True
                # Snapshot the list — new scopes appended after the
                # snapshot are caught by the ``_signal_cancelled`` flag
                # check in ``add_tool_call``.
                for sc in list(self._scopes):
                    sc.cancel()
        except anyio.get_cancelled_exc_class():
            # Clean shutdown — executor exited before signal fired.
            return

    # ------------------------------------------------------------------
    # Internal: result recording (drives both wait_all + iter_completions)
    # ------------------------------------------------------------------

    def _record_result(self, idx: int, result: ToolResultBlock) -> None:
        """Write the result for ``idx`` and notify completion subscribers.

        Idempotent: only the FIRST call for a given index pushes to the
        completion channel. Subsequent calls (race-window backfills in
        ``__aexit__`` / ``wait_all``) are silently dropped. This is what
        lets us reuse the same recording path for fast-fail short circuits
        AND post-invoke completions AND exit-time cleanup without ever
        double-emitting a result for the same tool_use_id."""
        if self._results[idx] is not None:
            return
        self._results[idx] = result
        if self._completion_closed:
            return
        try:
            self._completion_send.send_nowait((idx, result))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            # Receive side already aborted (consumer crashed) — drop the
            # event. The result is still recorded so wait_all sees it.
            pass

    def _close_completion_stream(self) -> None:
        """Mark the completion channel closed and close the send side so
        ``iter_completions`` consumers see ``EndOfStream`` and terminate.

        Idempotent — safe to call multiple times. Called from ``__aexit__``
        as part of teardown."""
        if self._completion_closed:
            return
        self._completion_closed = True
        try:
            self._completion_send.close()
        except Exception:  # noqa: BLE001 — close must never raise out
            _LOG.debug("completion send-stream close raised", exc_info=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_tool_call(self, block: ToolUseBlock) -> None:
        """Register a freshly-finalized tool_use block and dispatch it.

        Non-blocking — returns as soon as the task is scheduled. Order of
        ``add_tool_call`` calls is the order of the returned results."""
        if self._closed:
            raise RuntimeError("add_tool_call after executor exit")
        if self._tg is None:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        idx = len(self._calls)
        self._calls.append(block)
        self._results.append(None)
        self._scopes.append(anyio.CancelScope())
        # Fast-fail paths — return BEFORE spawning a task. Two flavors,
        # each producing a distinct error message so callers can tell
        # what happened from the ToolResultBlock alone.
        if self._signal_cancelled:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content="cancelled by signal",
                    is_error=True,
                ),
            )
            return
        if self._aborted:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content="aborted by sibling tool error",
                    is_error=True,
                ),
            )
            return
        # Bump pending and clear the idle gate.
        self._pending += 1
        if self._idle_event.is_set():
            self._idle_event = anyio.Event()
        self._tg.start_soon(self._run_one, idx, block)

    async def wait_all(self) -> list[ToolResultBlock]:
        """Block until every dispatched tool finishes and return result blocks
        in the order ``add_tool_call`` was called.

        Safe to call from inside ``async with``. The context exit will also
        flush — but most callers will await this explicitly to get the list
        before leaving the block."""
        if self._tg is None:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        while self._pending > 0:
            await self._idle_event.wait()
        # Fill any leftover Nones (cancellation races).
        fallback_reason = (
            "cancelled by signal"
            if self._signal_cancelled
            else "tool execution cancelled"
        )
        out: list[ToolResultBlock] = []
        for i, r in enumerate(self._results):
            if r is None:
                call = self._calls[i]
                self._record_result(
                    i,
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content=fallback_reason,
                        is_error=True,
                    ),
                )
                r = self._results[i]
            out.append(r)  # type: ignore[arg-type]
        return out

    def iter_completions(
        self,
    ) -> AsyncIterator[tuple[int, ToolResultBlock]]:
        """Yield ``(idx, ToolResultBlock)`` pairs as tools complete.

        Completion order — **not** insertion order. The ``idx`` is the
        insertion index (``add_tool_call`` ordinal, 0-based) so callers
        can correlate with ``self.calls`` if they need original-order
        positioning.

        Terminates when the executor exits (the completion send-stream
        is closed in ``__aexit__``). Safe to consume from inside the
        ``async with`` block in parallel with ``add_tool_call`` calls —
        the consumer will block on ``receive()`` whenever the queue is
        empty and wake the instant the next tool finishes.

        At-most-once delivery. Only one consumer can iterate at a time
        (memory streams have a single receive side). For multiple
        consumers, fan out via a downstream broadcast — the executor
        does not duplicate."""
        if self._tg is None and not self._closed:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        return self._iter_completions()

    async def _iter_completions(
        self,
    ) -> AsyncIterator[tuple[int, ToolResultBlock]]:
        try:
            async for item in self._completion_recv:
                yield item
        except anyio.EndOfStream:
            return
        except anyio.ClosedResourceError:
            return

    async def wait_one(self) -> tuple[int, ToolResultBlock] | None:
        """Block until the next tool completion, return ``(idx, result)``.

        Returns ``None`` if the executor has closed and no further
        completions will arrive. Use ``iter_completions`` for a clean
        ``async for`` loop; ``wait_one`` is for callers that want to
        interleave manual control flow between completions."""
        if self._tg is None and not self._closed:
            raise RuntimeError("StreamingToolExecutor used outside async with")
        try:
            return await self._completion_recv.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            return None

    # ------------------------------------------------------------------
    # Introspection — read-only views for callers iterating completions
    # ------------------------------------------------------------------

    @property
    def calls(self) -> tuple[ToolUseBlock, ...]:
        """Snapshot of every ``ToolUseBlock`` registered so far, in
        insertion order. Lets ``iter_completions`` consumers map
        ``idx`` → originating call without poking at internals."""
        return tuple(self._calls)

    @property
    def pending(self) -> int:
        """How many dispatched tools are still in flight. Reaches 0
        when every tool has either completed or short-circuited."""
        return self._pending

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_one(self, idx: int, block: ToolUseBlock) -> None:
        """One tool's lifetime: lookup → permission → concurrency gate →
        invoke → record result. Never raises out (all errors become
        ``is_error=True`` result blocks).

        The body runs under its own ``CancelScope`` so a sibling that calls
        ``_maybe_abort`` can yank in-flight work via ``scope.cancel()`` —
        this is how ``abort_siblings_on_error`` kills subprocess-bearing
        bash tools the moment a peer fails."""
        scope = self._scopes[idx]
        # Open a tool.call span (no-op when no tracer). Attributes capture
        # the input KEYS only (never values) to avoid leaking secrets into
        # observability backends.
        tool_span = None
        if self._tracer is not None:
            try:
                tool_span = self._tracer.start_span(
                    "tool.call",
                    parent=self._trace_parent,
                    attributes={
                        "tool.name": block.name,
                        "tool.id": block.id,
                        "tool.input.keys": sorted(
                            list((block.input or {}).keys())
                        ),
                    },
                )
            except Exception:  # noqa: BLE001 — tracer must never crash the run
                _LOG.debug("tracer.start_span failed", exc_info=True)
                tool_span = None
        try:
            with scope:
                await self._run_one_inner(idx, block)
            if scope.cancelled_caught and self._results[idx] is None:
                self._record_result(idx, self._cancelled_result(block))
        finally:
            self._pending -= 1
            if self._pending <= 0:
                self._pending = 0
                if not self._idle_event.is_set():
                    self._idle_event.set()
            # Stamp result attributes + close the span.
            if tool_span is not None and self._tracer is not None:
                try:
                    result = self._results[idx]
                    if result is not None:
                        tool_span.set_attributes({
                            "tool.is_error": bool(result.is_error),
                            "tool.result.len": (
                                len(result.content)
                                if isinstance(result.content, (str, list)) else 0
                            ),
                        })
                    tool_span.end(
                        status="error" if (
                            result is not None and result.is_error
                        ) else "ok"
                    )
                    mirror = getattr(
                        self._tracer, "_mirror", None
                    ) or self._tracer
                    close_fn = getattr(mirror, "_close", None)
                    if callable(close_fn):
                        close_fn(tool_span)
                except Exception:  # noqa: BLE001
                    _LOG.debug("tracer span end failed", exc_info=True)

    async def _run_one_inner(self, idx: int, block: ToolUseBlock) -> None:
        tool = self._registry.get(block.name)
        if tool is None:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content=f"tool {block.name!r} not found",
                    is_error=True,
                ),
            )
            return

        # Permission gate first — cheap and may short-circuit.
        if self._can_use_tool is not None:
            try:
                allowed, reason = await self._can_use_tool(tool, block.input, {})
            except Exception as e:  # noqa: BLE001 — caller code must not crash us
                _LOG.exception("can_use_tool raised")
                self._record_result(
                    idx,
                    ToolResultBlock(
                        tool_use_id=block.id,
                        content=f"permission check error: {e!r}",
                        is_error=True,
                    ),
                )
                return
            if not allowed:
                self._record_result(
                    idx,
                    ToolResultBlock(
                        tool_use_id=block.id,
                        content=f"permission denied: {reason or 'no reason given'}",
                        is_error=True,
                    ),
                )
                return

        safe = _is_safe(tool, block.input)
        try:
            if safe:
                async with self._sem:
                    if self._aborted:
                        self._record_result(idx, self._cancelled_result(block))
                        return
                    await self._invoke(idx, tool, block)
            else:
                async with self._serial_lock:
                    if self._aborted:
                        self._record_result(idx, self._cancelled_result(block))
                        return
                    await self._invoke(idx, tool, block)
        except anyio.get_cancelled_exc_class():
            # Cooperatively cancelled by sibling abort. Surface as error.
            if self._results[idx] is None:
                self._record_result(idx, self._cancelled_result(block))
            raise  # propagate so the TG records the cancellation

    async def _invoke(self, idx: int, tool: Tool, block: ToolUseBlock) -> None:
        """Run the tool body with optional timeout. Captures every exception
        and stores a ``ToolResultBlock``. May trigger sibling abort."""
        timeout = getattr(tool, "timeout_s", None)
        # Drop arguments the tool doesn't accept — small/local models routinely
        # hallucinate extra kwargs, which would TypeError the call and burn a turn.
        # Tools that take **kwargs (e.g. explicit-schema tools) are passed as-is.
        call_input = _filter_tool_input(tool.fn, block.input)
        try:
            if timeout is not None:
                with anyio.fail_after(timeout):
                    out = await tool.fn(**call_input)
            else:
                out = await tool.fn(**call_input)
        except TimeoutError:
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content=f"tool {tool.name!r} timed out after {timeout}s",
                    is_error=True,
                ),
            )
            self._maybe_abort(tool)
            return
        except anyio.get_cancelled_exc_class():
            # Bubbled up — let _run_one handle it.
            raise
        except Exception as e:  # noqa: BLE001 — user code must not crash us
            err = ToolExecutionError(tool.name, block.id, e)
            self._record_result(
                idx,
                ToolResultBlock(
                    tool_use_id=block.id,
                    content=str(err),
                    is_error=True,
                ),
            )
            self._maybe_abort(tool)
            return

        rich = _as_block_content(out)
        content: Any = rich if rich is not None else _truncate_tool_result(_stringify(out), tool.name)
        self._record_result(
            idx,
            ToolResultBlock(tool_use_id=block.id, content=content),
        )

    def _maybe_abort(self, tool: Tool) -> None:
        """Trigger sibling-abort if this tool requested it.

        Cancels every per-task ``CancelScope`` so in-flight peers die fast
        (this is the whole point — kill subprocesses, abort file writes).
        Already-recorded results are kept; the rest become cancellation
        error blocks. Subsequent ``add_tool_call`` invocations short-circuit
        via the ``_aborted`` flag."""
        if not getattr(tool, "abort_siblings_on_error", False):
            return
        if self._aborted:
            return
        self._aborted = True
        # Cancel every in-flight task; their scope's cancelled_caught path
        # will write a cancelled result block.
        for sc in self._scopes:
            sc.cancel()

    def _cancelled_result(self, block: ToolUseBlock) -> ToolResultBlock:
        """Build the cancellation-error result block for ``block``.

        The reason string depends on *why* the cancel scope fired —
        signal-driven cancellation produces ``"cancelled by signal"``
        so a user-facing UI can distinguish abort-on-sibling-error
        (recover-and-retry) from agent-cancel (user wants out).
        """
        reason = (
            "cancelled by signal"
            if self._signal_cancelled
            else "aborted by sibling tool error"
        )
        return ToolResultBlock(
            tool_use_id=block.id,
            content=reason,
            is_error=True,
        )
