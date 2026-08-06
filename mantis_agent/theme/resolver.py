"""Turning a token into an actual colour, for the terminal you actually have.

Two independent fallbacks meet here, and keeping them separate is what makes
the behaviour predictable:

1. **Theme inheritance.** ``fallback`` names another theme to inherit
   unspecified tokens from, so ``mantis-dark-16`` can be six lines rather than a
   copy of ``mantis-dark``. The chain is walked once at construction, with cycle
   detection, and flattened into one token map.
2. **Colour depth.** A token declares values per depth; the resolver picks by
   detected capability and walks *down* — truecolor → 256 → 16 → none. When the
   author declared nothing at or below the terminal's depth, the value is
   quantised down from the nearest richer one rather than dropped, because a
   truecolor-only theme on a 16-colour terminal should look coarse, not blank.

Everything is computed once and cached. The plan's performance requirement is
that theme resolution per frame is "effectively zero", and the way to get that
is not a fast resolver but a resolver that only runs once per token.

Nothing here reads the environment. ``depth`` is passed in — ``term_caps.detect()``
already answers "what can this terminal do", and wiring the two together is the
integration step, not this module's job. That also means a test can resolve every
theme at every depth without touching a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .schema import (
    DEPTHS,
    TOKENS,
    ColorValue,
    Theme,
    ThemeFallbackCycleError,
    ThemeNotFoundError,
    ThemeTokenMissingError,
    TokenStyle,
    parse_overrides,
)

__all__ = [
    "Color",
    "ResolvedStyle",
    "Resolver",
    "nearest_16",
    "nearest_256",
    "resolve_chain",
    "xterm_rgb",
]

# The token every unresolvable token falls back to (plan §10).
SUBSTITUTE_TOKEN = "text.primary"

# xterm's own 0-15. Terminals re-map these to the user's palette, which is the
# point of the `ansi-16` theme; these are the reference values used for
# quantisation and for contrast maths, and the docs say so.
_SYSTEM_16: Tuple[Tuple[int, int, int], ...] = (
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
    (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
    (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)

# The 6×6×6 cube's axis, and the 24-step grey ramp — both fixed by xterm.
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def _build_palette() -> Tuple[Tuple[int, int, int], ...]:
    palette: List[Tuple[int, int, int]] = list(_SYSTEM_16)
    for r in _CUBE_LEVELS:
        for g in _CUBE_LEVELS:
            for b in _CUBE_LEVELS:
                palette.append((r, g, b))
    for i in range(24):
        level = 8 + i * 10
        palette.append((level, level, level))
    return tuple(palette)


_PALETTE_256 = _build_palette()


def xterm_rgb(index: int) -> Tuple[int, int, int]:
    """The sRGB value of an xterm-256 palette index."""
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 255:
        raise ValueError("palette index must be an integer 0-255, got {!r}".format(index))
    return _PALETTE_256[index]


def _nearest(rgb: Tuple[int, int, int], palette, offset: int = 0) -> int:
    """Nearest palette entry by squared distance in sRGB.

    Plain Euclidean distance rather than a perceptual metric on purpose: this is
    the *degradation* path, its job is to be stable and explicable, and a
    perceptual metric here would make "why did my red become that red" a
    research question. The perceptual maths lives in ``contrast.py``, where it
    decides whether a theme is acceptable — a decision that deserves it.
    """
    best_index = 0
    best_distance = None
    for i, candidate in enumerate(palette):
        distance = sum((a - b) ** 2 for a, b in zip(rgb, candidate))
        if best_distance is None or distance < best_distance:
            best_index, best_distance = i, distance
            if distance == 0:
                break
    return best_index + offset


def nearest_256(rgb: Tuple[int, int, int]) -> int:
    """Closest xterm-256 index. Searches the whole table including 0-15, so a
    pure red quantises to 9 rather than to a cube approximation of it."""
    return _nearest(rgb, _PALETTE_256)


def nearest_16(rgb: Tuple[int, int, int]) -> int:
    """Closest of the 16 system colours."""
    return _nearest(rgb, _SYSTEM_16)


@dataclass(frozen=True)
class Color:
    """A colour the renderer can actually emit.

    ``rgb`` is always populated — palette indices are expanded through the xterm
    table — because the contrast validator has to be able to score a theme that
    only ever declared 256-colour indices.
    """

    depth: str                      # "truecolor" | "256" | "16"
    index: Optional[int]            # palette index, or None for truecolor
    rgb: Tuple[int, int, int]
    quantized: bool = False         # True when derived rather than declared


@dataclass(frozen=True)
class ResolvedStyle:
    """Everything a surface needs to paint one token, and nothing more."""

    token: str
    fg: Optional[Color]
    bg: Optional[Color]
    attrs: Tuple[str, ...]
    glyph: Optional[str]
    depth: str                      # the depth this was resolved *for*
    origin: str                     # theme in the chain the values came from
    substituted: Optional[str] = None  # set when the token itself was missing


def resolve_chain(theme: Theme, registry: Optional[Mapping[str, Theme]] = None) -> Tuple[Theme, ...]:
    """The inheritance chain, nearest first, with cycles refused.

    A cycle is a schema error, not a stack overflow: ``a → b → a`` is reported
    with the whole path so the author can see which file to edit.
    """
    registry = registry or {}
    chain: List[Theme] = [theme]
    seen = {theme.name}
    current = theme
    while current.fallback:
        name = current.fallback
        if name in seen:
            path = " -> ".join([t.name for t in chain] + [name])
            raise ThemeFallbackCycleError("fallback: theme inheritance cycle: {}".format(path))
        try:
            current = registry[name]
        except KeyError:
            raise ThemeNotFoundError(
                "theme {!r} names an unknown fallback theme {!r}".format(current.name, name)
            ) from None
        seen.add(name)
        chain.append(current)
    return tuple(chain)


class Resolver:
    """Resolves semantic tokens against one theme chain at one colour depth.

    Construct one per (theme, depth, overrides) triple and keep it: instances
    memoise, so the second frame costs a dict lookup.
    """

    __slots__ = ("_cache", "_depth", "_glyphs", "_missing", "_origins",
                 "_quantize", "_strict", "_tokens", "chain", "theme")

    def __init__(
        self,
        theme: Theme,
        *,
        depth: str,
        registry: Optional[Mapping[str, Theme]] = None,
        overrides: Any = None,
        quantize: bool = True,
        strict: bool = False,
    ) -> None:
        if depth not in DEPTHS:
            raise ValueError(
                "unknown colour depth {!r}; expected one of {}".format(depth, ", ".join(DEPTHS))
            )
        self.theme = theme
        self.chain = resolve_chain(theme, registry)
        self._depth = depth
        self._quantize = quantize
        self._strict = strict
        self._cache: Dict[str, ResolvedStyle] = {}
        self._missing: List[str] = []

        # Flatten the chain nearest-first: the first theme to declare a token
        # wins, and `origins` remembers which one did so `/theme` can explain an
        # inherited value instead of just showing it.
        tokens: Dict[str, TokenStyle] = {}
        glyphs: Dict[str, str] = {}
        origins: Dict[str, str] = {}
        for source in self.chain:
            for name, style in source.tokens.items():
                if name not in tokens:
                    tokens[name] = style
                    origins[name] = source.name
            for name, glyph in source.glyphs.items():
                glyphs.setdefault(name, glyph)

        # Overrides sit above the entire chain — that is the whole point of
        # `theme.overrides`: adjust one token without copying a theme. They go
        # through the theme validator, so a settings file cannot express a
        # colour a theme file could not.
        if overrides:
            for name, style in parse_overrides(overrides).items():
                tokens[name] = style
                origins[name] = "overrides"

        self._tokens = tokens
        self._glyphs = glyphs
        self._origins = origins

    # -- introspection ----------------------------------------------------

    @property
    def depth(self) -> str:
        return self._depth

    @property
    def missing(self) -> Tuple[str, ...]:
        """Tokens that had to be substituted, each listed once.

        The plan's rule is "reported once" — a per-frame renderer asking for the
        same missing token 60 times a second must not produce 60 reports.
        """
        return tuple(self._missing)

    def origin(self, token: str) -> Optional[str]:
        """Which theme in the chain (or ``"overrides"``) supplied ``token``."""
        return self._origins.get(token)

    # -- resolution -------------------------------------------------------

    def _resolve_color(self, value: Optional[ColorValue]) -> Optional[Color]:
        if value is None or self._depth == "none":
            # `none` is monochrome: no fg, no bg, meaning carried by attrs and
            # glyphs. It is a real theme (`mono`, and what NO_COLOR resolves to)
            # rather than a degraded path, which is why it is handled here once
            # instead of being stripped by every caller.
            return None

        start = DEPTHS.index(self._depth)
        # Walk down from the terminal's depth: declared values only.
        for depth in DEPTHS[start:]:
            if depth == "none":
                break
            declared = value.get(depth)
            if declared is None:
                continue
            if depth == "truecolor":
                return Color(depth="truecolor", index=None, rgb=declared)
            return Color(depth=depth, index=declared, rgb=xterm_rgb(declared))

        if not self._quantize:
            return None

        # Nothing at or below our depth. Rather than paint nothing, degrade the
        # richest declared value the author *did* give us.
        for depth in reversed(DEPTHS[:start]):
            declared = value.get(depth)
            if declared is None:
                continue
            rgb = declared if depth == "truecolor" else xterm_rgb(declared)
            index = nearest_256(rgb) if self._depth == "256" else nearest_16(rgb)
            return Color(depth=self._depth, index=index, rgb=xterm_rgb(index), quantized=True)
        return None

    def _build(self, token: str) -> ResolvedStyle:
        style = self._tokens.get(token)
        substituted = None
        if style is None:
            if self._strict:
                raise ThemeTokenMissingError(
                    "theme {!r} does not define token {!r}".format(self.theme.name, token)
                )
            if token != SUBSTITUTE_TOKEN:
                if token not in self._missing:
                    self._missing.append(token)
                style = self._tokens.get(SUBSTITUTE_TOKEN)
                substituted = SUBSTITUTE_TOKEN
            if style is None:
                # Even the substitute is absent: there is nothing left to fall
                # back to, and silently painting nothing would hide a broken
                # theme rather than surface it.
                raise ThemeTokenMissingError(
                    "theme {!r} defines neither {!r} nor the substitute {!r}".format(
                        self.theme.name, token, SUBSTITUTE_TOKEN
                    )
                )

        return ResolvedStyle(
            token=token,
            fg=self._resolve_color(style.fg),
            bg=self._resolve_color(style.bg),
            attrs=style.attrs,
            glyph=self._glyphs.get(token),
            depth=self._depth,
            origin=self._origins.get(substituted or token, ""),
            substituted=substituted,
        )

    def style(self, token: str) -> ResolvedStyle:
        """The resolved style for one token. Memoised.

        An unknown token name raises ``KeyError``: a caller asking for
        ``status.banana`` has a bug in *code*, which is a different failure from
        a theme that omits a real token, and the two must not be conflated.
        """
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        if token not in TOKENS:
            raise KeyError("unknown semantic token {!r}".format(token))
        resolved = self._build(token)
        self._cache[token] = resolved
        return resolved

    def styles(self) -> Dict[str, ResolvedStyle]:
        """Every token in the catalogue, resolved. Used by `/theme preview`, the
        contrast validator, and CSS-variable generation for the web surface."""
        return {token: self.style(token) for token in TOKENS}

    def declared(self) -> Tuple[str, ...]:
        """Tokens the chain (plus overrides) actually defines, in catalogue order."""
        return tuple(token for token in TOKENS if token in self._tokens)

    def has_color(self) -> bool:
        return self._depth != "none"

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return "Resolver(theme={!r}, depth={!r}, chain={})".format(
            self.theme.name, self._depth, [t.name for t in self.chain]
        )
