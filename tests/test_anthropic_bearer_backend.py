"""``anthropic_bearer_backend`` centralizes the OAuth/gateway-token wiring the
model-switch and auto-wire paths share (api_key_for returns None for these, so
the caller wires api_key=None → env Bearer). It must preserve an existing
Anthropic/gateway backend and never apply to other providers.
"""

from __future__ import annotations

from mantis_agent.catalog import BY_ID, anthropic_bearer_backend


def test_none_without_token(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert anthropic_bearer_backend(BY_ID["anthropic"], "http://localhost:11434") is None


def test_wires_default_api_from_non_anthropic_backend(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    got = anthropic_bearer_backend(BY_ID["anthropic"], "http://localhost:11434")
    assert got == BY_ID["anthropic"].base_url


def test_preserves_existing_gateway_backend(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    gw = "https://bedrock-gw.example.com/anthropic/v1"
    assert anthropic_bearer_backend(BY_ID["anthropic"], gw) == gw
    # The direct API is also "already Anthropic" — kept as-is.
    api = "https://api.anthropic.com/v1"
    assert anthropic_bearer_backend(BY_ID["anthropic"], api) == api


def test_not_applicable_for_other_providers(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")  # irrelevant for openai
    assert anthropic_bearer_backend(BY_ID["openai"], "http://localhost:11434") is None
    assert anthropic_bearer_backend(None, "http://localhost:11434") is None
