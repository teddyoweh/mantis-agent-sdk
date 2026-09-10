"""Five first-class provider families, one universal ``thinking`` config.

OpenAI, Anthropic Claude, Google Gemini, xAI Grok and the open-source backends
each take a different native reasoning knob. The providers translate the
universal ``{"type": "adaptive"|"enabled"|"disabled", "budget_tokens": N}``
config into whatever the *backend profile* says the vendor accepts — and never
a field the vendor would 400 on. These tests pin that translation per family,
the xAI routing / key / catalog contract, and the request-time shapes over a
mocked wire (respx).
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest
import respx

from mantis_agent import Agent, catalog
from mantis_agent.budget import lookup_pricing
from mantis_agent.capabilities import (
    HOSTED_PROFILES,
    hosted_profile_from_url,
    lookup_model,
)
from mantis_agent.context_limits import parse_limit
from mantis_agent.events import ContentBlockDelta, ContentBlockStart, ThinkingDelta
from mantis_agent.providers.anthropic_passthrough import (
    AnthropicPassthroughProvider,
    _claude_generation,
    _sampling_removed,
)
from mantis_agent.providers.base import detect_provider
from mantis_agent.providers.openai_compat import (
    OpenAICompatProvider,
    env_key_candidates,
)
from mantis_agent.routing import GEMINI_DEFAULT, OPENAI_DEFAULT, XAI_DEFAULT
from mantis_agent.types import ThinkingBlock, UserMessage


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _payload(
    base_url: str,
    model: str,
    *,
    thinking: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    temperature: float | None = 0.7,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    p = OpenAICompatProvider(base_url=base_url, api_key="x")
    return p._build_payload(
        model=model,
        messages=[UserMessage(content="hi")],
        system=None,
        tools=tools,
        max_tokens=100,
        temperature=temperature,
        extra=extra,
        path="A",
        thinking=thinking,
        model_capability=lookup_model(model),
    )


def _sse(*chunks: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1", "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


async def _drain(provider: Any, **kw: Any) -> list[Any]:
    out: list[Any] = []
    async for ev in provider.stream(**kw):
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# xAI Grok — routing, env key, catalog
# ---------------------------------------------------------------------------


class TestXaiContract:
    def test_url_detects_openai_compat_with_xai_profile(self) -> None:
        assert detect_provider(XAI_DEFAULT) == "openai_compat"
        profile = hosted_profile_from_url("https://api.x.ai/v1")
        assert profile is HOSTED_PROFILES["xai"]
        assert profile.provider_hint == "xai"
        assert profile.supports_native_tools is True
        assert profile.kind == "openai_compat"

    def test_provider_adopts_xai_profile(self) -> None:
        p = OpenAICompatProvider(base_url=XAI_DEFAULT, api_key="xai-k")
        assert p.backend_capability.provider_hint == "xai"
        anyio.run(p.aclose)

    def test_xai_api_key_resolves_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "xai-primary")
        monkeypatch.delenv("GROK_API_KEY", raising=False)
        p = OpenAICompatProvider(base_url=XAI_DEFAULT)
        assert p.client.headers["authorization"] == "Bearer xai-primary"
        anyio.run(p.aclose)

    def test_grok_api_key_alias_is_accepted(self, monkeypatch) -> None:
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        monkeypatch.setenv("GROK_API_KEY", "xai-alias")
        p = OpenAICompatProvider(base_url=XAI_DEFAULT)
        assert p.client.headers["authorization"] == "Bearer xai-alias"
        anyio.run(p.aclose)

    def test_vendor_key_outranks_a_stale_openai_key(self, monkeypatch) -> None:
        """A shell with OPENAI_API_KEY exported must not send it to x.ai."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-stale")
        monkeypatch.setenv("XAI_API_KEY", "xai-right")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-right")
        assert env_key_candidates(XAI_DEFAULT)[:2] == ("XAI_API_KEY", "GROK_API_KEY")
        assert env_key_candidates(GEMINI_DEFAULT)[:2] == ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        assert env_key_candidates(OPENAI_DEFAULT)[0] == "OPENAI_API_KEY"
        # Unknown / self-hosted URLs keep the generic chain.
        assert env_key_candidates("http://gpu-box:8000/v1")[0] == "OPENAI_API_KEY"
        p = OpenAICompatProvider(base_url=XAI_DEFAULT)
        assert p.client.headers["authorization"] == "Bearer xai-right"
        anyio.run(p.aclose)
        g = OpenAICompatProvider(base_url=GEMINI_DEFAULT)
        assert g.client.headers["authorization"] == "Bearer AIza-right"
        anyio.run(g.aclose)

    def test_agent_with_bare_grok_name_points_at_xai(self, monkeypatch) -> None:
        monkeypatch.delenv("MANTIS_AGENT_BASE_URL", raising=False)
        monkeypatch.setenv("XAI_API_KEY", "xai-k")
        agent = Agent(model="grok-4")
        assert isinstance(agent.provider, OpenAICompatProvider)
        assert str(agent.provider.client.base_url).rstrip("/") == XAI_DEFAULT
        assert agent.backend_capability is not None
        assert agent.backend_capability.provider_hint == "xai"
        anyio.run(agent.provider.aclose)

    def test_agent_with_bare_oss_name_keeps_the_vllm_default(self, monkeypatch) -> None:
        """Unchanged: a plain open-weight id with no backend stays on
        localhost:8000 — routing to a vendor is only for first-party names."""
        monkeypatch.delenv("MANTIS_AGENT_BASE_URL", raising=False)
        agent = Agent(model="qwen2.5-7b-instruct", api_key="")
        assert str(agent.provider.client.base_url).rstrip("/") == "http://localhost:8000/v1"
        anyio.run(agent.provider.aclose)

    def test_catalog_entry(self) -> None:
        prov = catalog.BY_ID["xai"]
        assert prov.label == "Grok (xAI)"
        assert prov.base_url == "https://api.x.ai/v1"
        assert prov.api_key_env == "XAI_API_KEY"
        assert "GROK_API_KEY" in prov.key_env_aliases
        assert prov.models[0] == "grok-4"
        assert {"grok-4-fast", "grok-3", "grok-3-mini"} <= set(prov.models)

    def test_catalog_key_hint_and_misdirection(self) -> None:
        hint = catalog.key_hint("xai")
        assert "xai-" in hint and "console.x.ai" in hint
        # An xai- key pasted into another provider's row is caught locally.
        assert catalog.misdirected_key("openai", "xai-" + "A" * 40) == "Grok (xAI)"
        assert catalog.misdirected_key("xai", "xai-" + "A" * 40) is None

    def test_catalog_alias_and_model_lookup(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
        assert catalog.provider_for_model("grok-4").id == "xai"
        assert catalog.provider_for_model("grok/grok-3-mini").id == "xai"
        assert catalog.api_key_for(catalog.BY_ID["xai"]) is None or True  # env-dependent
        monkeypatch.setenv("GROK_API_KEY", "xai-alias")
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        assert catalog.api_key_for(catalog.BY_ID["xai"]) == "xai-alias"
        assert catalog.is_enabled(catalog.BY_ID["xai"]) is True
        r = catalog.resolve_model_query("grok", [])
        assert r.provider_id == "xai" and r.model == "grok-4"

    def test_provider_guide_exists(self) -> None:
        from mantis_agent.provider_guides import guide_for

        g = guide_for("xai")
        assert g is not None and g["env_var"] == "XAI_API_KEY"
        assert "console.x.ai" in g["keys_url"]


# ---------------------------------------------------------------------------
# Capabilities / pricing / context tables — five families
# ---------------------------------------------------------------------------


class TestHostedTables:
    @pytest.mark.parametrize(
        ("model", "native", "reasoning", "ctx"),
        [
            ("claude-opus-5", True, True, 1_000_000),
            ("claude-sonnet-5", True, True, 1_000_000),
            ("claude-haiku-4-5", True, True, 200_000),
            ("claude-haiku-4-5-20251001", True, True, 200_000),
            ("claude-opus-4-8", True, True, 1_000_000),
            ("claude-sonnet-4-5", True, True, 200_000),   # family floor
            ("gpt-5", True, True, 400_000),
            ("gpt-5.4-mini", True, True, 400_000),
            ("o3", True, True, 200_000),
            ("o4-mini", True, True, 200_000),
            ("gpt-4o", True, False, 128_000),
            ("gemini-2.5-pro", True, True, 1_048_576),
            ("gemini-2.5-flash", True, True, 1_048_576),
            ("grok-4", True, False, 256_000),
            ("grok-4-fast", True, False, 2_000_000),
            ("grok-3-mini", True, True, 131_072),
            ("grok-3", True, False, 131_072),
            ("grok-code-fast-1", True, False, 256_000),
            ("grok-9-future", True, False, 131_072),       # family floor
        ],
    )
    def test_capability_rows(self, model: str, native: bool, reasoning: bool, ctx: int) -> None:
        cap = lookup_model(model)
        assert cap.supports_native_tools is native, model
        assert cap.supports_reasoning_effort is reasoning, model
        assert cap.context_window == ctx, model

    def test_prefix_match_does_not_bleed_across_generations(self) -> None:
        # ``claude-opus-4`` must not inherit the 1M window of ``claude-opus-4-8``.
        assert lookup_model("claude-opus-4").context_window == 200_000
        assert lookup_model("claude-opus-4-1").context_window == 200_000

    @pytest.mark.parametrize(
        ("hint", "model", "prompt", "completion"),
        [
            ("anthropic", "claude-opus-5", 5.00, 25.00),
            ("anthropic", "claude-opus-5-20260401", 5.00, 25.00),    # dated → prefix
            ("anthropic", "claude-sonnet-5", 2.00, 10.00),
            ("anthropic", "claude-haiku-4-5", 1.00, 5.00),
            ("anthropic", "claude-fable-5-1", 10.00, 50.00),
            ("openai", "gpt-6-astra", 10.00, 50.00),
            ("openai", "gpt-5", 1.25, 10.00),
            ("openai", "gpt-5-mini", 0.25, 2.00),
            ("openai", "o3", 2.00, 8.00),
            ("openai", "o4-mini", 1.10, 4.40),
            ("gemini", "gemini-2.5-pro", 1.25, 10.00),
            ("gemini", "gemini-2.5-flash", 0.30, 2.50),
            ("xai", "grok-4", 3.00, 15.00),
            ("xai", "grok-4-fast-reasoning", 0.20, 0.50),
            ("xai", "grok-3-mini", 0.30, 0.50),
        ],
    )
    def test_pricing_rows(self, hint: str, model: str, prompt: float, completion: float) -> None:
        pricing = lookup_pricing(model, hint)
        assert pricing is not None, (hint, model)
        assert (pricing.prompt_per_million, pricing.completion_per_million) == (prompt, completion)

    def test_pricing_prefix_never_overreaches(self) -> None:
        # ``gpt-5`` must not claim ``gpt-50`` and ``o1`` must not claim ``o1x``.
        assert lookup_pricing("gpt-50", "openai") is None
        assert lookup_pricing("o1x", "openai") is None
        # Without a hint the model is still found (Agent(model="claude-opus-5")
        # has no URL to derive a hint from).
        assert lookup_pricing("claude-opus-5-20260401").prompt_per_million == 5.00

    def test_context_limit_patterns_for_new_families(self) -> None:
        assert parse_limit(
            "This model's maximum prompt length is 131072 but the request contains 150000 tokens"
        ) == 131072
        assert parse_limit(
            "The input token count (1100000) exceeds the maximum number of tokens allowed (1048576)."
        ) == 1048576


# ---------------------------------------------------------------------------
# OpenAI reasoning models — max_completion_tokens, no temperature, effort
# ---------------------------------------------------------------------------


class TestOpenAIReasoningShape:
    @pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5", "gpt-5.4", "gpt-5-mini", "o1", "o3", "o4-mini", "o3-pro"])
    def test_reasoning_models_use_max_completion_tokens_and_no_temperature(self, model: str) -> None:
        pl = _payload(OPENAI_DEFAULT, model, thinking={"type": "enabled", "budget_tokens": 4096})
        assert pl["max_completion_tokens"] == 100
        assert "max_tokens" not in pl
        assert "temperature" not in pl
        assert pl["reasoning_effort"] == "high"

    def test_chat_models_keep_the_legacy_fields_and_get_no_knob(self) -> None:
        pl = _payload(OPENAI_DEFAULT, "gpt-4o", thinking={"type": "enabled"})
        assert pl["max_tokens"] == 100
        assert pl["temperature"] == 0.7
        assert "reasoning_effort" not in pl

    def test_disabled_is_none_on_gpt5_but_omitted_when_unsupported(self) -> None:
        assert _payload(OPENAI_DEFAULT, "gpt-5.4", thinking={"type": "disabled"})["reasoning_effort"] == "none"
        # GPT-6 Astra and the o-series cannot switch reasoning off and reject "none".
        for model in ("gpt-6-astra", "o3"):
            assert "reasoning_effort" not in _payload(OPENAI_DEFAULT, model, thinking={"type": "disabled"})

    def test_effort_words_normalize(self) -> None:
        assert "reasoning_effort" not in _payload(OPENAI_DEFAULT, "gpt-6-astra", extra={"effort": "none"})
        assert _payload(OPENAI_DEFAULT, "gpt-5.4", extra={"effort": "ultra"})["reasoning_effort"] == "high"
        assert _payload(OPENAI_DEFAULT, "gpt-5.4", extra={"effort": "max"})["reasoning_effort"] == "high"
        assert _payload(OPENAI_DEFAULT, "gpt-5.4", extra={"effort": "minimal"})["reasoning_effort"] == "low"
        assert _payload(OPENAI_DEFAULT, "gpt-5.4", extra={"effort": "xhigh"})["reasoning_effort"] == "xhigh"

    def test_streamed_reasoning_delta_becomes_a_thinking_block(self) -> None:
        """OpenAI-style gateways stream ``delta.reasoning`` (sometimes wrapped);
        every shape lands in the thinking channel, never in the text."""
        body = _sse(
            _chunk({"role": "assistant", "reasoning": {"text": "let me think"}}),
            _chunk({"reasoning": " more"}),
            _chunk({"content": "answer"}),
            _chunk({}, finish="stop"),
        )
        with respx.mock:
            respx.post(f"{OPENAI_DEFAULT}/chat/completions").mock(
                return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
            p = OpenAICompatProvider(base_url=OPENAI_DEFAULT, api_key="x")
            events = anyio.run(lambda: _drain(
                p, model="o3", messages=[UserMessage(content="hi")], max_tokens=50,
                model_capability=lookup_model("o3")))
            anyio.run(p.aclose)
        thinking = "".join(
            ev.delta.thinking for ev in events
            if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, ThinkingDelta))
        assert thinking == "let me think more"
        starts = [ev.block for ev in events if isinstance(ev, ContentBlockStart)]
        assert isinstance(starts[0], ThinkingBlock)


# ---------------------------------------------------------------------------
# Gemini — reasoning_effort for effort words, thinking_config for budgets
# ---------------------------------------------------------------------------


class TestGeminiThinking:
    def test_explicit_budget_goes_to_google_thinking_config(self) -> None:
        pl = _payload(GEMINI_DEFAULT, "gemini-2.5-pro", thinking={"type": "enabled", "budget_tokens": 4096})
        assert pl["extra_body"]["google"]["thinking_config"]["thinking_budget"] == 4096
        assert "reasoning_effort" not in pl
        assert pl["max_tokens"] == 100 and pl["temperature"] == 0.7

    def test_enabled_without_budget_is_high_effort(self) -> None:
        pl = _payload(GEMINI_DEFAULT, "gemini-2.5-flash", thinking={"type": "enabled"})
        assert pl["reasoning_effort"] == "high"
        assert "extra_body" not in pl

    def test_adaptive_is_geminis_own_dynamic_default(self) -> None:
        pl = _payload(GEMINI_DEFAULT, "gemini-2.5-pro", thinking={"type": "adaptive"})
        assert "reasoning_effort" not in pl and "extra_body" not in pl

    def test_disabled_is_none(self) -> None:
        pl = _payload(GEMINI_DEFAULT, "gemini-2.5-flash", thinking={"type": "disabled"})
        assert pl["reasoning_effort"] == "none"

    def test_effort_words_clamp_to_what_google_accepts(self) -> None:
        assert _payload(GEMINI_DEFAULT, "gemini-2.5-pro", extra={"effort": "xhigh"})["reasoning_effort"] == "high"
        assert _payload(GEMINI_DEFAULT, "gemini-2.5-pro", extra={"effort": "ultra"})["reasoning_effort"] == "high"
        assert _payload(GEMINI_DEFAULT, "gemini-2.5-pro", extra={"effort": "minimal"})["reasoning_effort"] == "low"

    def test_user_extra_body_merges_with_the_translated_budget(self) -> None:
        """Opting into thought summaries must not wipe the budget (or vice
        versa) — the vendor namespace is deep-merged."""
        pl = _payload(
            GEMINI_DEFAULT, "gemini-2.5-pro",
            thinking={"type": "enabled", "budget_tokens": 2048},
            extra={"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}},
        )
        cfg = pl["extra_body"]["google"]["thinking_config"]
        assert cfg == {"thinking_budget": 2048, "include_thoughts": True}

    def test_gemini_id_on_an_openai_base_speaks_openai(self) -> None:
        """The backend profile decides the dialect — a Gemini id through a base
        that reports itself as OpenAI gets OpenAI's knob (and the existing
        contract: adaptive → medium)."""
        pl = _payload(OPENAI_DEFAULT, "gemini-2.5-pro", thinking={"type": "adaptive"})
        assert pl["reasoning_effort"] == "medium"
        assert "extra_body" not in pl

    def test_thought_parts_stream_as_thinking(self) -> None:
        """With ``include_thoughts`` Google flags thought summaries on the
        content delta via ``extra_content.google.thought`` — they must land in
        the thinking channel, not the answer."""
        body = _sse(
            _chunk({"role": "assistant", "content": "Plan: ", "extra_content": {"google": {"thought": True}}}),
            _chunk({"content": "check the file", "extra_content": {"google": {"thought": True}}}),
            _chunk({"content": "Done."}),
            _chunk({}, finish="stop"),
        )
        with respx.mock:
            respx.post(f"{GEMINI_DEFAULT}/chat/completions").mock(
                return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
            p = OpenAICompatProvider(base_url=GEMINI_DEFAULT, api_key="x")
            events = anyio.run(lambda: _drain(
                p, model="gemini-2.5-pro", messages=[UserMessage(content="hi")], max_tokens=50,
                model_capability=lookup_model("gemini-2.5-pro")))
            anyio.run(p.aclose)
        thinking = "".join(
            ev.delta.thinking for ev in events
            if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, ThinkingDelta))
        text = "".join(
            ev.delta.text for ev in events
            if isinstance(ev, ContentBlockDelta) and not isinstance(ev.delta, ThinkingDelta)
            and hasattr(ev.delta, "text"))
        assert thinking == "Plan: check the file"
        assert text == "Done."


# ---------------------------------------------------------------------------
# xAI Grok — reasoning_effort low/high on grok-3-mini, reasoning_content
# ---------------------------------------------------------------------------


class TestGrokThinking:
    def test_grok_3_mini_takes_low_or_high(self) -> None:
        assert _payload(XAI_DEFAULT, "grok-3-mini", thinking={"type": "enabled", "budget_tokens": 2048})["reasoning_effort"] == "low"
        assert _payload(XAI_DEFAULT, "grok-3-mini", thinking={"type": "enabled", "budget_tokens": 12288})["reasoning_effort"] == "high"
        assert _payload(XAI_DEFAULT, "grok-3-mini", thinking={"type": "enabled"})["reasoning_effort"] == "high"
        assert _payload(XAI_DEFAULT, "grok-3-mini", extra={"effort": "medium"})["reasoning_effort"] == "high"
        assert _payload(XAI_DEFAULT, "grok-3-mini", extra={"effort": "minimal"})["reasoning_effort"] == "low"

    @pytest.mark.parametrize("model", ["grok-4", "grok-4-fast", "grok-3", "grok-code-fast-1"])
    def test_fixed_reasoning_models_never_get_the_field(self, model: str) -> None:
        """grok-4 / grok-3 / grok-code-fast reason at a fixed level and 400 on
        ``reasoning_effort`` — neither the thinking config nor an effort word
        may put it on the wire."""
        for kw in (
            {"thinking": {"type": "enabled", "budget_tokens": 8192}},
            {"thinking": {"type": "adaptive"}},
            {"thinking": {"type": "disabled"}},
            {"extra": {"effort": "high"}},
        ):
            pl = _payload(XAI_DEFAULT, model, **kw)  # type: ignore[arg-type]
            assert "reasoning_effort" not in pl, (model, kw)
            assert "effort" not in pl
        # Standard Chat Completions fields otherwise.
        pl = _payload(XAI_DEFAULT, model)
        assert pl["max_tokens"] == 100 and pl["temperature"] == 0.7

    def test_grok_through_a_generic_proxy_still_speaks_grok(self) -> None:
        pl = _payload("http://my-proxy:8000/v1", "grok-3-mini", thinking={"type": "enabled", "budget_tokens": 1024})
        assert pl["reasoning_effort"] == "low"
        assert "reasoning_effort" not in _payload("http://my-proxy:8000/v1", "grok-4", thinking={"type": "enabled"})

    def test_reasoning_content_deltas_become_a_thinking_block(self) -> None:
        body = _sse(
            _chunk({"role": "assistant", "reasoning_content": "Thinking about "}),
            _chunk({"reasoning_content": "the answer."}),
            _chunk({"content": "42"}),
            _chunk({}, finish="stop"),
        )
        with respx.mock:
            route = respx.post(f"{XAI_DEFAULT}/chat/completions").mock(
                return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
            p = OpenAICompatProvider(base_url=XAI_DEFAULT, api_key="xai-k")
            events = anyio.run(lambda: _drain(
                p, model="grok-4", messages=[UserMessage(content="hi")], max_tokens=50,
                model_capability=lookup_model("grok-4"),
                thinking={"type": "enabled", "budget_tokens": 8192}))
            anyio.run(p.aclose)
        sent = json.loads(route.calls[0].request.content)
        assert "reasoning_effort" not in sent
        assert route.calls[0].request.headers["authorization"] == "Bearer xai-k"
        blocks = [ev.block for ev in events if isinstance(ev, ContentBlockStart)]
        assert isinstance(blocks[0], ThinkingBlock)
        thinking = "".join(
            ev.delta.thinking for ev in events
            if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, ThinkingDelta))
        assert thinking == "Thinking about the answer."
        text = "".join(
            getattr(ev.delta, "text", "") for ev in events if isinstance(ev, ContentBlockDelta))
        assert text == "42"


# ---------------------------------------------------------------------------
# Anthropic — the thinking form follows the model generation
# ---------------------------------------------------------------------------


_ANTHROPIC_DONE = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _anthropic_body(
    model: str,
    *,
    thinking: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int = 1024,
    responses: list[httpx.Response] | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """``(bodies_sent, events)`` for one stream against a mocked api.anthropic.com."""
    with respx.mock:
        route = respx.post("https://api.anthropic.com/v1/messages")
        if responses:
            route.side_effect = responses
        else:
            route.mock(return_value=httpx.Response(
                200, text=_ANTHROPIC_DONE, headers={"content-type": "text/event-stream"}))
        p = AnthropicPassthroughProvider(api_key="sk-ant-test")
        events = anyio.run(lambda: _drain(
            p, model=model, messages=[UserMessage(content="hi")], max_tokens=max_tokens,
            temperature=temperature, extra=extra, thinking=thinking))
        anyio.run(p.aclose)
        bodies = [json.loads(c.request.content) for c in route.calls]
    return bodies, events


class TestClaudeGenerations:
    @pytest.mark.parametrize(
        ("model", "generation"),
        [
            ("claude-haiku-4-5", "budget"),
            ("claude-haiku-4-5-20251001", "budget"),
            ("claude-sonnet-4-5", "budget"),
            ("claude-opus-4-1-20250805", "budget"),
            ("claude-sonnet-4-20250514", "budget"),       # date suffix is not a minor
            ("claude-3-5-sonnet-20241022", "budget"),
            ("claude-opus-4-6", "hybrid"),
            ("claude-sonnet-4-6", "hybrid"),
            ("claude-opus-4-7", "adaptive"),
            ("claude-opus-4-8", "adaptive"),
            ("claude-opus-5", "adaptive"),
            ("claude-sonnet-5", "adaptive"),
            ("claude-opus-5-20260401", "adaptive"),
            ("claude-fable-5-1", "always_on"),
            ("claude-mythos-5", "always_on"),
            ("claude-something-new", "adaptive"),          # unknown → modern form
        ],
    )
    def test_generation_table(self, model: str, generation: str) -> None:
        assert _claude_generation(model) == generation

    def test_sampling_removed_only_on_current_models(self) -> None:
        assert _sampling_removed("claude-opus-5")
        assert _sampling_removed("claude-opus-4-7")
        assert _sampling_removed("claude-fable-5-1")
        assert not _sampling_removed("claude-sonnet-4-6")
        assert not _sampling_removed("claude-haiku-4-5")

    def test_opus_5_gets_adaptive_plus_effort_never_budget_tokens(self) -> None:
        """Opus 5 / Sonnet 5 / 4.7+ reject ``budget_tokens`` with a 400 — the
        agent's per-turn budget becomes an ``output_config.effort`` level."""
        for budget, effort in ((2048, "low"), (8192, "medium"), (12288, "high"), (24576, "max")):
            (body,), _ = _anthropic_body(
                "claude-opus-5", thinking={"type": "enabled", "budget_tokens": budget})
            assert body["thinking"] == {"type": "adaptive"}, budget
            assert body["output_config"] == {"effort": effort}, budget
            assert "budget_tokens" not in json.dumps(body)

    def test_effort_words_on_opus_5(self) -> None:
        for word, expect in (("ultra", "max"), ("max", "max"), ("xhigh", "xhigh"),
                             ("minimal", "low"), ("high", "high")):
            (body,), _ = _anthropic_body("claude-opus-5", extra={"effort": word})
            assert body["output_config"] == {"effort": expect}, word
            assert "effort" not in body and "thinking" not in body

    def test_xhigh_drops_to_high_on_the_46_pair(self) -> None:
        (body,), _ = _anthropic_body("claude-sonnet-4-6", extra={"effort": "xhigh"})
        assert body["output_config"] == {"effort": "high"}

    def test_haiku_4_5_keeps_the_budget_form_and_raises_max_tokens(self) -> None:
        """The budget generation needs ``budget_tokens < max_tokens``; a 24k
        "max" budget against the agent's 8k default would 400, so max_tokens
        is lifted above the budget. No effort field exists there."""
        (body,), _ = _anthropic_body(
            "claude-haiku-4-5", thinking={"type": "enabled", "budget_tokens": 24576},
            extra={"effort": "max"}, max_tokens=8192)
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 24576}
        assert body["max_tokens"] > 24576
        assert "output_config" not in body

    def test_haiku_adaptive_becomes_a_real_budget(self) -> None:
        (body,), _ = _anthropic_body("claude-haiku-4-5", thinking={"type": "adaptive"})
        assert body["thinking"]["type"] == "enabled"
        assert body["thinking"]["budget_tokens"] >= 1024

    def test_46_pair_honours_an_explicit_budget_and_adaptive_without_one(self) -> None:
        (body,), _ = _anthropic_body("claude-sonnet-4-6", thinking={"type": "enabled", "budget_tokens": 8000})
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 8000}
        assert "output_config" not in body   # never paired with the budget form
        (body,), _ = _anthropic_body("claude-sonnet-4-6", thinking={"type": "adaptive"})
        assert body["thinking"] == {"type": "adaptive"}

    def test_fable_gets_effort_only(self) -> None:
        """Thinking is always on and any explicit block is rejected."""
        (body,), _ = _anthropic_body(
            "claude-fable-5-1", thinking={"type": "enabled", "budget_tokens": 24576})
        assert "thinking" not in body
        assert body["output_config"] == {"effort": "max"}
        (body,), _ = _anthropic_body("claude-fable-5-1", thinking={"type": "disabled"})
        assert "thinking" not in body
        assert body["output_config"] == {"effort": "low"}

    def test_explicit_extra_thinking_block_still_wins(self) -> None:
        (body,), _ = _anthropic_body(
            "claude-opus-5", thinking={"type": "adaptive"},
            extra={"thinking": {"type": "adaptive", "display": "summarized"}})
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}

    def test_temperature_dropped_on_models_that_removed_sampling(self) -> None:
        (body,), _ = _anthropic_body("claude-opus-5", temperature=0.2)
        assert "temperature" not in body
        (body,), _ = _anthropic_body("claude-sonnet-4-6", temperature=0.2)
        assert body["temperature"] == 0.2

    def test_400_on_the_thinking_form_swaps_once(self) -> None:
        """An id the table doesn't know defaults to adaptive; if the API says
        that form is wrong, the request is retried once in the other form
        instead of failing the turn."""
        reject = httpx.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "thinking.type: adaptive is not supported on this model"}})
        ok = httpx.Response(200, text=_ANTHROPIC_DONE, headers={"content-type": "text/event-stream"})
        bodies, events = _anthropic_body(
            "claude-legacy-x", thinking={"type": "enabled", "budget_tokens": 12288},
            responses=[reject, ok])
        assert len(bodies) == 2
        assert bodies[0]["thinking"] == {"type": "adaptive"}
        assert bodies[1]["thinking"] == {"type": "enabled", "budget_tokens": 12288}
        assert "output_config" not in bodies[1]
        assert bodies[1]["max_tokens"] > 12288

    def test_400_for_an_unrelated_reason_is_not_retried(self) -> None:
        from mantis_agent.errors import ProviderError

        reject = httpx.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error", "message": "messages: roles must alternate"}})
        with pytest.raises(ProviderError, match="roles must alternate"):
            _anthropic_body("claude-opus-5", thinking={"type": "adaptive"}, responses=[reject])


