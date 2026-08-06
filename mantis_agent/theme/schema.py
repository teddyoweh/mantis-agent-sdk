"""The semantic token catalogue and the theme document schema.

Mantis paints with literals today: ``tui_fullscreen.py`` defines ``_MODE_ANSI``
as a table of ``\\033[38;5;…m`` strings and hands them to ``prompt_toolkit`` at a
dozen call sites, so "what colour is an error?" is answered independently in a
dozen places and cannot be answered differently for a light terminal, a
high-contrast user, or a colourblind one.

This module is the vocabulary that replaces those literals. Presentation refers
to *meaning* — ``status.error``, ``agent.untrusted`` — and a theme is data that
maps meaning to colour. Nothing here renders anything, opens a file, or reads
the environment; it is the pure half of the system so that the loader, the
resolver and the validators can all be tested without a terminal.

Three decisions worth knowing about:

* **Unknown token names are rejected, not ignored.** A theme is hand-written
  JSON and ``"status.eror"`` is the likeliest thing to be in one. Ignoring it
  produces a theme that silently doesn't apply; rejecting it produces a message
  that says which key is wrong.
* **Theme names are slugs.** ``fallback`` names a theme to inherit from, and a
  loader will eventually turn that name into a path under ``~/.mantis/themes/``.
  ``{"fallback": "../../etc/shadow"}`` must die here, in the schema, rather than
  in whatever code later joins it to a directory — the validation that matters
  is the one that happens before the value reaches a filesystem call.
* **Validation is hand-rolled rather than ``msgspec``-decoded.** The rest of the
  SDK decodes wire types with ``msgspec``, which is right for machine traffic;
  here the consumer is a human editing a file by hand, and the useful artefact
  is ``tokens.status.error.256: expected 0-255, got 999`` rather than a struct
  mismatch. The error path *is* the feature.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple

from ..errors import AgentError

__all__ = [
    "APPEARANCES",
    "ATTRS",
    "COLOR_DEPTHS",
    "CONTRAST_LEVELS",
    "DEPTHS",
    "TOKENS",
    "TOKEN_GROUPS",
    "ColorValue",
    "Theme",
    "ThemeContrastError",
    "ThemeError",
    "ThemeFallbackCycleError",
    "ThemeNotFoundError",
    "ThemeSchemaError",
    "ThemeTokenMissingError",
    "TokenStyle",
    "hex_to_rgb",
    "parse_overrides",
    "parse_theme",
    "parse_theme_json",
    "rgb_to_hex",
]


# --------------------------------------------------------------------------
# errors — the hierarchy from the plan's §10, rooted in the SDK's own base so
# a theme problem is catchable alongside every other Mantis error.
# --------------------------------------------------------------------------


class ThemeError(AgentError):
    """Base for every theme problem. A theme problem must never break the UI:
    callers are expected to catch this, report once, and fall back."""


class ThemeNotFoundError(ThemeError):
    """A theme was named — by config, or by another theme's ``fallback`` — and
    no theme by that name is registered."""


class ThemeSchemaError(ThemeError):
    """A theme document is malformed. The message always carries the JSON path
    of the offending key, because these are hand-edited files."""


class ThemeContrastError(ThemeError):
    """A theme failed automated contrast/colourblind validation. Raised for
    built-in themes (where it is a test failure); user themes get a warning
    instead — it is their terminal."""


class ThemeFallbackCycleError(ThemeSchemaError):
    """``fallback`` chains back to a theme already in the chain."""


class ThemeTokenMissingError(ThemeError):
    """A token could not be resolved anywhere in the chain and no substitute
    was available. Normally the resolver substitutes ``text.primary`` and
    reports; this is the case where even that is absent."""


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------

# Grouped exactly as the plan lists them, kept as an ordered structure so that
# `/theme preview` and the docs table can walk tokens in a stable, meaningful
# order rather than whatever a set iteration produces.
TOKEN_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("text", ("text.primary", "text.secondary", "text.muted", "text.inverse")),
    ("accent", ("accent.primary", "accent.secondary")),
    (
        "status",
        (
            "status.success", "status.warning", "status.error", "status.info",
            "status.running", "status.pending", "status.blocked", "status.cancelled",
        ),
    ),
    ("diff", ("diff.added", "diff.removed", "diff.context", "diff.header")),
    (
        "syntax",
        (
            "syntax.keyword", "syntax.string", "syntax.comment",
            "syntax.number", "syntax.function", "syntax.type",
        ),
    ),
    (
        "ui",
        (
            "ui.border", "ui.selection", "ui.focus", "ui.cursor",
            "ui.scrollbar", "ui.overlay.bg", "ui.overlay.border",
        ),
    ),
    ("tool", ("tool.read", "tool.write", "tool.exec", "tool.search")),
    # `agent.untrusted` earns its place: a child report, an MCP tool result and
    # a channel event are all *content produced by something that is not this
    # agent*, and several plans in this set label them structurally. Giving that
    # label a colour is what makes it legible at a glance instead of only on
    # inspection.
    ("agent", ("agent.self", "agent.child", "agent.peer", "agent.untrusted")),
)

TOKENS: Tuple[str, ...] = tuple(name for _, names in TOKEN_GROUPS for name in names)
_TOKEN_SET = frozenset(TOKENS)

# Ordered strongest → weakest: this *is* the fallback chain from the plan, and
# `DEPTHS.index` is how the resolver walks down it.
DEPTHS: Tuple[str, ...] = ("truecolor", "256", "16", "none")
COLOR_DEPTHS: Tuple[str, ...] = ("truecolor", "256", "16")

# SGR attributes we are willing to emit. `blink` and `conceal` are deliberately
# absent: one is an accessibility hazard and the other hides content.
ATTRS = frozenset({"bold", "dim", "italic", "underline", "reverse", "strike"})

APPEARANCES: Tuple[str, ...] = ("dark", "light", "any", "auto")
CONTRAST_LEVELS: Tuple[str, ...] = ("AA", "AAA")

# Lowercase slug: a letter or digit, then letters/digits/dot/dash/underscore.
# The leading-character rule is what makes "..", "./x" and "/abs" unspellable,
# and the length cap keeps a theme name inside every filesystem's limits.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

_TOP_LEVEL_KEYS = frozenset(
    {"name", "version", "appearance", "contrast", "colorblindSafe", "tokens", "glyphs", "fallback"}
)
_COLOR_KEYS = frozenset({"truecolor", "256", "16"})
_STYLE_KEYS = _COLOR_KEYS | {"attrs", "bg"}

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ColorValue:
    """One token's colour, as declared, at every depth the author supplied.

    All three fields are optional and at least one must be present. Keeping the
    declared values separate (rather than eagerly normalising to RGB) is what
    lets the resolver honour an author who deliberately chose palette index 203
    for 256-colour terminals *and* ``#ff5f5f`` for truecolor ones, which are not
    the same colour and are not meant to be.
    """

    truecolor: Optional[Tuple[int, int, int]] = None
    c256: Optional[int] = None
    c16: Optional[int] = None

    def get(self, depth: str) -> Any:
        """The declared value at ``depth``, or ``None``."""
        return {"truecolor": self.truecolor, "256": self.c256, "16": self.c16}[depth]


@dataclass(frozen=True)
class TokenStyle:
    """A token's full presentation: foreground, optional background, attributes."""

    fg: Optional[ColorValue] = None
    bg: Optional[ColorValue] = None
    attrs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Theme:
    """A parsed, validated theme document.

    ``tokens``/``glyphs`` are read-only mappings — a theme is shared between the
    resolver, the validator and any number of surfaces, and a mutable default
    that one of them edits in place is exactly the bug that makes "the theme
    changed and nobody knows why" unfindable.
    """

    name: str
    version: int
    appearance: str
    contrast: Optional[str]
    colorblind_safe: bool
    tokens: Mapping[str, TokenStyle]
    glyphs: Mapping[str, str]
    fallback: Optional[str]
    source: str = ""  # where it came from, for diagnostics; "" when synthesised


