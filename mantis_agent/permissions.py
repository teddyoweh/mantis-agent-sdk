"""Permission system — mirrors upstream Claude Code's model.

Permissions gate every tool call. The model is intentionally identical to
upstream so integrators can lift their existing config files across:

* **Modes**: ``default | auto | bypass``.
* **Rules**: separate ``allow``/``deny``/``ask`` lists matched against
  ``(tool_name, input)``. ``deny`` always wins.
* **Callback**: ``canUseTool(tool, input, ctx) -> Allow | Deny | Ask`` is
  the user-extensible fallback when no rule matches.

Decision precedence on each call::

    bypass mode                       -> Allow
    auto mode + tool.is_read_only     -> Allow
    any deny rule matches             -> Deny
    any allow rule matches            -> Allow
    any ask rule matches              -> Ask
    can_use_tool callback set         -> delegate
    no callback, mode=default         -> Allow
    no callback, mode=auto, ro=False  -> Ask
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import msgspec

from .tools import Tool

PermissionMode = Literal["default", "auto", "bypass"]


# ---------------------------------------------------------------------------
# Decisions — a tagged union over Allow / Deny / Ask
# ---------------------------------------------------------------------------


class Allow(msgspec.Struct, frozen=True, tag="allow", tag_field="decision"):
    """Permit the call to proceed.

    ``updated_input`` (optional) rewrites the tool input before dispatch.
    Matches Claude SDK's ``PermissionResultAllow(updated_input=...)``
    semantics — a permission callback can sanitize / patch arguments
    on the way through (e.g. PII redaction, path sandboxing, default
    injection) without the model knowing.
    """

    updated_input: dict | None = None


class Deny(msgspec.Struct, frozen=True, tag="deny", tag_field="decision"):
    """Block the call. ``reason`` flows into the ToolResult error content."""

    reason: str = "denied"


class Ask(msgspec.Struct, frozen=True, tag="ask", tag_field="decision"):
    """Defer to a human (or higher-level UI). The agent loop is responsible
    for actually surfacing the prompt — this struct just declares that the
    permission layer wants a decision it didn't make itself.

    ``prompt`` is the human-readable string to display.
    """

    prompt: str = ""


PermissionDecision = Allow | Deny | Ask


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class PermissionRule(msgspec.Struct, frozen=True, omit_defaults=True):
    """A single rule.

    ``pattern`` is matched against a stable string projection of the tool
    input — by default ``json.dumps(input, sort_keys=True)``. We also expose
    ``tool_name`` to scope the rule to a particular tool; ``None`` matches
    any tool. ``is_regex`` flips the matcher from fnmatch (the friendly
    default) to full regex (the surgical option).

    The shape is identical to upstream's settings.json schema so users can
    paste rules across without translation.
    """

    pattern: str
    action: Literal["allow", "deny", "ask"]
    tool_name: str | None = None
    is_regex: bool = False


@dataclass(slots=True)
class PermissionRuleSet:
    """Grouped rules, evaluated in (deny, allow, ask) precedence.

    Held as separate lists rather than a single list so the precedence is
    obvious from the data layout. The match loop walks them in that order.
    """

    allow: list[PermissionRule] = field(default_factory=list)
    deny: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)

    def match(self, tool_name: str, input: dict[str, Any]) -> PermissionRule | None:
        """Return the first matching rule across deny/allow/ask in that
        precedence order, or ``None`` if nothing matches."""

        projection = _project_input(input)
        for rule in self.deny:
            if _rule_matches(rule, tool_name, projection):
                return rule
        for rule in self.allow:
            if _rule_matches(rule, tool_name, projection):
                return rule
        for rule in self.ask:
            if _rule_matches(rule, tool_name, projection):
                return rule
        return None


def _project_input(input: dict[str, Any]) -> str:
    """Stable string used as the match target.

    Sorting keys is important: a user's rule like ``*secret*`` should fire
    regardless of which order the model happens to serialize fields.
    Failures fall back to ``repr()`` to keep matching best-effort even on
    weird inputs.
    """

    try:
        import json

        return json.dumps(input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(input)


def _rule_matches(rule: PermissionRule, tool_name: str, projection: str) -> bool:
    """Test whether a single rule fires."""

    if rule.tool_name is not None and rule.tool_name != tool_name:
        return False
    if rule.is_regex:
        try:
            return re.search(rule.pattern, projection) is not None
        except re.error:
            return False
    # Default: shell-style glob, matched against the JSON projection.
    return fnmatch.fnmatchcase(projection, rule.pattern)


# ---------------------------------------------------------------------------
# Callback + context
# ---------------------------------------------------------------------------


# canUseTool — user-supplied async function. The third arg is a free-form
# context dict the agent loop fills (session id, agent id, message count,
# etc.). Identical to upstream's surface.
CanUseToolFn = Callable[
    [Tool, dict[str, Any], dict[str, Any]],
    Awaitable[PermissionDecision],
]


@dataclass(slots=True)
class PermissionContext:
    """Configuration the agent threads through every permission check.

    ``mode`` is the global posture; ``rules`` is the declarative layer;
    ``can_use_tool`` is the imperative fallback. All three compose via
    :func:`check_permission`.
    """

    mode: PermissionMode = "default"
    rules: PermissionRuleSet | None = None
    can_use_tool: CanUseToolFn | None = None
    # Free-form metadata passed to ``can_use_tool`` (session id, etc.).
    extra: dict[str, Any] = field(default_factory=dict)
    # AbortSignal-like event shared with the agent loop. Fires when the
    # agent is cancelled, budgets are exhausted, or any abort path
    # triggers. Surfaces on ``ToolPermissionContext.signal`` for the
    # user's can_use_tool callback. None means "agent didn't wire one"
    # — the callback will still receive a fresh, never-fired event so
    # it can read .signal.is_set() safely.
    signal: Any = None


# ---------------------------------------------------------------------------
# Tool read-only hint
# ---------------------------------------------------------------------------
#
# ``auto`` mode wants to auto-allow read-only tools. ``Tool`` currently
# doesn't carry an ``is_read_only`` field; we honour it if present (so user
# code that adds it via ``@tool(..., parallel_safe=True)`` plus a separate
# attribute works) and otherwise fall back to a conservative naming
# heuristic.

_READ_ONLY_PREFIXES = ("read_", "get_", "list_", "search_", "find_", "describe_", "head_")


def _is_read_only(tool: Tool) -> bool:
    flag = getattr(tool, "is_read_only", None)
    if isinstance(flag, bool):
        return flag
    name = tool.name.lower()
    return any(name.startswith(p) for p in _READ_ONLY_PREFIXES)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


async def check_permission(
    tool: Tool,
    input: dict[str, Any],
    ctx: PermissionContext,
) -> PermissionDecision:
    """Apply the full decision pipeline. Returns one of Allow/Deny/Ask.

    See module docstring for precedence rules.
    """

    if ctx.mode == "bypass":
        return Allow()

    if ctx.mode == "auto" and _is_read_only(tool):
        return Allow()

    if ctx.rules is not None:
        hit = ctx.rules.match(tool.name, input)
        if hit is not None:
            if hit.action == "deny":
                return Deny(reason=f"denied by rule {hit.pattern!r}")
            if hit.action == "allow":
                return Allow()
            # ask
            return Ask(prompt=f"rule {hit.pattern!r} requires confirmation")

    if ctx.can_use_tool is not None:
        # Build a Claude-SDK-shaped ToolPermissionContext for the
        # callback. The user's can_use_tool wants ``.signal``,
        # ``.session_id``, and ``.suggestions`` — wrap our internal
        # PermissionContext.extra (a free-form dict) accordingly.
        from .claude_compat import ToolPermissionContext  # local to avoid cycle

        tpc = ToolPermissionContext(
            session_id=str(ctx.extra.get("session_id", "")),
            signal=ctx.signal,  # __post_init__ mints a fresh Event if None
            suggestions=list(ctx.extra.get("suggestions", [])),
        )
        return await ctx.can_use_tool(tool, input, tpc)

    # No callback — use the mode default.
    if ctx.mode == "auto":
        # auto with a non-read-only tool and no callback: prompt the user.
        return Ask(prompt=f"tool {tool.name!r} not read-only; confirm?")

    # default mode: permissive when nothing else has spoken.
    return Allow()


__all__ = [
    "Allow",
    "Ask",
    "CanUseToolFn",
    "Deny",
    "PermissionContext",
    "PermissionDecision",
    "PermissionMode",
    "PermissionRule",
    "PermissionRuleSet",
    "check_permission",
]
