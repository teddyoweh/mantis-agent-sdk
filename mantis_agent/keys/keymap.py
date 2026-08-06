"""Keymap documents — schema, ``extends`` composition, ``unbind``, resolution.

A keymap file is the §6 shape::

    {
      "version": 1,
      "extends": "default",
      "bindings": {"overlay": {"escape": "overlay.close"}},
      "unbind": ["input:c-r"]
    }

Three decisions in here carry the design.

**``unbind`` exists because merging cannot subtract.** A user file that extends
``default`` can override any key and can add new ones, but there is no value it
could write that means "this inherited binding should not exist". So removal is
a separate, explicit list, and it applies *after* the inherited tables are
merged and *before* this document's own bindings — which makes unbind-then-
rebind in one file read the way it looks.

**Composition keeps the lineage.** ``Keymap.docs`` holds every document in
application order, parents first. The effective table alone cannot answer "did
these two bindings collide, or did one deliberately override the other?" —
an override through ``extends`` is the whole point of ``extends``, while two
spellings of one key inside a single file is a mistake. ``conflicts`` needs the
lineage to tell those apart.

**Resolution walks a context stack, most specific first, and an exact match
anywhere beats a pending chord prefix.** That ordering is not a preference: it
is why binding ``c-x`` directly while ``c-x c-s`` also exists is reported as an
error rather than resolved by some rule — the chord simply becomes unreachable.

This module is pure. It never reads a file: ``load_keymap`` takes text, and
``extends`` resolves through an injected callable. Wiring in ``~/.mantis/
keybindings.json`` and the shipped presets is a separate step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .parse import (
    KeymapError,
    KeySequence,
    UnknownKeyError,
    format_sequence,
    parse_sequence,
)

__all__ = [
    "GLOBAL_CONTEXT",
    "KEYMAP_VERSION",
    "Binding",
    "ExtendsCycleError",
    "Keymap",
    "KeymapDoc",
    "KeymapSchemaError",
    "KeymapVersionError",
    "Resolution",
    "Unbind",
    # Re-exported: every caller that loads a keymap has to catch a bad key
    # alongside the schema errors, and importing that from a second module to
    # write one ``except`` clause is friction for no benefit.
    "UnknownKeyError",
    "UnknownPresetError",
    "compose",
    "context_stack",
    "expand_context",
    "is_descendant",
    "load_keymap",
    "parse_keymap",
    "preset_resolver",
]

#: The one schema version this package understands.
KEYMAP_VERSION = 1

#: The implicit root of the context tree. Always last in a stack, ancestor of
#: everything, and the only context name that is not dotted-hierarchical.
GLOBAL_CONTEXT = "global"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KeymapSchemaError(KeymapError):
    """The document is malformed. Carries a line number when one is knowable."""

    def __init__(self, message: str, *, source: str = "", line: Optional[int] = None) -> None:
        self.source = source
        self.line = line
        where = source or ""
        if where and line:
            where = "{}:{}".format(where, line)
        super().__init__("{}{}".format("{}: ".format(where) if where else "", message))


class KeymapVersionError(KeymapSchemaError):
    """``version`` is not one this build understands."""


class ExtendsCycleError(KeymapError):
    """``extends`` loops back on itself."""


class UnknownPresetError(KeymapError):
    """``extends`` names a document the resolver does not have."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Binding:
    """One key sequence bound to one action id, inside one context."""

    context: str
    sequence: KeySequence
    action: str
    source: str = ""
    line: Optional[int] = None
    raw_key: str = ""

    @property
    def spec(self) -> str:
        """Canonical key text — ``c-x c-s``, never the spelling as written."""
        return format_sequence(self.sequence)

    @property
    def where(self) -> str:
        """``default:12`` — the breadcrumb every diagnostic ends up quoting."""
        if self.line is None:
            return self.source
        return "{}:{}".format(self.source, self.line)


@dataclass(frozen=True)
class Unbind:
    """One ``"context:key"`` removal request."""

    context: str
    sequence: KeySequence
    raw: str = ""
    source: str = ""
    line: Optional[int] = None

    @property
    def spec(self) -> str:
        return format_sequence(self.sequence)


