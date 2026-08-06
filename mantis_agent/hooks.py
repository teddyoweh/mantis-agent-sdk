"""Hook event system — adopts upstream Claude Code's 28-event taxonomy.

Hooks let integrators observe and (selectively) veto every interesting thing
the agent does. The vocabulary is intentionally identical to upstream so
ecosystem tooling — dashboards, audit middleware, replay recorders — can
plug into either runtime without translation.

Design
------
* ``Hooks`` is a flat dataclass with one optional async callable per event.
  This shape is plain to read, plays nicely with ``replace()``, and avoids
  msgspec's awkwardness with ``Callable`` fields.
* ``HookDispatcher`` is the single funnel the agent loop calls into. It
  routes by event name and **swallows hook exceptions** — a misbehaving
  user hook must never crash the agent. The exception is logged and the
  dispatch returns a default ``HookResult()`` (no veto, no mutation).
  The one nuance: for the veto-carrying events in ``BLOCKING_EVENTS`` a
  swallowed exception is a *fail-open* — the guard that was supposed to
  deny simply didn't answer — so ``HookDispatcher(fail_closed=True)`` (or
  ``MANTIS_HOOKS_FAIL_CLOSED=1``) turns a raising hook into a denial.
* ``HookContext`` carries everything a hook might want: the event name, the
  tool involved (for tool-use events), input/output payloads, a snapshot
  of the message list, the agent id, plus an ``arbitrary`` extras dict for
  future fields we haven't predicted.
* ``HookResult.block`` is the veto signal. On ``PreToolUse``, the agent
  loop turns a blocked call into a ``ToolResultBlock(is_error=True)`` so
  the model still sees a tool result and can recover gracefully.
* ``HookResult.mutated_input`` lets a ``PreToolUse`` hook rewrite the input
  before the tool runs (e.g. PII scrubbing, path sandboxing, argument
  injection). When ``None``, the original input is used unchanged.

The agent loop pattern looks like::

    ctx = HookContext(event="PreToolUse", tool=t, input=call.input, ...)
    res = await dispatcher.dispatch("PreToolUse", ctx)
    if res.block:
        return ToolResultBlock(..., is_error=True, content="blocked by hook")
    payload = res.mutated_input if res.mutated_input is not None else call.input
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import msgspec

from .tools import Tool
from .types import Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event vocabulary
# ---------------------------------------------------------------------------

# Mirrors upstream Claude Code 1:1. Strings (not an Enum) so integrators can
# extend with their own custom events without subclassing.
HOOK_EVENTS: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "PermissionRequest",
    "PermissionDenied",
    "Setup",
    "TaskCreated",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "FileChanged",
    "CwdChanged",
    "InstructionsLoaded",
    "WorktreeCreate",
    "WorktreeRemove",
    "TeammateIdle",
)


# Of the full upstream vocabulary above, these are the events the mantis runtime
# actually *fires* today. The rest are declared for upstream Claude Code parity
# (so ecosystem tooling can register handlers by the same names) and for
# custom/out-of-band dispatch, but the built-in runtime does not emit them yet —
# a hook registered for one of them will simply never be called. Keeping this set
# explicit makes the contract honest: we don't silently advertise events that
# never fire. Call ``HookDispatcher.is_dispatched(event)`` to check at runtime.
#
# Wired dispatch points, by module:
#
#   mantis_agent/agent.py
#     PreToolUse / PostToolUse / PostToolUseFailure — around each tool execution
#     UserPromptSubmit — before a user turn is sent
#     PreCompact — before context compaction
#     Stop — when a run reaches a stopping point
#     PermissionDenied — when a tool call is refused by the permission layer
#
#   mantis_agent/compact.py
#     PostCompact — after a compaction that actually replaced turns, carrying
#       the NEW message list (``SimpleCompactor.compact`` and
#       ``run_manual_compaction``)
#
#   mantis_agent/session.py
#     SessionStart — ``Session.create`` (reason "create"), ``Session.load`` /
#       ``resume_session`` (reason "resume"), ``Session.fork`` (reason "fork")
#     SessionEnd — ``Session.close`` and ``async with`` exit, including the
#       error and cancellation paths
#
# Membership means "some built-in code path emits this", the same way the
# agent.py events only fire when an ``Agent`` is actually run. The compaction
# and session events are emitted by objects the integrator constructs, so they
# reach hooks once the ``dispatcher=`` argument is threaded through (the SDK
# does this for you; see the guides). An event whose only dispatch site is a
# code path nobody can reach does NOT belong in this set.
DISPATCHED_EVENTS: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "UserPromptSubmit",
        "PreCompact",
        "PostCompact",
        "SessionStart",
        "SessionEnd",
        "Stop",
        "PermissionDenied",
    }
)

# Declared for parity/custom dispatch but not emitted by the built-in loop.
RESERVED_EVENTS: frozenset[str] = frozenset(HOOK_EVENTS) - DISPATCHED_EVENTS


# Events where a hook's *veto* is the whole point — the caller asked permission
# before doing something consequential and treats ``block=False`` as consent.
#
# For these, swallowing an exception is not "graceful degradation", it is a
# fail-open: a ``PreToolUse`` guard that crashes on an input it never
# anticipated silently grants the very tool call it exists to deny. The safe
# reading of "the guard did not answer" is *no*, not *yes*. Everything not in
# this set is observability (``PostToolUse``, ``Notification``, ``SessionEnd``,
# ...) where the caller ignores ``block`` anyway and swallow-and-continue is
# both correct and required — a broken dashboard hook must never stop the agent.
#
# Membership rationale, event by event:
#   PreToolUse         — the sandbox/permission veto on every tool call.
#   UserPromptSubmit   — content gate on what reaches the model.
#   PermissionRequest  — an explicit "may I?"; silence must not mean yes.
#   SubagentStop/Stop  — a veto here keeps the run going; a crash that reads as
#                        "don't continue" is the conservative failure.
#   PreCompact         — compaction is lossy and irreversible; skip it on doubt.
#   InstructionsLoaded — gate on instruction text entering the system prompt.
BLOCKING_EVENTS: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "UserPromptSubmit",
        "PermissionRequest",
        "SubagentStop",
        "Stop",
        "PreCompact",
        "InstructionsLoaded",
    }
)

# Escape hatch for operators who want the safe behaviour before it becomes the
# default. Read from the environment (not settings) so it can be flipped for a
# single process without touching config, and so this module stays dependency
# free. It is an *escalation* switch only: setting it forces fail-closed on,
# but leaving it unset/false never downgrades an explicit ``fail_closed=True``.
_FAIL_CLOSED_ENV = "MANTIS_HOOKS_FAIL_CLOSED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_forces_fail_closed() -> bool:
    """Is ``MANTIS_HOOKS_FAIL_CLOSED`` set to a truthy value right now?

    Read at dispatch time rather than cached at import/construction so tests
    (and long-lived processes toggling the var) see changes immediately; the
    cost is one ``os.environ`` lookup on the already-exceptional error path.
    """

    return os.environ.get(_FAIL_CLOSED_ENV, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Hook context + result
# ---------------------------------------------------------------------------


class HookContext(msgspec.Struct, omit_defaults=True):
    """Payload delivered to a hook.

    Most fields are optional because each event populates a different subset.
    For ``PreToolUse``/``PostToolUse`` the ``tool`` and ``input`` are set;
    ``PostToolUse`` additionally sets ``output``. Lifecycle events
    (``SessionStart``, ``Stop``, ...) carry ``messages_snapshot``.

    ``arbitrary`` is a forward-compat extras bag — adapters can stash
    provider-specific data here without us shipping new fields.
    """

    event: str
    tool: Tool | None = None
    input: dict[str, Any] | None = None
    output: Any = None
    messages_snapshot: list[Message] | None = None
    agent_id: str | None = None
    arbitrary: dict[str, Any] = msgspec.field(default_factory=dict)


class HookResult(msgspec.Struct, omit_defaults=True):
    """Decision a hook returns.

    ``block``: when True, the hooked operation is cancelled. The agent loop
        is responsible for turning that into a graceful surface (e.g. a
        ``ToolResultBlock`` with ``is_error=True``).
    ``mutated_input``: only meaningful for ``PreToolUse``. When set, the
        agent loop uses this dict as the tool input in place of the original.
    ``note``: optional free-form reason for observability/logging.
    """

    block: bool = False
    mutated_input: dict[str, Any] | None = None
    note: str | None = None


# A hook is a callable taking the context and returning a result. Async is the
# documented shape (and what this alias describes), but a plain ``def`` that
# returns the result directly works too — ``dispatch`` awaits only awaitable
# returns. Returning ``None`` is allowed and is equivalent to ``HookResult()``.
HookFn = Callable[[HookContext], Awaitable["HookResult | None"]]


@dataclass(frozen=True)
class HookMatcher:
    """A hook scoped to a subset of tools (Claude-SDK ``HookMatcher`` parity).

    ``matcher`` is an fnmatch pattern tested against the tool name for tool
    events (``PreToolUse``/``PostToolUse``/...): ``"Bash"`` runs only for bash,
    ``"*"`` / ``None`` for every tool. On non-tool events the matcher is ignored
    and the hook always runs.
    """

    hook: HookFn
    matcher: str | None = None


# What a ``Hooks`` field may hold: a bare callable, a HookMatcher, or a list of
# either — so multiple hooks can fire per event, each optionally tool-scoped.
HookSpec = "HookFn | HookMatcher | list[HookFn | HookMatcher]"


def _normalize_hooks(value: Any) -> list[tuple[str | None, HookFn]]:
    """Flatten a hook field value to ``[(matcher, fn), ...]``."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[tuple[str | None, HookFn]] = []
    for it in items:
        if isinstance(it, HookMatcher):
            out.append((it.matcher, it.hook))
        elif hasattr(it, "hooks") and getattr(it, "hooks", None):
            # SDK-shaped HookMatcher: {matcher, hooks: [fn, ...]}.
            pat = getattr(it, "matcher", None)
            out.extend((pat, fn) for fn in it.hooks if callable(fn))
        elif callable(it):
            out.append((None, it))
    return out


