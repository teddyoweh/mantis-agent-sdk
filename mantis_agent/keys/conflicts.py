"""Conflict detection — §7. Everything is found at load time, never at press time.

The failure this whole package exists to prevent is silent: two bindings for one
key, resolved by registration order, discoverable only by pressing the key and
being surprised. So the check runs over the composed keymap and returns a list
of :class:`Diagnostic` records, each of which names *both* sides. A diagnostic
that says "conflict in overlay" and stops has moved the problem, not solved it.

Errors (the keymap is wrong):

* ``duplicate-binding`` — one context, one key, two actions. Reachable because
  ``s-a`` and ``A``, or ``esc`` and ``escape``, are two spellings of one key and
  JSON happily holds both.
* ``chord-prefix`` — ``c-x`` bound directly makes ``c-x c-s`` unreachable.
* ``unknown-action`` — with a near-match suggestion.
* ``action-context`` — an action bound outside the contexts it declares.

Warnings (the keymap is legal but probably not what was meant):

* ``duplicate-spelling`` — two spellings of one key, same action. Harmless,
  worth saying, since one of the two lines does nothing.
* ``shadowed-binding`` — a broader context's binding that a narrower one always
  covers. Often deliberate, which is exactly why it is not an error.
* ``unreachable-action`` — a visible action nothing binds; a feature shipped
  inaccessible.
* ``terminal-intercept`` — the terminal eats the key before Mantis sees it.
* ``unbind-no-match`` — an ``unbind`` entry that removed nothing, usually a
  stale entry left behind after the preset it targeted changed.

**Overrides through ``extends`` are not conflicts.** A user file rebinding
``escape`` is ``extends`` working. That is why duplicate detection runs over
``Keymap.docs`` — per document — rather than over the effective table, which by
construction has already collapsed every override.

Pure and injectable: the action registry and the intercepted-key table are both
parameters, so this module imports nothing but :mod:`keymap` and stdlib, and the
capability layer can supply real intercept data later without a change here.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .keymap import Binding, Keymap, is_descendant
from .parse import KeymapError, format_sequence, label_sequence

__all__ = [
    "DEFAULT_INTERCEPTED",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "ActionContextError",
    "ActionSpec",
    "BindingConflictError",
    "ChordPrefixConflictError",
    "Diagnostic",
    "UnknownActionError",
    "check",
    "errors",
    "has_errors",
    "raise_for_errors",
    "warnings",
]

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BindingConflictError(KeymapError):
    """Same key, same context, two actions."""


class ChordPrefixConflictError(KeymapError):
    """A chord's prefix is bound directly, making the chord unreachable."""


class UnknownActionError(KeymapError):
    """A binding names an action id that does not exist."""


