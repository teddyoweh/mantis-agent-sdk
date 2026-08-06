"""A provider's model list should come from the provider, not from our memory.

We shipped Cerebras `llama-3.3-70b` well after it left their public endpoints —
a starter pick aimed at a model no key could reach. The ordinary /v1/models
refresh cannot prevent that, because it needs a key the user does not have
until *after* they have picked a model. Cerebras publishes a keyless catalog,
so the picker can show the real line-up for a provider nobody has enabled yet.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mantis_agent import catalog

PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "zai-glm-4.7", "preview": True, "deprecated": False},
        {"id": "gemma-4-31b", "preview": False, "deprecated": False},
        {"id": "gpt-oss-120b", "preview": False, "deprecated": False},
        {"id": "legacy-retired", "preview": False, "deprecated": True},
    ],
}


@pytest.fixture()
def _fake_http(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    seen: dict[str, object] = {}

    class _Client:
        def __init__(self, **kw):
            seen["timeout"] = kw.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            seen["url"] = url
            seen["headers"] = headers
            return httpx.Response(200, content=json.dumps(PAYLOAD),
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "Client", _Client)
    return seen


def test_public_catalog_is_fetched_without_a_key(_fake_http) -> None:
    ids = catalog.refresh_public_models(catalog.BY_ID["cerebras"])
    assert ids is not None
    assert _fake_http["url"] == "https://api.cerebras.ai/public/v1/models"
    # No auth header: the whole point is that this works before a key exists.
    assert not (_fake_http["headers"] or {})


def test_deprecated_models_sort_last_and_previews_after_ga(_fake_http) -> None:
    ids = catalog.refresh_public_models(catalog.BY_ID["cerebras"])
    assert ids == ["gemma-4-31b", "gpt-oss-120b", "zai-glm-4.7", "legacy-retired"]
    # A model being retired must never lead — it would become the default pick.
    assert ids[-1] == "legacy-retired"


def test_the_result_feeds_the_cache_every_consumer_reads(_fake_http) -> None:
    catalog.refresh_public_models(catalog.BY_ID["cerebras"])
    assert catalog.cached_live_models("cerebras") == [
        "gemma-4-31b", "gpt-oss-120b", "zai-glm-4.7", "legacy-retired"]


def test_keyless_refresh_routes_through_the_public_endpoint(_fake_http, monkeypatch) -> None:
    # refresh_live_models with no key must not spend a round trip on a 403.
    monkeypatch.setattr(catalog, "api_key_for", lambda p: None)
    ids = catalog.refresh_live_models(catalog.BY_ID["cerebras"])
    assert ids and _fake_http["url"].endswith("/public/v1/models")


def test_providers_without_a_public_catalog_are_unaffected(_fake_http) -> None:
    assert catalog.refresh_public_models(catalog.BY_ID["openai"]) is None
    assert "url" not in _fake_http, "should not have hit the network"


def test_network_failure_leaves_the_starter_list_alone(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))

    class _Boom:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "Client", _Boom)
    assert catalog.refresh_public_models(catalog.BY_ID["cerebras"]) is None
    assert catalog.cached_live_models("cerebras") is None


@pytest.mark.live
def test_the_real_cerebras_catalog_matches_what_we_ship() -> None:
    """Guards the hardcoded starter list against drift — this is exactly the
    check that would have caught llama-3.3-70b going away."""
    try:
        r = httpx.get("https://api.cerebras.ai/public/v1/models", timeout=10)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"network unavailable: {exc}")
    live = {m["id"] for m in r.json()["data"]}
    shipped = set(catalog.BY_ID["cerebras"].models)
    assert shipped <= live, f"we ship models Cerebras no longer serves: {shipped - live}"
