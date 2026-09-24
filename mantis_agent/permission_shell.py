"""Conservative shell decomposition for the permission layer.

The permission matcher used to treat a shell tool call as ONE opaque string:
an allow rule of ``git status*`` whole-string globs the command, and because a
glob matches from the LEFT, ``git status && cat ~/.ssh/id_rsa`` was authorized
by it — the appended command rode in for free on the approved prefix.

This module splits a command into the pieces a shell would actually run, so the
permission layer can require EVERY piece to be independently allowed. It is
deliberately a *conservative* recognizer, not a shell implementation: whenever
the text is not fully resolvable — command substitution, ``eval``, a variable in
command position, unbalanced quotes — it reports ``confident=False`` and the
caller must refuse to auto-allow rather than trust a partial parse. Failing
closed on an unparseable command is the whole point; a partial parse must never
be treated as a full one.

Everything here is pure and side-effect free, so :func:`decompose` is cached.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

__all__ = ["ShellDecomposition", "ShellSegment", "classify_bash_readonly", "decompose"]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShellSegment:
    """One command the shell would run as its own unit.

    ``argv`` is the word list with quoting/escaping resolved and redirections
    removed; ``raw`` is the source text of the segment (a contiguous slice of
    the original command, whitespace-stripped) — that is what rule patterns are
    matched against, so a segment can never be "laundered" through the parser.
    ``operator`` is the control operator that JOINED this segment to the one
    before it (``""`` for the first): one of ``&& || ; | &``. ``in_subshell``
    marks segments that came from inside a GROUPING construct — ``( ... )``,
    ``{ ...; }``, or the body of a substitution (``$(...)``, backticks,
    ``<(...)``/``>(...)``) — i.e. anything the outer command line only *contains*
    rather than runs directly. ``redirects`` holds the
    redirections as written (``">out.txt"``, ``"2>/dev/null"``) so a caller can
    police redirect targets separately from the command itself.
    """

    argv: tuple[str, ...]
    raw: str
    operator: str = ""
    in_subshell: bool = False
    redirects: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShellDecomposition:
    """Segments plus an honest statement of how much we understood.

    ``confident`` is False when ANY part of the command resisted parsing. The
    permission layer treats that as "no allow rule may fire" — the segments are
    still exposed (they remain useful for *denying* and for risk classification)
    but they must not be used to justify running something.
    """

    segments: tuple[ShellSegment, ...]
    confident: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Command wrappers that run *another* command. We keep the wrapper segment AND
# expose the wrapped command as its own segment, so both face the rule set:
# `sudo git status` must satisfy a rule for the sudo form, not just for the
# inner `git status`. Decomposition adds precision here, never permissiveness.
_WRAPPERS = frozenset({"sudo", "doas", "pkexec", "env", "nice", "nohup", "time"})

# Commands whose real argv is decided at runtime. There is no honest static
# answer for these, so they poison confidence outright.
_OPAQUE = frozenset({"eval", "xargs"})

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A redirect token: optional fd number (or `&`) followed by a redirection op.
_REDIR_RE = re.compile(r"^(\d*|&)(<<<|<<|<&|<|>>|>&|>\||>)(.*)$", re.DOTALL)

_WS = " \t\r\n"
# Guard against pathological nesting (subshell inside wrapper inside subshell…).
_MAX_DEPTH = 6

# Reserved words that may sit in front of a `( … )` / `{ …; }` group without
# changing what the group runs. `!` negates the exit status, `coproc` runs it
# asynchronously, `time` reports its duration — none of them alter the commands
# inside, so the group behind them must still be decomposed. Matched only when a
# group actually follows; see the peel in `_analyze`.
_GROUP_PREFIX_RE = re.compile(r"^(?:!|coproc|time)\s*(?=[({])")


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tok:
    """One shell word, plus the provenance the analyzer needs."""

    value: str          # quoting/escaping resolved
    start: int          # offset of the word in the segment text
    dollar: bool        # contains an *unquoted* `$` expansion
    redirect: bool      # the word STARTS a redirection (`>x`, `2>x`, `<f`)
    bad_redirect: bool  # unquoted `<`/`>` in the middle of a word (`a>b`)


def _tokenize(text: str) -> tuple[list[_Tok], list[str]]:
    """Split ``text`` into words. Returns (tokens, reasons-we-are-unsure)."""

    toks: list[_Tok] = []
    reasons: list[str] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in _WS:
            i += 1
        if i >= n:
            break
        start = i
        buf: list[str] = []
        dollar = False
        saw_redir = False
        redir_leads = False  # the redirect sits at the head of the word
        while i < n and text[i] not in _WS:
            c = text[i]
            if c == "\\":
                # Backslash escapes the next character (and hides it from every
                # classifier below — `\>` is a literal, not a redirection).
                if i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                else:
                    reasons.append("trailing backslash")
                    i += 1
                continue
            if c == "'":
                j = text.find("'", i + 1)
                if j < 0:
                    reasons.append("unbalanced single quote")
                    buf.append(text[i + 1 :])
                    i = n
                    break
                buf.append(text[i + 1 : j])
                i = j + 1
                continue
            if c == '"':
                i += 1
                closed = False
                while i < n:
                    d = text[i]
                    if d == "\\" and i + 1 < n and text[i + 1] in '"\\$`\n':
                        buf.append(text[i + 1])
                        i += 2
                        continue
                    if d == '"':
                        i += 1
                        closed = True
                        break
                    if d == "$":
                        # `"$x"` still expands — quoting does not make it known.
                        dollar = True
                    buf.append(d)
                    i += 1
                if not closed:
                    reasons.append("unbalanced double quote")
                continue
            if c == "$":
                dollar = True
            if c in "<>" and not saw_redir:
                saw_redir = True
                # `>f`, `2>f`, `&>f` redirect; `a>b` is a word with a redirect
                # glued to it — real bash splits that, we refuse to guess.
                redir_leads = all(ch.isdigit() for ch in buf) or buf == ["&"]
            buf.append(c)
            i += 1
        toks.append(
            _Tok(
                value="".join(buf),
                start=start,
                dollar=dollar,
                redirect=saw_redir and redir_leads,
                bad_redirect=saw_redir and not redir_leads,
            )
        )
    return toks, reasons


# ---------------------------------------------------------------------------
# Top-level splitter
# ---------------------------------------------------------------------------


def _is_word_start(text: str, i: int) -> bool:
    return i == 0 or text[i - 1] in _WS + ";&|("


def _split_top_level(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split on UNQUOTED control operators at nesting depth zero.

    Returns ``([(raw, preceding_operator), ...], reasons)``. Quotes, backslash
    escapes and `(`/`{`/`$(`/backtick nesting are all tracked so an operator
    inside them never splits — and so an UNBALANCED one is reported, because a
    stray `{` would otherwise swallow the rest of the command into a single
    segment that still matches a left-anchored allow glob.
    """

    parts: list[tuple[str, str]] = []
    reasons: list[str] = []
    stack: list[str] = []
    i, n = 0, len(text)
    seg_start = 0
    op = ""
    single = double = False

    def cut(end: int, next_op: str) -> None:
        nonlocal op
        parts.append((text[seg_start:end], op))
        op = next_op

    while i < n:
        c = text[i]
        if single:
            if c == "'":
                single = False
            i += 1
            continue
        if double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                double = False
            i += 1
            continue
        if c == "\\":
            i += 2 if i + 1 < n else 1
            continue
        if c == "'":
            single = True
            i += 1
            continue
        if c == '"':
            double = True
            i += 1
            continue
        if c == "`":
            reasons.append("command substitution")
            j = text.find("`", i + 1)
            if j < 0:
                reasons.append("unbalanced backtick")
                break
            i = j + 1
            continue
        if c == "$" and text[i + 1 : i + 2] == "(":
            reasons.append(
                "arithmetic expansion" if text[i + 2 : i + 3] == "(" else "command substitution"
            )
            stack.append("(")
            i += 2
            continue
        if c == "(":
            stack.append("(")
            i += 1
            continue
        if c == ")":
            if stack and stack[-1] == "(":
                stack.pop()
            else:
                reasons.append("unbalanced parenthesis")
            i += 1
            continue
        # `{` only opens a group when it stands alone as a word (`{ a; b; }`);
        # `${VAR}` and `{a,b}` brace expansion are ordinary word characters.
        if c == "{" and _is_word_start(text, i) and text[i + 1 : i + 2] in ("", " ", "\t", "\n"):
            stack.append("{")
            i += 1
            continue
        # …and a `}` only CLOSES one when it stands alone as a word too. bash
        # requires `{ list; }` — the closer follows `;`/`&`/newline/space. The
        # `}` of a `{a,b}` brace expansion is glued to the word before it, so it
        # must not pop the group and hand the rest of the line back to the
        # top level (where a `;` would then split mid-group).
        if c == "}" and stack and stack[-1] == "{" and _is_word_start(text, i):
            stack.pop()
            i += 1
            continue
        if stack:
            i += 1
            continue
        two = text[i : i + 2]
        if two in ("&&", "||"):
            cut(i, two)
            i += 2
            seg_start = i
            continue
        if two == "|&":  # bash's "pipe stdout+stderr" — a pipe for our purposes
            cut(i, "|")
            i += 2
            seg_start = i
            continue
        if two == ";;":  # `case` arm terminator — a separator all the same
            cut(i, ";")
            i += 2
            seg_start = i
            continue
        # `>&`, `2>&1`, `<&-`, `>|` — a `&`/`|` glued to a redirection belongs
        # to the redirection, not to the operator grammar.
        after_redir = i > 0 and text[i - 1] in "<>"
        if c == "&":
            if two == "&>" or after_redir:  # a redirection, not "background"
                i += 2 if two == "&>" else 1
                continue
            cut(i, "&")
            i += 1
            seg_start = i
            continue
        if c == "|":
            if after_redir:  # `>|` clobber redirection
                i += 1
                continue
            cut(i, "|")
            i += 1
            seg_start = i
            continue
        if c in ";\n":  # a newline separates commands exactly like `;`
            cut(i, ";")
            i += 1
            seg_start = i
            continue
        i += 1

    parts.append((text[seg_start:], op))
    if single or double:
        reasons.append("unbalanced quote")
    if stack:
        reasons.append("unbalanced group")
    return parts, reasons


