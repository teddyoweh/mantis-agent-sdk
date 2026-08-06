"""Deciding whether a theme is *readable* — by arithmetic, not by eye.

"Looks fine on my machine" is how palettes ship with a 2.9:1 grey in them. The
plan's rule is that every theme is validated automatically and that a built-in
failing validation fails the test suite, so this module answers two questions
with numbers:

**Can it be read?** WCAG 2.x relative luminance and contrast ratio. AA wants
≥ 4.5:1 for normal text, AAA wants ≥ 7:1, and non-text marks (borders, cursors,
scrollbars) want ≥ 3:1 under WCAG 1.4.11.

**Does it still carry meaning without normal colour vision?** Roughly 8% of men
have a red/green deficiency, and "green means it worked, red means it failed" is
the single most common thing a terminal UI encodes in colour alone. We simulate
protanopia, deuteranopia and tritanopia with the Machado et al. (2009)
severity-1.0 matrices, then measure the remaining perceptual distance in CIE Lab
(ΔE*ab, CIE76) for the pairs whose *distinction carries meaning* —
``status.success`` versus ``status.error`` above all.

Two assumptions are documented rather than hidden:

* **The terminal background is not knowable.** No query-and-wait detection is
  permitted (it hangs terminals that don't answer), so contrast is scored
  against the reference background implied by the theme's declared
  ``appearance``. A theme that declares ``dark`` and is used on a white terminal
  is outside what any static check can promise.
* **A distinct glyph counts.** Meaning carried by shape rather than hue is still
  carried. This is not a loophole: it is what makes ``mono`` — which has no
  colour at all, and is what ``NO_COLOR`` resolves to — a first-class theme that
  passes validation honestly rather than a degraded path that is exempted from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .resolver import Resolver
from .schema import ThemeContrastError, hex_to_rgb, rgb_to_hex

__all__ = [
    "LEVEL_RATIOS",
    "LEVEL_RATIOS_LARGE",
    "MEANING_DELTA_E",
    "MEANING_PAIRS",
    "NONTEXT_RATIO",
    "REFERENCE_BACKGROUNDS",
    "VISION_KINDS",
    "ContrastFinding",
    "ThemeReport",
    "VisionFinding",
    "assert_theme_valid",
    "contrast_ratio",
    "delta_e76",
    "relative_luminance",
    "simulate",
    "srgb_to_lab",
    "validate_theme",
]

# WCAG 1.4.3 / 1.4.6, normal text and (relaxed) large text.
LEVEL_RATIOS: Dict[str, float] = {"AA": 4.5, "AAA": 7.0}
LEVEL_RATIOS_LARGE: Dict[str, float] = {"AA": 3.0, "AAA": 4.5}
# WCAG 1.4.11: user-interface components and graphical objects. Flat 3:1 at both
# levels — the success criterion has no AAA counterpart, and inventing one would
# be this module asserting a standard that does not exist.
NONTEXT_RATIO = 3.0

# What a "dark" or "light" theme is scored against. #1e1e1e rather than pure
# black because it is what the common dark terminal profiles actually use and
# scoring against #000000 flatters every palette by a few tenths.
REFERENCE_BACKGROUNDS: Dict[str, Tuple[int, int, int]] = {
    "dark": hex_to_rgb("#1e1e1e"),
    "light": hex_to_rgb("#ffffff"),
}

VISION_KINDS: Tuple[str, ...] = ("protanopia", "deuteranopia", "tritanopia")

# Pairs whose *difference* is the message. If these collapse, the UI has lied.
MEANING_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("status.success", "status.error"),
    ("status.success", "status.warning"),
    ("status.warning", "status.error"),
    ("diff.added", "diff.removed"),
    # First-party output versus content from a child, an MCP server or a
    # channel. The plan gives this its own token precisely so the distinction is
    # visible; a theme where it isn't has removed a security affordance.
    ("agent.self", "agent.untrusted"),
)

# ΔE*ab threshold for "still distinguishable after simulation". A just-noticeable
# difference is ~2.3, but that is two swatches side by side under controlled
# light; a status glyph is seen alone, briefly, among other colours. 25 is where
# the naive palettes fail — pure red against pure green collapses to ΔE ≈ 10
# under deuteranopia — while palettes separated on a surviving axis (lightness,
# or blue/yellow) clear it comfortably.
MEANING_DELTA_E = 25.0

# Tokens excluded from the reference-background check, with reasons:
#   text.inverse   — drawn on an inverted/accent background by definition, so
#                    scoring it against the page background is meaningless. A
#                    theme that wants it checked declares its `bg`.
#   ui.overlay.bg  — it *is* a background.
_NOT_ON_BACKGROUND = frozenset({"text.inverse", "ui.overlay.bg"})

# Marks rather than text: WCAG 1.4.11 applies.
_NONTEXT_TOKENS = frozenset(
    {"ui.border", "ui.selection", "ui.focus", "ui.cursor", "ui.scrollbar", "ui.overlay.border"}
)

# Machado, Oliveira & Fernandes (2009), severity 1.0. Applied in *linear* RGB,
# which is what the paper derives them for; applying them to gamma-encoded
# values (a common shortcut) shifts every result and would make the thresholds
# here meaningless.
_VISION_MATRICES: Dict[str, Tuple[Tuple[float, float, float], ...]] = {
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritanopia": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}

# D65 white point, and the sRGB → XYZ matrix (IEC 61966-2-1).
_D65 = (0.95047, 1.00000, 1.08883)
_RGB_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)


# --------------------------------------------------------------------------
# colour science primitives
# --------------------------------------------------------------------------


def _linearize(channel: int) -> float:
    """8-bit sRGB → linear-light 0..1, per the sRGB transfer function."""
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _encode(value: float) -> int:
    """Linear-light → 8-bit sRGB, clamped back into gamut."""
    c = min(1.0, max(0.0, value))
    v = c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return int(round(min(1.0, max(0.0, v)) * 255))


def relative_luminance(rgb: Sequence[int]) -> float:
    """WCAG relative luminance: ``0.2126R + 0.7152G + 0.0722B`` over linearized
    channels. 0.0 for black, 1.0 for white."""
    r, g, b = (_linearize(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: Sequence[int], b: Sequence[int]) -> float:
    """WCAG contrast ratio, ``(L1 + 0.05) / (L2 + 0.05)``. Symmetric, 1.0 … 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = (la, lb) if la >= lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


