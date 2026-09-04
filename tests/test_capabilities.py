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

    @pytest.mark.parametrize(
        "model,family,ctx",
        [
            ("claude-opus-5", "claude", 1_000_000),
            ("claude-haiku-4-5", "claude", 200_000),
            ("gpt-5.4-mini", "openai", 400_000),
            ("o4-mini", "openai", 200_000),
            ("gemini-2.5-flash", "gemini", 1_048_576),
            ("grok-4", "grok", 256_000),
            ("grok-3-mini", "grok", 131_072),
        ],
    )
    def test_hosted_flagships_resolve_by_prefix(self, model: str, family: str, ctx: int) -> None:
        cap = lookup_model(model)
        assert cap.family == family
        assert cap.supports_native_tools is True
        assert cap.context_window == ctx

    def test_hosted_prefix_is_anchored(self) -> None:
        # ``claude-opus-4`` is not ``claude-opus-4-8``; ``gpt-50`` is not ``gpt-5``.
        assert lookup_model("claude-opus-4").context_window == 200_000
        assert lookup_model("gpt-50").context_window != 400_000


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
            ("https://api.openai.com/v1", "openai"),
            ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini"),
            ("https://api.x.ai/v1", "xai"),
            ("https://api.anthropic.com/v1", "anthropic"),
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


# ---------------------------------------------------------------------------
# structured_output — how each backend takes ``response_format``
# ---------------------------------------------------------------------------


class TestStructuredOutputFlag:
    @pytest.mark.parametrize(
        "profile,expected",
        [
            ("openai", "json_schema"), ("gemini", "json_schema"), ("xai", "json_schema"),
            ("together", "json_schema"), ("fireworks", "json_schema"),
            ("vllm", "json_schema"), ("ollama", "json_schema"), ("llamacpp", "json_schema"),
            ("tgi", "json_schema"), ("modal", "json_schema"), ("cerebras", "json_schema"),
            ("openrouter", "json_schema"), ("mock", "json_schema"),
            ("groq", "json_object"), ("deepseek", "json_object"), ("moonshot", "json_object"),
            ("glm", "json_object"), ("qwen", "json_object"),
            ("anthropic", "none"),
        ],
    )
    def test_hosted_profiles_carry_the_flag(self, profile: str, expected: str) -> None:
        assert HOSTED_PROFILES[profile].structured_output == expected

    def test_every_profile_has_a_valid_value(self) -> None:
        for name, cap in HOSTED_PROFILES.items():
            assert cap.structured_output in ("json_schema", "json_object", "none"), name

    def test_default_is_the_native_envelope(self) -> None:
        cap = BackendCapability(kind="openai_compat", supports_native_tools=True, supports_grammar=True)
        assert cap.structured_output == "json_schema"

    def test_engine_reads_the_flag(self, monkeypatch) -> None:
        """The agent's structured-output gate keys on the flag: Groq's profile
        downgrades to json_object, Anthropic's to prompt-only."""
        from mantis_agent import Agent

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        a = Agent(model="claude-opus-5")
        assert a._structured_output_mode() == "instruct"
        g = Agent(model="llama-3.3-70b-versatile", backend="https://api.groq.com/openai/v1", api_key="k")
        assert g._structured_output_mode() == "json_object"
        o = Agent(model="gpt-5.4", api_key="k")
        assert o._structured_output_mode() == "native"