# ---------------------------------------------------------------------------
# Segment analysis
# ---------------------------------------------------------------------------


def _match_paren(body: str) -> int:
    """Index of the `)` closing ``body[0] == "("``, or -1 when unbalanced."""
    depth = 0
    i, n = 0, len(body)
    single = double = False
    while i < n:
        c = body[i]
        if single:
            if c == "'":
                single = False
        elif double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                double = False
        elif c == "\\":
            i += 2 if i + 1 < n else 1
            continue
        elif c == "'":
            single = True
        elif c == '"':
            double = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _match_brace(body: str) -> int:
    """Index of the `}` closing ``body[0] == "{"``, or -1 when unbalanced.

    Mirrors :func:`_split_top_level`'s group rules: `{` opens a group only as a
    standalone word and `}` closes one only as a standalone word, so neither
    ``${VAR}`` nor ``{a,b}`` brace expansion is mistaken for the group.
    """
    depth = 0
    i, n = 0, len(body)
    single = double = False
    while i < n:
        c = body[i]
        if single:
            if c == "'":
                single = False
        elif double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                double = False
        elif c == "\\":
            i += 2 if i + 1 < n else 1
            continue
        elif c == "'":
            single = True
        elif c == '"':
            double = True
        elif c == "(":
            # Skip a nested `( ... )` / `$( ... )` wholesale — a `}` inside it
            # belongs to that construct, not to this group.
            end = _match_paren(body[i:])
            if end < 0:
                return -1
            i += end + 1
            continue
        elif c == "{" and _is_word_start(body, i) and body[i + 1 : i + 2] in ("", " ", "\t", "\n"):
            depth += 1
        elif c == "}" and depth and _is_word_start(body, i):
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _embedded_commands(text: str) -> tuple[list[str], list[str]]:
    """Command bodies EMBEDDED in ``text``: ``$(...)``, backticks, ``<(...)``
    and ``>(...)``. Returns ``(inner_texts, reasons)``.

    These all run a command that the surrounding word never names, which is why
    they poison confidence — but they must also be handed back so the caller can
    check (and above all *deny*) what they actually run. ``git status
    <(cat ~/.ssh/id_rsa)`` is two commands wearing one command's clothes.

    Only THIS level is scanned; nesting is handled by recursing on the returned
    bodies. Single quotes suppress every form, double quotes suppress only the
    process-substitution forms — inside ``"..."`` a ``$( )`` still expands.
    """

    inners: list[str] = []
    reasons: list[str] = []
    i, n = 0, len(text)
    double = False

    def span(open_at: int) -> int:
        """End index (exclusive) of the `( ... )` starting at ``open_at``."""
        end = _match_paren(text[open_at:])
        return -1 if end < 0 else open_at + end + 1

    while i < n:
        c = text[i]
        if c == "\\":
            i += 2 if i + 1 < n else 1
            continue
        if c == "'" and not double:
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            double = not double
            i += 1
            continue
        if c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                reasons.append("unbalanced backtick")
                break
            reasons.append("command substitution")
            inners.append(text[i + 1 : j])
            i = j + 1
            continue
        if c == "$" and text[i + 1 : i + 2] == "(":
            end = span(i + 1)
            if end < 0:
                reasons.append("unbalanced parenthesis")
                break
            if text[i + 2 : i + 3] == "(":
                reasons.append("arithmetic expansion")  # `$(( ))` runs nothing
            else:
                reasons.append("command substitution")
                inners.append(text[i + 2 : end - 1])
            i = end
            continue
        if c in "<>" and text[i + 1 : i + 2] == "(" and not double:
            end = span(i + 1)
            if end < 0:
                reasons.append("unbalanced parenthesis")
                break
            reasons.append("process substitution")
            inners.append(text[i + 2 : end - 1])
            i = end
            continue
        i += 1
    return inners, reasons