def meets(ratio: float, level: str, *, large: bool = False) -> bool:
    """Does ``ratio`` satisfy ``level`` (``"AA"``/``"AAA"``)?"""
    table = LEVEL_RATIOS_LARGE if large else LEVEL_RATIOS
    try:
        required = table[level]
    except KeyError:
        raise ValueError("unknown contrast level {!r}".format(level)) from None
    return ratio >= required


def simulate(rgb: Sequence[int], kind: str) -> Tuple[int, int, int]:
    """``rgb`` as seen with ``kind`` (protanopia/deuteranopia/tritanopia)."""
    try:
        matrix = _VISION_MATRICES[kind]
    except KeyError:
        raise ValueError(
            "unknown vision kind {!r}; expected one of {}".format(kind, ", ".join(VISION_KINDS))
        ) from None
    linear = [_linearize(c) for c in rgb]
    out = []
    for row in matrix:
        out.append(_encode(sum(m * c for m, c in zip(row, linear))))
    return (out[0], out[1], out[2])


def srgb_to_lab(rgb: Sequence[int]) -> Tuple[float, float, float]:
    """CIE L*a*b* under D65. The perceptual space ΔE is measured in."""
    linear = [_linearize(c) for c in rgb]
    xyz = [sum(m * c for m, c in zip(row, linear)) for row in _RGB_TO_XYZ]

    def f(t: float) -> float:
        # CIE's cube-root with the linear segment near black, in the exact
        # rational form (216/24389, 841/108) so there is no drift from the
        # rounded 0.008856/7.787 constants.
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = (f(c / w) for c, w in zip(xyz, _D65))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e76(a: Sequence[int], b: Sequence[int]) -> float:
    """CIE76 ΔE*ab between two sRGB colours.

    CIE76 rather than CIEDE2000 deliberately: we are asking "are these two
    obviously different?", not "do these two match?". CIE76's known weakness is
    over-reporting saturated-hue differences at small ΔE, which is irrelevant at
    a threshold of 25, and it is short enough to be read and checked by hand.
    """
    la, lb = srgb_to_lab(a), srgb_to_lab(b)
    return sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContrastFinding:
    """One token scored against one background."""

    token: str
    foreground: Tuple[int, int, int]
    background: Tuple[int, int, int]
    ratio: float
    required: float
    ok: bool

    def describe(self) -> str:
        return "{}: {} on {} is {:.2f}:1, needs {:.1f}:1".format(
            self.token, rgb_to_hex(self.foreground), rgb_to_hex(self.background),
            self.ratio, self.required,
        )


@dataclass(frozen=True)
class VisionFinding:
    """One meaning-bearing token pair under one simulated colour vision."""

    pair: Tuple[str, str]
    vision: str
    delta_e: float
    required: float
    ok: bool
    via_glyph: bool = False  # distinguished by glyph rather than by colour

    def describe(self) -> str:
        return "{} vs {} under {}: ΔE {:.1f}, needs {:.1f}".format(
            self.pair[0], self.pair[1], self.vision, self.delta_e, self.required
        )