class ActionContextError(KeymapError):
    """An action is bound in a context it does not declare."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """The slice of an action this module needs.

    ``keys/actions.py`` will own the real ``Action`` (title, handler, repeatable,
    destructive). :func:`check` only reads ``id``, ``contexts`` and ``hidden``,
    and it reads them by attribute, so the real dataclass drops in unchanged and
    tests do not have to build handlers to exercise a conflict.
    """

    id: str
    contexts: Tuple[str, ...] = ()
    hidden: bool = False


#: Keys a terminal or its stack usually consumes before an application sees
#: them, mapped to what to say about it. Deliberately conservative: ``c-c`` is
#: absent because Mantis binds it to interrupt on purpose and a warning on the
#: default preset is noise. The capability layer will pass a measured table
#: through the ``intercepted`` parameter; this is the floor.
DEFAULT_INTERCEPTED: Dict[str, str] = {
    "c-s": "most terminals use it for XON/XOFF flow control and will freeze the "
    "display instead; disable it with 'stty -ixon' or pick another key",
    "c-q": "the flow-control partner of ctrl+s; the terminal consumes it unless "
    "'stty -ixon' is set",
    "c-z": "the shell's suspend key (SIGTSTP); it will background Mantis rather "
    "than reach this binding",
    "c-\\": "the terminal's quit key (SIGQUIT); it will kill Mantis rather than "
    "reach this binding",
}


@dataclass(frozen=True)
class Diagnostic:
    """One finding. ``code`` is the stable identifier; ``message`` is for humans."""

    severity: str
    code: str
    message: str
    context: Optional[str] = None
    key: Optional[str] = None
    source: Optional[str] = None
    line: Optional[int] = None

    @property
    def where(self) -> str:
        if not self.source:
            return ""
        if self.line is None:
            return self.source
        return "{}:{}".format(self.source, self.line)

    def __str__(self) -> str:
        where = self.where
        return "{}[{}] {}{}".format(
            self.severity, self.code, "{}: ".format(where) if where else "", self.message
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def check(
    keymap: Keymap,
    actions: Iterable[object] = (),
    *,
    intercepted: Optional[Mapping[str, str]] = None,
) -> Tuple[Diagnostic, ...]:
    """Run every §7 check over a composed keymap.

    ``actions`` is any iterable of objects with ``id`` / ``contexts`` /
    ``hidden``; leave it empty and the action-aware checks are skipped rather
    than reporting every id as unknown — a keymap can legitimately be validated
    before the registry exists (``mantis keys check --file F``).

    Diagnostics come back errors first, then warnings, each group in the order
    the checks produced them, so printing the list top to bottom leads with what
    actually breaks.
    """
    table = _action_table(actions)
    intercept_table = DEFAULT_INTERCEPTED if intercepted is None else intercepted

    found: List[Diagnostic] = []
    found.extend(_duplicate_bindings(keymap))
    found.extend(_chord_prefix_conflicts(keymap))
    if table is not None:
        found.extend(_unknown_actions(keymap, table))
        found.extend(_action_contexts(keymap, table))
    found.extend(_shadowing(keymap))
    if table is not None:
        found.extend(_unreachable_actions(keymap, table))
    found.extend(_terminal_intercepts(keymap, intercept_table))
    found.extend(_unmatched_unbinds(keymap))

    return tuple(
        sorted(found, key=lambda d: 0 if d.severity == SEVERITY_ERROR else 1)
    )


def errors(diagnostics: Sequence[Diagnostic]) -> Tuple[Diagnostic, ...]:
    return tuple(d for d in diagnostics if d.severity == SEVERITY_ERROR)


def warnings(diagnostics: Sequence[Diagnostic]) -> Tuple[Diagnostic, ...]:
    return tuple(d for d in diagnostics if d.severity == SEVERITY_WARNING)


def has_errors(diagnostics: Sequence[Diagnostic]) -> bool:
    """True when the keymap must not be loaded. ``mantis keys check`` exit code."""
    return any(d.severity == SEVERITY_ERROR for d in diagnostics)


#: Which exception each error code raises. Codes are the stable contract; the
#: classes exist so callers can catch one kind of failure specifically.
_ERROR_CLASSES = {
    "duplicate-binding": BindingConflictError,
    "chord-prefix": ChordPrefixConflictError,
    "unknown-action": UnknownActionError,
    "action-context": ActionContextError,
}


def raise_for_errors(diagnostics: Sequence[Diagnostic]) -> None:
    """Raise the typed error for the first error-severity diagnostic, if any.

    Warnings never raise: a shadowed binding is usually intentional, and a
    keymap that refuses to load over one would be worse than the drift.
    """
    for d in errors(diagnostics):
        cls = _ERROR_CLASSES.get(d.code, KeymapError)
        raise cls(str(d))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _action_table(actions: Iterable[object]) -> Optional[Dict[str, ActionSpec]]:
    """Normalize whatever the caller passed into ``{id: ActionSpec}``, or None."""
    table: Dict[str, ActionSpec] = {}
    for action in actions:
        table[getattr(action, "id")] = ActionSpec(
            id=getattr(action, "id"),
            contexts=tuple(getattr(action, "contexts", ()) or ()),
            hidden=bool(getattr(action, "hidden", False)),
        )
    return table or None


def _duplicate_bindings(keymap: Keymap) -> List[Diagnostic]:
    """Two spellings of one key inside one document's context.

    Per document, not per effective table: across documents this is ``extends``
    doing its job. Within one document there is no ordering the author could
    have intended, which is what makes it an error rather than "last wins".
    """
    out: List[Diagnostic] = []
    for doc in keymap.docs:
        groups: Dict[tuple, List[Binding]] = {}
        for entry in doc.entries:
            groups.setdefault((entry.context, entry.sequence), []).append(entry)
        for (context, sequence), entries in groups.items():
            if len(entries) < 2:
                continue
            spec = format_sequence(sequence)
            spellings = " and ".join(repr(e.raw_key) for e in entries)
            first, second = entries[0], entries[-1]
            if len({e.action for e in entries}) == 1:
                out.append(
                    Diagnostic(
                        severity=SEVERITY_WARNING,
                        code="duplicate-spelling",
                        message=(
                            "context {!r} spells the key {} twice ({}), both bound to "
                            "{!r}; one of them is redundant".format(
                                context, spec, spellings, first.action
                            )
                        ),
                        context=context,
                        key=spec,
                        source=doc.name,
                        line=first.line,
                    )
                )
                continue
            out.append(
                Diagnostic(
                    severity=SEVERITY_ERROR,
                    code="duplicate-binding",
                    message=(
                        "key {} is bound twice in context {!r}: {!r} -> {!r} ({}) and "
                        "{!r} -> {!r} ({}); {} are the same keystroke".format(
                            spec,
                            context,
                            first.raw_key,
                            first.action,
                            first.where,
                            second.raw_key,
                            second.action,
                            second.where,
                            spellings,
                        )
                    ),
                    context=context,
                    key=spec,
                    source=doc.name,
                    line=first.line,
                )
            )
    return out


def _chord_prefix_conflicts(keymap: Keymap) -> List[Diagnostic]:
    """A directly bound sequence that is a proper prefix of a longer one.

    Only within one lineage: resolution walks a context stack, so ``input:c-x``
    and ``rail:c-x c-s`` never meet. When they do meet, the short binding fires
    first and the chord can never complete — hence an error.
    """
    out: List[Diagnostic] = []
    for short in keymap.bindings:
        n = len(short.sequence)
        for long in keymap.bindings:
            if len(long.sequence) <= n or long.sequence[:n] != short.sequence:
                continue
            if not (
                is_descendant(long.context, short.context)
                or is_descendant(short.context, long.context)
            ):
                continue
            out.append(
                Diagnostic(
                    severity=SEVERITY_ERROR,
                    code="chord-prefix",
                    message=(
                        "{!r} in context {!r} is bound directly to {!r} ({}), which makes "
                        "the chord {!r} -> {!r} ({}) in context {!r} unreachable; unbind one "
                        "of them".format(
                            short.spec,
                            short.context,
                            short.action,
                            short.where,
                            long.spec,
                            long.action,
                            long.where,
                            long.context,
                        )
                    ),
                    context=short.context,
                    key=short.spec,
                    source=short.source,
                    line=short.line,
                )
            )
    return out


def _shadowing(keymap: Keymap) -> List[Diagnostic]:
    """A binding a nested context always covers.

    "Always" is scoped to the stacks that actually contain both contexts, which
    is what makes this a warning: ``global:escape`` is still reachable outside
    any overlay. The message says which context does the covering so the reader
    can decide whether that was the intent.
    """
    out: List[Diagnostic] = []
    for outer in keymap.bindings:
        for inner in keymap.bindings:
            if inner is outer or inner.sequence != outer.sequence:
                continue
            if inner.context == outer.context or not is_descendant(inner.context, outer.context):
                continue
            if inner.action == outer.action:
                continue  # same behavior either way; nothing is lost
            out.append(
                Diagnostic(
                    severity=SEVERITY_WARNING,
                    code="shadowed-binding",
                    message=(
                        "{!r} -> {!r} in context {!r} ({}) is shadowed whenever context "
                        "{!r} is active, where it is bound to {!r} ({})".format(
                            outer.spec,
                            outer.action,
                            outer.context,
                            outer.where,
                            inner.context,
                            inner.action,
                            inner.where,
                        )
                    ),
                    context=outer.context,
                    key=outer.spec,
                    source=outer.source,
                    line=outer.line,
                )
            )
    return out


def _unknown_actions(keymap: Keymap, table: Mapping[str, ActionSpec]) -> List[Diagnostic]:
    """Action ids nothing declares, with a near-match when there is one."""
    out: List[Diagnostic] = []
    known = sorted(table)
    for binding in keymap.bindings:
        if binding.action in table:
            continue
        near = difflib.get_close_matches(binding.action, known, n=1, cutoff=0.6)
        hint = " — did you mean {!r}?".format(near[0]) if near else ""
        out.append(
            Diagnostic(
                severity=SEVERITY_ERROR,
                code="unknown-action",
                message=(
                    "{!r} in context {!r} is bound to unknown action {!r}{}".format(
                        binding.spec, binding.context, binding.action, hint
                    )
                ),
                context=binding.context,
                key=binding.spec,
                source=binding.source,
                line=binding.line,
            )
        )
    return out


def _action_contexts(keymap: Keymap, table: Mapping[str, ActionSpec]) -> List[Diagnostic]:
    """An action bound where it does not apply.

    A declared context covers its descendants: an action valid in ``overlay``
    is valid in ``overlay.picker``. An action declaring no contexts is
    unrestricted, which is how ``overlay.help`` binds everywhere.
    """
    out: List[Diagnostic] = []
    for binding in keymap.bindings:
        spec = table.get(binding.action)
        if spec is None or not spec.contexts:
            continue
        if any(is_descendant(binding.context, declared) for declared in spec.contexts):
            continue
        out.append(
            Diagnostic(
                severity=SEVERITY_ERROR,
                code="action-context",
                message=(
                    "action {!r} is bound to {!r} in context {!r}, but it declares only "
                    "{}".format(
                        binding.action,
                        binding.spec,
                        binding.context,
                        ", ".join(repr(c) for c in spec.contexts),
                    )
                ),
                context=binding.context,
                key=binding.spec,
                source=binding.source,
                line=binding.line,
            )
        )
    return out


def _unreachable_actions(keymap: Keymap, table: Mapping[str, ActionSpec]) -> List[Diagnostic]:
    """A visible action with no binding in any context — a feature shipped hidden."""
    bound = set(keymap.actions())
    out: List[Diagnostic] = []
    for action_id in sorted(table):
        spec = table[action_id]
        if spec.hidden or action_id in bound:
            continue
        out.append(
            Diagnostic(
                severity=SEVERITY_WARNING,
                code="unreachable-action",
                message=(
                    "action {!r} has no binding in any context, so nothing can invoke "
                    "it; bind it or mark it hidden".format(action_id)
                ),
            )
        )
    return out


def _terminal_intercepts(keymap: Keymap, intercepted: Mapping[str, str]) -> List[Diagnostic]:
    """Keys the terminal eats first — reported, not fought (§3 non-goal).

    Any keystroke of a chord counts: ``c-x c-s`` is just as dead as ``c-s`` if
    flow control swallows the second press.
    """
    out: List[Diagnostic] = []
    for binding in keymap.bindings:
        for key in binding.sequence:
            reason = intercepted.get(key.spec())
            if reason is None:
                continue
            out.append(
                Diagnostic(
                    severity=SEVERITY_WARNING,
                    code="terminal-intercept",
                    message=(
                        "{!r} (in {!r} -> {!r}, context {!r}) may never reach Mantis: "
                        "{}".format(
                            key.spec(),
                            binding.spec,
                            binding.action,
                            binding.context,
                            reason,
                        )
                    ),
                    context=binding.context,
                    key=binding.spec,
                    source=binding.source,
                    line=binding.line,
                )
            )
    return out


def _unmatched_unbinds(keymap: Keymap) -> List[Diagnostic]:
    """An ``unbind`` that removed nothing — usually stale after a preset change."""
    return [
        Diagnostic(
            severity=SEVERITY_WARNING,
            code="unbind-no-match",
            message=(
                "unbind {!r} matched no inherited binding ({} in context {!r} is not "
                "bound)".format(ub.raw, label_sequence(ub.sequence), ub.context)
            ),
            context=ub.context,
            key=ub.spec,
            source=ub.source,
            line=ub.line,
        )
        for ub in keymap.unmatched_unbinds
    ]