def _embedded_segments(text: str, depth: int) -> tuple[list[ShellSegment], list[str]]:
    """:func:`_embedded_commands`, decomposed into segments of their own."""
    inners, reasons = _embedded_commands(text)
    segs: list[ShellSegment] = []
    for inner in inners:
        isegs, ireasons = _decompose_inner(inner, depth + 1)
        segs.extend(replace(s, in_subshell=True) for s in isegs)
        reasons.extend(ireasons)
    return segs, reasons


def _split_redirects(toks: list[_Tok]) -> tuple[list[_Tok], tuple[str, ...], list[str]]:
    """Peel redirections off a word list. Returns (words, redirects, reasons)."""

    words: list[_Tok] = []
    redirects: list[str] = []
    reasons: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.redirect:
            m = _REDIR_RE.match(t.value)
            if m is None:  # pragma: no cover — _Tok.redirect implies a match
                reasons.append("unparsed redirection")
                i += 1
                continue
            fd, op, target = m.group(1), m.group(2), m.group(3)
            if op in ("<<", "<<<"):
                # A heredoc body follows on later LINES; our newline split would
                # shred it into bogus segments. Refuse to claim we parsed it.
                reasons.append("heredoc")
            if not target and i + 1 < len(toks):
                i += 1
                target = toks[i].value
            redirects.append(f"{fd}{op}{target}")
        else:
            words.append(t)
        i += 1
    return words, tuple(redirects), reasons