# --------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------


def hex_to_rgb(value: str) -> Tuple[int, int, int]:
    """``"#ff5f5f"`` / ``"#f55"`` → ``(255, 95, 95)``.

    Raises :class:`ThemeSchemaError` rather than ``ValueError`` so a bad colour
    in a theme file reports as a theme problem wherever it is parsed from.
    """
    if not isinstance(value, str) or not _HEX_RE.match(value):
        raise ThemeSchemaError(f"expected a #rgb or #rrggbb colour, got {value!r}")
    digits = value[1:]
    if len(digits) == 3:  # #abc is #aabbcc, per CSS
        digits = "".join(ch * 2 for ch in digits)
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


def rgb_to_hex(rgb: Sequence[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def _fail(path: str, message: str) -> "ThemeSchemaError":
    return ThemeSchemaError("{}: {}".format(path, message) if path else message)


def _require_mapping(obj: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise _fail(path, "expected an object, got {}".format(type(obj).__name__))
    for key in obj:
        if not isinstance(key, str):
            raise _fail(path, "keys must be strings, got {!r}".format(key))
    return obj


def _parse_color(obj: Mapping[str, Any], path: str, *, allow_missing: bool) -> Optional[ColorValue]:
    """The ``{"256": …, "truecolor": …, "16": …}`` triple shared by fg and bg."""
    truecolor = None
    if "truecolor" in obj:
        raw = obj["truecolor"]
        try:
            truecolor = hex_to_rgb(raw)
        except ThemeSchemaError as exc:
            raise _fail(path + ".truecolor", str(exc)) from None

    indexed = {}
    for key, ceiling in (("256", 255), ("16", 15)):
        if key not in obj:
            continue
        raw = obj[key]
        # `bool` is an `int` in Python and `{"256": true}` is a typo, not a
        # colour, so it is excluded explicitly.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise _fail(path + "." + key, "expected an integer 0-{}, got {!r}".format(ceiling, raw))
        if not 0 <= raw <= ceiling:
            raise _fail(path + "." + key, "expected an integer 0-{}, got {!r}".format(ceiling, raw))
        indexed[key] = raw

    if truecolor is None and not indexed:
        if allow_missing:
            return None
        raise _fail(path, "declares no colour at any depth")
    return ColorValue(truecolor=truecolor, c256=indexed.get("256"), c16=indexed.get("16"))


def _parse_attrs(raw: Any, path: str) -> Tuple[str, ...]:
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise _fail(path, "expected a list of attribute names, got {!r}".format(raw))
    out = []
    for item in raw:
        if not isinstance(item, str) or item not in ATTRS:
            raise _fail(
                path,
                "unknown attribute {!r}; known: {}".format(item, ", ".join(sorted(ATTRS))),
            )
        if item not in out:  # order preserved, duplicates dropped
            out.append(item)
    return tuple(out)


def _parse_style(raw: Any, path: str) -> TokenStyle:
    obj = _require_mapping(raw, path)
    unknown = set(obj) - _STYLE_KEYS
    if unknown:
        raise _fail(path, "unknown keys: {}".format(", ".join(sorted(unknown))))

    fg = _parse_color(obj, path, allow_missing=True)
    attrs = _parse_attrs(obj["attrs"], path + ".attrs") if "attrs" in obj else ()

    bg = None
    if "bg" in obj:
        bg_obj = _require_mapping(obj["bg"], path + ".bg")
        bg_unknown = set(bg_obj) - _COLOR_KEYS
        if bg_unknown:
            raise _fail(path + ".bg", "unknown keys: {}".format(", ".join(sorted(bg_unknown))))
        bg = _parse_color(bg_obj, path + ".bg", allow_missing=False)

    if fg is None and bg is None and not attrs:
        raise _fail(path, "declares nothing — give it a colour, a bg, or attrs")
    return TokenStyle(fg=fg, bg=bg, attrs=attrs)


def _parse_token_map(raw: Any, path: str) -> Mapping[str, TokenStyle]:
    obj = _require_mapping(raw, path)
    out = {}
    for name, value in obj.items():
        if name not in _TOKEN_SET:
            raise _fail(
                "{}.{}".format(path, name),
                "unknown token; see mantis_agent.theme.TOKENS for the catalogue",
            )
        out[name] = _parse_style(value, "{}.{}".format(path, name))
    return MappingProxyType(out)


def _parse_glyphs(raw: Any, path: str) -> Mapping[str, str]:
    obj = _require_mapping(raw, path)
    out = {}
    for name, value in obj.items():
        if name not in _TOKEN_SET:
            raise _fail("{}.{}".format(path, name), "unknown token")
        if not isinstance(value, str) or not value:
            raise _fail("{}.{}".format(path, name), "expected a non-empty string")
        # A glyph goes straight into the UI chrome. Control characters there
        # would let a theme file move the cursor or open an escape sequence,
        # which is a theme file writing ANSI — the one thing the whole token
        # system exists to prevent.
        if any(ch < " " or ch == "\x7f" for ch in value):
            raise _fail("{}.{}".format(path, name), "must not contain control characters")
        if len(value) > 8:
            raise _fail("{}.{}".format(path, name), "glyph is too long (max 8 chars)")
        out[name] = value
    return MappingProxyType(out)


def _parse_name(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or not _NAME_RE.match(raw):
        raise _fail(
            path,
            "expected a lowercase slug matching {} (got {!r}) — theme names become "
            "filenames, so path separators and dot-prefixes are refused".format(
                _NAME_RE.pattern, raw
            ),
        )
    return raw


def parse_theme(document: Any, *, source: str = "") -> Theme:
    """Validate a decoded theme document and return the :class:`Theme`.

    ``document`` is whatever ``json.loads`` produced. Every failure is a
    :class:`ThemeSchemaError` naming the offending path.
    """
    obj = _require_mapping(document, "")
    unknown = set(obj) - _TOP_LEVEL_KEYS
    if unknown:
        raise _fail("", "unknown keys: {}".format(", ".join(sorted(unknown))))

    for required in ("name", "version", "tokens"):
        if required not in obj:
            raise _fail("", "missing required key {!r}".format(required))

    name = _parse_name(obj["name"], "name")

    version = obj["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _fail("version", "expected the integer {}, got {!r}".format(SCHEMA_VERSION, version))
    if version != SCHEMA_VERSION:
        # Refuse rather than guess: a v2 document means keys we have no
        # semantics for, and applying it half-understood is worse than saying so.
        raise _fail("version", "unsupported schema version {} (this build reads {})".format(
            version, SCHEMA_VERSION))

    appearance = obj.get("appearance", "dark")
    if appearance not in APPEARANCES:
        raise _fail("appearance", "expected one of {}, got {!r}".format(
            ", ".join(APPEARANCES), appearance))

    contrast = obj.get("contrast")
    if contrast is not None and contrast not in CONTRAST_LEVELS:
        raise _fail("contrast", "expected one of {} or null, got {!r}".format(
            ", ".join(CONTRAST_LEVELS), contrast))

    colorblind_safe = obj.get("colorblindSafe", False)
    if not isinstance(colorblind_safe, bool):
        raise _fail("colorblindSafe", "expected true or false, got {!r}".format(colorblind_safe))

    fallback = obj.get("fallback")
    if fallback is not None:
        fallback = _parse_name(fallback, "fallback")
        if fallback == name:
            # The degenerate cycle, caught here rather than in the resolver
            # because it is visible without a registry — and a theme that says
            # it inherits from itself is a typo, not a design.
            raise ThemeFallbackCycleError(
                "fallback: theme inheritance cycle: {} -> {}".format(name, name)
            )

    return Theme(
        name=name,
        version=version,
        appearance=appearance,
        contrast=contrast,
        colorblind_safe=colorblind_safe,
        tokens=_parse_token_map(obj["tokens"], "tokens"),
        glyphs=_parse_glyphs(obj.get("glyphs", {}), "glyphs"),
        fallback=fallback,
        source=source,
    )


def parse_theme_json(text: str, *, source: str = "") -> Theme:
    """Parse theme JSON text. Decode failures surface as schema errors so a
    caller only has to catch :class:`ThemeError`."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ThemeSchemaError("not valid JSON: {}".format(exc)) from None
    return parse_theme(document, source=source)


def parse_overrides(raw: Any) -> Mapping[str, TokenStyle]:
    """``theme.overrides`` from settings — the same token map, standing alone.

    Deliberately the *same* validator as a theme's ``tokens``: an override is a
    partial theme, and letting it through a looser path would mean a settings
    file could express a colour a theme file cannot.
    """
    return _parse_token_map(raw, "overrides")
