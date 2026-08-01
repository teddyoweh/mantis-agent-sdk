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
  an *explicit* Ask (a ``can_use_tool`` policy or an ``ask`` rule that asked to
  be prompted, or a dangerous shell command) fails CLOSED (Deny) so nothing
  runs unattended; only the implicit "mutating tool in default mode" fallback
  stays non-blocking so ordinary library use doesn't hang.

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
import functools
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

        targets = _match_targets(input)
        for rule in self.deny:
            if _rule_matches(rule, tool_name, targets):
                return rule
        for rule in self.allow:
            if _rule_matches(rule, tool_name, targets):
                return rule
        for rule in self.ask:
            if _rule_matches(rule, tool_name, targets):
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


def _match_targets(input: dict[str, Any]) -> list[str]:
    """Strings a rule pattern is tested against.

    Includes the whole JSON projection (backward compat) *plus* each scalar
    field value on its own. Matching individual values is what makes intuitive
    patterns like ``rm -rf*`` or ``ls*`` fire: they whole-string glob against
    the field value (``"rm -rf /"``) instead of the JSON blob
    (``{"command": "rm -rf /"}``), which no bare pattern could ever match. It is
    deliberately NOT a substring match against the projection — that would
    over-broaden *allow* rules (a pattern could match text anywhere in the blob).
    """

    targets = [_project_input(input)]
    if isinstance(input, dict):
        for v in input.values():
            if isinstance(v, str):
                targets.append(v)
            elif isinstance(v, bool | int | float):
                targets.append(str(v))
    return targets