def _wrapped_index(words: list[_Tok]) -> tuple[int | None, list[str]]:
    """Index of the command a leading wrapper (``sudo``/``env``/…) runs.

    Skips the wrapper's own options (``-n`` and a bare numeric argument for
    ``nice``). A ``VAR=val`` before the command means the environment is being
    rewritten — that is unresolvable, so it becomes a confidence failure rather
    than a wrapped segment.
    """

    j = 1
    while j < len(words):
        v = words[j].value
        if v.startswith("-") or v.isdigit():
            j += 1
            continue
        if _ASSIGN_RE.match(v):
            return None, ["variable assignment before command"]
        return j, []
    return None, ["wrapper with no command"]


def _analyze(
    raw: str, operator: str, in_subshell: bool, depth: int
) -> tuple[list[ShellSegment], list[str]]:
    """Turn one split part into segments (possibly several: groups, embedded
    substitutions and wrapper unwrapping all expand). Returns (segments,
    reasons)."""

    body = raw.strip()
    if not body:
        return [], []
    if depth > _MAX_DEPTH:
        return [ShellSegment((), body, operator, in_subshell, ())], ["nesting too deep"]

    # A grouping construct: `( ... )` subshell or `{ ...; }` brace group. Both
    # run their contents as ordinary commands, so both are recursed into — a
    # group must never be a wrapper that hides what is inside it from the rules.
    # (`{ a; b; }` used to be swallowed whole, which let the group ride in on a
    # rule written for `a` alone.)
    # `!`, `coproc` and `time` may PREFIX a grouping construct without changing
    # what the group runs. They have to be peeled before the group is detected,
    # or `! { a; b; }` never matches the `startswith("{")` test below, collapses
    # into a single opaque segment, and rides in on a rule written for `a` alone.
    # Only peel when a group actually follows: `time foo` is an ordinary wrapper
    # and is handled by the wrapper unwrapping further down, which emits both the
    # wrapped and unwrapped forms. Peeling it here would drop the wrapper segment.
    prefix = _GROUP_PREFIX_RE.match(body)
    if prefix is not None:
        return _analyze(body[prefix.end() :], operator, in_subshell, depth + 1)

    grouped: tuple[str, str] | None = None
    if body.startswith("("):
        grouped = ("subshell", "unbalanced parenthesis")
    elif body.startswith("{") and body[1:2] in ("", " ", "\t", "\n"):
        grouped = ("group", "unbalanced group")
    if grouped is not None:
        what, unbalanced = grouped
        end = _match_paren(body) if what == "subshell" else _match_brace(body)
        if end < 0:
            return [ShellSegment((), body, operator, in_subshell, ())], [unbalanced]
        inner, reasons = _decompose_inner(body[1:end], depth + 1)
        segs = [
            replace(s, in_subshell=True, operator=operator if k == 0 else s.operator)
            for k, s in enumerate(inner)
        ]
        trailing = body[end + 1 :].strip()
        if trailing:
            last = len(segs) - 1  # before embedded bodies are appended below
            esegs, ereasons = _embedded_segments(trailing, depth)
            segs.extend(esegs)
            reasons.extend(ereasons)
            ttoks, treasons = _tokenize(trailing)
            reasons.extend(treasons)
            twords, tredirs, tred_reasons = _split_redirects(ttoks)
            reasons.extend(tred_reasons)
            if twords:
                reasons.append(f"text after {what}")
            elif last >= 0 and tredirs:
                segs[last] = replace(segs[last], redirects=segs[last].redirects + tredirs)
        if not segs:
            return [ShellSegment((), body, operator, in_subshell, ())], reasons or [f"empty {what}"]
        return segs, reasons

    toks, reasons = _tokenize(body)
    words, redirects, red_reasons = _split_redirects(toks)
    reasons.extend(red_reasons)
    if any(t.bad_redirect for t in words):
        reasons.append("ambiguous redirection")

    seg = ShellSegment(
        argv=tuple(t.value for t in words),
        raw=body,
        operator=operator,
        in_subshell=in_subshell,
        redirects=redirects,
    )
    segs = [seg]

    # `$(...)`, backticks and `<(...)`/`>(...)` embed whole commands inside a
    # word. They cost confidence (the word's own argv is no longer knowable) AND
    # they contribute segments, so a deny rule reaches what they really run.
    esegs, ereasons = _embedded_segments(body, depth)
    segs.extend(esegs)
    reasons.extend(ereasons)

    if not words:
        reasons.append("no command word")
        return segs, reasons

    head = words[0]
    if head.dollar:
        # `$CMD args` / `"$CMD" args` — the command is decided at runtime.
        reasons.append("variable in command position")
    elif _ASSIGN_RE.match(head.value):
        reasons.append("variable assignment before command")
    elif head.value in _OPAQUE:
        reasons.append(f"{head.value} runs a command we cannot resolve")
    elif head.value in _WRAPPERS:
        idx, wreasons = _wrapped_index(words)
        reasons.extend(wreasons)
        if idx is not None:
            wsegs, wreasons2 = _analyze(
                body[words[idx].start :], operator, in_subshell, depth + 1
            )
            segs.extend(wsegs)
            reasons.extend(wreasons2)
    return segs, reasons


