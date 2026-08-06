"""Semantic theming: meaning in, colour out.

Mantis currently answers "what colour is an error?" with a literal escape at
every call site — ``_MODE_ANSI`` in ``tui_fullscreen.py``, and independent
decisions again in ``tui.py`` and ``workflow_view.py``. That is why there is no
theme: changing one would mean editing a dozen sites, and honouring a light
background, a high-contrast need or a colourblind-safe palette would mean
editing them once per variant.

This package is the vocabulary and the machinery that replaces those literals,
and it is deliberately the *pure* core: no terminal detection, no filesystem, no
rendering, no configuration.

* :mod:`~mantis_agent.theme.schema` — the semantic token catalogue (including
  ``agent.untrusted``, so content from a child, an MCP server or a channel is
  visually separable from first-party output) and the theme-document validator.
* :mod:`~mantis_agent.theme.resolver` — the two fallback chains: theme
  inheritance via ``fallback``, and colour depth truecolor → 256 → 16 → none.
* :mod:`~mantis_agent.theme.contrast` — WCAG contrast ratios and colour-vision
  simulation, so a theme is judged by computation rather than by eye.

Typical use::

    from mantis_agent.theme import Resolver, parse_theme_json, validate_theme

    theme = parse_theme_json(Path("mantis-dark.json").read_text())
    resolver = Resolver(theme, depth="256", registry=known_themes)
    style = resolver.style("status.error")     # -> ResolvedStyle
    report = validate_theme(resolver)          # -> ThemeReport; .ok, .summary()

What is *not* here, on purpose: reading ``~/.mantis/themes``, mapping
``term_caps.detect()`` to a depth, honouring ``NO_COLOR``, emitting SGR escapes,
and the built-in theme JSON. Those are the wiring; this is the part that can be
proven correct without a terminal.
"""

from __future__ import annotations

from .contrast import (
    LEVEL_RATIOS,
    LEVEL_RATIOS_LARGE,
    MEANING_DELTA_E,
    MEANING_PAIRS,
    NONTEXT_RATIO,
    REFERENCE_BACKGROUNDS,
    VISION_KINDS,
    ContrastFinding,
    ThemeReport,
    VisionFinding,
    assert_theme_valid,
    check_colorblind,
    check_contrast,
    contrast_ratio,
    delta_e76,
    meets,
    reference_backgrounds,
    relative_luminance,
    simulate,
    srgb_to_lab,
    validate_theme,
)
from .resolver import (
    Color,
    ResolvedStyle,
    Resolver,
    nearest_16,
    nearest_256,
    resolve_chain,
    xterm_rgb,
)
from .schema import (
    APPEARANCES,
    ATTRS,
    COLOR_DEPTHS,
    CONTRAST_LEVELS,
    DEPTHS,
    TOKEN_GROUPS,
    TOKENS,
    ColorValue,
    Theme,
    ThemeContrastError,
    ThemeError,
    ThemeFallbackCycleError,
    ThemeNotFoundError,
    ThemeSchemaError,
    ThemeTokenMissingError,
    TokenStyle,
    hex_to_rgb,
    parse_overrides,
    parse_theme,
    parse_theme_json,
    rgb_to_hex,
)

__all__ = [
    "APPEARANCES",
    "ATTRS",
    "COLOR_DEPTHS",
    "CONTRAST_LEVELS",
    "DEPTHS",
    "LEVEL_RATIOS",
    "LEVEL_RATIOS_LARGE",
    "MEANING_DELTA_E",
    "MEANING_PAIRS",
    "NONTEXT_RATIO",
    "REFERENCE_BACKGROUNDS",
    "TOKENS",
    "TOKEN_GROUPS",
    "VISION_KINDS",
    "Color",
    "ColorValue",
    "ContrastFinding",
    "ResolvedStyle",
    "Resolver",
    "Theme",
    "ThemeContrastError",
    "ThemeError",
    "ThemeFallbackCycleError",
    "ThemeNotFoundError",
    "ThemeReport",
    "ThemeSchemaError",
    "ThemeTokenMissingError",
    "TokenStyle",
    "VisionFinding",
    "assert_theme_valid",
    "check_colorblind",
    "check_contrast",
    "contrast_ratio",
    "delta_e76",
    "hex_to_rgb",
    "meets",
    "nearest_16",
    "nearest_256",
    "parse_overrides",
    "parse_theme",
    "parse_theme_json",
    "reference_backgrounds",
    "relative_luminance",
    "resolve_chain",
    "rgb_to_hex",
    "simulate",
    "srgb_to_lab",
    "validate_theme",
    "xterm_rgb",
]