@dataclass(frozen=True)
class KeymapDoc:
    """One parsed keymap file, before composition.

    ``entries`` keeps every binding *as written*, in file order, including two
    spellings of the same key — deduplicating here would hide exactly the
    mistake ``conflicts`` is looking for.
    """

    name: str
    version: int = KEYMAP_VERSION
    extends: Tuple[str, ...] = ()
    entries: Tuple[Binding, ...] = ()
    unbinds: Tuple[Unbind, ...] = ()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_keymap(text: str, *, name: str) -> KeymapDoc:
    """Parse keymap JSON text into a :class:`KeymapDoc`.

    Decode failures become :class:`KeymapSchemaError` with the line the decoder
    reported, because "expecting property name at line 3" is the difference
    between fixing a typo and bisecting a config file by hand.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KeymapSchemaError(exc.msg, source=name, line=exc.lineno) from exc
    return parse_keymap(data, name=name, text=text)


def parse_keymap(data: object, *, name: str, text: Optional[str] = None) -> KeymapDoc:
    """Validate an already-decoded document.

    ``text`` is the original source; when present, every binding gets the line
    it was written on. The locator is a scan rather than a real JSON position
    index (:mod:`json` does not expose one), so it is best-effort by design —
    a wrong line is annoying, a missing line makes a conflict report useless,
    and a scan gets it right for any file a human or an exporter wrote.
    """
    if not isinstance(data, dict):
        raise KeymapSchemaError(
            "expected a JSON object, got {}".format(type(data).__name__), source=name
        )

    version = data.get("version", KEYMAP_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise KeymapSchemaError("'version' must be an integer", source=name)
    if version != KEYMAP_VERSION:
        raise KeymapVersionError(
            "unsupported keymap version {} (this build understands {})".format(
                version, KEYMAP_VERSION
            ),
            source=name,
        )

    extends = _parse_extends(data.get("extends"), name=name)
    locator = _LineLocator(text)

    raw_bindings = data.get("bindings", {})
    if not isinstance(raw_bindings, dict):
        raise KeymapSchemaError("'bindings' must be an object", source=name)

    entries: List[Binding] = []
    for context, table in raw_bindings.items():
        if not isinstance(context, str) or not context.strip():
            raise KeymapSchemaError("context names must be non-empty strings", source=name)
        if not isinstance(table, dict):
            raise KeymapSchemaError(
                "bindings for context {!r} must be an object".format(context), source=name
            )
        ctx_line = locator.find(context, after=0)
        for raw_key, action in table.items():
            if not isinstance(action, str) or not action.strip():
                raise KeymapSchemaError(
                    "binding {!r} in context {!r} must name an action".format(raw_key, context),
                    source=name,
                    line=locator.find(raw_key, after=ctx_line),
                )
            sequence = parse_sequence(
                raw_key, where="{}: bindings.{}".format(name, context)
            )
            entries.append(
                Binding(
                    context=context,
                    sequence=sequence,
                    action=action.strip(),
                    source=name,
                    line=locator.find(raw_key, after=ctx_line),
                    raw_key=raw_key,
                )
            )

    unbinds = _parse_unbinds(data.get("unbind", []), name=name, locator=locator)
    return KeymapDoc(
        name=name,
        version=version,
        extends=extends,
        entries=tuple(entries),
        unbinds=unbinds,
    )


def _parse_extends(value: object, *, name: str) -> Tuple[str, ...]:
    """``extends`` is a string, a list of strings, or absent."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise KeymapSchemaError(
            "'extends' must be a preset name or a list of preset names", source=name
        )
    return tuple(v.strip() for v in value)


def _parse_unbinds(value: object, *, name: str, locator: "_LineLocator") -> Tuple[Unbind, ...]:
    """``unbind`` is a list of ``"context:key"`` strings.

    Split on the *first* colon so a bound colon key (``"input::"``) still works.
    """
    if not isinstance(value, list):
        raise KeymapSchemaError("'unbind' must be a list of \"context:key\" strings", source=name)
    out: List[Unbind] = []
    for item in value:
        if not isinstance(item, str) or ":" not in item:
            raise KeymapSchemaError(
                "unbind entry {!r} must be \"context:key\"".format(item), source=name
            )
        context, _, raw_key = item.partition(":")
        context = context.strip()
        if not context:
            raise KeymapSchemaError(
                "unbind entry {!r} has no context".format(item), source=name
            )
        sequence = parse_sequence(raw_key, where="{}: unbind".format(name))
        out.append(
            Unbind(
                context=context,
                sequence=sequence,
                raw=item,
                source=name,
                line=locator.find(item, after=0),
            )
        )
    return tuple(out)