def _decompose_inner(command: str, depth: int) -> tuple[list[ShellSegment], list[str]]:
    parts, reasons = _split_top_level(command)
    segments: list[ShellSegment] = []
    for raw, op in parts:
        segs, rs = _analyze(raw, op, False, depth)
        segments.extend(segs)
        reasons.extend(rs)
    return segments, reasons


@functools.lru_cache(maxsize=256)
def decompose(command: str) -> ShellDecomposition:
    """Decompose ``command`` into independently-checkable segments.

    ``confident=False`` (with a human-readable ``reason``) means the parse is
    incomplete — callers MUST NOT let an allow rule fire on the strength of
    these segments. They may still use them to deny or to classify risk, since
    both of those directions fail safe.
    """

    text = command or ""
    segments, reasons = _decompose_inner(text, 0)
    if text.strip() and not segments:
        reasons.append("no command found")
    # De-duplicate reasons, preserving order, so the message stays short.
    seen: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return ShellDecomposition(
        segments=tuple(segments),
        confident=not seen,
        reason="; ".join(seen[:3]),
    )


# ---------------------------------------------------------------------------
# Read-only classification
# ---------------------------------------------------------------------------
#
# Asking for every `ls` / `cat` / `git status` trains people to flip the whole
# session to allow-all. This recognizer names the commands that only READ, so
# the permission layer can let them through in default and plan mode while
# every mutation still asks. It is an allowlist over the decomposer above and
# inherits its fail-closed posture: anything it is not sure about is False.
#
# Two classes of command:
#
# * ``_RO_ANY_ARGS`` — no argument can make them write or run something else,
#   so a `$VAR`, a `$(…)` whose body is itself read-only, or a glob in their
#   arguments cannot change the answer.
# * everything in ``_RO_CHECKERS`` — read-only only for SOME argument shapes
#   (`find` without `-delete`, `git branch` without `-D`, `sort` without `-o`).
#   Their arguments are vetted literally, so an expansion (`find . $FLAGS`) or
#   an unquoted glob (`find *` next to a file named `-delete`) could smuggle in
#   the very flag the checker refuses — those are rejected outright.
#
# Reading is not harmless everywhere: `cat ~/.ssh/id_rsa` is read-only and is
# exactly what a prompt-injected model wants. So a command that takes PATHS is
# auto-read-only only inside the working tree: no absolute or `~` path, no
# `..` component, no `$VAR`/`$(…)` that could point anywhere (including via an
# input redirect). `_RO_NO_PATHS` commands never open a file and are exempt.

_RO_NO_PATHS = frozenset({
    "echo", "printf", "which", "type", "uname", "whoami", "id", "printenv",
    "true", "false", "tr", "basename", "dirname", "pwd", "date", "hostname",
    "env", "python", "python3", "node",
})

_RO_ANY_ARGS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "stat", "du", "df", "which",
    "type", "echo", "printf", "grep", "egrep", "fgrep", "diff", "cmp",
    "basename", "dirname", "realpath", "readlink", "uname", "whoami", "id",
    "true", "false", "tr", "nl", "od", "cd",
})

# Plain stateless flag checks: {command: (forbidden exact args, forbidden
# prefixes, short letters that may not appear in a `-abc` cluster)}.
_FLAG_RULES: dict[str, tuple[frozenset[str], tuple[str, ...], str]] = {
    "find": (
        frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls",
                   "-fprint", "-fprint0", "-fprintf"}),
        (), "",
    ),
    "sort": (frozenset(), ("--output", "--compress-program"), "o"),
    # `-o FILE` writes; `-R` re-runs tree with `-o 00Tree.html` in every dir.
    # tree parses short clusters, so `-ao out` is `-a -o out`.
    "tree": (frozenset({"-o", "-R"}), ("--output", "-o"), "oR"),
    "file": (frozenset({"-C"}), ("--compile",), "C"),
    # `--pre CMD` runs a preprocessor, `--hostname-bin CMD` runs a program.
    "rg": (frozenset(), ("--pre", "--hostname-bin"), ""),
    "ag": (frozenset(), ("--pager",), ""),
    "jq": (frozenset({"-i"}), ("--in-place",), ""),
}

_SUBST_REASONS = frozenset({"command substitution", "process substitution"})


def _has_unquoted(text: str, chars: str) -> bool:
    """Does ``text`` carry any of ``chars`` outside single/double quotes and
    not backslash-escaped? Used to spot a live glob or expansion."""
    i, n = 0, len(text)
    single = double = False
    while i < n:
        c = text[i]
        if single:
            if c == "'":
                single = False
        elif c == "\\":
            i += 2
            continue
        elif c == '"':
            double = not double
        elif c == "'" and not double:
            single = True
        elif c in chars and not double:
            return True
        elif c in "$`" and c in chars:  # expansions still fire inside "…"
            return True
        i += 1
    return False