def _matcher_hits(pattern: str | None, ctx: HookContext) -> bool:
    """Does ``pattern`` apply to this context? Non-tool events always match;
    tool events match when the tool name fnmatches the pattern."""
    if pattern is None:
        return True
    name = getattr(ctx.tool, "name", None)
    if name is None:
        return True  # not a tool event → matcher irrelevant
    import fnmatch  # noqa: PLC0415

    return fnmatch.fnmatchcase(name, pattern)


# ---------------------------------------------------------------------------
# Hooks registry
# ---------------------------------------------------------------------------


@dataclass
class Hooks:
    """Per-event hook registry.

    One field per event in ``HOOK_EVENTS`` (snake_cased). All optional, all
    default ``None``. Build like::

        Hooks(pre_tool_use=my_fn, post_tool_use=log_fn)

    We use a dataclass with slots rather than ``msgspec.Struct`` because
    msgspec doesn't model ``Callable`` cleanly and we never serialize
    ``Hooks`` over the wire — it's a runtime-only container.
    """

    pre_tool_use: "HookSpec | None" = None
    post_tool_use: "HookSpec | None" = None
    post_tool_use_failure: "HookSpec | None" = None
    notification: "HookSpec | None" = None
    user_prompt_submit: "HookSpec | None" = None
    session_start: "HookSpec | None" = None
    session_end: "HookSpec | None" = None
    stop: "HookSpec | None" = None
    stop_failure: "HookSpec | None" = None
    subagent_start: "HookSpec | None" = None
    subagent_stop: "HookSpec | None" = None
    pre_compact: "HookSpec | None" = None
    post_compact: "HookSpec | None" = None
    permission_request: "HookSpec | None" = None
    permission_denied: "HookSpec | None" = None
    setup: "HookSpec | None" = None
    task_created: "HookSpec | None" = None
    task_completed: "HookSpec | None" = None
    elicitation: "HookSpec | None" = None
    elicitation_result: "HookSpec | None" = None
    config_change: "HookSpec | None" = None
    file_changed: "HookSpec | None" = None
    cwd_changed: "HookSpec | None" = None
    instructions_loaded: "HookSpec | None" = None
    worktree_create: "HookSpec | None" = None
    worktree_remove: "HookSpec | None" = None
    teammate_idle: "HookSpec | None" = None