class _LineLocator:
    """Best-effort "which line was this JSON string written on?".

    :mod:`json` throws positions away, and hand-rolling a position-tracking
    decoder to label a diagnostic would be a poor trade. Instead: index every
    line once, then look for the JSON-encoded form of the string (``"escape"``)
    at or after a hint line. Ordinary files — hand-written or produced by
    ``mantis keys export`` — put one binding per line, so the scan is exact;
    a minified file gets line 1 everywhere, which is still better than nothing.
    """

    def __init__(self, text: Optional[str]) -> None:
        self._lines = text.splitlines() if text else []

    def find(self, needle: str, *, after: Optional[int] = 0) -> Optional[int]:
        if not self._lines:
            return None
        target = json.dumps(needle)
        start = max((after or 1) - 1, 0)
        for offset in (start, 0):
            for i in range(offset, len(self._lines)):
                if target in self._lines[i]:
                    return i + 1
        return None


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


def expand_context(context: str) -> Tuple[str, ...]:
    """``"overlay.picker"`` → ``("overlay.picker", "overlay")``, most specific first.

    ``global`` is not appended here — :func:`context_stack` owns that, so this
    stays a pure string operation on one name.
    """
    parts = context.split(".")
    return tuple(".".join(parts[: i + 1]) for i in range(len(parts) - 1, -1, -1))


def context_stack(active: Sequence[str]) -> Tuple[str, ...]:
    """Build the resolution stack from the currently active leaf contexts.

    Each leaf expands through its dotted ancestors, the leaves keep the caller's
    order (the UI knows which overlay is on top; this module cannot), duplicates
    collapse to their first appearance, and ``global`` is always last.
    """
    out: List[str] = []
    for leaf in active:
        for ctx in expand_context(leaf):
            if ctx != GLOBAL_CONTEXT and ctx not in out:
                out.append(ctx)
    out.append(GLOBAL_CONTEXT)
    return tuple(out)


def is_descendant(child: str, parent: str) -> bool:
    """Is ``child`` the same context as ``parent`` or nested inside it?

    ``global`` is the implicit root, so every context descends from it — that is
    what makes a ``global`` binding shadowable by any more specific one.
    """
    if child == parent:
        return True
    if parent == GLOBAL_CONTEXT:
        return True
    return child.startswith(parent + ".")


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

#: How ``extends`` names become documents. Injected so this module does no I/O.
Resolver = Callable[[str], KeymapDoc]


def preset_resolver(presets: Mapping[str, KeymapDoc]) -> Resolver:
    """A resolver backed by an in-memory table of documents."""

    def resolve(name: str) -> KeymapDoc:
        try:
            return presets[name]
        except KeyError:
            known = ", ".join(sorted(presets)) or "none"
            raise UnknownPresetError(
                "unknown keymap {!r} in 'extends' (known: {})".format(name, known)
            ) from None

    return resolve


def compose(doc: KeymapDoc, *, resolve: Optional[Resolver] = None) -> "Keymap":
    """Flatten a document and everything it extends into one :class:`Keymap`."""
    resolver = resolve if resolve is not None else preset_resolver({})
    table: Dict[Tuple[str, KeySequence], Binding] = {}
    lineage: List[KeymapDoc] = []
    unmatched: List[Unbind] = []
    _flatten(doc, resolver, (), table, lineage, unmatched)
    bindings = tuple(sorted(table.values(), key=lambda b: (b.context, b.spec)))
    return Keymap(
        name=doc.name,
        bindings=bindings,
        docs=tuple(lineage),
        unmatched_unbinds=tuple(unmatched),
    )