def _abbreviates(arg: str, longs: Iterable[str]) -> bool:
    """Is ``arg`` a (possibly abbreviated) spelling of one of ``longs``?

    getopt_long and git's parse-options accept any unambiguous PREFIX of a long
    option — ``sort --out=x`` is ``--output=x``, ``git grep --open=cmd`` is
    ``--open-files-in-pager``. A literal ``startswith`` check misses those."""
    if not arg.startswith("--") or len(arg) < 3:
        return False
    name = arg.split("=", 1)[0]
    return any(lo.startswith(name) or name.startswith(lo) for lo in longs)


def _flags_ok(cmd: str, args: tuple[str, ...]) -> bool:
    exact, prefixes, letters = _FLAG_RULES[cmd]
    longs = [p for p in prefixes if p.startswith("--")]
    for a in args:
        if a == "--":
            break
        if a in exact or any(a.startswith(p) for p in prefixes) or _abbreviates(a, longs):
            return False
        if letters and a.startswith("-") and not a.startswith("--"):
            if any(ch in a[1:] for ch in letters):
                return False
    return True


def _uniq_ok(args: tuple[str, ...]) -> bool:
    # `uniq IN OUT` writes OUT. One positional at most (conservatively counting
    # an option's value as a positional, so `uniq -f 1 x` is refused).
    return sum(1 for a in args if not a.startswith("-")) <= 1


_DATE_FLAGS = frozenset({"-u", "--utc", "--universal", "-R", "--rfc-email", "-I"})


def _date_ok(args: tuple[str, ...]) -> bool:
    # An allowlist: `--set`/`-s` (and its abbreviation `--se`) set the clock,
    # `date MMDDhhmm` sets it too, and GNU `--file=F` / `-f F` READS F (echoing
    # every line back in its "invalid date" errors) — date is exempt from the
    # path check, so it may only format the current time.
    return all(
        a.startswith("+") or a in _DATE_FLAGS
        or a.startswith(("--iso-8601", "--rfc-3339=", "-I"))
        for a in args
    )


_HOSTNAME_FLAGS = frozenset({
    "-f", "-s", "-d", "-i", "-I", "-a", "-A", "--fqdn", "--long", "--short",
    "--domain", "--ip-address", "--all-ip-addresses", "--all-fqdns",
})


def _version_only(args: tuple[str, ...]) -> bool:
    return args in (("--version",), ("-V",), ("-v",))


# git: global options that are safe to skip (and whether they take a value).
_GIT_SAFE_GLOBALS = {"-C": True, "--no-pager": False, "-P": False,
                     "--no-optional-locks": False, "--literal-pathspecs": False}
_GIT_RO_ANY = frozenset({
    "status", "log", "show", "diff", "rev-parse", "ls-files", "ls-tree",
    "blame", "describe", "shortlog", "cat-file", "rev-list", "merge-base",
    "show-ref", "whatchanged", "count-objects", "version", "grep", "diff-tree",
    "annotate",
})
_GIT_BRANCH_BAD = frozenset({
    "-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "--delete", "--move",
    "--copy", "--force", "--set-upstream-to", "--unset-upstream",
    "--edit-description", "--track", "--no-track",
})
_GIT_BRANCH_BAD_LONG = tuple(a for a in _GIT_BRANCH_BAD if a.startswith("--")) + (
    "--create-reflog", "--recurse-submodules")
_GIT_BRANCH_LIST = frozenset({"-l", "--list", "-a", "--all", "-r", "--remotes",
                              "--contains", "--no-contains", "--merged",
                              "--no-merged", "--points-at"})
_GIT_CONFIG_BAD = ("--unset", "--add", "--replace-all", "--edit", "-e",
                   "--rename-section", "--remove-section", "--unset-all")
_GIT_CONFIG_BAD_LONG = tuple(a for a in _GIT_CONFIG_BAD if a.startswith("--"))
_GIT_CONFIG_MODIFIERS = frozenset({
    "--global", "--system", "--local", "--worktree", "--show-origin",
    "--show-scope", "--name-only", "-z", "--null", "--includes", "--no-includes",
})
_GIT_CONFIG_READ = frozenset({"--get", "--get-all", "--get-regexp", "--list",
                              "-l", "--get-urlmatch", "--get-color",
                              "--get-colorbool"})


