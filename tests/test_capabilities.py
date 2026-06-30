"""Capability registry + tool-use path resolver tests."""

from __future__ import annotations

import pytest

from mantis_agent.capabilities import (
    HOSTED_PROFILES,
    BackendCapability,
    ModelCapability,
    hosted_profile_from_url,
    lookup_model,
    resolve_tool_use_path,
)


# ---------------------------------------------------------------------------
# lookup_model
# ---------------------------------------------------------------------------


class TestLookupModel:
    def test_exact_match_lowercase(self) -> None:
        cap = lookup_model("qwen2.5-72b-instruct")
        assert cap.name == "qwen2.5-72b-instruct"
        assert cap.family == "qwen2.5"
        assert cap.supports_native_tools is True

    def test_case_insensitive(self) -> None:
        cap = lookup_model("Qwen2.5-72B-Instruct")
        assert cap.family == "qwen2.5"

    def test_underscore_normalization(self) -> None:
        # ``_`` should normalize to ``-`` so HF-style slugs work.
        cap = lookup_model("qwen2.5_7b_instruct")
        assert cap.family == "qwen2.5"

    def test_strips_provider_prefix(self) -> None:
        cap = lookup_model("meta-llama/Llama-3.1-70B-Instruct")
        # Path B: substring match against the table — should land on llama3.
        assert cap.family == "llama3"

    def test_family_fallback_qwen(self) -> None:
        cap = lookup_model("qwen2.5-future-model-not-in-table-123")
        assert cap.family == "qwen2.5"
        # Family fallback keeps the native-tools bit on by default for qwen.
        assert cap.supports_native_tools is True

    def test_family_fallback_phi(self) -> None:
        cap = lookup_model("phi-99")
        assert cap.family == "phi"
        # Phi family lacks native tools — Path C territory.
        assert cap.supports_native_tools is False

    def test_unknown_falls_back_to_chatml(self) -> None:
        cap = lookup_model("totally-made-up-model-xyz")
        # Generic fallback: chatml template, no native tools.
        assert cap.chat_template_id == "chatml"
        assert cap.supports_native_tools is False

    def test_known_reasoning_model_emits_thinking(self) -> None:
        cap = lookup_model("qwq-32b-preview")
        assert cap.emits_inline_thinking is True


# ---------------------------------------------------------------------------
# hosted_profile_from_url
# ---------------------------------------------------------------------------


class TestHostedProfileFromUrl:
    @pytest.mark.parametrize(
        "url,expected_hint",
        [
            ("https://api.together.xyz/v1", "together"),
            ("https://api.fireworks.ai/inference/v1", "fireworks"),
            ("https://api.groq.com/openai/v1", "groq"),
            ("https://openrouter.ai/api/v1", "openrouter"),
            ("https://api.deepinfra.com/v1/openai", "deepinfra"),
            ("https://api.cerebras.ai/v1", "cerebras"),
            ("https://api.deepseek.com", "deepseek"),
            ("http://localhost:11434", "ollama"),
        ],
    )
    def test_known_hosts(self, url: str, expected_hint: str) -> None:
        profile = hosted_profile_from_url(url)
        assert profile is not None
        assert profile.provider_hint == expected_hint

    def test_unknown_returns_none(self) -> None:
        assert hosted_profile_from_url("https://my-custom-vllm.example.com/v1") is None

    def test_case_insensitive(self) -> None:
        profile = hosted_profile_from_url("https://API.Fireworks.ai/v1")
        assert profile is not None
        assert profile.provider_hint == "fireworks"


# ---------------------------------------------------------------------------
# resolve_tool_use_path
# ---------------------------------------------------------------------------


class TestResolveToolUsePath:
    def test_native_path_a(self) -> None:
        # Model supports tools, backend supports tools → Path A.
        model = lookup_model("qwen2.5-72b-instruct")
        backend = HOSTED_PROFILES["fireworks"]
        assert resolve_tool_use_path(model, backend) == "A"

    def test_grammar_path_c(self) -> None:
        # Model lacks native tools, backend has grammar → Path C.
        model = ModelCapability(
            name="weird-model",
            family="unknown",
            supports_native_tools=False,
            supports_grammar=True,
        )
        backend = BackendCapability(
            kind="openai_compat",
            supports_native_tools=False,
            supports_grammar=True,
        )
        assert resolve_tool_use_path(model, backend) == "C"

    def test_prompt_only_path_b(self) -> None:
        # No native tools, no grammar → Path B (prompt-engineered text only).
        model = ModelCapability(
            name="primitive",
            family="unknown",
            supports_native_tools=False,
            supports_grammar=False,
        )
        backend = BackendCapability(
            kind="raw",
            supports_native_tools=False,
            supports_grammar=False,
        )
        assert resolve_tool_use_path(model, backend) == "B"

    def test_native_model_but_backend_lacks_native_falls_to_c(self) -> None:
        # If the backend can't speak native tools, fall to grammar-constrained
        # even when the model could — backend wins.
        model = lookup_model("qwen2.5-72b-instruct")  # native=True
        backend = BackendCapability(
            kind="llamacpp",
            supports_native_tools=False,
            supports_grammar=True,
        )
        assert resolve_tool_use_path(model, backend) == "C"
