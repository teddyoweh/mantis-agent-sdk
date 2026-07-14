"""Provider-side translation of the universal thinking config.

Tier 2 gives every provider's ``stream()`` a keyword-only ``thinking=None``
param carrying ``{"type": "adaptive"|"enabled"|"disabled", "budget_tokens":
int|None}``. Each backend maps it to its own reasoning knob:

* openai_compat -> ``reasoning_effort`` / ``max_thinking_tokens`` (gated on
  models that accept a request-side knob)
* anthropic_passthrough -> the native ``thinking`` block
* ollama -> the top-level ``think`` flag (best-effort, gated on capability)

The load-bearing invariant these lock in: **when ``thinking`` is ``None`` the
built request body is byte-for-byte unchanged.** No network — openai is checked
via ``_build_payload``; anthropic + ollama via an httpx MockTransport that
captures the outbound body.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx

from mantis_agent.capabilities import lookup_model
from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider
from mantis_agent.providers.ollama import OllamaProvider
from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.types import UserMessage


# ---------------------------------------------------------------------------
# openai_compat — reasoning_effort / max_thinking_tokens
# ---------------------------------------------------------------------------


def _openai_payload(
    model: str,
    *,
    thinking: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    model_capability: Any = None,
) -> dict[str, Any]:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    return p._build_payload(
        model=model,
        messages=[UserMessage(content="hi")],
        system=None,
        tools=None,
        max_tokens=100,
        temperature=0.7,
        extra=extra,
        path="A",
        thinking=thinking,
        model_capability=model_capability,
    )


def test_openai_thinking_none_is_unchanged() -> None:
    # No thinking + no extra: no reasoning fields at all — the baseline body.
    pl = _openai_payload("gpt-5.5")
    assert "reasoning_effort" not in pl
    assert "max_thinking_tokens" not in pl


def test_openai_adaptive_sets_medium_effort() -> None:
    pl = _openai_payload("gpt-5.5", thinking={"type": "adaptive", "budget_tokens": None})
    assert pl["reasoning_effort"] == "medium"
    assert "max_thinking_tokens" not in pl


def test_openai_enabled_with_budget_sets_high_effort_and_budget() -> None:
    pl = _openai_payload("gpt-5.5", thinking={"type": "enabled", "budget_tokens": 4096})
    assert pl["reasoning_effort"] == "high"
    assert pl["max_thinking_tokens"] == 4096


def test_openai_disabled_turns_reasoning_off() -> None:
    pl = _openai_payload("gpt-5.5", thinking={"type": "disabled", "budget_tokens": None})
    assert pl["reasoning_effort"] == "none"


def test_openai_o_series_supported_by_name() -> None:
    pl = _openai_payload("o3", thinking={"type": "enabled", "budget_tokens": 2048})
    assert pl["reasoning_effort"] == "high"
    assert pl["max_thinking_tokens"] == 2048


def test_openai_non_reasoning_model_gets_no_field() -> None:
    # gpt-4o resolves to the bare "openai" family (supports_reasoning_effort
    # False) and its name doesn't match the gpt-5/o-series check — sending
    # reasoning_effort would 400, so it must be omitted.
    cap = lookup_model("gpt-4o")
    assert cap.supports_reasoning_effort is False
    pl = _openai_payload(
        "gpt-4o", thinking={"type": "adaptive"}, model_capability=cap
    )
    assert "reasoning_effort" not in pl
    assert "max_thinking_tokens" not in pl


def test_openai_capability_signal_enables_non_openai_flagship() -> None:
    # A gemini/glm-style model carries supports_reasoning_effort=True even though
    # its name isn't gpt-5/o-series.
    cap = lookup_model("gemini-2.5-pro")
    assert cap.supports_reasoning_effort is True
    pl = _openai_payload(
        "gemini-2.5-pro", thinking={"type": "adaptive"}, model_capability=cap
    )
    assert pl["reasoning_effort"] == "medium"


def test_openai_extra_reasoning_wins_over_thinking_kwarg() -> None:
    # An explicit reasoning field in extra is authoritative; the thinking kwarg
    # must not override it.
    pl = _openai_payload(
        "gpt-5.5", thinking={"type": "enabled", "budget_tokens": 9999},
        extra={"effort": "low"},
    )
    assert pl["reasoning_effort"] == "low"
    assert "max_thinking_tokens" not in pl


# ---------------------------------------------------------------------------
# anthropic_passthrough — native thinking block
# ---------------------------------------------------------------------------


_ANTHROPIC_SSE = "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"


def _anthropic_body(
    *,
    thinking: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, text=_ANTHROPIC_SSE, headers={"content-type": "text/event-stream"}
        )

    p = AnthropicPassthroughProvider(api_key="x")
    p.client = httpx.AsyncClient(
        base_url="https://api.anthropic.com/v1",
        transport=httpx.MockTransport(handler),
    )

    async def go() -> None:
        async for _ in p.stream(
            model="claude-sonnet-4-6",
            messages=[UserMessage(content="hi")],
            max_tokens=1024,
            temperature=temperature,
            extra=extra,
            thinking=thinking,
        ):
            pass

    anyio.run(go)
    return captured["body"]


def test_anthropic_thinking_none_omits_block() -> None:
    body = _anthropic_body()
    assert "thinking" not in body


def test_anthropic_adaptive_block() -> None:
    body = _anthropic_body(thinking={"type": "adaptive", "budget_tokens": None})
    assert body["thinking"] == {"type": "adaptive"}


def test_anthropic_enabled_with_budget_block() -> None:
    body = _anthropic_body(thinking={"type": "enabled", "budget_tokens": 8000})
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8000}


def test_anthropic_disabled_block() -> None:
    body = _anthropic_body(thinking={"type": "disabled", "budget_tokens": None})
    assert body["thinking"] == {"type": "disabled"}


def test_anthropic_extra_thinking_wins() -> None:
    body = _anthropic_body(
        thinking={"type": "adaptive"},
        extra={"thinking": {"type": "disabled"}},
    )
    assert body["thinking"] == {"type": "disabled"}


def test_anthropic_enabling_thinking_drops_temperature() -> None:
    # Non-default temperature + a live thinking block is a 400 on Anthropic —
    # enabling thinking must strip the temperature it would otherwise carry.
    body = _anthropic_body(
        thinking={"type": "enabled", "budget_tokens": 4096}, temperature=0.5
    )
    assert "temperature" not in body


def test_anthropic_temperature_kept_when_thinking_none() -> None:
    body = _anthropic_body(temperature=0.5)
    assert body["temperature"] == 0.5


# ---------------------------------------------------------------------------
# ollama — best-effort ``think`` flag
# ---------------------------------------------------------------------------


_OLLAMA_DONE = (
    '{"model":"x","message":{"role":"assistant","content":""},'
    '"done":true,"done_reason":"stop"}\n'
)


def _ollama_body(
    *,
    model: str,
    thinking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, text=_OLLAMA_DONE, headers={"content-type": "application/x-ndjson"}
        )

    p = OllamaProvider(base_url="http://localhost:11434")
    p.client = httpx.AsyncClient(
        base_url="http://localhost:11434",
        transport=httpx.MockTransport(handler),
    )

    async def go() -> None:
        async for _ in p.stream(
            model=model,
            messages=[UserMessage(content="hi")],
            max_tokens=100,
            model_capability=lookup_model(model),
            thinking=thinking,
        ):
            pass

    anyio.run(go)
    return captured["body"]


def test_ollama_thinking_none_omits_think() -> None:
    body = _ollama_body(model="deepseek-r1")
    assert "think" not in body


def test_ollama_enabled_sets_think_true_for_reasoning_model() -> None:
    # deepseek-r1 emits thinking, so ``think`` is safe to send.
    assert lookup_model("deepseek-r1").emits_inline_thinking is True
    body = _ollama_body(model="deepseek-r1", thinking={"type": "enabled"})
    assert body["think"] is True


def test_ollama_disabled_sets_think_false_for_reasoning_model() -> None:
    body = _ollama_body(model="deepseek-r1", thinking={"type": "disabled"})
    assert body["think"] is False


def test_ollama_non_reasoning_model_is_noop() -> None:
    # A plain chat model has no reasoning signal — sending ``think`` would error,
    # so the provider must no-op cleanly.
    cap = lookup_model("qwen2.5-7b-instruct")
    assert not (
        cap.supports_reasoning_effort
        or cap.emits_thinking_blocks
        or cap.emits_inline_thinking
    )
    body = _ollama_body(model="qwen2.5-7b-instruct", thinking={"type": "adaptive"})
    assert "think" not in body