def _flatten(
    doc: KeymapDoc,
    resolve: Resolver,
    path: Tuple[str, ...],
    table: Dict[Tuple[str, KeySequence], Binding],
    lineage: List[KeymapDoc],
    unmatched: List[Unbind],
) -> None:
    """Depth-first merge: parents in order, then unbinds, then own bindings.

    ``path`` is the current ``extends`` chain, not the set of everything seen,
    so a diamond (two parents sharing a grandparent) composes fine and only a
    genuine loop raises.
    """
    if doc.name in path:
        raise ExtendsCycleError(
            "keymap 'extends' cycle: {}".format(" -> ".join(path + (doc.name,)))
        )
    for parent_name in doc.extends:
        _flatten(resolve(parent_name), resolve, path + (doc.name,), table, lineage, unmatched)

    # Removal first: an inherited binding disappears before this document's own
    # bindings land, so "unbind then rebind" in one file keeps the rebind.
    for ub in doc.unbinds:
        if table.pop((ub.context, ub.sequence), None) is None:
            unmatched.append(ub)

    for entry in doc.entries:
        table[(entry.context, entry.sequence)] = entry

    if doc not in lineage:
        lineage.append(doc)


# ---------------------------------------------------------------------------
# The composed map + resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """What a pressed sequence resolved to in one context stack."""

    sequence: KeySequence
    binding: Optional[Binding] = None
    pending: Tuple[Binding, ...] = ()

    @property
    def matched(self) -> bool:
        return self.binding is not None

    @property
    def is_pending(self) -> bool:
        """The sequence is a proper prefix of at least one reachable chord."""
        return self.binding is None and bool(self.pending)


@dataclass(frozen=True)
class Keymap:
    """A composed, effective keymap: one action per (context, key sequence)."""

    name: str
    bindings: Tuple[Binding, ...] = ()
    docs: Tuple[KeymapDoc, ...] = ()
    unmatched_unbinds: Tuple[Unbind, ...] = ()
    # Lookup index, built once. ``compare=False`` keeps two keymaps with the
    # same bindings equal, and ``repr=False`` keeps the repr readable.
    _index: Dict[str, Dict[KeySequence, Binding]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        index: Dict[str, Dict[KeySequence, Binding]] = {}
        for b in self.bindings:
            index.setdefault(b.context, {})[b.sequence] = b
        object.__setattr__(self, "_index", index)

    # -- inspection --------------------------------------------------------

    def contexts(self) -> Tuple[str, ...]:
        """Every context with at least one binding, sorted."""
        return tuple(sorted(self._index))

    def for_context(self, context: str) -> Tuple[Binding, ...]:
        """Bindings declared directly in one context (no inheritance)."""
        return tuple(b for b in self.bindings if b.context == context)

    def actions(self) -> Tuple[str, ...]:
        """Every action id bound anywhere, sorted and deduplicated."""
        return tuple(sorted({b.action for b in self.bindings}))

    def lookup(self, context: str, sequence: KeySequence) -> Optional[Binding]:
        """Exact lookup in one context. No stack walking, no chord logic."""
        return self._index.get(context, {}).get(sequence)

    # -- resolution --------------------------------------------------------

    def resolve(self, stack: Sequence[str], sequence: KeySequence) -> Resolution:
        """Resolve a pressed sequence against a context stack.

        The stack is most specific first — build it with :func:`context_stack`.
        An exact match wins as soon as one is found, in stack order. Only when
        no context in the stack has an exact match do chord prefixes matter, and
        then every reachable continuation is reported so the caller can show a
        pending indicator listing them.
        """
        for context in stack:
            hit = self._index.get(context, {}).get(sequence)
            if hit is not None:
                return Resolution(sequence=sequence, binding=hit)

        n = len(sequence)
        pending: List[Binding] = []
        seen = set()
        for context in stack:
            for seq, binding in self._index.get(context, {}).items():
                if len(seq) > n and seq[:n] == sequence and seq not in seen:
                    seen.add(seq)
                    pending.append(binding)
        return Resolution(sequence=sequence, pending=tuple(pending))

    # -- export ------------------------------------------------------------

    def to_data(self) -> Dict[str, object]:
        """The effective map as a document — the shape ``keys export`` writes."""
        bindings: Dict[str, Dict[str, str]] = {}
        for b in self.bindings:
            bindings.setdefault(b.context, {})[b.spec] = b.action
        return {"version": KEYMAP_VERSION, "bindings": bindings}
