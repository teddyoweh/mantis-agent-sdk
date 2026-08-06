"""Theme core: catalogue, schema, resolver, and the computed validators.

The plan's rule is that themes are checked *by computation, not by eye*
(``p_statusline_themes_output_styles.md`` §5), so most of this file is
arithmetic with published answers: WCAG contrast ratios that appear verbatim in
the W3C/WebAIM documentation, and colour-vision simulations that must make a
red/green-only palette fail.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from mantis_agent.theme import (
    DEPTHS,
    LEVEL_RATIOS,
    MEANING_DELTA_E,
    MEANING_PAIRS,
    TOKENS,
    VISION_KINDS,
    Resolver,
    ThemeContrastError,
    ThemeFallbackCycleError,
    ThemeNotFoundError,
    ThemeSchemaError,
    ThemeTokenMissingError,
    assert_theme_valid,
    contrast_ratio,
    delta_e76,
    hex_to_rgb,
    meets,
    parse_theme,
    parse_theme_json,
    relative_luminance,
    simulate,
    validate_theme,
    xterm_rgb,
)

# --------------------------------------------------------------------------
# fixtures: themes built inline so the core stays free of on-disk data
# --------------------------------------------------------------------------

# A palette proven colourblind-safe by the probe in this module's own asserts:
# success is a teal-green rather than a pure green, which is what keeps it apart
# from the red under deuteranopia.
GOOD_TOKENS = {
    "text.primary": {"truecolor": "#e6e6e6", "256": 252},
    "text.secondary": {"truecolor": "#c0c0c0"},
    "text.muted": {"truecolor": "#9e9e9e"},
    "text.inverse": {"truecolor": "#1e1e1e"},
    "accent.primary": {"truecolor": "#5fafff"},
    "accent.secondary": {"truecolor": "#d787ff"},
    "status.success": {"truecolor": "#5fd7af", "256": 79},
    "status.warning": {"truecolor": "#ffd75f"},
    "status.error": {"truecolor": "#ff5f5f", "256": 203, "attrs": ["bold"]},
    "status.info": {"truecolor": "#5fafff"},
    "status.running": {"truecolor": "#5fd7ff"},
    "status.pending": {"truecolor": "#9e9e9e"},
    "status.blocked": {"truecolor": "#ffd75f"},
    "status.cancelled": {"truecolor": "#9e9e9e"},
    "diff.added": {"truecolor": "#5fd7af"},
    "diff.removed": {"truecolor": "#ff5f5f"},
    "diff.context": {"truecolor": "#9e9e9e"},
    "diff.header": {"truecolor": "#5fafff"},
    "syntax.keyword": {"truecolor": "#d787ff"},
    "syntax.string": {"truecolor": "#5fd7af"},
    "syntax.comment": {"truecolor": "#9e9e9e"},
    "syntax.number": {"truecolor": "#ffd75f"},
    "syntax.function": {"truecolor": "#5fafff"},
    "syntax.type": {"truecolor": "#5fd7ff"},
    "ui.border": {"truecolor": "#767676"},
    "ui.selection": {"truecolor": "#767676"},
    "ui.focus": {"truecolor": "#5fafff"},
    "ui.cursor": {"truecolor": "#e6e6e6"},
    "ui.scrollbar": {"truecolor": "#767676"},
    "ui.overlay.bg": {"truecolor": "#2a2a2a"},
    "ui.overlay.border": {"truecolor": "#767676"},
    "tool.read": {"truecolor": "#5fafff"},
    "tool.write": {"truecolor": "#ffd75f"},
    "tool.exec": {"truecolor": "#ff5f5f"},
    "tool.search": {"truecolor": "#5fd7ff"},
    "agent.self": {"truecolor": "#e6e6e6"},
    "agent.child": {"truecolor": "#5fd7ff"},
    "agent.peer": {"truecolor": "#5fafff"},
    "agent.untrusted": {"truecolor": "#d787ff", "attrs": ["italic"]},
}


def good_theme_doc(**over):
    doc = {
        "name": "test-dark",
        "version": 1,
        "appearance": "dark",
        "contrast": "AA",
        "colorblindSafe": True,
        "tokens": dict(GOOD_TOKENS),
        "glyphs": {"status.running": "●", "status.blocked": "⊘"},
    }
    doc.update(over)
    return doc


def solid(color, **over):
    """A theme whose every token is one colour — the shortest way to build a
    deliberately terrible palette."""
    doc = good_theme_doc(tokens={name: {"truecolor": color} for name in TOKENS}, **over)
    return doc


def resolver_for(doc, depth="truecolor", **kw):
    return Resolver(parse_theme(doc), depth=depth, **kw)


# --------------------------------------------------------------------------
# §5 token catalogue
# --------------------------------------------------------------------------


def test_token_catalogue_matches_plan_section_5():
    # Transcribed from the plan's token block. Any drift here is a spec change,
    # not an implementation detail, so it is asserted as an exact set.
    expected = {
        "text.primary", "text.secondary", "text.muted", "text.inverse",
        "accent.primary", "accent.secondary",
        "status.success", "status.warning", "status.error", "status.info",
        "status.running", "status.pending", "status.blocked", "status.cancelled",
        "diff.added", "diff.removed", "diff.context", "diff.header",
        "syntax.keyword", "syntax.string", "syntax.comment", "syntax.number",
        "syntax.function", "syntax.type",
        "ui.border", "ui.selection", "ui.focus", "ui.cursor",
        "ui.scrollbar", "ui.overlay.bg", "ui.overlay.border",
        "tool.read", "tool.write", "tool.exec", "tool.search",
        "agent.self", "agent.child", "agent.peer", "agent.untrusted",
    }
    assert set(TOKENS) == expected
    assert len(TOKENS) == len(expected)  # no duplicates in the ordered tuple


def test_untrusted_token_exists_and_is_distinct_from_self():
    # The plan singles this one out: child/MCP/channel content must be visually
    # separable from first-party output.
    assert "agent.untrusted" in TOKENS
    r = resolver_for(good_theme_doc())
    assert r.style("agent.untrusted").fg != r.style("agent.self").fg


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------


def test_parse_valid_theme():
    theme = parse_theme(good_theme_doc())
    assert theme.name == "test-dark"
    assert theme.appearance == "dark"
    assert theme.contrast == "AA"
    assert theme.colorblind_safe is True
    assert theme.tokens["status.error"].attrs == ("bold",)
    assert theme.tokens["status.error"].fg.truecolor == (255, 95, 95)
    assert theme.tokens["status.error"].fg.c256 == 203
    assert theme.glyphs["status.running"] == "●"


def test_parse_theme_json_round_trips():
    theme = parse_theme_json(json.dumps(good_theme_doc()))
    assert theme.name == "test-dark"


def test_theme_is_immutable():
    theme = parse_theme(good_theme_doc())
    with pytest.raises(TypeError):
        theme.tokens["status.error"] = None


@pytest.mark.parametrize(
    "mutate, needle",
    [
        ({"name": "../../etc/passwd"}, "name"),
        ({"name": "/abs/theme"}, "name"),
        ({"name": ".."}, "name"),
        ({"name": ""}, "name"),
        ({"version": 2}, "version"),
        ({"version": "1"}, "version"),
        ({"appearance": "beige"}, "appearance"),
        ({"contrast": "AAAA"}, "contrast"),
        ({"colorblindSafe": "yes"}, "colorblindSafe"),
        ({"fallback": "../escape"}, "fallback"),
        ({"fallback": 3}, "fallback"),
        ({"tokens": []}, "tokens"),
        ({"tokens": {"status.bogus": {"truecolor": "#ffffff"}}}, "status.bogus"),
        ({"tokens": {"status.error": {"truecolor": "ff5f5f"}}}, "truecolor"),
        ({"tokens": {"status.error": {"truecolor": "#gggggg"}}}, "truecolor"),
        ({"tokens": {"status.error": {"256": 256}}}, "256"),
        ({"tokens": {"status.error": {"256": -1}}}, "256"),
        ({"tokens": {"status.error": {"256": "203"}}}, "256"),
        ({"tokens": {"status.error": {"16": 16}}}, "16"),
        ({"tokens": {"status.error": {"truecolor": "#ff5f5f", "attrs": "bold"}}}, "attrs"),
        ({"tokens": {"status.error": {"truecolor": "#ff5f5f", "attrs": ["blink"]}}}, "attrs"),
        ({"tokens": {"status.error": {"truecolor": "#ff5f5f", "bg": {"attrs": ["bold"]}}}}, "bg"),
        ({"tokens": {"status.error": {}}}, "status.error"),
        ({"tokens": {"status.error": "#ff5f5f"}}, "status.error"),
        ({"glyphs": {"status.nope": "x"}}, "status.nope"),
        ({"glyphs": {"status.running": 3}}, "glyphs"),
        ({"glyphs": {"status.running": "\x1b[31m"}}, "glyphs"),
    ],
)
def test_schema_rejects(mutate, needle):
    with pytest.raises(ThemeSchemaError) as excinfo:
        parse_theme(good_theme_doc(**mutate))
    assert needle in str(excinfo.value)


def test_schema_rejects_non_mapping_document():
    with pytest.raises(ThemeSchemaError):
        parse_theme([1, 2, 3])


def test_schema_rejects_unknown_top_level_key():
    with pytest.raises(ThemeSchemaError) as excinfo:
        parse_theme(good_theme_doc(sneaky=True))
    assert "sneaky" in str(excinfo.value)


def test_partial_theme_is_legal_when_it_declares_a_fallback():
    # "a variant is a small file" — a theme with two tokens is valid on its own.
    doc = {
        "name": "test-dark-tweak",
        "version": 1,
        "appearance": "dark",
        "tokens": {"status.error": {"truecolor": "#ff0000"}},
        "fallback": "test-dark",
    }
    theme = parse_theme(doc)
    assert theme.fallback == "test-dark"
    assert set(theme.tokens) == {"status.error"}


def test_short_hex_and_background_parse():
    doc = good_theme_doc(
        tokens=dict(GOOD_TOKENS, **{"diff.added": {"truecolor": "#5f8", "bg": {"256": 22}}})
    )
    style = parse_theme(doc).tokens["diff.added"]
    assert style.fg.truecolor == (0x55, 0xFF, 0x88)
    assert style.bg.c256 == 22


# --------------------------------------------------------------------------
# resolver: depth chain
# --------------------------------------------------------------------------


def test_depth_chain_constant():
    assert DEPTHS == ("truecolor", "256", "16", "none")


def test_resolves_at_native_depth():
    r = resolver_for(good_theme_doc(), depth="truecolor")
    style = r.style("status.error")
    assert style.fg.depth == "truecolor"
    assert style.fg.rgb == (255, 95, 95)
    assert style.fg.index is None


def test_truecolor_falls_back_to_256_when_absent():
    doc = good_theme_doc(tokens=dict(GOOD_TOKENS, **{"status.error": {"256": 203}}))
    style = resolver_for(doc, depth="truecolor").style("status.error")
    assert style.fg.depth == "256"
    assert style.fg.index == 203
    assert style.fg.rgb == xterm_rgb(203)


def test_256_depth_prefers_declared_256_over_truecolor():
    style = resolver_for(good_theme_doc(), depth="256").style("status.error")
    assert style.fg.depth == "256"
    assert style.fg.index == 203


def test_256_depth_falls_back_to_declared_16():
    doc = good_theme_doc(
        tokens=dict(GOOD_TOKENS, **{"status.error": {"16": 9, "truecolor": "#ff0000"}})
    )
    style = resolver_for(doc, depth="256").style("status.error")
    assert style.fg.depth == "16"
    assert style.fg.index == 9


def test_16_depth_quantizes_down_from_truecolor():
    # Nothing at or below 16 is declared, so the resolver quantises rather than
    # dropping the colour: a truecolor-only theme still paints on a 16-colour
    # terminal.
    style = resolver_for(good_theme_doc(), depth="16").style("status.error")
    assert style.fg.depth == "16"
    assert 0 <= style.fg.index <= 15
    assert style.fg.index == 9  # #ff5f5f is nearest the bright red
    assert style.fg.quantized is True


def test_16_depth_quantizes_from_256_when_that_is_all_there_is():
    doc = good_theme_doc(tokens=dict(GOOD_TOKENS, **{"status.success": {"256": 79}}))
    style = resolver_for(doc, depth="16").style("status.success")
    assert style.fg.depth == "16"
    assert style.fg.quantized is True


def test_quantize_disabled_drops_the_colour():
    style = resolver_for(good_theme_doc(), depth="16", quantize=False).style("status.error")
    assert style.fg is None
    assert style.attrs == ("bold",)  # attributes survive a colourless render


def test_none_depth_is_monochrome_but_keeps_attrs_and_glyphs():
    r = resolver_for(good_theme_doc(), depth="none")
    style = r.style("status.error")
    assert style.fg is None and style.bg is None
    assert style.attrs == ("bold",)
    assert r.style("status.running").glyph == "●"


def test_background_resolves_through_the_same_chain():
    doc = good_theme_doc(
        tokens=dict(GOOD_TOKENS, **{"diff.added": {"truecolor": "#5fd7af", "bg": {"256": 22}}})
    )
    style = resolver_for(doc, depth="truecolor").style("diff.added")
    assert style.bg.depth == "256"
    assert style.bg.rgb == xterm_rgb(22)


def test_unknown_depth_rejected():
    with pytest.raises(ValueError):
        resolver_for(good_theme_doc(), depth="88")


# --------------------------------------------------------------------------
# resolver: inheritance chain
# --------------------------------------------------------------------------


def registry_with_variant():
    base = parse_theme(good_theme_doc())
    variant = parse_theme(
        {
            "name": "test-dark-loud",
            "version": 1,
            "appearance": "dark",
            "tokens": {"status.error": {"truecolor": "#ff0000"}},
            "glyphs": {"status.blocked": "X"},
            "fallback": "test-dark",
        }
    )
    return variant, {"test-dark": base, "test-dark-loud": variant}


def test_fallback_theme_supplies_unspecified_tokens():
    variant, registry = registry_with_variant()
    r = Resolver(variant, depth="truecolor", registry=registry)
    assert r.style("status.error").fg.rgb == (255, 0, 0)      # variant wins
    assert r.style("status.success").fg.rgb == (95, 215, 175)  # inherited
    assert r.style("status.blocked").glyph == "X"              # variant glyph
    assert r.style("status.running").glyph == "●"         # inherited glyph
    assert [t.name for t in r.chain] == ["test-dark-loud", "test-dark"]


def test_fallback_chain_is_three_deep():
    a = parse_theme({"name": "a", "version": 1, "appearance": "dark",
                     "tokens": {"status.error": {"truecolor": "#ff0000"}}, "fallback": "b"})
    b = parse_theme({"name": "b", "version": 1, "appearance": "dark",
                     "tokens": {"status.warning": {"truecolor": "#ffff00"}}, "fallback": "c"})
    c = parse_theme({"name": "c", "version": 1, "appearance": "dark", "tokens": GOOD_TOKENS})
    r = Resolver(a, depth="truecolor", registry={"a": a, "b": b, "c": c})
    assert [t.name for t in r.chain] == ["a", "b", "c"]
    assert r.style("status.warning").fg.rgb == (255, 255, 0)
    assert r.style("status.success").fg.rgb == (95, 215, 175)


def test_fallback_cycle_detected():
    a = parse_theme({"name": "a", "version": 1, "appearance": "dark",
                     "tokens": GOOD_TOKENS, "fallback": "b"})
    b = parse_theme({"name": "b", "version": 1, "appearance": "dark",
                     "tokens": GOOD_TOKENS, "fallback": "a"})
    with pytest.raises(ThemeFallbackCycleError) as excinfo:
        Resolver(a, depth="truecolor", registry={"a": a, "b": b})
    assert "a -> b -> a" in str(excinfo.value)


def test_self_referential_fallback_detected():
    # The degenerate cycle is visible without a registry, so it is refused at
    # parse time rather than waiting for a resolver to be built.
    with pytest.raises(ThemeFallbackCycleError):
        parse_theme({"name": "a", "version": 1, "appearance": "dark",
                     "tokens": GOOD_TOKENS, "fallback": "a"})


def test_missing_fallback_theme_raises_not_found():
    a = parse_theme({"name": "a", "version": 1, "appearance": "dark",
                     "tokens": GOOD_TOKENS, "fallback": "nowhere"})
    with pytest.raises(ThemeNotFoundError) as excinfo:
        Resolver(a, depth="truecolor", registry={"a": a})
    assert "nowhere" in str(excinfo.value)


# --------------------------------------------------------------------------
# resolver: overrides, missing tokens, caching
# --------------------------------------------------------------------------


def test_overrides_beat_every_theme_in_the_chain():
    variant, registry = registry_with_variant()
    r = Resolver(
        variant,
        depth="256",
        registry=registry,
        overrides={"status.error": {"256": 196}, "status.success": {"256": 46}},
    )
    assert r.style("status.error").fg.index == 196
    assert r.style("status.success").fg.index == 46
    assert r.style("status.warning").fg.rgb == (255, 215, 95)  # untouched


def test_invalid_override_rejected_by_the_same_schema():
    with pytest.raises(ThemeSchemaError):
        resolver_for(good_theme_doc(), overrides={"status.error": {"256": 999}})


def test_missing_token_falls_back_to_text_primary_and_is_reported_once():
    tokens = {k: v for k, v in GOOD_TOKENS.items() if k != "status.blocked"}
    r = resolver_for(good_theme_doc(tokens=tokens))
    style = r.style("status.blocked")
    assert style.fg == r.style("text.primary").fg
    assert style.substituted == "text.primary"
    r.style("status.blocked")
    r.style("status.blocked")
    assert r.missing == ("status.blocked",)  # reported once, not per lookup


def test_strict_resolver_raises_on_a_missing_token():
    tokens = {k: v for k, v in GOOD_TOKENS.items() if k != "status.blocked"}
    r = resolver_for(good_theme_doc(tokens=tokens), strict=True)
    with pytest.raises(ThemeTokenMissingError):
        r.style("status.blocked")


def test_missing_text_primary_is_fatal_even_when_lenient():
    tokens = {k: v for k, v in GOOD_TOKENS.items() if k != "text.primary"}
    r = resolver_for(good_theme_doc(tokens=tokens))
    with pytest.raises(ThemeTokenMissingError):
        r.style("text.primary")


def test_unknown_token_name_is_a_programming_error():
    r = resolver_for(good_theme_doc())
    with pytest.raises(KeyError):
        r.style("status.banana")


def test_resolution_is_cached_per_token():
    r = resolver_for(good_theme_doc())
    assert r.style("status.error") is r.style("status.error")


def test_styles_covers_every_token():
    r = resolver_for(good_theme_doc())
    assert set(r.styles()) == set(TOKENS)


# --------------------------------------------------------------------------
# contrast: published WCAG reference values
# --------------------------------------------------------------------------


def test_relative_luminance_endpoints():
    assert relative_luminance((0, 0, 0)) == pytest.approx(0.0)
    assert relative_luminance((255, 255, 255)) == pytest.approx(1.0)
    # The sRGB primaries carry the coefficients of the WCAG formula exactly.
    assert relative_luminance((255, 0, 0)) == pytest.approx(0.2126, abs=1e-6)
    assert relative_luminance((0, 255, 0)) == pytest.approx(0.7152, abs=1e-6)
    assert relative_luminance((0, 0, 255)) == pytest.approx(0.0722, abs=1e-6)


@pytest.mark.parametrize(
    "fg, bg, expected",
    [
        # Values published by W3C/WebAIM for these exact pairs.
        ("#ffffff", "#000000", 21.00),
        ("#ffffff", "#767676", 4.54),   # the canonical AA-on-white boundary
        ("#ffffff", "#595959", 7.00),   # the canonical AAA-on-white boundary
        ("#000000", "#ffff00", 19.56),
        ("#ffffff", "#0000ff", 8.59),
        ("#ffffff", "#777777", 4.48),
        ("#808080", "#808080", 1.00),
    ],
)
def test_contrast_ratio_matches_published_values(fg, bg, expected):
    assert contrast_ratio(hex_to_rgb(fg), hex_to_rgb(bg)) == pytest.approx(expected, abs=0.01)


def test_contrast_ratio_is_symmetric():
    a, b = hex_to_rgb("#123456"), hex_to_rgb("#fedcba")
    assert contrast_ratio(a, b) == pytest.approx(contrast_ratio(b, a))


def test_level_thresholds():
    assert LEVEL_RATIOS["AA"] == 4.5
    assert LEVEL_RATIOS["AAA"] == 7.0
    assert meets(4.5, "AA") and not meets(4.49, "AA")
    assert meets(7.0, "AAA") and not meets(6.99, "AAA")
    # Large text relaxes by one step, per WCAG 1.4.3.
    assert meets(3.0, "AA", large=True)
    assert not meets(2.99, "AA", large=True)
    assert meets(4.5, "AAA", large=True)


# --------------------------------------------------------------------------
# contrast: whole-theme validation
# --------------------------------------------------------------------------


def test_good_theme_passes_its_declared_level():
    report = validate_theme(resolver_for(good_theme_doc()))
    assert report.ok, report.summary()
    assert report.level == "AA"
    assert report.background == hex_to_rgb("#1e1e1e")


def test_good_theme_also_passes_when_resolved_at_256():
    # Same theme, coarser terminal: the quantised colours must still be legible.
    report = validate_theme(resolver_for(good_theme_doc(), depth="256"))
    assert report.ok, report.summary()


def test_low_contrast_theme_fails_and_names_the_tokens():
    # Dark grey text on the dark reference background: ~1.5:1.
    doc = solid("#3a3a3a")
    report = validate_theme(resolver_for(doc))
    assert not report.ok
    failed = {f.token for f in report.contrast if not f.ok}
    assert "text.primary" in failed
    assert "status.error" in failed
    assert all(f.ratio < 4.5 for f in report.contrast if not f.ok)


def test_aaa_is_stricter_than_aa_on_the_same_palette():
    doc = good_theme_doc(
        tokens=dict(GOOD_TOKENS, **{"text.muted": {"truecolor": "#6e6e6e"}}),  # ~3.2:1
    )
    assert not validate_theme(resolver_for(doc)).ok
    doc_aaa = good_theme_doc(contrast="AAA")
    aa_report = validate_theme(resolver_for(good_theme_doc()))
    aaa_report = validate_theme(resolver_for(doc_aaa))
    assert aa_report.ok
    assert aaa_report.level == "AAA"
    assert all(f.required == 7.0 for f in aaa_report.contrast if f.token == "text.primary")


def test_declared_background_overrides_the_reference_background():
    # A token that declares its own bg is judged against that bg, not against
    # the appearance default — otherwise every diff row would be mis-scored.
    doc = good_theme_doc(
        tokens=dict(
            GOOD_TOKENS,
            **{"diff.added": {"truecolor": "#1e1e1e", "bg": {"truecolor": "#5fd7af"}}},
        )
    )
    report = validate_theme(resolver_for(doc))
    finding = next(f for f in report.contrast if f.token == "diff.added")
    assert finding.background == hex_to_rgb("#5fd7af")
    assert finding.ok


def test_light_appearance_validates_against_a_light_background():
    doc = good_theme_doc(
        appearance="light",
        colorblindSafe=False,  # one flat colour; this test is about contrast only
        tokens={name: {"truecolor": "#1a1a1a"} for name in TOKENS},
    )
    report = validate_theme(resolver_for(doc))
    assert report.background == hex_to_rgb("#ffffff")
    assert report.ok


def test_mono_theme_is_vacuously_contrast_clean():
    doc = good_theme_doc(name="mono", appearance="any", contrast="AAA",
                         colorblindSafe=False, tokens={"text.primary": {"truecolor": "#ffffff"}})
    report = validate_theme(Resolver(parse_theme(doc), depth="none"))
    assert report.contrast == ()
    assert report.ok


def test_assert_theme_valid_raises_for_a_builtin():
    with pytest.raises(ThemeContrastError) as excinfo:
        assert_theme_valid(resolver_for(solid("#3a3a3a")))
    assert "text.primary" in str(excinfo.value)
    assert_theme_valid(resolver_for(good_theme_doc()))  # does not raise


# --------------------------------------------------------------------------
# colour-vision simulation
# --------------------------------------------------------------------------


def test_vision_kinds():
    assert VISION_KINDS == ("protanopia", "deuteranopia", "tritanopia")


@pytest.mark.parametrize("kind", VISION_KINDS)
def test_greys_are_invariant_under_simulation(kind):
    # An achromatic colour has nothing for a cone deficiency to lose; the
    # matrices preserve it to within rounding.
    for level in (0, 64, 128, 192, 255):
        out = simulate((level, level, level), kind)
        assert max(abs(c - level) for c in out) <= 2


@pytest.mark.parametrize("kind", VISION_KINDS)
def test_simulation_stays_in_gamut(kind):
    for rgb in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]:
        assert all(0 <= c <= 255 for c in simulate(rgb, kind))


def test_deuteranopia_collapses_pure_red_and_green():
    red, green = hex_to_rgb("#d70000"), hex_to_rgb("#00af00")
    assert delta_e76(red, green) > 100                       # obvious normally
    assert delta_e76(simulate(red, "deuteranopia"),
                     simulate(green, "deuteranopia")) < MEANING_DELTA_E


def test_tritanopia_collapses_blue_and_green_not_red_and_green():
    red, green = hex_to_rgb("#d70000"), hex_to_rgb("#00af00")
    assert delta_e76(simulate(red, "tritanopia"), simulate(green, "tritanopia")) > 100


def test_unknown_vision_kind_rejected():
    with pytest.raises(ValueError):
        simulate((1, 2, 3), "achromatopsia")


def test_delta_e_of_identical_colours_is_zero():
    assert delta_e76((12, 34, 56), (12, 34, 56)) == pytest.approx(0.0)


# --------------------------------------------------------------------------
# colourblind validation of whole themes
# --------------------------------------------------------------------------


def test_meaning_pairs_include_success_versus_error():
    assert ("status.success", "status.error") in MEANING_PAIRS
    assert ("diff.added", "diff.removed") in MEANING_PAIRS
    assert ("agent.self", "agent.untrusted") in MEANING_PAIRS


def test_colourblind_safe_theme_survives_all_three_simulations():
    report = validate_theme(resolver_for(good_theme_doc()))
    assert report.ok, report.summary()
    assert {f.vision for f in report.colorblind} == set(VISION_KINDS)
    assert all(f.ok for f in report.colorblind)


def test_red_green_only_theme_fails_colourblind_validation():
    # The classic mistake: success is green, error is red, nothing else differs.
    # Both colours clear AA against the dark reference background, so the only
    # thing this theme fails on is the colour-vision check — which is the point.
    doc = good_theme_doc(
        tokens=dict(
            GOOD_TOKENS,
            **{
                "status.success": {"truecolor": "#5fd75f"},
                "status.error": {"truecolor": "#ff5f5f"},
                "diff.added": {"truecolor": "#5fd75f"},
                "diff.removed": {"truecolor": "#ff5f5f"},
            },
        )
    )
    report = validate_theme(resolver_for(doc))
    assert not report.ok
    assert all(f.ok for f in report.contrast)
    failures = {(f.pair, f.vision) for f in report.colorblind if not f.ok}
    assert (("status.success", "status.error"), "deuteranopia") in failures
    assert (("diff.added", "diff.removed"), "deuteranopia") in failures
    # ...and it is only the red/green axis that breaks, not tritanopia.
    assert (("status.success", "status.error"), "tritanopia") not in failures
    assert "deuteranopia" in report.summary()


def test_colourblind_check_is_skipped_when_the_theme_does_not_claim_safety():
    doc = good_theme_doc(
        colorblindSafe=False,
        tokens=dict(
            GOOD_TOKENS,
            **{"status.success": {"truecolor": "#5fd75f"},
               "status.error": {"truecolor": "#ff5f5f"}},
        ),
    )
    report = validate_theme(resolver_for(doc))
    assert report.colorblind == ()
    assert report.ok


def test_distinct_glyphs_rescue_an_indistinguishable_pair():
    # This is what makes `mono` a first-class theme rather than a degraded path:
    # with no colour at all, meaning is carried by glyphs, and the validator has
    # to be able to see that.
    doc = good_theme_doc(
        name="mono",
        tokens={name: {"truecolor": "#e6e6e6"} for name in TOKENS},
        glyphs={"status.success": "✓", "status.error": "✗",
                "diff.added": "+", "diff.removed": "-",
                "status.warning": "!", "agent.self": ">", "agent.untrusted": "?"},
    )
    report = validate_theme(Resolver(parse_theme(doc), depth="none"))
    assert report.colorblind
    assert all(f.ok and f.via_glyph for f in report.colorblind)
    assert report.ok


def test_identical_colours_and_no_glyphs_fail_every_vision():
    doc = solid("#e6e6e6", glyphs={})
    report = validate_theme(resolver_for(doc))
    assert all(not f.ok for f in report.colorblind)
    assert all(f.delta_e == pytest.approx(0.0) for f in report.colorblind)


def test_report_summary_is_a_readable_multiline_diagnosis():
    report = validate_theme(resolver_for(solid("#3a3a3a")))
    text = report.summary()
    assert "test-dark" in text
    assert "AA" in text
    assert text.count("\n") >= 1
    assert not validate_theme(resolver_for(good_theme_doc())).summary()


# --------------------------------------------------------------------------
# housekeeping
# --------------------------------------------------------------------------


def test_xterm_palette_reference_points():
    assert xterm_rgb(0) == (0, 0, 0)
    assert xterm_rgb(15) == (255, 255, 255)
    assert xterm_rgb(16) == (0, 0, 0)          # first cube cell
    assert xterm_rgb(231) == (255, 255, 255)   # last cube cell
    assert xterm_rgb(232) == (8, 8, 8)         # first grey
    assert xterm_rgb(255) == (238, 238, 238)   # last grey
    assert xterm_rgb(203) == (255, 95, 95)
    with pytest.raises(ValueError):
        xterm_rgb(256)


def test_theme_package_parses_under_python_39():
    # 3.9 is a supported runtime and this package must import there; the
    # syntax check is the part a 3.13 test run cannot otherwise catch.
    root = Path(__file__).resolve().parent.parent / "mantis_agent" / "theme"
    for path in sorted(root.glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                  feature_version=(3, 9))
