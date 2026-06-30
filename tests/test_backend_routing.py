"""Auto-route a model name to a backend URL.

``ClaudeAgentOptions(model="qwen2.5:7b")`` works without a ``backend=``
kwarg because ``infer_backend`` maps the Ollama-tag shape to
``http://localhost:11434``. Same script with ``model="Qwen/Qwen2.5-72B-Instruct-Turbo"``
goes to Together AI. Explicit ``backend=`` and ``$MANTIS_AGENT_BASE_URL``
still win over inference.
"""

from __future__ import annotations

import pytest

from mantis_agent.routing import (
    BackendRoutingError,
    FIREWORKS_DEFAULT,
    GEMINI_DEFAULT,
    OLLAMA_DEFAULT,
    OPENAI_DEFAULT,
    TOGETHER_DEFAULT,
    infer_backend,
    resolve_backend,
)


# ---------------------------------------------------------------------------
# infer_backend — pure model-name routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "qwen2.5:7b",
        "deepseek-r1:1.5b",
        "llama3.2:3b",
        "mistral:7b",
        "gemma2:2b",
    ],
)
def test_ollama_tag_routes_to_ollama(model: str) -> None:
    """Ollama tag form (``name:tag``) → localhost:11434."""

    assert infer_backend(model) == OLLAMA_DEFAULT


@pytest.mark.parametrize(
    "model",
    [
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
        "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "deepseek-ai/DeepSeek-V3",
        "google/gemma-2-27b-it",
    ],
)
def test_hf_shape_routes_to_together(model: str) -> None:
    """HuggingFace ``org/repo`` shape → Together AI."""

    assert infer_backend(model) == TOGETHER_DEFAULT


def test_fireworks_path_routes_to_fireworks() -> None:
    assert (
        infer_backend("accounts/fireworks/models/llama-v3p1-70b-instruct")
        == FIREWORKS_DEFAULT
    )


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5",
        "o1-mini",
        "o3-mini",
        "o4-mini",
    ],
)
def test_openai_native_routes_to_openai(model: str) -> None:
    assert infer_backend(model) == OPENAI_DEFAULT


@pytest.mark.parametrize(
    "model",
    [
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini/gemini-2.0-flash",
    ],
)
def test_gemini_routes_to_google(model: str) -> None:
    assert infer_backend(model) == GEMINI_DEFAULT


def test_anthropic_model_raises_with_helpful_message() -> None:
    """We don't proxy Claude — refuse loudly."""

    with pytest.raises(BackendRoutingError) as exc_info:
        infer_backend("claude-sonnet-4-5")

    msg = str(exc_info.value)
    assert "Anthropic" in msg or "claude-agent-sdk" in msg


def test_bare_name_falls_back_to_ollama() -> None:
    """A bare name like 'qwen2.5' with no shape hints → Ollama default."""

    assert infer_backend("qwen2.5") == OLLAMA_DEFAULT
    assert infer_backend("mistral") == OLLAMA_DEFAULT
    assert infer_backend("") == OLLAMA_DEFAULT


# ---------------------------------------------------------------------------
# resolve_backend — precedence chain
# ---------------------------------------------------------------------------


def test_explicit_backend_wins_over_everything(monkeypatch) -> None:
    """``backend=`` passed in always wins, even when env or inference disagree."""

    monkeypatch.setenv("MANTIS_AGENT_BASE_URL", "http://env-says-this.example.com")
    assert (
        resolve_backend("qwen2.5:7b", explicit="http://user-says-this.example.com")
        == "http://user-says-this.example.com"
    )


def test_env_var_wins_over_inference(monkeypatch) -> None:
    """When no ``backend=``, ``$MANTIS_AGENT_BASE_URL`` beats inference."""

    monkeypatch.setenv("MANTIS_AGENT_BASE_URL", "http://vllm-local:8000/v1")
    assert resolve_backend("qwen2.5:7b") == "http://vllm-local:8000/v1"


def test_inference_used_when_no_explicit_no_env(monkeypatch) -> None:
    """No ``backend=``, no env var → fall through to inference."""

    monkeypatch.delenv("MANTIS_AGENT_BASE_URL", raising=False)
    assert resolve_backend("Qwen/Qwen2.5-72B-Instruct-Turbo") == TOGETHER_DEFAULT
    assert resolve_backend("qwen2.5:7b") == OLLAMA_DEFAULT


def test_empty_explicit_is_treated_as_unset(monkeypatch) -> None:
    """``backend=""`` shouldn't short-circuit the chain."""

    monkeypatch.delenv("MANTIS_AGENT_BASE_URL", raising=False)
    assert resolve_backend("qwen2.5:7b", explicit="") == OLLAMA_DEFAULT


# ---------------------------------------------------------------------------
# Integration — exercising via ClaudeAgentOptions/query() path
# ---------------------------------------------------------------------------


def test_compat_query_routes_hf_model_to_together(monkeypatch) -> None:
    """``ClaudeAgentOptions(model="Qwen/...")`` flows through
    ``_build_agent`` which calls ``resolve_backend`` — the constructed
    Agent ends up with the Together base URL even though no
    ``backend=`` was passed."""

    monkeypatch.delenv("MANTIS_AGENT_BASE_URL", raising=False)
    from mantis_agent.compat_query import _build_agent

    agent = _build_agent({"model": "Qwen/Qwen2.5-72B-Instruct-Turbo"})
    assert agent.backend == TOGETHER_DEFAULT


def test_compat_query_routes_ollama_tag_to_localhost(monkeypatch) -> None:
    monkeypatch.delenv("MANTIS_AGENT_BASE_URL", raising=False)
    from mantis_agent.compat_query import _build_agent

    agent = _build_agent({"model": "qwen2.5:7b"})
    assert agent.backend == OLLAMA_DEFAULT


def test_compat_query_explicit_backend_wins(monkeypatch) -> None:
    monkeypatch.setenv("MANTIS_AGENT_BASE_URL", "http://env-default")
    from mantis_agent.compat_query import _build_agent

    agent = _build_agent(
        {"model": "qwen2.5:7b", "backend": "http://explicit-wins"}
    )
    assert agent.backend == "http://explicit-wins"