def _git_ok(args: tuple[str, ...]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        takes_value = _GIT_SAFE_GLOBALS.get(args[i])
        if takes_value is None:
            # `-c core.pager=…`, `--exec-path`, `--git-dir` … can make a read
            # command run arbitrary programs. Unknown ⇒ refuse.
            return False
        i += 2 if takes_value else 1
    if i >= len(args):
        return False
    sub, rest = args[i], args[i + 1:]
    # Any `--output` writes a file; `-O` / `--open-files-in-pager` (git grep)
    # runs a program; `--ext-diff` hands the diff to a configured program.
    # parse-options accepts unambiguous prefixes (`git grep --open=CMD`), and
    # `-O` takes its argument glued into a short cluster (`git grep -iOCMD`).
    for a in rest:
        if a.startswith(("--output", "--open-files-in-pager", "--ext-diff")) or a == "-O":
            return False
        if _abbreviates(a, ("--output", "--open-files-in-pager", "--ext-diff")):
            return False
        if sub == "grep" and a.startswith("-") and not a.startswith("--") and "O" in a:
            return False
    if sub in _GIT_RO_ANY:
        return True
    if sub == "branch":
        if any(a in _GIT_BRANCH_BAD or a.startswith(("--set-upstream", "--delete",
                                                        "--move", "--copy"))
               or _abbreviates(a, _GIT_BRANCH_BAD_LONG)
               for a in rest):
            return False
        for a in rest:
            if a.startswith("-") and not a.startswith("--") and len(a) > 2:
                if any(ch in a[1:] for ch in "dDmMcCfu"):
                    return False
        positionals = [a for a in rest if not a.startswith("-")]
        # `git branch NAME` CREATES a branch; positionals are only patterns
        # when a listing flag is present.
        return not positionals or any(a in _GIT_BRANCH_LIST for a in rest)
    if sub == "tag":
        # Bare `git tag` lists; `git tag v1` creates. Only an explicit list.
        if not rest:
            return True
        if not any(a in ("-l", "--list") for a in rest):
            return False
        bad = ("--delete", "--annotate", "--sign", "--force", "--message",
               "--file", "--local-user", "--edit", "--trailer")
        return not any(a in ("-d", "-a", "-s", "-f", "-m", "-F", "-u", "-e")
                       or _abbreviates(a, bad) for a in rest)
    if sub == "remote":
        if not rest or rest in (("-v",), ("--verbose",)):
            return True
        if not (rest[0] in ("show", "get-url") or rest[:2] in (("-v", "show"),)):
            return False
        # `git remote show URL` contacts an arbitrary URL — a free network
        # channel for whatever the model puts in the path. Named remotes only.
        return not any(ch in a for a in rest for ch in ":/@\\")
    if sub == "config":
        if any(a.startswith(_GIT_CONFIG_BAD) or _abbreviates(a, _GIT_CONFIG_BAD_LONG)
               for a in rest):
            return False
        # The ACTION must come first (after file/display options). A read flag
        # anywhere else can be a VALUE: `git config set a.b --get` and the
        # legacy `git config a.b c --get` both WRITE `a.b`.
        j = 0
        while j < len(rest) and rest[j] in _GIT_CONFIG_MODIFIERS:
            j += 1
        if j >= len(rest):
            return False
        return rest[j] in ("get", "list") or rest[j] in _GIT_CONFIG_READ
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if sub == "reflog":
        return not rest or rest[0] == "show" or rest[0].startswith("-")
    return False


def _env_ok(args: tuple[str, ...]) -> bool:
    # `env X=1 cmd` / `env cmd` RUN cmd, and bare `env` dumps every API key in
    # the environment into the model's context — never auto-allowed.
    return False


_RO_CHECKERS: dict[str, Callable[[tuple[str, ...]], bool]] = {
    **{c: functools.partial(_flags_ok, c) for c in _FLAG_RULES},
    "uniq": _uniq_ok,
    "date": _date_ok,
    "hostname": lambda a: all(x in _HOSTNAME_FLAGS for x in a),
    "git": _git_ok,
    "env": _env_ok,
    "python": _version_only,
    "python3": _version_only,
    "node": _version_only,
}


def _dev_null_only(seg: ShellSegment) -> bool:
    """True when every OUTPUT redirect on ``seg`` goes to /dev/null or merely
    dups a descriptor (``2>&1``). Input redirects (``<file``) only read."""
    for redir in seg.redirects:
        op = redir.lstrip("0123456789")
        if op.startswith("&>"):
            target = op.lstrip("&>").strip()
        elif op.startswith(">"):
            target = op.lstrip(">|").strip()
            if target.startswith("&"):
                fd = target[1:].strip()
                if fd == "-" or fd.rstrip("-").isdigit():
                    continue
                target = fd
        elif op.startswith("<>"):
            target = op[2:].strip()  # opens read-WRITE, creating the file
        else:
            continue  # `<in.txt`
        if target != "/dev/null":
            return False
    return True


# `-[A-Za-z0-9]*` covers a value glued onto a short CLUSTER (`grep -rf/etc/x`).
_OUT_OF_TREE_RE = re.compile(r"(?:^|=|^-[A-Za-z0-9]*)[/~]|(?:^|[=/])\.\.(?:/|$)")

# Expansions that can RUN code even with no `$(…)` in sight: `${x@P}` prompt
# expansion, array subscripts (`${a[…]}` is arithmetic, which evaluates
# `$(…)` hiding in a variable's value), `$[…]` legacy arithmetic, `${!x}`
# indirection. Only a plain `$NAME` / `${NAME}` / special parameter is let
# through for commands whose arguments are otherwise unconstrained.
_COMPLEX_EXPANSION_RE = re.compile(
    r"\$\[|\$\{(?![A-Za-z_][A-Za-z0-9_]*\}|[0-9#?@*$!-]\})"
)
# A path component that starts with `.` and carries a glob: bash 3.2 (macOS
# /bin/bash) and bash < 5.2 match `..` with `.*`, `.?` and `.[.]`, so
# `cat .*/secret` reads the PARENT directory. Lexical `..` checks miss it.
_DOT_GLOB_RE = re.compile(r"(?:^|[\s/=])\.+[*?\[]")


def _brace_expands(text: str) -> bool:
    """Does ``text`` hold an unquoted ``{a,b}`` / ``{a..b}`` brace expansion?
    It turns one inert-looking word into several: ``cat {/etc/passwd,}`` or
    ``sort {-o,x} y`` hide the path / flag from every per-word check."""
    i, n = 0, len(text)
    single = double = False
    while i < n:
        c = text[i]
        if single:
            single = c != "'"
        elif c == "\\":
            i += 2
            continue
        elif c == '"':
            double = not double
        elif c == "'" and not double:
            single = True
        elif c == "{" and not double:
            close = text.find("}", i + 1)
            body = text[i + 1 : close] if close > 0 else text[i + 1 :]
            if "," in body or ".." in body:
                return True
        i += 1
    return False


def _in_tree(values: Iterable[str]) -> bool:
    """No value names a path outside the working tree (``/abs``, ``~``,
    ``../x``, ``--file=/abs``, ``-f/abs``). ``/dev/null`` is always fine."""
    return not any(
        v != "/dev/null" and _OUT_OF_TREE_RE.search(v) is not None for v in values
    )


def _segment_read_only(seg: ShellSegment, substituted: bool) -> bool:
    if not seg.argv or not _dev_null_only(seg):
        return False
    cmd, args = seg.argv[0], seg.argv[1:]
    if "/" in cmd:
        return False  # `./ls` or `/tmp/cat` is whatever that file is
    if _brace_expands(seg.raw) or _COMPLEX_EXPANSION_RE.search(seg.raw):
        return False
    # An input redirect reads a file even for a command that takes no paths:
    # `tr a b </etc/passwd` prints it.
    inputs = [r.lstrip("0123456789<") for r in seg.redirects if r.lstrip("0123456789").startswith("<")]
    if not _in_tree(inputs):
        return False
    if cmd == "printf" and args and args[0].startswith("-v"):
        return False  # `printf -v NAME` assigns; `-v 'a[…]'` evaluates a subscript
    if cmd == "cd" and (len(args) != 1 or args[0].startswith("-")):
        # Bare `cd` goes to $HOME and `cd -` to $OLDPWD — out of the tree with
        # no path in sight, and the bash tool CARRIES the cwd into later calls.
        return False
    if cmd not in _RO_NO_PATHS:
        # Path-taking: confine to the working tree (see the section comment).
        if (substituted or _has_unquoted(seg.raw, "$`") or not _in_tree(args)
                or _DOT_GLOB_RE.search(seg.raw)):
            return False
    if cmd in _RO_ANY_ARGS:
        return True
    check = _RO_CHECKERS.get(cmd)
    if check is None:
        return False
    # Argument-sensitive command: the args must be exactly what we vetted.
    if substituted or _has_unquoted(seg.raw, "$`*?["):
        return False
    return check(args)


@functools.lru_cache(maxsize=256)
def classify_bash_readonly(command: str) -> bool:
    """True only when EVERY command in ``command`` is on the read-only allowlist.

    Splits on ``; && || |`` and newlines (via :func:`decompose`), refuses
    background ``&``, output redirects other than to ``/dev/null``, heredocs,
    env-assignment prefixes, wrappers (``sudo``/``env cmd``/``nice``…),
    ``eval``/``xargs`` and anything else the decomposer cannot fully resolve.
    A ``$(…)`` / backtick / process substitution is tolerated only when its
    body is itself read-only AND the command carrying it is one whose
    arguments cannot change what it does (``echo $(pwd)`` yes,
    ``find . $(echo -delete)`` no).

    Pure and conservative: a False here only means "ask as before".
    """
    text = command or ""
    if not text.strip():
        return False
    segments, reasons = _decompose_inner(text, 0)
    # "wrapper with no command" is bare `env` (or `sudo` alone — which the
    # allowlist refuses on its own); anything else unresolved is a refusal.
    reasons = [r for r in reasons if r != "wrapper with no command"]
    if not segments or any(r not in _SUBST_REASONS for r in reasons):
        return False
    substituted = bool(reasons)
    for seg in segments:
        if seg.operator == "&":
            return False
        # A segment whose own text embeds a substitution: only an any-args
        # command may carry one (its args cannot become a dangerous flag).
        seg_subst = substituted and any(m in seg.raw for m in ("$(", "`", "<(", ">("))
        if not _segment_read_only(seg, seg_subst):
            return False
    # A trailing `&` leaves an empty final part with operator "&"; the loop
    # above only sees non-empty segments, so check the raw tail too.
    return not text.rstrip().endswith("&") or text.rstrip().endswith("&&")
