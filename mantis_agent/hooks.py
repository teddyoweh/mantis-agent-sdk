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

import logging
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


# Of the full upstream vocabulary above, these are the events the mantis agent
# loop actually *fires* today. The rest are declared for upstream Claude Code
# parity (so ecosystem tooling can register handlers by the same names) and for
# custom/out-of-band dispatch, but the built-in runtime does not emit them yet —
# a hook registered for one of them will simply never be called. Keeping this set
# explicit makes the contract honest: we don't silently advertise events that
# never fire. Call ``HookDispatcher.is_dispatched(event)`` to check at runtime.
#
# Wired dispatch points (see mantis_agent/agent.py):
#   PreToolUse / PostToolUse / PostToolUseFailure — around each tool execution
#   UserPromptSubmit — before a user turn is sent
#   PreCompact — before context compaction
#   Stop — when a run reaches a stopping point
#   PermissionDenied — when a tool call is refused by the permission layer
DISPATCHED_EVENTS: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "UserPromptSubmit",
        "PreCompact",
        "Stop",
        "PermissionDenied",
    }
)

# Declared for parity/custom dispatch but not emitted by the built-in loop.
RESERVED_EVENTS: frozenset[str] = frozenset(HOOK_EVENTS) - DISPATCHED_EVENTS


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


# A hook is an async callable taking the context and returning a result.
# Returning ``None`` is allowed and is equivalent to ``HookResult()``.
HookFn = Callable[[HookContext], Awaitable["HookResult | None"]]


@dataclass(slots=True, frozen=True)
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


@dataclass(slots=True)
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


@dataclass(slots=True)
class HookDispatcher:
    """Single funnel for hook delivery.

    The agent loop calls ``await dispatcher.dispatch("PreToolUse", ctx)``
    everywhere it needs to give integrators a say. The dispatcher:

    1. Looks up the hook field by event name (no hook → return default).
    2. Awaits the hook with the context.
    3. Catches *any* exception and logs it — user hook bugs do not
       propagate into the agent loop.
    4. Normalizes a ``None`` return to ``HookResult()``.
    """

    hooks: Hooks

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
                res = await fn(ctx)
            except Exception:  # noqa: BLE001 — user code; never crash the loop
                logger.exception("hook %r raised; treating as no-op", event)
                continue
            if res is None:
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
    "DISPATCHED_EVENTS",
    "RESERVED_EVENTS",
    "HookContext",
    "HookMatcher",
    "HookDispatcher",
    "HookFn",
    "HookResult",
    "Hooks",
]
