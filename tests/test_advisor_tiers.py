"""Capability tiers — the check that the advisor is an escalation.

The advisor's whole premise is that consulting it buys capability the session
doesn't already have. Nothing verified that, so pointing ``/advisor`` at a 7B
local model from an Opus session silently escalated *downwards*. These tests
pin the ordering that fixes it, and the two properties that make the ordering
worth having: it is provider-neutral (a ranking that only understands one
vendor defeats the point of a cross-provider advisor), and it never pretends to
know more than it does — an unrecognized model is resolved AND flagged.
"""

from __future__ import annotations

import json

import pytest

from mantis_agent.advisor_tiers import (
    DOWNGRADE,
    ESCALATION,
    SAME_TIER,
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_OVERRIDE,
    SOURCE_PATTERN,
    UNKNOWN,
    CapabilityTable,
    compare,
    load_tier_table,
    normalize_overrides,
    override_problems,
    parse_tier_table,
    resolve_tier,
    tier_table_path,
    user_capability_overrides,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No stray table override, and settings land in a scratch home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.delenv("MANTIS_CAPABILITY_TIERS", raising=False)
    return home


# -- the shipped table -------------------------------------------------------


def test_the_shipped_table_parses_and_is_ranked() -> None:
    """A broken data file is a broken feature, so the ship copy is a test."""
    table = load_tier_table()
    assert isinstance(table, CapabilityTable)
    assert table.rank("frontier") == 5
    assert table.rank("advanced") == 4
    assert table.rank("standard") == 3
    assert table.rank("fast") == 2
    assert table.rank("small") == 1
    assert table.default == "standard"
    assert tier_table_path().name == "capability_tiers.json"


def test_every_table_entry_names_a_real_tier() -> None:
    """A typo'd tier name in the data would silently vanish a model into the
    default; parsing rejects it instead."""
    table = load_tier_table()
    for model, tier in table.models.items():
        assert tier in table.tiers, f"{model} → unknown tier {tier}"
    for pat in table.patterns:
        assert pat.tier in table.tiers, f"{pat.match} → unknown tier {pat.tier}"


def test_a_malformed_table_is_rejected_loudly() -> None:
    with pytest.raises(ValueError):
        parse_tier_table({"capabilityTiers": {"tiers": {}, "default": "standard"}})
    with pytest.raises(ValueError):
        parse_tier_table({"tiers": {"a": {"rank": 1}}, "default": "nope"})
    with pytest.raises(ValueError):
        parse_tier_table({"tiers": {"a": {"rank": 1}}, "default": "a",
                          "models": {"m": "ghost"}})


def test_the_table_is_refreshable_without_a_release(tmp_path, monkeypatch) -> None:
    """Model landscapes move faster than releases, so the table is a file the
    user can point somewhere else."""
    custom = tmp_path / "tiers.json"
    custom.write_text(json.dumps({
        "capabilityTiers": {
            "tiers": {"big": {"rank": 2}, "little": {"rank": 1}},
            "models": {"my-model": "big"},
            "default": "little",
        }
    }), encoding="utf-8")
    monkeypatch.setenv("MANTIS_CAPABILITY_TIERS", str(custom))
    assert resolve_tier("my-model").tier == "big"

    # ...and edits to that file are picked up, not cached for the process life.
    custom.write_text(json.dumps({
        "capabilityTiers": {
            "tiers": {"big": {"rank": 2}, "little": {"rank": 1}},
            "models": {"my-model": "little"},
            "default": "little",
        }
    }), encoding="utf-8")
    assert resolve_tier("my-model").tier == "little"


def test_an_unreadable_custom_table_falls_back_to_the_shipped_one(
    tmp_path, monkeypatch,
) -> None:
    """A bad override file must not take the session down — tiers are advice."""
    bad = tmp_path / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("MANTIS_CAPABILITY_TIERS", str(bad))
    assert resolve_tier("claude-opus-5").tier == "frontier"


# -- resolution: explicit > pattern > default --------------------------------


def test_an_explicit_entry_resolves_with_high_confidence() -> None:
    res = resolve_tier("claude-opus-5")
    assert res.tier == "frontier" and res.rank == 5
    assert res.source == SOURCE_EXPLICIT
    assert res.matched == "claude-opus-5"
    assert res.known is True and res.confidence == "high"


def test_a_pattern_resolves_and_says_which_one() -> None:
    res = resolve_tier("qwen3:32b")
    assert res.tier == "fast"
    assert res.source == SOURCE_PATTERN
    assert res.matched == "*32b*"
    assert res.known is True and res.confidence == "medium"


def test_an_explicit_entry_beats_a_pattern() -> None:
    """``qwen3-235b-a22b`` matches the 235B size pattern (standard) but is
    listed explicitly as advanced — a big MoE is stronger than its dense
    parameter count suggests, and the table is allowed to say so."""
    assert resolve_tier("qwen3-235b-a22b").source == SOURCE_EXPLICIT
    assert resolve_tier("qwen3-235b-a22b").tier == "advanced"


def test_the_most_specific_pattern_wins() -> None:
    """``gpt-oss-120b`` matches both ``*20b*`` and ``*120b*``. The longer
    literal is the more informed guess."""
    res = resolve_tier("gpt-oss-120b")
    assert res.matched == "*120b*" and res.tier == "standard"
    assert resolve_tier("gpt-oss:20b").tier == "fast"


def test_an_unknown_model_defaults_and_is_flagged() -> None:
    """The flag is the whole point: the comparison is then reported as a guess
    rather than as fact."""
    res = resolve_tier("my-finetune:latest")
    assert res.tier == "standard" and res.rank == 3
    assert res.source == SOURCE_DEFAULT
    assert res.known is False and res.confidence == "low"
    assert "unknown" in res.explain().lower()


def test_an_empty_model_is_unknown_not_a_crash() -> None:
    for value in ("", "   ", None):
        res = resolve_tier(value)
        assert res.known is False and res.tier == "standard"


def test_resolution_is_case_insensitive_and_ignores_the_vendor_prefix() -> None:
    """The same weights are spelled four ways across routers; a tier table that
    only recognized one spelling would be a single-vendor table."""
    assert resolve_tier("CLAUDE-OPUS-5").tier == "frontier"
    assert resolve_tier("anthropic/claude-opus-5").tier == "frontier"
    assert resolve_tier("z-ai/GLM-4.7").tier == "advanced"
    assert resolve_tier("accounts/fireworks/models/deepseek-v3").tier == "advanced"


def test_explain_names_the_route_it_took() -> None:
    """``/advisor tiers <model>`` has to answer 'why is this small?' without
    anyone reading the source."""
    assert "explicit" in resolve_tier("claude-opus-5").explain()
    assert "*32b*" in resolve_tier("qwen3:32b").explain()
    assert "override" in resolve_tier("x", overrides={"x": "frontier"}).explain()


# -- user overrides ----------------------------------------------------------


def test_an_override_beats_everything() -> None:
    """Someone running strong weights the table has never heard of must be able
    to say so — and must be able to correct the table when it is wrong."""
    res = resolve_tier("my-local-model", overrides={"my-local-model": "advanced"})
    assert res.tier == "advanced" and res.source == SOURCE_OVERRIDE
    assert res.known is True and res.confidence == "high"

    lowered = resolve_tier("claude-opus-5", overrides={"claude-opus-5": "fast"})
    assert lowered.tier == "fast" and lowered.source == SOURCE_OVERRIDE


def test_overrides_match_the_same_spellings_resolution_does() -> None:
    res = resolve_tier("openrouter/My-Model", overrides={"my-model": "frontier"})
    assert res.tier == "frontier" and res.source == SOURCE_OVERRIDE


def test_a_junk_override_is_dropped_with_a_reason() -> None:
    """Ignoring it silently would leave the user believing a pin took effect."""
    raw = {"good": "fast", "bad": "gigabrain", "worse": 7, "": "fast"}
    assert normalize_overrides(raw) == {"good": "fast"}
    problems = override_problems(raw)
    assert any("gigabrain" in p for p in problems)
    assert len(problems) == 3
    assert resolve_tier("bad", overrides=raw).source == SOURCE_DEFAULT


def test_overrides_come_from_user_settings_only(clean_env, tmp_path) -> None:
    """A cloned repository must not get to assert that its preferred model is
    frontier — ``project``/``local`` live inside the working tree."""
    (clean_env / "settings.json").write_text(json.dumps(
        {"advisor": {"capabilityOverrides": {"my-local-model": "frontier"}}}
    ), encoding="utf-8")
    project = tmp_path / "repo" / ".mantis-agent"
    project.mkdir(parents=True)
    (project / "settings.json").write_text(json.dumps(
        {"advisor": {"capabilityOverrides": {"repo-model": "frontier"}}}
    ), encoding="utf-8")

    found = user_capability_overrides(cwd=tmp_path / "repo")
    assert found == {"my-local-model": "frontier"}
    assert "repo-model" not in found


def test_a_broken_settings_file_yields_no_overrides(clean_env) -> None:
    (clean_env / "settings.json").write_text("{ not json", encoding="utf-8")
    assert user_capability_overrides() == {}


# -- comparison --------------------------------------------------------------


def test_escalation_is_the_normal_case() -> None:
    cmp = compare("claude-opus-5", "qwen3:8b")
    assert cmp.relation == ESCALATION
    assert cmp.confident is True and cmp.severity == "ok"
    assert cmp.is_downgrade is False
    assert cmp.needs_confirmation() is False
    assert "frontier" in cmp.summary and "small" in cmp.summary


def test_same_tier_is_allowed_but_noted() -> None:
    """A same-tier second opinion from a different provider has real value, so
    this is a note rather than a warning."""
    cmp = compare("claude-sonnet-5", "gpt-5.4")
    assert cmp.relation == SAME_TIER
    assert cmp.severity == "note"
    assert cmp.needs_confirmation() is False
    assert "same tier" in cmp.summary.lower()


def test_a_downgrade_is_unmissable_but_never_blocked() -> None:
    """The failure this feature exists to prevent: escalating to something
    weaker. It warns loudly; it does not refuse."""
    cmp = compare("qwen3:7b", "claude-sonnet-5")
    assert cmp.relation == DOWNGRADE
    assert cmp.severity == "warning"
    assert cmp.is_downgrade is True
    assert "DOWNGRADE" in cmp.summary
    assert cmp.needs_confirmation() is True
    assert cmp.needs_confirmation(allow_downgrade=True) is False


def test_an_unknown_side_makes_the_comparison_low_confidence() -> None:
    cmp = compare("my-finetune:latest", "claude-sonnet-5")
    assert cmp.relation == UNKNOWN
    assert cmp.confident is False
    assert "low confidence" in cmp.summary.lower()
    # The ordering is still reported, so the UI can say what it *looks* like.
    assert cmp.ordering == DOWNGRADE
    assert cmp.severity == "warning"      # a suspected downgrade still warns
    assert cmp.needs_confirmation() is True


def test_an_unknown_side_that_looks_fine_is_only_a_note() -> None:
    cmp = compare("claude-opus-5", "my-finetune:latest")
    assert cmp.relation == UNKNOWN and cmp.ordering == ESCALATION
    assert cmp.severity == "note" and cmp.needs_confirmation() is False


def test_both_unknown_is_a_wash() -> None:
    cmp = compare("mystery-a", "mystery-b")
    assert cmp.relation == UNKNOWN and cmp.ordering == SAME_TIER
    assert cmp.confident is False and cmp.severity == "note"


def test_comparison_carries_both_resolutions_and_explains_itself() -> None:
    cmp = compare("claude-opus-5", "qwen3:32b")
    assert cmp.advisor.tier == "frontier" and cmp.main.tier == "fast"
    text = cmp.explain()
    assert "claude-opus-5" in text and "qwen3:32b" in text
    assert "*32b*" in text                    # why the main model ranked there


def test_an_override_changes_the_verdict() -> None:
    """The escape hatch has to actually reach the comparison, or pinning a tier
    is theatre."""
    plain = compare("my-local-model", "claude-sonnet-5")
    assert plain.ordering == DOWNGRADE
    pinned = compare("my-local-model", "claude-sonnet-5",
                     overrides={"my-local-model": "frontier"})
    assert pinned.relation == ESCALATION and pinned.confident is True


def test_comparison_never_raises_on_junk() -> None:
    for advisor, main in (("", ""), (None, "claude-opus-5"), ("claude-opus-5", None)):
        assert compare(advisor, main).relation in (ESCALATION, SAME_TIER, DOWNGRADE, UNKNOWN)


# -- provider neutrality -----------------------------------------------------


@pytest.mark.parametrize(
    ("model", "tier"),
    [
        # Anthropic
        ("claude-opus-5", "frontier"),
        ("claude-sonnet-5", "advanced"),
        ("claude-haiku-4-5-20251001", "fast"),
        # OpenAI
        ("gpt-5.6", "frontier"),
        ("gpt-5.4-mini", "fast"),
        ("gpt-5.4-nano", "small"),
        # Google
        ("gemini-2.5-pro", "advanced"),
        ("gemini-2.5-flash", "fast"),
        # Chinese labs, hosted
        ("deepseek-chat", "advanced"),
        ("glm-4.7", "advanced"),
        ("kimi-k2-0905-preview", "advanced"),
        ("qwen-max", "advanced"),
        # open weights, hosted under org/repo names
        ("openai/gpt-oss-120b", "standard"),
        ("meta-llama/Llama-3.3-70B-Instruct-Turbo", "standard"),
        ("zai-org/GLM-4.7", "advanced"),
        # local Ollama tags — the case no registry will ever cover
        ("qwen3:32b", "fast"),
        ("qwen3:8b", "small"),
        ("llama3:8b", "small"),
        ("llama3.2:3b", "small"),
        ("deepseek-r1:8b", "small"),
        ("gemma2:9b", "small"),
        ("mistral:7b", "small"),
        ("gpt-oss:20b", "fast"),
        ("phi4", "fast"),
        ("qwen3:0.6b", "small"),
        ("mixtral:8x7b", "fast"),
        ("yi:34b", "fast"),
        ("gemma-4-31b", "fast"),
        ("falcon:180b", "standard"),
        ("llama3.1:405b", "standard"),
    ],
)
def test_models_from_every_provider_resolve_sensibly(model: str, tier: str) -> None:
    """No vendor is privileged, and a local tag carrying its own size is read
    for what it says."""
    res = resolve_tier(model)
    assert res.tier == tier, res.explain()
    assert res.known is True


def test_local_sizes_order_against_each_other() -> None:
    """The ladder has to be monotonic in the one signal a local tag carries."""
    ranks = [resolve_tier(m).rank for m in ("llama3:8b", "qwen3:32b", "llama3.3:70b")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 3


def test_a_size_tag_beats_the_family_it_came_from() -> None:
    """``deepseek-r1:8b`` is a distill, not DeepSeek-R1. Escalating to it from a
    hosted DeepSeek would be a downgrade, and the table must see that."""
    assert compare("deepseek-r1:8b", "deepseek-reasoner").relation == DOWNGRADE
    assert compare("deepseek-reasoner", "deepseek-r1:8b").relation == ESCALATION


def test_the_motivating_bug() -> None:
    """The exact configuration that started this: a 7B local advisor for an
    Opus session. It resolves, it compares, and it screams."""
    cmp = compare("qwen3:7b", "claude-opus-5")
    assert cmp.relation == DOWNGRADE and cmp.severity == "warning"
    assert cmp.needs_confirmation() is True
