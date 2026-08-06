"""Key syntax — ``c-x``, ``s-tab``, ``a-b``, ``escape``, ``f7``, ``c-x c-s``.

Every binding in a keymap is written as text and has to become a value that can
be compared, hashed and put in a dictionary. This module is that conversion and
nothing else: no keymap, no contexts, no ``prompt_toolkit``. It is the leaf of
the ``keys`` package, which is why the error base class lives here.

The syntax is the one §6 of the plan specifies::

    c-      control          c-x
    s-      shift            s-tab
    a-/m-   alt / meta       a-b
    named   escape enter tab space backspace delete insert
            home end pageup pagedown up down left right f1-f12
    literal any single character, case-sensitive   a  A  ?  /  -
    chord   space-separated                        c-x c-s

**Normalization is the point.** A terminal does not deliver "shift" alongside a
letter — it delivers the uppercase letter — and it cannot distinguish ``c-a``
from ``c-A`` because both are the byte ``0x01``. If ``s-a`` and ``A`` stayed
distinct, a keymap could bind them to two different actions and the conflict
checker would see two unrelated bindings while the user sees one key that does
something unpredictable. So the parser folds:

* ``s-`` + a character with an uppercase form → that uppercase character, shift
  cleared. ``s-a`` **is** ``A``.
* ``s-`` + a character with no uppercase form (``s-1``) → rejected, because the
  key the user means is the shifted glyph (``!``) and guessing the keyboard
  layout is not something this layer can do.
* ``c-`` + a letter → the lowercase letter.
* ``s-`` + a *named* key is kept: ``s-tab`` and ``s-enter`` are genuinely
  distinct keystrokes that terminals do report.
* ``a-`` keeps case, since alt+A is ``ESC A`` and is distinguishable.

Canonical modifier order is control, alt, shift, so ``a-c-x`` and ``c-a-x``
round-trip to the same string and a keymap file can be diffed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Tuple

__all__ = [
    "KEY_ALIASES",
    "NAMED_KEYS",
    "Key",
    "KeySequence",
    "KeymapError",
    "UnknownKeyError",
    "format_key",
    "format_sequence",
    "label_key",
    "label_sequence",
    "parse_key",
    "parse_sequence",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
#
# The whole ``keys`` error tree from §11 is rooted at ``KeymapError``, and it is
# declared here rather than in a dedicated module because ``parse`` is the only
# module every other one imports; putting the base anywhere else would make the
# package's import graph a cycle waiting to happen. ``ValueError`` is the parent
# so a settings loader that already catches ``ValueError`` keeps working.


class KeymapError(ValueError):
    """Base for every keymap failure."""


class UnknownKeyError(KeymapError):
    """A key string did not parse.

    Carries the offending token so a caller can point at it, plus the optional
    ``where`` breadcrumb (``"user: bindings.global"``) that file-level callers
    prepend — a bad key is nearly always found while walking a file, and "this
    key is bad" without saying which line is not actionable.
    """

    def __init__(self, message: str, *, token: str = "", where: str = "") -> None:
        self.token = token
        self.where = where
        super().__init__("{}{}".format("{}: ".format(where) if where else "", message))


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Keys with a name rather than a glyph. Anything longer than one character
#: must be in here (after alias folding) or it is a typo, not a key.
NAMED_KEYS = frozenset(
    {
        "escape",
        "enter",
        "tab",
        "space",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "pageup",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
    }
    | {"f{}".format(i) for i in range(1, 13)}
)

#: Spellings people reach for, folded onto the canonical name. Aliases matter
#: more than they look: ``esc`` and ``escape`` in one context are the *same*
#: binding, and folding here is what lets the conflict checker say so.
KEY_ALIASES = {
    "esc": "escape",
    "return": "enter",
    "ret": "enter",
    "cr": "enter",
    "newline": "enter",
    "spc": "space",
    "bs": "backspace",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "page_up": "pageup",
    "page_down": "pagedown",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}

# Modifier letters accepted as a ``X-`` prefix, mapped onto the attribute they
# set. ``m`` (meta) is the readline spelling of alt and costs nothing to accept.
_MODIFIERS = {"c": "ctrl", "s": "shift", "a": "alt", "m": "alt"}

# Human labels, used by help generation and by every diagnostic message.
_MODIFIER_LABELS = (("ctrl", "ctrl+"), ("alt", "alt+"), ("shift", "shift+"))


@dataclass(frozen=True)
class Key:
    """One keystroke: a base key plus the modifiers that survive normalization."""

    key: str
    ctrl: bool = False
    shift: bool = False
    alt: bool = False

    @property
    def is_named(self) -> bool:
        """True for ``escape``/``f7``/``up``, false for a literal character."""
        return len(self.key) > 1

    def spec(self) -> str:
        """The canonical text form — what ``format_key`` writes back out."""
        out = []
        if self.ctrl:
            out.append("c-")
        if self.alt:
            out.append("a-")
        if self.shift:
            out.append("s-")
        out.append(self.key)
        return "".join(out)

    def label(self) -> str:
        """The human form used in help and diagnostics: ``ctrl+x``, ``shift+tab``."""
        out = []
        for attr, text in _MODIFIER_LABELS:
            if getattr(self, attr):
                out.append(text)
        out.append(self.key)
        return "".join(out)

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.spec()


#: A binding's key side: one keystroke, or several for a chord.
KeySequence = Tuple[Key, ...]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_key(token: str, *, where: str = "") -> Key:
    """Parse one keystroke token. Raises :class:`UnknownKeyError`.

    ``where`` is an optional breadcrumb (``"user: bindings.overlay"``) that the
    keymap loader passes down so a bad key names its file and context.
    """
    raw = token
    token = (token or "").strip()
    if not token:
        raise UnknownKeyError("empty key", token=raw, where=where)
    if " " in token or "\t" in token:
        raise UnknownKeyError(
            "{!r} contains whitespace; chords are parsed with parse_sequence()".format(raw),
            token=raw,
            where=where,
        )

    ctrl = shift = alt = False
    seen = set()
    rest = token
    # Strip ``X-`` prefixes while what remains could still be a key. The guard
    # is ``len(rest) >= 2``: it lets ``c--`` (control + the minus key) through —
    # strip ``c-`` and the remaining ``-`` is one character, so the loop stops —
    # while ``c-`` alone strips to the empty string and is caught below.
    while len(rest) >= 2 and rest[1] == "-" and rest[0].lower() in _MODIFIERS:
        attr = _MODIFIERS[rest[0].lower()]
        if attr in seen:
            raise UnknownKeyError(
                "{!r} repeats the {} modifier".format(raw, attr), token=raw, where=where
            )
        seen.add(attr)
        ctrl = ctrl or attr == "ctrl"
        shift = shift or attr == "shift"
        alt = alt or attr == "alt"
        rest = rest[2:]

    if not rest:
        raise UnknownKeyError(
            "{!r} ends with a modifier and no key".format(raw), token=raw, where=where
        )

    base = _canonical_base(rest, raw=raw, where=where)

    # Fold the two spellings a terminal cannot tell apart (see module docstring).
    if len(base) == 1:
        if ctrl:
            base = base.lower()
        if shift:
            upper = base.upper()
            if upper == base.lower():
                raise UnknownKeyError(
                    "{!r} is ambiguous — write the shifted character directly "
                    "(e.g. '!' rather than 's-1')".format(raw),
                    token=raw,
                    where=where,
                )
            base = upper
            shift = False

    return Key(key=base, ctrl=ctrl, shift=shift, alt=alt)


def _canonical_base(rest: str, *, raw: str, where: str) -> str:
    """Fold a base token to its canonical name, or keep it as a literal."""
    if len(rest) == 1:
        return rest
    folded = rest.lower()
    folded = KEY_ALIASES.get(folded, folded)
    if folded in NAMED_KEYS:
        return folded
    near = difflib.get_close_matches(folded, sorted(NAMED_KEYS | set(KEY_ALIASES)), n=1, cutoff=0.6)
    hint = " — did you mean {!r}?".format(near[0]) if near else ""
    raise UnknownKeyError(
        "unknown key {!r}{}".format(raw, hint),
        token=raw,
        where=where,
    )


def parse_sequence(spec: str, *, where: str = "") -> KeySequence:
    """Parse a whole binding: one token, or several for a chord (``"c-x c-s"``)."""
    tokens = (spec or "").split()
    if not tokens:
        raise UnknownKeyError("empty key sequence", token=spec or "", where=where)
    return tuple(parse_key(tok, where=where) for tok in tokens)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_key(key: Key) -> str:
    """Canonical text form of one keystroke."""
    return key.spec()


def format_sequence(sequence: KeySequence) -> str:
    """Canonical text form of a sequence — the inverse of :func:`parse_sequence`."""
    return " ".join(k.spec() for k in sequence)


def label_key(key: Key) -> str:
    """Human form of one keystroke: ``ctrl+x``."""
    return key.label()


def label_sequence(sequence: KeySequence) -> str:
    """Human form of a sequence: ``ctrl+x ctrl+s``."""
    return " ".join(k.label() for k in sequence)