@dataclass(frozen=True)
class ThemeReport:
    """The whole verdict on one theme at one colour depth."""

    theme: str
    depth: str
    appearance: str
    level: Optional[str]
    background: Optional[Tuple[int, int, int]]
    contrast: Tuple[ContrastFinding, ...]
    colorblind: Tuple[VisionFinding, ...]

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.contrast) and all(f.ok for f in self.colorblind)

    def failures(self) -> Tuple[str, ...]:
        return tuple(
            [f.describe() for f in self.contrast if not f.ok]
            + [f.describe() for f in self.colorblind if not f.ok]
        )

    def summary(self) -> str:
        """A human-readable diagnosis, or ``""`` when the theme passes.

        Empty-on-success is deliberate: this string is what a ``/theme validate``
        command prints and what an assertion message carries, and both want
        silence when there is nothing wrong.
        """
        failures = self.failures()
        if not failures:
            return ""
        head = "theme {!r} ({} @ {}, target {}) has {} problem(s):".format(
            self.theme, self.appearance, self.depth, self.level or "no declared level",
            len(failures),
        )
        return "\n".join([head] + ["  - " + line for line in failures])


# --------------------------------------------------------------------------
# whole-theme validation
# --------------------------------------------------------------------------


def reference_backgrounds(appearance: str) -> Tuple[Tuple[int, int, int], ...]:
    """The background(s) a theme with this ``appearance`` is judged against.

    ``any``/``auto`` is judged against *both*, because a theme that claims to
    work anywhere has claimed exactly that.
    """
    if appearance in REFERENCE_BACKGROUNDS:
        return (REFERENCE_BACKGROUNDS[appearance],)
    return (REFERENCE_BACKGROUNDS["dark"], REFERENCE_BACKGROUNDS["light"])


def check_contrast(resolver: Resolver, level: Optional[str] = None) -> Tuple[ContrastFinding, ...]:
    """Score every declared foreground against the background it will sit on."""
    theme = resolver.theme
    level = level or theme.contrast
    if level is None:
        return ()  # a theme declaring no target (`ansi-16`) is not scored
    normal = LEVEL_RATIOS[level]
    backgrounds = reference_backgrounds(theme.appearance)

    findings: List[ContrastFinding] = []
    for token in resolver.declared():
        style = resolver.style(token)
        if style.fg is None:
            continue  # monochrome depth, or an attrs-only token: nothing to score
        required = NONTEXT_RATIO if token in _NONTEXT_TOKENS else normal
        if style.bg is not None:
            # An explicit pairing — a diff row, a selected line — is scored as
            # the author wrote it rather than against the page background.
            candidates: Tuple[Tuple[int, int, int], ...] = (style.bg.rgb,)
        elif token in _NOT_ON_BACKGROUND:
            continue
        else:
            candidates = backgrounds
        for background in candidates:
            ratio = contrast_ratio(style.fg.rgb, background)
            findings.append(
                ContrastFinding(
                    token=token,
                    foreground=style.fg.rgb,
                    background=background,
                    ratio=ratio,
                    required=required,
                    ok=ratio >= required,
                )
            )
    return tuple(findings)


def check_colorblind(
    resolver: Resolver,
    pairs: Sequence[Tuple[str, str]] = MEANING_PAIRS,
    threshold: float = MEANING_DELTA_E,
) -> Tuple[VisionFinding, ...]:
    """Check that meaning-bearing pairs survive each simulated colour vision."""
    declared = set(resolver.declared())
    findings: List[VisionFinding] = []
    for left, right in pairs:
        if left not in declared or right not in declared:
            continue  # a partial theme is judged on what it actually defines
        a, b = resolver.style(left), resolver.style(right)
        distinct_glyphs = bool(a.glyph and b.glyph and a.glyph != b.glyph)
        for vision in VISION_KINDS:
            if a.fg is None or b.fg is None:
                delta = 0.0
            else:
                delta = delta_e76(simulate(a.fg.rgb, vision), simulate(b.fg.rgb, vision))
            findings.append(
                VisionFinding(
                    pair=(left, right),
                    vision=vision,
                    delta_e=delta,
                    required=threshold,
                    ok=delta >= threshold or distinct_glyphs,
                    via_glyph=distinct_glyphs and delta < threshold,
                )
            )
    return tuple(findings)


def validate_theme(
    resolver: Resolver,
    *,
    level: Optional[str] = None,
    threshold: float = MEANING_DELTA_E,
) -> ThemeReport:
    """Full report: contrast for every token, colour vision for meaning pairs.

    The colourblind check runs only when the theme claims ``colorblindSafe``.
    A theme that does not claim it is not failed for not being it — the claim is
    what is being verified.
    """
    theme = resolver.theme
    backgrounds = reference_backgrounds(theme.appearance)
    return ThemeReport(
        theme=theme.name,
        depth=resolver.depth,
        appearance=theme.appearance,
        level=level or theme.contrast,
        background=backgrounds[0] if len(backgrounds) == 1 else None,
        contrast=check_contrast(resolver, level),
        colorblind=check_colorblind(resolver, threshold=threshold) if theme.colorblind_safe else (),
    )


def assert_theme_valid(resolver: Resolver, **kwargs) -> ThemeReport:
    """Validate, raising :class:`ThemeContrastError` on failure.

    This is the built-in-theme path: the plan says a built-in that fails
    validation fails the suite. User themes should call :func:`validate_theme`
    and warn instead — it is their terminal.
    """
    report = validate_theme(resolver, **kwargs)
    if not report.ok:
        raise ThemeContrastError(report.summary())
    return report