# CamelCase event name → dataclass field name. Built once at import time so
# dispatch is O(1) and never re-parses strings.
def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


_EVENT_TO_FIELD: dict[str, str] = {ev: _camel_to_snake(ev) for ev in HOOK_EVENTS}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass
class HookDispatcher:
    """Single funnel for hook delivery.

    The agent loop calls ``await dispatcher.dispatch("PreToolUse", ctx)``
    everywhere it needs to give integrators a say. The dispatcher:

    1. Looks up the hook field by event name (no hook → return default).
    2. Awaits the hook with the context.
    3. Catches *any* exception and logs it — user hook bugs do not
       propagate into the agent loop.
    4. Normalizes a ``None`` return to ``HookResult()``.

    ``fail_closed`` decides what step 3 means for the veto-carrying events in
    :data:`BLOCKING_EVENTS`. When True, a hook that raises *denies* the
    operation instead of silently permitting it (see the ``BLOCKING_EVENTS``
    commentary for why that is the safe reading). It defaults to False so this
    release changes no existing behaviour — the open path logs a WARNING
    announcing the coming flip. Setting ``MANTIS_HOOKS_FAIL_CLOSED=1`` forces
    fail-closed on regardless of the constructor argument.
    """

    hooks: Hooks
    fail_closed: bool = False

    async def dispatch(self, event: str, ctx: HookContext) -> HookResult:
        """Deliver ``ctx`` to the hook registered for ``event``, if any.

        Returns the hook's ``HookResult`` (or the default when no hook is
        registered or the hook raised).
        """

        field_name = _EVENT_TO_FIELD.get(event)
        if field_name is None:
            # Unknown event name — treat as "no hook configured". We don't
            # raise because the agent loop may pass custom event names.
            return HookResult()

        registered = _normalize_hooks(getattr(self.hooks, field_name, None))
        if not registered:
            return HookResult()

        # Run every matching hook in order. Mutations chain (each hook sees the
        # prior one's mutated input); the FIRST block short-circuits. A hook that
        # raises or matches nothing is skipped — never crashes the loop.
        mutated: dict[str, Any] | None = None
        notes: list[str] = []
        for pattern, fn in registered:
            if not _matcher_hits(pattern, ctx):
                continue
            try:
                # A hook may be a coroutine function OR a plain ``def`` that
                # returns the HookResult directly — same tolerance as
                # ``JobManager._fire_on_event``. Awaiting a non-awaitable
                # raises TypeError, and under fail_closed that TypeError would
                # be read as "the guard crashed" and DENY: a sync no-op hook
                # would silently start blocking every tool call. Only await
                # what is actually awaitable.
                res = fn(ctx)
                if inspect.isawaitable(res):
                    res = await res
            except Exception as exc:  # noqa: BLE001 — user code; never crash the loop
                # Observability events: unchanged — log and carry on.
                if event not in BLOCKING_EVENTS:
                    logger.exception("hook %r raised; treating as no-op", event)
                    continue
                # Veto events: the hook was asked for permission and did not
                # answer. Deny (fail closed) or, for now, permit-with-a-warning.
                name = getattr(fn, "__qualname__", None) or repr(fn)
                if self.fail_closed or _env_forces_fail_closed():
                    logger.exception(
                        "blocking hook %s raised on %r; failing closed (denying)",
                        name,
                        event,
                    )
                    return HookResult(
                        block=True,
                        mutated_input=mutated,
                        note=(
                            f"hook {event} failed: {type(exc).__name__}; failing closed"
                        ),
                    )
                logger.exception("hook %r raised; treating as no-op", event)
                logger.warning(
                    "blocking hook %s raised on %r and was IGNORED — the %s call "
                    "proceeds unguarded (fail-open). A future version will fail "
                    "closed and deny instead; set %s=1 (or HookDispatcher("
                    "fail_closed=True)) to opt in now.",
                    name,
                    event,
                    event,
                    _FAIL_CLOSED_ENV,
                )
                continue
            if res is None:
                continue
            if not isinstance(res, HookResult):
                # A hook that returns something else — a dict, a bool, a bare
                # string — is a misbehaving hook, not a veto. Before sync hooks
                # were supported, ``await <dict>`` raised TypeError *inside* the
                # try above and was swallowed here; now the value arrives intact
                # and would reach ``res.mutated_input`` and raise AttributeError
                # straight out of dispatch. That would violate this module's
                # central promise — a broken user hook must never crash the agent
                # loop — and it would do so on observability events too.
                logger.warning(
                    "hook %r returned %s, expected HookResult or None; ignoring",
                    event,
                    type(res).__name__,
                )
                continue
            if res.mutated_input is not None:
                mutated = res.mutated_input
                ctx = msgspec.structs.replace(ctx, input=mutated)  # thread onward
            if res.note:
                notes.append(res.note)
            if res.block:
                return HookResult(block=True, mutated_input=mutated, note=res.note)
        # Propagate accumulated notes from non-blocking hooks too — a
        # UserPromptSubmit hook uses this to inject additional context.
        combined = "\n".join(notes) if notes else None
        return HookResult(mutated_input=mutated, note=combined)

    async def dispatch_run_end(
        self,
        messages: list[Message] | None = None,
        *,
        error: BaseException | None = None,
        agent_id: str | None = None,
        arbitrary: dict[str, Any] | None = None,
    ) -> HookResult:
        """Fire the terminal event for a run: ``Stop``, or ``StopFailure``
        when ``error`` is set.

        The two events are one decision — "the run is over, and here is how it
        ended" — so they get one call site rather than two that can drift. A
        caller that already knows it succeeded can keep calling
        ``dispatch("Stop", ...)`` directly; this helper exists so the *failure*
        path is a single line at the point where the exception is known, which
        is the only reason ``StopFailure`` has historically gone unfired.

        ``error`` is recorded in ``ctx.arbitrary`` as ``error`` (the ``str()``
        of the exception) and ``error_type`` (its class name) rather than as
        the live exception object: hook contexts are serialized by some
        integrations, and a traceback-bearing exception is neither portable nor
        safe to hand to arbitrary consumers. Explicit ``arbitrary`` keys win
        over the derived ones so a caller can supply a redacted message.

        ``StopFailure`` is deliberately *not* in :data:`BLOCKING_EVENTS`: the
        run has already failed, so there is nothing left for a hook to veto,
        and a raising observability hook must not turn one failure into two.
        """

        event = "Stop" if error is None else "StopFailure"
        if not self.has(event):
            # Same fast path the loop uses elsewhere — no hook, no context
            # assembly, no allocation beyond the empty result.
            return HookResult()
        extras: dict[str, Any] = dict(arbitrary) if arbitrary else {}
        if error is not None:
            extras.setdefault("error", str(error))
            extras.setdefault("error_type", type(error).__name__)
        return await self.dispatch(
            event,
            HookContext(
                event=event,
                messages_snapshot=messages,
                agent_id=agent_id,
                arbitrary=extras,
            ),
        )

    def has(self, event: str) -> bool:
        """Cheap check used by the loop to skip context assembly when nothing
        is registered. Not strictly required, but saves allocations on the
        common path where most events have no hook."""

        field_name = _EVENT_TO_FIELD.get(event)
        if field_name is None:
            return False
        return bool(_normalize_hooks(getattr(self.hooks, field_name, None)))

    @staticmethod
    def is_dispatched(event: str) -> bool:
        """Whether the built-in agent loop actually fires ``event``.

        A hook registered for an event not in :data:`DISPATCHED_EVENTS` is
        accepted (for upstream parity / custom dispatch) but the built-in
        runtime will never call it. Integrators can use this to warn early.
        """

        return event in DISPATCHED_EVENTS


__all__ = [
    "HOOK_EVENTS",
    "BLOCKING_EVENTS",
    "DISPATCHED_EVENTS",
    "RESERVED_EVENTS",
    "HookContext",
    "HookMatcher",
    "HookDispatcher",
    "HookFn",
    "HookResult",
    "Hooks",
]