# ---------------------------------------------------------------------------
# CLI — probe / list-models across the families
# ---------------------------------------------------------------------------


class TestCliProviders:
    def test_resolve_api_key_is_host_aware(self, monkeypatch) -> None:
        from mantis_agent.cli import _resolve_api_key

        monkeypatch.delenv("MANTIS_AGENT_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("XAI_API_KEY", "xai-k")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-k")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-k")
        assert _resolve_api_key(XAI_DEFAULT) == "xai-k"
        assert _resolve_api_key("anthropic") == "sk-ant-k"
        assert _resolve_api_key("https://api.anthropic.com/v1") == "sk-ant-k"
        assert _resolve_api_key(GEMINI_DEFAULT) == "AIza-k"
        assert _resolve_api_key(OPENAI_DEFAULT) == "sk-openai"
        assert _resolve_api_key(None) == "sk-openai"
        monkeypatch.setenv("MANTIS_AGENT_API_KEY", "override")
        assert _resolve_api_key(XAI_DEFAULT) == "override"

    def test_list_models_anthropic_uses_x_api_key_and_version(self, monkeypatch, capsys) -> None:
        from mantis_agent.cli import main

        monkeypatch.delenv("MANTIS_AGENT_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-k")
        with respx.mock:
            route = respx.get("https://api.anthropic.com/v1/models").mock(
                return_value=httpx.Response(200, json={"data": [
                    {"id": "claude-opus-5", "display_name": "Claude Opus 5"},
                    {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
                ]}))
            for backend in ("anthropic", "https://api.anthropic.com/v1"):
                assert main(["list-models", "--backend", backend]) == 0
        req = route.calls[0].request
        assert req.headers["x-api-key"] == "sk-ant-k"
        assert req.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in req.headers
        out = capsys.readouterr().out
        assert "claude-opus-5" in out and "anthropic_passthrough" in out

    def test_list_models_xai_is_standard_bearer(self, monkeypatch, capsys) -> None:
        from mantis_agent.cli import main

        monkeypatch.delenv("MANTIS_AGENT_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-stale")
        monkeypatch.setenv("XAI_API_KEY", "xai-k")
        with respx.mock:
            route = respx.get("https://api.x.ai/v1/models").mock(
                return_value=httpx.Response(200, json={"data": [
                    {"id": "grok-4", "owned_by": "xai"}, {"id": "grok-3-mini", "owned_by": "xai"}]}))
            assert main(["list-models", "--backend", XAI_DEFAULT]) == 0
        assert route.calls[0].request.headers["authorization"] == "Bearer xai-k"
        out = capsys.readouterr().out
        assert "grok-4" in out and "256000" in out

    def test_probe_anthropic_sentinel(self, monkeypatch, capsys) -> None:
        from mantis_agent.cli import main

        monkeypatch.delenv("MANTIS_AGENT_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-k")
        with respx.mock:
            route = respx.get("https://api.anthropic.com/v1/models").mock(
                return_value=httpx.Response(200, json={"data": []}))
            assert main(["probe", "--backend", "anthropic"]) == 0
        assert route.calls[0].request.headers["x-api-key"] == "sk-ant-k"
        out = capsys.readouterr().out
        assert "detected provider: anthropic_passthrough" in out
        assert "matched hosted profile: anthropic" in out
        assert "reachable: /v1/models -> HTTP 200" in out

    def test_probe_xai_and_gemini(self, monkeypatch, capsys) -> None:
        from mantis_agent.cli import main

        monkeypatch.delenv("MANTIS_AGENT_API_KEY", raising=False)
        monkeypatch.setenv("XAI_API_KEY", "xai-k")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-k")
        with respx.mock:
            x = respx.get("https://api.x.ai/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
            g = respx.get(f"{GEMINI_DEFAULT}/models").mock(return_value=httpx.Response(200, json={"data": []}))
            assert main(["probe", "--backend", XAI_DEFAULT]) == 0
            assert main(["probe", "--backend", GEMINI_DEFAULT]) == 0
        assert x.calls[0].request.headers["authorization"] == "Bearer xai-k"
        assert g.calls[0].request.headers["authorization"] == "Bearer AIza-k"
        out = capsys.readouterr().out
        assert "matched hosted profile: xai" in out
        assert "matched hosted profile: gemini" in out


# ---------------------------------------------------------------------------
# Open-source reasoning models — inline <think> tags split while streaming
# ---------------------------------------------------------------------------


class TestInlineThinkingOnNativePath:
    def _events(self, model: str, *pieces: str, cap: Any = None) -> list[Any]:
        chunks = [_chunk({"role": "assistant", "content": pieces[0]})]
        chunks += [_chunk({"content": pc}) for pc in pieces[1:]]
        chunks.append(_chunk({}, finish="stop"))
        body = _sse(*chunks)
        with respx.mock:
            respx.post("http://gpu-box:8000/v1/chat/completions").mock(
                return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
            p = OpenAICompatProvider(base_url="http://gpu-box:8000/v1", api_key="")
            events = anyio.run(lambda: _drain(
                p, model=model, messages=[UserMessage(content="hi")], max_tokens=50,
                tools=[{"name": "bash", "description": "run", "input_schema": {"type": "object"}}],
                model_capability=cap or lookup_model(model)))
            anyio.run(p.aclose)
        return events

    @staticmethod
    def _split(events: list[Any]) -> tuple[str, str]:
        thinking = "".join(
            ev.delta.thinking for ev in events
            if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, ThinkingDelta))
        text = "".join(
            ev.delta.text for ev in events
            if isinstance(ev, ContentBlockDelta) and hasattr(ev.delta, "text"))
        return thinking, text

    def test_think_tags_become_thinking_events_mid_stream(self) -> None:
        """Qwen3 on vLLM takes native tools (path A) yet emits <think> inline;
        the tags must be split as they stream — even when a tag straddles two
        deltas — so consumers never see raw ``<think>`` in the text."""
        cap = lookup_model("qwen3:8b")
        assert cap.supports_native_tools and cap.emits_inline_thinking is False
        from dataclasses import replace
        cap = replace(cap, emits_inline_thinking=True)
        events = self._events("qwen3:8b", "<thi", "nk>plan it out</th", "ink>The answer.", cap=cap)
        thinking, text = self._split(events)
        assert thinking == "plan it out"
        assert text == "The answer."
        blocks = [ev.block for ev in events if isinstance(ev, ContentBlockStart)]
        assert isinstance(blocks[0], ThinkingBlock)
        assert "<think>" not in text and "</think>" not in text
        # The engine's post-hoc net finds nothing left to strip (no double-strip).
        from mantis_agent.agent import _split_inline_thinking
        from mantis_agent.types import AssistantMessage, TextBlock

        net = _split_inline_thinking(AssistantMessage(content=[TextBlock(text=text)]))
        assert [type(b).__name__ for b in net.content] == ["TextBlock"]
        assert net.content[0].text == text

    def test_deepseek_r1_splits_by_capability(self) -> None:
        cap = lookup_model("deepseek-r1")
        assert cap.emits_inline_thinking is True
        events = self._events("deepseek-r1", "<think>hmm</think>", "42", cap=cap)
        assert self._split(events) == ("hmm", "42")

    def test_models_without_inline_thinking_are_untouched(self) -> None:
        """A plain chat model that happens to print a tag keeps it as text —
        no parser is constructed, so the stream is byte-identical."""
        events = self._events("qwen2.5-7b-instruct", "literal <think> text")
        assert self._split(events) == ("", "literal <think> text")
