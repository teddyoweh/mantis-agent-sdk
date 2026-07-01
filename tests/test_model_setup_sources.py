"""`mantis setup` must cover every model source — local, hosted API, free
hosted, self-host, and Anthropic/Claude — and route each correctly. These lock
in the wiring the /loop built so a regression can't silently drop a source.
"""

from __future__ import annotations

import mantis_agent.catalog as catalog
from mantis_agent.setup_wizard import FREE_PROVIDER_IDS, _probe_openai_models


# -- Anthropic / Claude ------------------------------------------------------


def test_anthropic_is_a_catalog_provider() -> None:
    prov = catalog.BY_ID.get("anthropic")
    assert prov is not None
    assert prov.base_url == "https://api.anthropic.com/v1"
    assert prov.api_key_env == "ANTHROPIC_API_KEY"


def test_claude_models_route_to_anthropic() -> None:
    for m in ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"):
        prov = catalog.provider_for_model(m)
        assert prov is not None and prov.id == "anthropic", m


def test_anthropic_uses_x_api_key_not_bearer() -> None:
    anth = catalog.BY_ID["anthropic"]
    h = catalog._models_headers(anth, "sk-secret")
    assert h["x-api-key"] == "sk-secret"
    assert "anthropic-version" in h
    assert "Authorization" not in h


def test_openai_compat_providers_use_bearer() -> None:
    h = catalog._models_headers(catalog.BY_ID["openai"], "sk-secret")
    assert h == {"Authorization": "Bearer sk-secret"}


def test_no_key_means_no_auth_headers() -> None:
    assert catalog._models_headers(catalog.BY_ID["anthropic"], None) == {}


# -- Free hosted filter ------------------------------------------------------


def test_free_provider_ids_are_all_real_catalog_ids() -> None:
    for pid in FREE_PROVIDER_IDS:
        assert pid in catalog.BY_ID, pid


# -- Self-host probe ---------------------------------------------------------


def test_selfhost_probe_unreachable_returns_none() -> None:
    # A closed port must degrade to None (→ manual model entry), never raise.
    assert _probe_openai_models("http://127.0.0.1:59999/v1", "") is None
