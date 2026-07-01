"""Permission system — mirrors upstream Claude Code's model.

Permissions gate every tool call. The model is intentionally identical to
upstream so integrators can lift their existing config files across:

* **Modes**: ``default | acceptEdits | auto | bypass``.
* **Rules**: separate ``allow``/``deny``/``ask`` lists matched against
  ``(tool_name, input)``. ``deny`` always wins.
* **Callback**: ``canUseTool(tool, input, ctx) -> Allow | Deny | Ask`` is
  the user-extensible policy fallback when no rule matches.
* **Asker**: ``asker(tool, input, prompt) -> allow_once|allow_session|deny``
  resolves any ``Ask`` interactively. With no asker wired (library / headless),
  ``Ask`` falls back to a non-blocking default so nothing hangs.

Decision precedence on each call (``check_permission``)::

    bypass mode                       -> Allow
    (tool, input) in session_allows   -> Allow
    any deny rule matches             -> Deny
    any allow rule matches            -> Allow
    any ask rule matches              -> Ask*
    acceptEdits mode + edit tool      -> Allow
    can_use_tool callback set         -> delegate (may return Ask*)
    no callback + read-only tool      -> Allow
    no callback + mutating tool       -> Ask*

    *Ask is then resolved via the asker (or the non-blocking fallback).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import msgspec

from .tools import Tool

PermissionMode = Literal["default", "acceptEdits", "auto", "bypass"]

# The interactive "asker": the human-in-the-loop hook. When a decision lands on
# Ask and an asker is wired, the resolver awaits it and honors the answer.
# Separate from ``can_use_tool`` on purpose — that's programmatic *policy*; this
# is "ask the person at the terminal". Returns one of three literals.
AskResult = Literal["allow_once", "allow_session", "deny"]
AskerFn = Callable[[Tool, dict[str, Any], str], Awaitable[AskResult]]


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
    # Interactive human-in-the-loop hook. When a decision resolves to Ask and
    # this is set, the resolver awaits it (Allow once / Allow for session /
    # Deny). ``None`` means no interactive surface (library / headless use):
    # an Ask falls back to the historical non-blocking default so nothing hangs.
    asker: AskerFn | None = None
    # Tools the user chose "allow for session" for, keyed by
    # ``(tool_name, input-projection)`` so an *identical* later call is
    # auto-allowed without re-prompting. Mutated only by the resolver.
    session_allows: set[tuple[str, str]] = field(default_factory=set)
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


_EDIT_TOOL_NAMES = {
    "write_file", "edit_file", "multi_edit", "notebook_edit", "write", "edit",
    "create_file", "apply_patch",
}


def _is_edit_tool(tool: Tool) -> bool:
    """A file-editing tool — what ``acceptEdits`` auto-approves (vs. bash and
    other mutations, which still prompt)."""
    flag = getattr(tool, "is_file_edit", None)
    if isinstance(flag, bool):
        return flag
    n = tool.name.lower()
    return n in _EDIT_TOOL_NAMES or n.startswith("write_") or n.startswith("edit_")


def _session_key(tool: Tool, input: dict[str, Any]) -> tuple[str, str]:
    return (tool.name, _project_input(input))


# ---------------------------------------------------------------------------
# Bash danger classifier — annotates the Ask prompt for shell commands. A
# pragmatic subset of Claude's bashSecurity chain: enough to flag the obvious
# foot-guns so the human sees *why* they're being asked.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BashRisk:
    is_dangerous: bool
    reason: str = ""


_DANGEROUS_BASH: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-[a-z]*[rf]"), "recursive/forced delete"),
    (re.compile(r":\(\)\s*\{.*\};\s*:"), "fork bomb"),
    (re.compile(r"\bmkfs\b|\bdd\s+if="), "raw disk write"),
    (re.compile(r">\s*/dev/(sd|nvme|disk)"), "writes to a block device"),
    (re.compile(r"\bchmod\s+-R\s+0?777\b"), "world-writable recursive chmod"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh"), "pipe-to-shell from network"),
    (re.compile(r"\bsudo\b"), "privilege escalation"),
    (re.compile(r">\s*/etc/"), "overwrites a system config"),
]


def classify_bash_command(command: str) -> BashRisk:
    """Flag obviously-dangerous shell commands so the Ask prompt can warn."""
    for pat, why in _DANGEROUS_BASH:
        if pat.search(command or ""):
            return BashRisk(True, why)
    return BashRisk(False)


def _is_dangerous_bash(tool: Tool, input: dict[str, Any]) -> bool:
    """True for a shell tool call whose command trips the danger classifier."""
    if tool.name != "bash":
        return False
    return classify_bash_command(str((input or {}).get("command", ""))).is_dangerous


def _format_prompt(tool: Tool, input: dict[str, Any]) -> str:
    """Human-readable one-liner for the Ask prompt."""
    if tool.name == "bash":
        cmd = str(input.get("command", "")).strip()
        risk = classify_bash_command(cmd)
        short = cmd if len(cmd) <= 80 else cmd[:77] + "…"
        warn = f"  ⚠ {risk.reason}" if risk.is_dangerous else ""
        return f"bash: {short}{warn}"
    summary = ", ".join(f"{k}={v!r}" for k, v in list((input or {}).items())[:2])
    return f"{tool.name}({summary})"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


async def check_permission(
    tool: Tool,
    input: dict[str, Any],
    ctx: PermissionContext,
) -> PermissionDecision:
    """Apply the full decision pipeline. Returns Allow or Deny.

    Computes a provisional decision (rules → mode → ``can_use_tool``); any Ask
    is then resolved through ``ctx.asker`` (the interactive surface). When no
    asker is wired, Ask falls back to the historical non-blocking default so
    library / headless callers never hang.
    """

    decision = await _decide(tool, input, ctx)
    if isinstance(decision, Ask):
        decision = await _resolve_ask(tool, input, ctx, decision)
    return decision


async def _decide(
    tool: Tool, input: dict[str, Any], ctx: PermissionContext
) -> PermissionDecision:
    """The provisional decision, before interactive resolution."""

    if ctx.mode == "bypass":
        return Allow()

    # "Allow for session" — an identical (tool, input) the user already
    # approved this run. Checked before everything else so it never re-prompts.
    if _session_key(tool, input) in ctx.session_allows:
        return Allow()

    # Explicit deny rule always wins — evaluate it before anything else.
    hit = ctx.rules.match(tool.name, input) if ctx.rules is not None else None
    if hit is not None and hit.action == "deny":
        return Deny(reason=f"denied by rule {hit.pattern!r}")

    # A dangerous shell command can NEVER be auto-allowed by a broad allow rule,
    # by acceptEdits, or by the mode default — it must be confirmed live (or
    # denied when headless). Only an explicit deny rule (above) or bypass mode
    # short-circuits it. This is the bypass-immune-style safety check.
    if _is_dangerous_bash(tool, input):
        return Ask(prompt=_format_prompt(tool, input))

    # Declarative allow / ask rules.
    if hit is not None:
        if hit.action == "allow":
            return Allow()
        return Ask(prompt=_format_prompt(tool, input))

    # acceptEdits auto-approves file edits (but not bash / other mutations).
    if ctx.mode == "acceptEdits" and _is_edit_tool(tool):
        return Allow()

    # Imperative policy hook. It may itself return Ask, which we then resolve.
    if ctx.can_use_tool is not None:
        from .claude_compat import ToolPermissionContext  # local to avoid cycle

        tpc = ToolPermissionContext(
            session_id=str(ctx.extra.get("session_id", "")),
            signal=ctx.signal,  # __post_init__ mints a fresh Event if None
            suggestions=list(ctx.extra.get("suggestions", [])),
        )
        return await ctx.can_use_tool(tool, input, tpc)

    # No callback — decide by mode. Reads never prompt; mutations ask.
    if _is_read_only(tool):
        return Allow()
    if ctx.mode in ("default", "acceptEdits", "auto"):
        return Ask(prompt=_format_prompt(tool, input))
    return Allow()


async def _resolve_ask(
    tool: Tool, input: dict[str, Any], ctx: PermissionContext, ask: Ask
) -> PermissionDecision:
    """Turn an Ask into Allow/Deny via the interactive asker, or a safe
    non-blocking fallback when none is wired."""

    if ctx.asker is None:
        # No interactive surface (library / headless). A dangerous shell
        # command with nobody to approve it is DENIED — never auto-run.
        if _is_dangerous_bash(tool, input):
            return Deny(
                reason="dangerous command blocked (no interactive approval available)"
            )
        # Otherwise preserve historical behavior: ``auto`` surfaced Ask (the
        # loop treats it permissively); everything else was permissive too.
        return ask if ctx.mode == "auto" else Allow()

    result = await ctx.asker(tool, input, ask.prompt)
    if result == "deny":
        return Deny(reason="denied by user")
    if result == "allow_session":
        ctx.session_allows.add(_session_key(tool, input))
    return Allow()


__all__ = [
    "Allow",
    "Ask",
    "AskResult",
    "AskerFn",
    "BashRisk",
    "CanUseToolFn",
    "Deny",
    "classify_bash_command",
    "PermissionContext",
    "PermissionDecision",
    "PermissionMode",
    "PermissionRule",
    "PermissionRuleSet",
    "check_permission",
]
