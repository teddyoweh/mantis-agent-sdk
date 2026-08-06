"""Enabling a provider should say what to paste and catch a misfiled paste.

The prompt used to read "paste your CEREBRAS_API_KEY to enable cerebras",
naming an env var and nothing else — not the key's shape, not where to get one.
And a key pasted into the wrong provider row was only caught by the network,
which answers "invalid API key": the key reads as bad rather than misfiled.
"""

from __future__ import annotations

import pytest

from mantis_agent import catalog


def test_every_provider_offers_somewhere_to_get_a_key() -> None:
    for prov in catalog.CATALOG:
        assert prov.id in catalog.KEY_SHAPES, f"{prov.id} has no key hint"
        assert catalog.key_hint(prov.id), f"{prov.id} hint is empty"


def test_cerebras_hint_names_shape_and_console() -> None:
    hint = catalog.key_hint("cerebras")
    assert "csk-" in hint
    assert "cloud.cerebras.ai" in hint


def test_shared_sk_prefix_is_not_claimed_by_anyone() -> None:
    # sk- belongs to OpenAI, DeepSeek, Moonshot and Qwen alike, so it must not
    # appear as a distinctive prefix or the mis-paste guard would misfire.
    for pid in ("openai", "deepseek", "moonshot", "qwen"):
        assert catalog.KEY_SHAPES[pid][0] is None


@pytest.mark.parametrize(
    ("target", "key", "expect_owner"),
    [
        ("cerebras", "sk-ant-api03-" + "A" * 40, True),   # anthropic key -> cerebras
        ("cerebras", "csk-" + "A" * 40, False),           # correct key
        ("groq", "csk-" + "A" * 40, True),                # cerebras key -> groq
        ("anthropic", "sk-ant-oat01-" + "A" * 40, False), # right provider
        ("cerebras", "sk-" + "A" * 40, False),            # ambiguous: let the network judge
        ("openai", "sk-proj-" + "A" * 40, False),         # ambiguous
        ("cerebras", "", False),                          # nothing pasted
    ],
)
def test_misdirected_key_is_conservative(target: str, key: str, expect_owner: bool) -> None:
    got = catalog.misdirected_key(target, key)
    assert (got is not None) is expect_owner, got


def test_misdirected_key_names_the_real_owner() -> None:
    assert catalog.misdirected_key("cerebras", "sk-ant-api03-" + "A" * 40) == \
        catalog.BY_ID["anthropic"].label


def test_cerebras_starter_models_are_ones_the_key_can_reach() -> None:
    # llama-3.3-70b left the public endpoints; it resolves only on a Dedicated
    # Endpoint, so listing it as a starter pick pointed at a dead model.
    models = catalog.BY_ID["cerebras"].models
    assert "llama-3.3-70b" not in models
    assert models[0] == "gpt-oss-120b", "the production model should lead"
    assert "zai-glm-4.7" == models[-1], "deprecated model should sort last"