@functools.lru_cache(maxsize=256)
def _compiled_regex(pattern: str) -> re.Pattern[str] | None:
    """Compile (and cache) a rule regex. ``None`` on a bad pattern so a broken
    rule fails safe (matches nothing) instead of raising on every call."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _rule_matches(rule: PermissionRule, tool_name: str, targets: list[str]) -> bool:
    """Test whether a single rule fires against any match target."""

    if rule.tool_name is not None and rule.tool_name != tool_name:
        return False
    if rule.is_regex:
        pat = _compiled_regex(rule.pattern)
        if pat is None:
            return False
        return any(pat.search(t) is not None for t in targets)
    # Default: shell-style glob, matched (whole-string) against each target.
    return any(fnmatch.fnmatchcase(t, rule.pattern) for t in targets)


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
# ``auto`` mode wants to auto-allow read-only tools. We treat a tool as
# read-only ONLY when it explicitly carries ``is_read_only=True``. We do NOT
# infer read-only-ness from the name: a tool called ``find_and_delete`` or
# ``get_and_reset`` reads as read-only by prefix but mutates state, and
# auto-allowing it unprompted is a fail-open hole. Unknown tools (no flag)
# default to the mutating / ask path.


def _is_read_only(tool: Tool) -> bool:
    return getattr(tool, "is_read_only", False) is True


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


# Tools whose "allow for session" should scope to the FILE, not the exact call.
# Every edit to a file has different old_string/new_string/content, so keying on
# the full input would re-prompt on every single edit — making session-allow
# pointless for exactly the highest-friction case. Keying by path means: approve
# editing foo.py once, and further edits to foo.py this session don't re-prompt
# (bar.py still asks).
_SESSION_BY_PATH = {"edit_file", "write_file", "multi_edit", "notebook_edit"}


def _session_key(tool: Tool, input: dict[str, Any]) -> tuple[str, str]:
    if tool.name in _SESSION_BY_PATH:
        path = (
            input.get("path")
            or input.get("file_path")
            or input.get("notebook_path")
            or input.get("filename")
            or ""
        )
        return (tool.name, str(path))
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


# Compiled case-insensitively and with long-option forms so trivial variants
# (``rm --recursive --force``, ``CHMOD``, reordered ``dd`` args, ``doas``/
# ``pkexec``) don't slip past the annotator. This is best-effort defense in
# depth layered on top of the shell-tool Ask/deny gate — never the sole gate.
_DANGEROUS_BASH: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(?:-[a-z]*[rf]|--(?:recursive|force)\b)", re.IGNORECASE), "recursive/forced delete"),
    (re.compile(r":\(\)\s*\{.*\};\s*:"), "fork bomb"),
    (re.compile(r"\bmkfs\b|\bdd\b[^|;&]*\b(?:if|of)=", re.IGNORECASE), "raw disk write"),
    (re.compile(r">\s*/dev/(?:sd|nvme|disk)", re.IGNORECASE), "writes to a block device"),
    (re.compile(r"\bchmod\s+(?:-R|--recursive)\s+0?777\b", re.IGNORECASE), "world-writable recursive chmod"),
    (re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh", re.IGNORECASE), "pipe-to-shell from network"),
    (re.compile(r"\b(?:sudo|doas|pkexec)\b", re.IGNORECASE), "privilege escalation"),
    (re.compile(r">\s*/etc/"), "overwrites a system config"),
]


def classify_bash_command(command: str) -> BashRisk:
    """Flag obviously-dangerous shell commands so the Ask prompt can warn."""
    for pat, why in _DANGEROUS_BASH:
        if pat.search(command or ""):
            return BashRisk(True, why)
    return BashRisk(False)


# Any of these tool names (or a tool explicitly flagged ``is_shell=True``) is
# treated as a shell surface — the danger classifier and the headless hard-deny
# key off shell-tool IDENTITY, not the single literal "bash". A shell tool
# registered under another name must never bypass the safety net.
_SHELL_TOOL_NAMES = {
    "bash", "sh", "shell", "zsh", "fish", "ksh",
    "exec", "run", "run_command", "runcommand", "command", "cmd",
    "system", "powershell", "pwsh",
    # `watch` runs its argument through ``bash -lc`` exactly as ``bash`` does —
    # it is a shell surface that happens to stream, and must face the same
    # danger classifier rather than becoming a way around it.
    "watch",
}

# Fields a shell tool conventionally carries its command in.
_SHELL_COMMAND_FIELDS = ("command", "cmd", "script", "code", "commands")


def _is_shell_tool(tool: Tool) -> bool:
    """True for a command-executing tool, by explicit flag or known name."""
    flag = getattr(tool, "is_shell", None)
    if isinstance(flag, bool):
        return flag
    return tool.name.lower() in _SHELL_TOOL_NAMES


def _shell_command(input: dict[str, Any]) -> str:
    """Best-effort extraction of the command string from a shell tool input."""
    inp = input or {}
    for f in _SHELL_COMMAND_FIELDS:
        v = inp.get(f)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, list | tuple):
            return " ".join(str(x) for x in v)
    return ""


def _is_dangerous_bash(tool: Tool, input: dict[str, Any]) -> bool:
    """True for a shell tool call whose command trips the danger classifier."""
    if not _is_shell_tool(tool):
        return False
    return classify_bash_command(_shell_command(input)).is_dangerous


def _snip(s: Any, limit: int = 32) -> str:
    """One-line, whitespace-collapsed, length-capped preview of a value."""
    t = " ".join(str(s or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _format_prompt(tool: Tool, input: dict[str, Any]) -> str:
    """Human-readable one-liner for the Ask prompt. File-editing tools get a
    clear, path-focused summary of the CHANGE (so the user reviews what's actually
    about to happen), not a raw ``tool(old_string=..., new_string=...)`` repr."""
    name = tool.name
    inp = input or {}
    if _is_shell_tool(tool):
        cmd = _shell_command(inp).strip()
        risk = classify_bash_command(cmd)
        short = cmd if len(cmd) <= 80 else cmd[:77] + "…"
        warn = f"  ⚠ {risk.reason}" if risk.is_dangerous else ""
        return f"{name}: {short}{warn}"

    path = inp.get("path") or inp.get("file_path") or inp.get("notebook_path")
    if path:
        if name == "edit_file":
            return f'edit {path}:  "{_snip(inp.get("old_string"))}" → "{_snip(inp.get("new_string"))}"'
        if name == "multi_edit":
            n = len(inp.get("edits") or [])
            return f"edit {path} ({n} change{'s' if n != 1 else ''})"
        if name == "write_file":
            body = inp.get("content") or ""
            lines = body.count("\n") + 1 if body else 0
            return f"write {path} ({lines} line{'s' if lines != 1 else ''})"
        if name == "notebook_edit":
            return f"edit notebook {path} (cell {inp.get('cell_number', '?')})"

    summary = ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:2])
    return f"{name}({summary})"


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

    # Explicit deny rule always wins — evaluate it before anything else,
    # including "allow for session": a prior session approval must never
    # override a deny rule.
    hit = ctx.rules.match(tool.name, input) if ctx.rules is not None else None
    if hit is not None and hit.action == "deny":
        return Deny(reason=f"denied by rule {hit.pattern!r}")

    # A dangerous shell command can NEVER be auto-allowed by a broad allow rule,
    # by acceptEdits, by the mode default, or by a prior "allow for session" —
    # it must be confirmed live (or denied when headless) on EVERY call. Only an
    # explicit deny rule (above) or bypass mode short-circuits it. Evaluated
    # before the session-allows check so session approval can't defeat it.
    if _is_dangerous_bash(tool, input):
        return Ask(prompt=_format_prompt(tool, input))

    # "Allow for session" — an identical (tool, input) the user already approved
    # this run. Checked after the deny + dangerous-command guards so it can never
    # bypass them, but before the remaining allow/ask/mode logic so it doesn't
    # re-prompt.
    if _session_key(tool, input) in ctx.session_allows:
        return Allow()

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
        # An EXPLICIT request for confirmation — a user-configured ``ask`` rule
        # or a ``can_use_tool`` policy that deliberately returned Ask — means the
        # user asked to be prompted. With no interactive surface, fail CLOSED
        # (Deny) rather than silently running the mutating call. Only the
        # implicit "mutating tool in default mode" fallback stays non-blocking,
        # preserving historical library behavior.
        hit = ctx.rules.match(tool.name, input) if ctx.rules is not None else None
        explicit_ask = (hit is not None and hit.action == "ask") or ctx.can_use_tool is not None
        if explicit_ask:
            return Deny(
                reason="approval required but no interactive approver is available"
            )
        # Otherwise preserve historical behavior: ``auto`` surfaced Ask (the
        # loop treats it permissively); everything else was permissive too.
        return ask if ctx.mode == "auto" else Allow()

    result = await ctx.asker(tool, input, ask.prompt)
    if result == "deny":
        return Deny(reason="denied by user")
    if result == "allow_session":
        # A dangerous command is never remembered for the session — it must be
        # re-confirmed live on every call (the guard in _decide re-Asks anyway).
        if not _is_dangerous_bash(tool, input):
            ctx.session_allows.add(_session_key(tool, input))
    return Allow()


async def recheck_mutated_input(
    tool: Tool, updated_input: dict[str, Any], ctx: PermissionContext
) -> PermissionDecision:
    """Re-validate an input that a ``can_use_tool`` / ``PreToolUse`` callback
    REWROTE *after* it was approved.

    The callback approved some *other* input and handed back this rewritten one;
    the rewrite itself was never vetted. Re-run the mandatory, non-overridable
    guards — an explicit ``deny`` rule and the dangerous-shell-command gate —
    against the MUTATED input so an approved ``ls`` can't be rewritten into an
    unreviewed ``rm -rf``. Fails CLOSED: a denied rewrite is denied, and a
    dangerous rewrite must be confirmed live (or is denied when headless).

    We deliberately do NOT re-invoke the callback (it just ran and produced this
    input — calling it again would recurse / re-mutate) nor re-apply the mode /
    allow-with-modified-input logic (that would defeat the legitimate "approve a
    normalized input" flow). Only the guards that ``_decide`` applies BEFORE any
    approval are re-checked.
    """

    if ctx.mode == "bypass":
        return Allow()

    hit = ctx.rules.match(tool.name, updated_input) if ctx.rules is not None else None
    if hit is not None and hit.action == "deny":
        return Deny(reason=f"denied by rule {hit.pattern!r} (after input rewrite)")

    if _is_dangerous_bash(tool, updated_input):
        ask = Ask(prompt=_format_prompt(tool, updated_input))
        return await _resolve_ask(tool, updated_input, ctx, ask)

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
    "recheck_mutated_input",
]
