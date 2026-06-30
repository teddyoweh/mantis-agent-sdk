"""Tests for the model catalog (``mantis_agent.catalog``): the saved-key store,
last-model + recents persistence, the live-model cache (with TTL), provider
lookup, and key validation. These back the ``mantis`` ``/models`` experience.
"""

from __future__ import annotations

import functools
from pathlib import Path

import httpx
import pytest

from mantis_agent import catalog


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate ``~/.mantis-agent`` to a tmpdir and clear provider key env vars
    so the store starts empty and nothing leaks between tests."""
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    for p in catalog.CATALOG:
        if p.api_key_env:
            monkeypatch.delenv(p.api_key_env, raising=False)
    return tmp_path


def test_key_store_roundtrip(tmp_home: Path) -> None:
    assert catalog.saved_key("deepseek") is None
    catalog.set_key("deepseek", "sk-1")
    assert catalog.saved_key("deepseek") == "sk-1"
    assert catalog.is_enabled(catalog.BY_ID["deepseek"]) is True
    assert catalog.clear_key("deepseek") is True
    assert catalog.saved_key("deepseek") is None
    assert catalog.clear_key("deepseek") is False  # nothing left to clear


def test_api_key_env_precedence(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prov = catalog.BY_ID["deepseek"]
    catalog.set_key("deepseek", "stored")
    monkeypatch.setenv(prov.api_key_env, "from-env")
    assert catalog.api_key_for(prov) == "from-env"  # env wins
    monkeypatch.delenv(prov.api_key_env, raising=False)
    assert catalog.api_key_for(prov) == "stored"  # falls back to the store


def test_last_model_roundtrip(tmp_home: Path) -> None:
    assert catalog.get_last_model() is None
    catalog.set_last_model("deepseek-chat", "https://api.deepseek.com/v1")
    assert catalog.get_last_model() == {
        "model": "deepseek-chat", "backend": "https://api.deepseek.com/v1"}


def test_recent_models_mru_and_cap(tmp_home: Path) -> None:
    assert catalog.get_recent_models() == []
    for m in ["a", "b", "c", "a"]:
        catalog.push_recent_model(m)
    assert catalog.get_recent_models() == ["a", "c", "b"]  # MRU, deduped
    for i in range(catalog.RECENT_MAX + 5):
        catalog.push_recent_model(f"m{i}")
    assert len(catalog.get_recent_models()) == catalog.RECENT_MAX


def test_live_cache_roundtrip_and_ttl(tmp_home: Path) -> None:
    assert catalog.cached_live_models("openai") is None
    catalog.store_live_models("openai", ["gpt-5.4", "gpt-4o"])
    assert catalog.cached_live_models("openai") == ["gpt-5.4", "gpt-4o"]
    assert catalog.cached_live_models("openai", ttl_s=-1) is None  # forced stale
    assert catalog.cached_live_models("nope") is None  # unknown provider


def test_provider_for_model() -> None:
    assert catalog.provider_for_model("deepseek-chat").id == "deepseek"
    assert catalog.provider_for_model("accounts/fireworks/models/x").id == "fireworks"
    assert catalog.provider_for_model("z-ai/glm-4.7").id == "openrouter"
    assert catalog.provider_for_model("gpt-5.4").id == "openai"
    assert catalog.provider_for_model("qwen2.5:1.5b") is None  # local Ollama tag


def test_stores_coexist(tmp_home: Path) -> None:
    """key / last / recent / live-cache all share models.json without clobber."""
    catalog.set_key("deepseek", "sk")
    catalog.set_last_model("deepseek-chat")
    catalog.push_recent_model("deepseek-chat")
    catalog.store_live_models("deepseek", ["deepseek-chat"])
    assert catalog.saved_key("deepseek") == "sk"
    assert catalog.get_last_model()["model"] == "deepseek-chat"
    assert catalog.get_recent_models() == ["deepseek-chat"]
    assert catalog.cached_live_models("deepseek") == ["deepseek-chat"]


def test_validate_provider_no_key(tmp_home: Path) -> None:
    ok, detail = catalog.validate_provider(catalog.BY_ID["deepseek"])
    assert ok is False
    assert "no API key" in detail


@pytest.mark.parametrize(
    ("status", "expect_ok", "needle"),
    [
        (200, True, "models available"),
        (401, False, "invalid API key"),
        (403, False, "invalid API key"),
        (500, False, "HTTP 500"),
    ],
)
def test_validate_provider(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    status: int, expect_ok: bool, needle: str,
) -> None:
    catalog.set_key("deepseek", "sk")
    body = {"data": [{"id": "deepseek-chat"}]} if status == 200 else {}
    transport = httpx.MockTransport(lambda _req: httpx.Response(status, json=body))
    monkeypatch.setattr(httpx, "Client", functools.partial(httpx.Client, transport=transport))
    ok, detail = catalog.validate_provider(catalog.BY_ID["deepseek"])
    assert ok is expect_ok
    assert needle in detail
