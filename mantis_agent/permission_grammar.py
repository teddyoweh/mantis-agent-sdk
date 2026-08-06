"""Structured permission rule grammar — ``Tool(param:value)`` parsing and matching.

The historical rule form is a flat glob: a ``PermissionRule`` carries a
``pattern`` that ``fnmatch``es a JSON projection of the tool input plus every
scalar field in it. That is compact and it is why ``rm -rf*`` fires at all, but
it cannot express what people actually want to say — "allow ``read_file`` only
under ``docs/``", "allow ``bash`` only when the command starts with
``git status``", "deny writes to ``.env`` however they are spelled". A pattern
applies to whichever field happens to hold a matching string.

This module adds the structured form *beside* the legacy one. It is pure: no
imports from :mod:`mantis_agent.permissions`, no I/O beyond the filesystem
probes path matching genuinely requires (``realpath`` and a case-sensitivity
probe). Wiring it into the decision pipeline is a separate step; this file is
the parser + matcher and nothing else.

Grammar (each form maps to one :class:`CompiledRule`)::

    Tool                    any call to this tool          (opt-in, see below)
    Tool()                  any call to this tool
    Tool(arg)               arg vs. the tool's PRIMARY parameter
    Tool(prefix:*)          prefix match on the primary parameter
    Tool(param=value)       explicit named parameter, exact
    Tool(param~glob)        explicit named parameter, glob
    Tool(param=/re/)        explicit named parameter, regex

**Legacy detection is deliberately unambiguous**: a rule string that contains
``(`` *and* ends with ``)`` is structured; everything else is a legacy flat
pattern and keeps today's ``fnmatch`` behavior byte for byte. That is why the
bare ``Tool`` form needs the explicit ``bare_tool=True`` opt-in — an unwrapped
word is indistinguishable from a legacy pattern, and backward compatibility
wins over convenience. ``Tool()`` spells the same thing unambiguously.

Two properties matter more than the rest and each has its own tests:

* **Resolution before matching.** Path values go through ``os.path.realpath``
  *first*, so ``docs/../.env`` and a symlink pointing at ``.env`` both hit
  ``Write(.env)``. Matching the string as written is the single most common
  source of permission bypasses.
* **Never degrade to matching everything.** ``Tool(value)`` needs to know which
  parameter ``value`` targets. When that cannot be resolved the rule raises
  :class:`AmbiguousPrimaryParamError` instead of quietly matching any field (or
  every call), because a rule that silently widens is worse than a rule that
  refuses to load.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

from .tools import _TOOL_ALIASES

__all__ = [
    "AmbiguousPrimaryParamError",
    "CompiledRule",
    "PermissionRuleError",
    "RuleMatch",
    "RuleSyntaxError",
    "UnknownToolInRuleError",
    "best_match",
    "bind_rule",
    "canonical_tool_name",
    "compile_rule",
    "fs_case_sensitive",
    "is_structured_rule",
    "path_matches",
    "primary_param_for",
    "resolve_path",
    "rule_matches",
    "specificity",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
#
# All rooted at ValueError so a settings loader that already catches ValueError
# keeps working. They stay in this module rather than the permission package so
# the grammar has no import edges back into the decision pipeline.


class PermissionRuleError(ValueError):
    """Base for every rule-compilation failure."""


class RuleSyntaxError(PermissionRuleError):
    """``Tool(param:value)`` did not parse."""


class UnknownToolInRuleError(PermissionRuleError):
    """The rule names a tool that is not registered."""


class AmbiguousPrimaryParamError(PermissionRuleError):
    """``Tool(value)`` with no resolvable primary parameter.

    Raised rather than falling back to "match any field": a positional rule
    whose target is unknown would otherwise widen an allow list to everything
    the tool can be handed.
    """


# ---------------------------------------------------------------------------
# Tool-name canonicalization
# ---------------------------------------------------------------------------
#
# Rules are written with the Claude-Code-style capitalized names (`Bash`,
# `Read`, `Write`, `Edit`) so they paste across ecosystems; mantis registers
# `bash`, `read_file`, `write_file`, `edit_file`. ``ToolRegistry.resolve``
# already owns that mapping, so we reuse its table instead of inventing a second
# one — but we also register each canonical name under its own normalized form,
# because the table only carries the *aliases* (`edit` → `edit_file`) and not
# the identity entry (`editfile` → `edit_file`).


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


_CANONICAL_TOOLS: dict[str, str] = {}
for _alias, _target in _TOOL_ALIASES.items():
    _CANONICAL_TOOLS[_alias] = _target
    _CANONICAL_TOOLS[_norm(_target)] = _target


def canonical_tool_name(name: str) -> str:
    """Fold a tool name to the form rules and inputs are compared in.

    ``Bash`` / ``bash`` → ``bash``; ``Read`` / ``read_file`` → ``read_file``.
    An unknown name folds to its normalized spelling, which is stable on both
    sides of a comparison — that is all matching requires.
    """
    key = _norm(name)
    if not key:
        return ""
    return _CANONICAL_TOOLS.get(key, key)


# ---------------------------------------------------------------------------
# Primary parameter
# ---------------------------------------------------------------------------

# Built-in table, keyed by canonical tool name. Step 2 of the resolution order;
# it exists so a rule compiles at settings-load time, before any registry is
# available.
_PRIMARY_PARAMS: dict[str, str] = {
    "bash": "command",
    "watch": "command",
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "multi_edit": "path",
    "notebook_edit": "path",
    "ls": "path",
    "glob": "pattern",
    "grep": "pattern",
    "web_fetch": "url",
    "web_search": "query",
    "task": "prompt",
}

# Parameter names that hold a filesystem path. A rule against one of these gets
# gitignore semantics + realpath resolution instead of a flat glob; that is the
# "implied ``matcher='path'``" of the plan. ``pattern`` is deliberately absent:
# ``glob(pattern="**/*.py")`` is a query, not a location on disk.
_PATH_PARAMS = frozenset({
    "path", "file_path", "filepath", "notebook_path", "filename", "file",
    "dir", "directory", "cwd", "dest", "destination", "target_path", "output_path",
})


def _first_required_string(schema: Any) -> Optional[str]:
    """Step 3: the first ``required`` property of string type, in schema order."""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    required = schema.get("required")
    if not isinstance(props, dict) or not isinstance(required, (list, tuple)):
        return None
    for name in required:
        if not isinstance(name, str):
            continue
        spec = props.get(name)
        if isinstance(spec, dict) and spec.get("type") == "string":
            return name
    return None


def primary_param_for(tool: Any) -> str:
    """Resolve the parameter a positional ``Tool(value)`` rule targets.

    Order (the plan's §5): an explicit ``primary_param`` attribute on the tool,
    then the built-in table, then the first required string property of the
    input schema. ``tool`` may be a ``Tool``-like object or a bare name string
    (in which case only the table can answer).

    Raises :class:`AmbiguousPrimaryParamError` — naming the tool — when nothing
    resolves. It must never silently degrade to matching everything.
    """
    if isinstance(tool, str):
        name, obj = tool, None
    else:
        name, obj = str(getattr(tool, "name", "") or ""), tool

    if obj is not None:
        declared = getattr(obj, "primary_param", None)
        if isinstance(declared, str) and declared:
            return declared

    table = _PRIMARY_PARAMS.get(canonical_tool_name(name))
    if table:
        return table

    if obj is not None:
        derived = _first_required_string(getattr(obj, "input_schema", None))
        if derived:
            return derived

    raise AmbiguousPrimaryParamError(
        f"cannot resolve a primary parameter for tool {name or '<unnamed>'!r}: "
        f"write the rule as {name or 'Tool'}(param=value) or give the tool a "
        f"primary_param attribute"
    )


def _is_path_param(param: str) -> bool:
    return param in _PATH_PARAMS


# ---------------------------------------------------------------------------
# Compiled rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledRule:
    """One parsed rule.

    ``matcher`` is the *form* the rule was written in; ``is_path`` says whether
    the value is compared with path semantics (the plan's ``matcher="path"``,
    kept as a separate flag so exact-vs-glob specificity survives).

    ``tool`` is the canonical tool name for structured rules and the raw
    ``tool_name`` field for legacy ones — legacy scoping compares the string
    exactly, as it does today, and must not start alias-folding.
    """

    text: str                       # the rule exactly as written
    tool: Optional[str]             # None = any tool (legacy rules only)
    param: Optional[str]            # None = positional, not yet bound to a tool
    value: str                      # right-hand side (regex body without the //)
    matcher: str                    # exact | glob | prefix | regex | tool | legacy
    explicit_param: bool = False    # written as Tool(param=...) / Tool(param~...)
    is_path: bool = False           # compare with gitignore + realpath semantics
    structured: bool = True         # False for legacy flat patterns
    legacy_regex: bool = False      # legacy PermissionRule.is_regex passthrough

    @property
    def needs_binding(self) -> bool:
        """True when the positional target could not be resolved at compile
        time; :func:`bind_rule` must supply the tool before matching."""
        return self.structured and self.matcher != "tool" and self.param is None


@dataclass(frozen=True)
class RuleMatch:
    """A rule that fired, plus which parameter and value made it fire.

    ``value`` is unredacted on purpose — the caller owns redaction policy for
    whatever sink it lands in (prompt, log, trace, decision record).
    """

    rule: CompiledRule
    param: str
    value: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_GLOB_META = "*?["
_TOOL_HEAD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
# `param=` / `param~` must sit at the very start with NO whitespace around the
# operator. That keeps discrimination total: `Bash(git status:*)` and
# `Bash(rm -rf ~)` are positional because a space intervenes, while
# `Bash(FOO=bar make)` reads as an explicit `FOO` parameter — spell it
# `Bash(command=FOO=bar make)` if you meant the command. Determinism beats a
# heuristic that guesses which of the two the user meant.
_EXPLICIT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)([=~])(.*)$", re.DOTALL)


def is_structured_rule(text: str) -> bool:
    """True when ``text`` uses the ``Tool(...)`` form.

    The whole compatibility story rests on this predicate being trivial: a
    string containing ``(`` and ending with ``)`` is structured, everything else
    is a legacy flat pattern. No stripping, no lookahead, no fallback if the
    structured parse then fails — a malformed structured rule raises rather than
    silently re-reading as a legacy glob, since that would let a typo widen a
    deny list into a pattern that matches nothing.
    """
    return isinstance(text, str) and "(" in text and text.endswith(")")


def _has_glob(value: str) -> bool:
    return any(ch in _GLOB_META for ch in value)


@functools.lru_cache(maxsize=1024)
def compile_rule(
    text: str,
    tool_name: Optional[str] = None,
    is_regex: bool = False,
    bare_tool: bool = False,
    known_tools: Optional[frozenset] = None,
) -> CompiledRule:
    """Parse one rule string. Cached — the decision path is a hot loop.

    ``tool_name`` and ``is_regex`` mirror the existing ``PermissionRule`` fields
    and only affect legacy patterns (a structured rule names its own tool).
    ``bare_tool`` opts an unwrapped identifier into the bare ``Tool`` form
    instead of the legacy path. ``known_tools`` (canonical names) turns a rule
    naming a tool that does not exist into :class:`UnknownToolInRuleError`.

    All arguments are hashable so the whole call caches.
    """
    if not isinstance(text, str) or not text:
        raise RuleSyntaxError("empty permission rule")

    if not is_structured_rule(text):
        stripped = text.strip()
        if bare_tool and _TOOL_HEAD_RE.match(stripped):
            canon = canonical_tool_name(stripped)
            _check_known(canon, stripped, known_tools)
            return CompiledRule(
                text=text, tool=canon, param=None, value="", matcher="tool"
            )
        # Legacy flat pattern: preserved verbatim, including any surrounding
        # whitespace, because fnmatch treats it literally.
        return CompiledRule(
            text=text,
            tool=tool_name,
            param=None,
            value=text,
            matcher="legacy",
            structured=False,
            legacy_regex=bool(is_regex),
        )

    head, _, rest = text.partition("(")
    head = head.strip()
    body = rest[:-1]  # drop the trailing ")" that is_structured_rule guaranteed
    if not _TOOL_HEAD_RE.match(head):
        raise RuleSyntaxError(
            f"permission rule {text!r}: {head!r} is not a valid tool name"
        )
    canon = canonical_tool_name(head)
    _check_known(canon, head, known_tools)

    if not body.strip():
        # `Tool()` — any call to this tool.
        return CompiledRule(text=text, tool=canon, param=None, value="", matcher="tool")

    explicit = _EXPLICIT_RE.match(body)
    if explicit is not None:
        param, op, value = explicit.group(1), explicit.group(2), explicit.group(3)
        if op == "~":
            matcher = "glob"
        elif len(value) >= 2 and value.startswith("/") and value.endswith("/"):
            # `param=/re/`. This wins over an absolute directory path spelled
            # `param=/etc/` — write that one as `param~/etc/` or positionally.
            matcher, value = "regex", value[1:-1]
            if _compiled_regex(value) is None:
                raise RuleSyntaxError(
                    f"permission rule {text!r}: {value!r} is not a valid regex"
                )
        else:
            matcher = "exact"
        if not value:
            raise RuleSyntaxError(f"permission rule {text!r}: empty value for {param!r}")
        return CompiledRule(
            text=text,
            tool=canon,
            param=param,
            value=value,
            matcher=matcher,
            explicit_param=True,
            is_path=_is_path_param(param) and matcher in ("exact", "glob"),
        )

    # Positional: matched against the tool's primary parameter.
    if body.endswith(":*"):
        matcher, value = "prefix", body[:-2]
        if not value:
            raise RuleSyntaxError(f"permission rule {text!r}: empty prefix")
    else:
        matcher, value = ("glob" if _has_glob(body) else "exact"), body

    param = _PRIMARY_PARAMS.get(canon)  # may be None → bind_rule resolves it later
    return CompiledRule(
        text=text,
        tool=canon,
        param=param,
        value=value,
        matcher=matcher,
        is_path=bool(param) and _is_path_param(param) and matcher in ("exact", "glob"),
    )


def _check_known(canon: str, written: str, known_tools: Optional[frozenset]) -> None:
    if known_tools is None:
        return
    if canon not in {canonical_tool_name(n) for n in known_tools}:
        raise UnknownToolInRuleError(f"rule names unknown tool {written!r}")


def bind_rule(rule: CompiledRule, tool: Any) -> CompiledRule:
    """Resolve a positional rule's target parameter against a concrete tool.

    A no-op for rules that already know their parameter (and for legacy / bare
    forms). Call it at settings load with the real registry so an unresolvable
    rule fails loudly there instead of on the first tool call.
    """
    if not rule.needs_binding:
        return rule
    param = primary_param_for(tool)
    return replace(
        rule,
        param=param,
        is_path=_is_path_param(param) and rule.matcher in ("exact", "glob"),
    )


# ---------------------------------------------------------------------------
# Legacy match targets — a byte-for-byte copy of permissions._match_targets
# ---------------------------------------------------------------------------
#
# Copied rather than imported: `permissions` will import THIS module once the
# grammar is wired in, so an import edge in the other direction would be a
# cycle. `tests/test_permission_grammar.py` pins the copy to the original by
# importing both and asserting they agree on a corpus.


def _project_input(input: dict) -> str:
    try:
        import json  # noqa: PLC0415

        return json.dumps(input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(input)


def _match_targets(input: dict) -> list:
    targets = [_project_input(input)]
    if isinstance(input, dict):
        for v in input.values():
            if isinstance(v, str):
                targets.append(v)
            elif isinstance(v, (bool, int, float)):
                targets.append(str(v))
    return targets


@functools.lru_cache(maxsize=256)
def _compiled_regex(pattern: str) -> Optional["re.Pattern"]:
    """Compile (and cache) a regex. ``None`` on a bad pattern so a broken rule
    fails safe (matches nothing) instead of raising on every call."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


# ---------------------------------------------------------------------------
# Filesystem case sensitivity — probed, never guessed
# ---------------------------------------------------------------------------


def _swapcase_probe(directory: str) -> Optional[bool]:
    """True/False if the probe is conclusive for ``directory``, else None.

    Read-only: take an existing entry, swap its case, and ask whether that name
    also resolves to the same file. Never creates anything in the user's tree.
    """
    try:
        entries = []
        with os.scandir(directory) as it:
            for i, entry in enumerate(it):
                if i >= 32:
                    break
                entries.append(entry.name)
    except OSError:
        return None
    for name in entries:
        swapped = name.swapcase()
        if swapped == name:
            continue  # no alphabetic character to flip
        original = os.path.join(directory, name)
        candidate = os.path.join(directory, swapped)
        try:
            if not os.path.lexists(candidate):
                return True  # the swapped spelling does not exist → sensitive
            a, b = os.stat(original), os.stat(candidate)
        except OSError:
            continue
        return not (a.st_dev == b.st_dev and a.st_ino == b.st_ino)
    return None


@functools.lru_cache(maxsize=64)
def fs_case_sensitive(path: Optional[str] = None) -> bool:
    """Is the filesystem holding ``path`` case-sensitive?

    Probed from the filesystem rather than inferred from ``sys.platform``: a
    case-sensitive volume on macOS and a case-insensitive one mounted on Linux
    are both real, and getting this backwards either lets ``Write(.ENV)`` slip
    past a ``Write(.env)`` deny or blocks legitimate work. The platform default
    is only the last resort when the probe cannot conclude.
    """
    directory = os.path.realpath(path or os.getcwd())
    while directory and not os.path.isdir(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    probed = _swapcase_probe(directory)
    if probed is not None:
        return probed
    return not (sys.platform == "darwin" or os.name == "nt")


# ---------------------------------------------------------------------------
# Path matching — gitignore semantics over realpath-resolved values
# ---------------------------------------------------------------------------


def _slashes(p: str) -> str:
    return p.replace(os.sep, "/") if os.sep != "/" else p


# ``realpath`` is a syscall chain, and a 200-rule set asks the same question
# about the same value up to 200 times — measured at ~21 µs per rule, which
# blows the 200 µs budget for the whole decision on its own. So resolutions are
# memoized for a very short window: long enough that one decision pass costs one
# resolution, short enough that a symlink swapped between tool calls is still
# seen. A permanent cache would be a TOCTOU hole (write a file, replace it with
# a symlink, get judged against the stale answer); an unmemoized path would make
# the matcher too slow to sit on the hot loop. The window is the honest tradeoff
# and is deliberately named so a caller can shrink it to 0.
_RESOLVE_TTL_S = 0.05
_RESOLVE_CACHE_MAX = 4096
_resolve_memo: dict = {}
_candidate_memo: dict = {}


def _memo_realpath(path: str) -> str:
    now = time.monotonic()
    hit = _resolve_memo.get(path)
    if hit is not None and now - hit[0] < _RESOLVE_TTL_S:
        return hit[1]
    resolved = os.path.realpath(path)
    if len(_resolve_memo) >= _RESOLVE_CACHE_MAX:
        _resolve_memo.clear()
    _resolve_memo[path] = (now, resolved)
    return resolved


def _clear_resolve_cache() -> None:
    """Drop the memos. For tests and for a caller that just mutated the tree."""
    _resolve_memo.clear()
    _candidate_memo.clear()


def _root_of(project_root: Optional[str]) -> str:
    return _memo_realpath(project_root or os.getcwd())


def resolve_path(value: str, project_root: Optional[str] = None) -> str:
    """Absolute, symlink- and ``..``-resolved form of a path a tool was given.

    This is the load-bearing step: matching the string as written lets
    ``docs/../.env`` and ``link-to-env`` walk straight past ``Write(.env)``.
    ``~`` is expanded first so a rule about ``~/.ssh`` sees the same path the
    shell would.
    """
    root = _root_of(project_root)
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        expanded = os.path.join(root, expanded)
    return _memo_realpath(expanded)


def _relative_to(abs_path: str, root: str) -> Optional[str]:
    """Project-relative form, or None when the path is outside the root.

    Refusing to produce a ``../..``-style relative path is a security decision:
    any-depth matching runs against the relative candidate, and a pattern like
    ``docs/**`` must not match ``../other/docs/x`` by having ``**`` swallow the
    escape.
    """
    try:
        rel = os.path.relpath(abs_path, root)
    except ValueError:  # different drive on Windows
        return None
    rel = _slashes(rel)
    if rel == "." or rel.startswith("../"):
        return None
    return rel


def _translate_segment(seg: str) -> str:
    out = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and seg[j] in "!^":
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:  # unterminated class → literal '['
                out.append(re.escape(c))
            else:
                inner = seg[i + 1:j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@functools.lru_cache(maxsize=1024)
def _path_regex(pattern: str, case_sensitive: bool) -> "re.Pattern":
    """gitignore-flavored glob → regex. ``**`` crosses separators, ``*`` does not."""
    parts = pattern.split("/")
    out = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        if part == "**":
            out.append(".*" if i == last else "(?:[^/]+/)*")
            continue
        out.append(_translate_segment(part))
        if i != last:
            out.append("/")
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile("(?s:" + "".join(out) + r")\Z", flags)


def _resolved_pattern(pat: str, root: str) -> str:
    """The pattern with its literal leading directories realpath-resolved.

    A rule and the value it judges have to be canonicalized the same way or the
    resolution step quietly breaks rules instead of bypasses: ``/etc/**`` misses
    ``/etc/hosts`` on macOS (``/etc`` is a symlink into ``/private/etc``, which
    is where the value resolves to), and a rule aimed at a symlinked directory
    inside the project never fires. Only the LITERAL head is resolved — every
    glob segment is left exactly as written. Returns ``""`` when there is
    nothing to resolve.
    """
    segments = pat.split("/")
    literal: list = []
    for seg in segments:
        if _has_glob(seg):
            break
        literal.append(seg)
    else:
        # Fully literal pattern: resolve the parent, never the leaf — the leaf
        # may not exist yet (a write target) and realpath would still answer.
        literal = literal[:-1]
    if not literal or literal == [""]:
        return ""
    head = "/".join(literal)
    absolute_head = head if head.startswith("/") else os.path.join(root, head)
    resolved = _slashes(_memo_realpath(absolute_head)) + pat[len(head):]
    return "" if resolved == pat else resolved


def _candidates(value: str, root: str) -> tuple:
    """``(abs_real, abs_path, rel_path, case_sensitive)`` for one value.

    Every rule in the set asks the same question about the same value, so this
    whole preamble — resolve, relativize, probe — is memoized under the same
    short TTL as :func:`_memo_realpath`, for the same reason.
    """
    now = time.monotonic()
    key = (value, root)
    hit = _candidate_memo.get(key)
    if hit is not None and now - hit[0] < _RESOLVE_TTL_S:
        return hit[1]
    abs_real = resolve_path(value, root)
    built = (abs_real, _slashes(abs_real), _relative_to(abs_real, root), fs_case_sensitive(root))
    if len(_candidate_memo) >= _RESOLVE_CACHE_MAX:
        _candidate_memo.clear()
    _candidate_memo[key] = (now, built)
    return built


def _memo_resolved_pattern(pat: str, root: str) -> str:
    """TTL-memoized :func:`_resolved_pattern` — same tradeoff, rule side."""
    now = time.monotonic()
    key = ("pat", pat, root)
    hit = _candidate_memo.get(key)
    if hit is not None and now - hit[0] < _RESOLVE_TTL_S:
        return hit[1]
    built = _resolved_pattern(pat, root)
    if len(_candidate_memo) >= _RESOLVE_CACHE_MAX:
        _candidate_memo.clear()
    _candidate_memo[key] = (now, built)
    return built


def _core_match(pattern: str, candidate: str, any_depth: bool, cs: bool) -> bool:
    if _path_regex(pattern, cs).match(candidate) is not None:
        return True
    if any_depth and not pattern.startswith("**/"):
        return _path_regex("**/" + pattern, cs).match(candidate) is not None
    return False


def path_matches(
    pattern: str, value: str, project_root: Optional[str] = None
) -> bool:
    """Does ``value`` (a path a tool was handed) match ``pattern``?

    Semantics, in the plan's order:

    * ``**`` crosses directory separators, ``*`` does not.
    * A leading ``/`` anchors — to the project root, *and* (because the
      protected-path list is written as ``/etc/**``) to the filesystem root.
      Both are tried; a deny that reaches further is the safe direction.
    * A trailing ``/`` matches directories only — the directory itself when it
      exists as one, and anything beneath it.
    * A pattern with no ``/`` at all matches the basename at any depth
      (``.env`` catches ``a/b/.env``). A pattern containing ``/`` is tried
      anchored at the root and at any depth, but the any-depth form is only ever
      tried against the *project-relative* candidate: letting ``**`` chew
      through an absolute path outside the root would turn ``docs/**`` into a
      match for ``/anywhere/docs/x``.
    * The value is realpath-resolved first, always.
    """
    if not pattern or not isinstance(value, str) or not value:
        return False

    root = _root_of(project_root)
    abs_real, abs_path, rel_path, cs = _candidates(value, root)

    pat = _slashes(pattern.strip())
    if pat.startswith("~"):
        pat = _slashes(os.path.expanduser(pat))
    if not pat:
        return False

    dir_only = pat.endswith("/") and pat != "/"
    if dir_only:
        pat = pat.rstrip("/")
        if not pat:
            return False
        # "directories only" = the directory itself (if it really is one) or
        # anything under it. The `/**` form expresses the second half exactly.
        if os.path.isdir(abs_real) and path_matches(pat, value, root):
            return True
        return path_matches(pat + "/**", value, root)

    anchored = pat.startswith("/")
    if anchored:
        body = pat.lstrip("/")
        if rel_path is not None and _core_match(body, rel_path, False, cs):
            return True  # anchored at the project root
        if _core_match(pat, abs_path, False, cs):  # absolute filesystem path
            return True
        canon = _memo_resolved_pattern(pat, root)
        return bool(canon) and _core_match(canon, abs_path, False, cs)

    # A literal (glob-free) pattern also matches by resolved identity, which is
    # what makes a rule written as `docs/../.env` — or one written inside a
    # symlinked checkout — line up with the resolved value.
    if not _has_glob(pat) and resolve_path(pat, root) == abs_real:
        return True

    if "/" not in pat:
        base = abs_path.rsplit("/", 1)[-1]
        if _core_match(pat, base, False, cs):
            return True

    if rel_path is not None and _core_match(pat, rel_path, True, cs):
        return True
    if _core_match(pat, abs_path, False, cs):
        return True
    # Last: the rule's own literal head, resolved the same way the value was —
    # this is what makes a rule written against a symlinked directory inside the
    # project agree with the resolved value it is judging.
    canon = _memo_resolved_pattern(pat, root)
    return bool(canon) and _core_match(canon, abs_path, False, cs)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

# Characters that may follow a `Tool(prefix:*)` prefix. Requiring a boundary is
# what stops `Bash(git status:*)` from authorizing `git statuses-are-lies`; the
# shell operators are here so `git status&&x` is still recognized as a prefix
# match (the compound gate in permissions.py is what then refuses it).
_PREFIX_BOUNDARY = frozenset(" \t\r\n;&|<>()'\"")


def _prefix_matches(prefix: str, value: str) -> bool:
    if _has_glob(prefix):
        # `Bash(curl *:*)` — the prefix is itself a glob; anything may follow.
        return fnmatch.fnmatchcase(value, prefix if prefix.endswith("*") else prefix + "*")
    if not value.startswith(prefix):
        return False
    rest = value[len(prefix):]
    return rest == "" or rest[0] in _PREFIX_BOUNDARY


def _scalar(value: Any) -> Optional[str]:
    """The string a rule is compared against, mirroring ``_match_targets``'s
    treatment of scalars. Non-scalars never match — a rule must not be satisfied
    by the repr of a list."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return str(value)
    return None


def _value_matches(rule: CompiledRule, value: str, project_root: Optional[str]) -> bool:
    if rule.matcher == "regex":
        pat = _compiled_regex(rule.value)
        return pat is not None and pat.search(value) is not None
    if rule.matcher == "prefix":
        return _prefix_matches(rule.value, value)
    if rule.is_path:
        return path_matches(rule.value, value, project_root)
    if rule.matcher == "exact":
        return value == rule.value
    return fnmatch.fnmatchcase(value, rule.value)


def rule_matches(
    rule: CompiledRule,
    tool_name: str,
    input: dict,
    *,
    tool: Any = None,
    project_root: Optional[str] = None,
) -> Optional[RuleMatch]:
    """Test one compiled rule against a call. ``None`` when it does not fire.

    ``tool`` (the ``Tool`` object, when the caller has it) is only needed to
    bind a positional rule whose tool is outside the built-in table. Passing the
    name alone is fine for every built-in.
    """
    if rule.matcher == "legacy":
        # Byte-for-byte today's `_rule_matches`: exact tool_name comparison, no
        # alias folding, whole-string fnmatch over every match target.
        if rule.tool is not None and rule.tool != tool_name:
            return None
        targets = _match_targets(input if isinstance(input, dict) else {})
        if rule.legacy_regex:
            pat = _compiled_regex(rule.value)
            if pat is None:
                return None
            for t in targets:
                if pat.search(t) is not None:
                    return RuleMatch(rule, "", t)
            return None
        for t in targets:
            if fnmatch.fnmatchcase(t, rule.value):
                return RuleMatch(rule, "", t)
        return None

    if rule.tool is not None and rule.tool != canonical_tool_name(tool_name):
        return None
    if rule.matcher == "tool":
        return RuleMatch(rule, "", "")

    bound = rule
    if rule.needs_binding:
        bound = bind_rule(rule, tool if tool is not None else tool_name)
    value = _scalar((input or {}).get(bound.param))
    if value is None:
        return None
    if _value_matches(bound, value, project_root):
        return RuleMatch(bound, bound.param or "", value)
    return None


# ---------------------------------------------------------------------------
# Specificity
# ---------------------------------------------------------------------------

# Higher wins. exact > prefix > glob > regex > bare tool > legacy: a rule that
# spells the value out is a stronger statement about intent than one that
# wildcards it, and a legacy flat pattern is the weakest because it may have
# matched some unrelated field.
_MATCHER_RANK = {"legacy": 0, "tool": 1, "regex": 2, "glob": 3, "prefix": 4, "exact": 5}


def _literal_prefix_len(value: str) -> int:
    for i, ch in enumerate(value):
        if ch in _GLOB_META:
            return i
    return len(value)


def specificity(rule: CompiledRule) -> tuple:
    """Sort key for "most specific rule wins" *within* a precedence category.

    Ordered by the plan's rules: an explicit parameter outranks a positional
    one, then the matcher form, then the longer literal prefix, then the longer
    value. Ties keep the caller's order, so a rule set stays deterministic.
    """
    return (
        1 if rule.explicit_param else 0,
        _MATCHER_RANK.get(rule.matcher, 0),
        _literal_prefix_len(rule.value),
        len(rule.value),
    )


def best_match(
    rules,
    tool_name: str,
    input: dict,
    *,
    tool: Any = None,
    project_root: Optional[str] = None,
) -> Optional[RuleMatch]:
    """The most specific rule that fires, or ``None``.

    Deliberately not "the first that fires": a broad ``Bash(git:*)`` listed
    before a narrow ``Bash(git push:*)`` must not be what explains the decision,
    or "why did this fire" has no honest answer.
    """
    best: Optional[RuleMatch] = None
    best_key: Optional[tuple] = None
    for rule in rules:
        hit = rule_matches(rule, tool_name, input, tool=tool, project_root=project_root)
        if hit is None:
            continue
        key = specificity(hit.rule)
        if best_key is None or key > best_key:
            best, best_key = hit, key
    return best
