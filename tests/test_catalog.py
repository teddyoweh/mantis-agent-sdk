"""Tests for the model catalog (``mantis_agent.catalog``): the saved-key store,
last-model + recents persistence, the live-model cache (with TTL), provider
lookup, and key validation. These back the ``mantis`` ``/models`` experience.
"""

from __future__ import annotations

import functools
import json
import time
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


def test_a_model_picker_only_offers_models_you_can_talk_to() -> None:
    """A provider's /v1/models is its whole product line. OpenAI answers with
    image generators, TTS, transcription, embeddings and moderation; xAI ships
    grok-imagine-*. None of them serve a chat completion, so offering one in a
    model picker guarantees a failure at the first request."""
    for mid in ("gpt-image-2.5-sunburst", "chatgpt-image-latest", "sora-2",
                "grok-imagine-image-2.0", "grok-imagine-video-1.5",
                "tts-1-hd", "gpt-4o-mini-tts", "gpt-audio-mini", "whisper-1",
                "gpt-transcribe", "gpt-4o-transcribe-diarize",
                "gpt-realtime-2.1-mini", "gpt-live-1", "gpt-live-transcribe",
                "text-embedding-3-large", "omni-moderation-latest",
                "babbage-002", "davinci-002", "gpt-3.5-turbo-instruct-0914"):
        assert not catalog.is_chat_model_id(mid), mid

    # ...and every real chat model survives. `-instruct` is how essentially
    # every open-weight chat model is named, so it can never be a marker.
    for mid in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5-codex", "gpt-5.1-codex-max",
                "gpt-4o", "gpt-3.5-turbo", "o3-mini", "o4-mini-deep-research",
                "computer-use-preview", "gpt-4o-search-preview",
                "claude-opus-5", "grok-4.6", "grok-build-0.1", "qwen-3.8-27b",
                "gpt-oss-120b", "deepseek-chat", "glm-4.7", "moonshot-v1-128k",
                "llama-3.3-70b-instruct", "Qwen2.5-72B-Instruct", "gemini-3-pro"):
        assert catalog.is_chat_model_id(mid), mid


def test_the_live_cache_filters_non_chat_models_on_both_sides(tmp_home: Path) -> None:
    """Writing filters, and reading filters too — so a cache written by an older
    build can't surface an image model as something you could switch to."""
    catalog.store_live_models("openai", ["gpt-5.4", "gpt-image-1", "whisper-1", "gpt-4o"])
    assert catalog.cached_live_models("openai") == ["gpt-5.4", "gpt-4o"]

    # a cache from a build that had no such filter
    path = catalog._live_path()
    path.write_text(json.dumps({
        "openai": {"models": ["gpt-image-2", "tts-1", "gpt-5.4"], "ts": time.time()}}))
    assert catalog.cached_live_models("openai") == ["gpt-5.4"]


def test_provider_for_model() -> None:
    assert catalog.provider_for_model("claude-opus-5").id == "anthropic"
    assert catalog.provider_for_model("grok-4").id == "xai"
    assert catalog.provider_for_model("gemini-2.5-pro").id == "gemini"
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
    # "no credential set", not "no API key set": Anthropic can be authed by an
    # OAuth bearer token with no x-api-key at all, and the old wording made a
    # perfectly good OAuth session report as a missing key.
    ok, detail = catalog.validate_provider(catalog.BY_ID["deepseek"])
    assert ok is False
    assert "no credential" in detail


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


def test_five_families_are_in_the_catalog() -> None:
    """OpenAI, Claude, Gemini, Grok and the open-source hosts all appear, each
    with the env var the vendor documents."""
    expect = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "xai": "XAI_API_KEY",
        "together": "TOGETHER_API_KEY",
    }
    for pid, env in expect.items():
        assert catalog.BY_ID[pid].api_key_env == env, pid
    assert catalog.BY_ID["xai"].key_env_aliases == ("GROK_API_KEY",)
    assert catalog.BY_ID["anthropic"].models[0] == "claude-opus-5"


def test_xai_alias_words_resolve_to_the_flagship(tmp_home: Path) -> None:
    for word in ("xai", "grok", "x.ai"):
        r = catalog.resolve_model_query(word, [])
        assert r.provider_id == "xai" and r.model == "grok-4", word
