"""Provider-side translation of the universal thinking config.

Tier 2 gives every provider's ``stream()`` a keyword-only ``thinking=None``
param carrying ``{"type": "adaptive"|"enabled"|"disabled", "budget_tokens":
int|None}``. Each backend maps it to its own reasoning knob:

* openai_compat -> ``reasoning_effort`` (gated on models that accept a
  request-side knob; the budget is dropped — Chat Completions has no field
  for it)
* anthropic_passthrough -> the native ``thinking`` block
* ollama -> the top-level ``think`` flag (best-effort, gated on capability)

Two load-bearing invariants:

1. **When ``thinking`` is ``None`` the built request body is byte-for-byte
   unchanged.**
2. **No SDK control key ever reaches a vendor wire.** ``extra`` carries the
   Claude-SDK spellings for the loop's benefit; a provider translates what it
   can and drops the rest. Forwarding one verbatim is a hard 400 on OpenAI and
   Anthropic — which is how ``max_thinking_tokens`` broke every reasoning
   request in 2.59.0.

No network — openai is checked via ``_build_payload``; anthropic + ollama via
an httpx MockTransport that captures the outbound body.
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


def test_openai_enabled_sets_high_effort_and_drops_the_budget() -> None:
    """Chat Completions has no per-request thinking budget — ``reasoning_effort``
    is the whole knob. Emitting a budget field is a 400 (`Unknown parameter`),
    so the budget is honoured by dropping it, not by inventing a field."""
    pl = _openai_payload("gpt-5.5", thinking={"type": "enabled", "budget_tokens": 4096})
    assert pl["reasoning_effort"] == "high"
    assert "max_thinking_tokens" not in pl


def test_openai_disabled_turns_reasoning_off() -> None:
    pl = _openai_payload("gpt-5.5", thinking={"type": "disabled", "budget_tokens": None})
    assert pl["reasoning_effort"] == "none"


def test_openai_o_series_supported_by_name() -> None:
    pl = _openai_payload("o3", thinking={"type": "enabled", "budget_tokens": 2048})
    assert pl["reasoning_effort"] == "high"
    assert "max_thinking_tokens" not in pl


def test_openai_never_emits_a_thinking_budget_field() -> None:
    """The regression that shipped in 2.59.0: ``max_thinking_tokens`` is the
    Claude-SDK option name, not an OpenAI one. Every route that could set it —
    the kwarg, the Claude-style alias in extra, a raw extra key — must leave it
    off the wire."""
    for kw in (
        {"thinking": {"type": "enabled", "budget_tokens": 4096}},
        {"extra": {"thinking": {"effort": "high", "budget_tokens": 4096}}},
        {"extra": {"max_thinking_tokens": 4096}},
    ):
        pl = _openai_payload("gpt-5.5", **kw)  # type: ignore[arg-type]
        assert "max_thinking_tokens" not in pl, kw


def test_openai_extra_does_not_erase_the_thinking_kwarg() -> None:
    """A non-empty ``extra`` without a "thinking" key used to rebind the local
    and silently discard the universal config — so passing verbosity turned
    reasoning off."""
    pl = _openai_payload(
        "gpt-5.5", thinking={"type": "enabled"}, extra={"verbosity": "high"})
    assert pl["reasoning_effort"] == "high"
    assert pl["verbosity"] == "high"


def test_openai_verbosity_only_goes_to_gpt5() -> None:
    """``verbosity`` is a real GPT-5 field and a 400 everywhere else."""
    assert _openai_payload("gpt-5.5", extra={"verbosity": "low"})["verbosity"] == "low"
    assert "verbosity" not in _openai_payload("gpt-4o", extra={"verbosity": "low"})
    assert "verbosity" not in _openai_payload("qwen3:8b", extra={"verbosity": "low"})


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


def _anthropic_body_msgs(
    messages: list[Any], *, system: str | None = None
) -> dict[str, Any]:
    """Capture the outbound Anthropic body for an arbitrary history."""
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
            model="claude-sonnet-4-6", messages=messages, max_tokens=1024,
            system=system,
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
    extra: dict[str, Any] | None = None,
    messages: list[Any] | None = None,
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
            messages=messages or [UserMessage(content="hi")],
            max_tokens=100,
            model_capability=lookup_model(model),
            thinking=thinking,
            extra=extra,
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


# ---------------------------------------------------------------------------
# no SDK control key ever reaches a vendor wire
# ---------------------------------------------------------------------------


# Everything ``MantisAgentOptions`` parks in ``extra`` for the loop and the
# providers to read. None of it is a wire field that every vendor accepts, so
# each provider translates what it can and drops the rest.
_CONTROL_EXTRA = {
    "max_thinking_tokens": 4096,
    "verbosity": "high",
    "reasoning_mode": "deep",
    "reasoning_context": "some notes",
    "effort": "high",
    "allowed_tools": ["read_file"],
    "disallowed_tools": ["bash"],
}


def test_control_keys_never_reach_the_anthropic_wire() -> None:
    """Anthropic 400s on any unrecognized top-level field, so one leaked key
    breaks every request. ``max_thinking_tokens`` becomes a native block."""
    body = _anthropic_body(extra=dict(_CONTROL_EXTRA))
    leaked = [k for k in _CONTROL_EXTRA if k in body]
    assert leaked == [], f"leaked to Anthropic: {leaked}"
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_control_keys_never_reach_the_ollama_wire() -> None:
    body = _ollama_body(model="deepseek-r1", extra=dict(_CONTROL_EXTRA))
    leaked = [k for k in _CONTROL_EXTRA if k in body]
    assert leaked == [], f"leaked to Ollama: {leaked}"


def test_control_keys_never_reach_the_openai_wire() -> None:
    pl = _openai_payload("gpt-5.5", extra=dict(_CONTROL_EXTRA))
    # verbosity + effort ARE real GPT-5 fields; the rest have no wire form.
    leaked = [k for k in _CONTROL_EXTRA
              if k in pl and k not in ("verbosity",)]
    assert leaked == [], f"leaked to OpenAI: {leaked}"
    assert pl["reasoning_effort"] == "high"       # 'effort' translated, not passed


def test_tool_permission_lists_are_local_policy_not_wire_fields() -> None:
    """allowed_tools/disallowed_tools are decisions mantis enforces itself.
    Shipping them to a vendor leaks the policy and enforces nothing."""
    for body in (_anthropic_body(extra=dict(_CONTROL_EXTRA)),
                 _ollama_body(model="deepseek-r1", extra=dict(_CONTROL_EXTRA)),
                 _openai_payload("gpt-5.5", extra=dict(_CONTROL_EXTRA))):
        assert "allowed_tools" not in body
        assert "disallowed_tools" not in body


def test_genuine_vendor_knobs_still_pass_through() -> None:
    """The filter is a named deny-list, not a whitelist — an opaque vendor
    parameter must still reach the wire."""
    assert _anthropic_body(extra={"top_k": 40})["top_k"] == 40
    assert _ollama_body(model="deepseek-r1", extra={"keep_alive": "5m"})["keep_alive"] == "5m"
    assert _openai_payload("gpt-5.5", extra={"seed": 7})["seed"] == 7


# ---------------------------------------------------------------------------
# a compaction boundary must not break the next request
# ---------------------------------------------------------------------------


def _boundary_history() -> list[Any]:
    from mantis_agent.compact import CompactBoundaryMessage

    return [
        UserMessage(content="solve the thing"),
        CompactBoundaryMessage(
            summary="Earlier: we derived the n=14 formula.", compacted_count=40),
        UserMessage(content="hi"),
    ]


def test_openai_encodes_a_compaction_boundary() -> None:
    """``CompactBoundaryMessage`` is not a wire type, and no encoder knew it —
    so the first request after an auto-compact died with "unsupported message
    type" and every retry hit the same boundary, killing the session."""
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    pl = p._build_payload(
        model="gpt-5.5", messages=_boundary_history(), system=None, tools=None,
        max_tokens=100, temperature=None, extra=None, path="A",
        thinking=None, model_capability=None)
    assert [m["role"] for m in pl["messages"]] == ["system", "user", "user"]
    assert "n=14 formula" in pl["messages"][0]["content"]
    # Labelled, so a recap of the model's own past turns doesn't read as a
    # fresh instruction from the user.
    assert "[previous summary]" in pl["messages"][0]["content"]


def test_anthropic_encodes_a_compaction_boundary() -> None:
    body = _anthropic_body_msgs(_boundary_history())
    assert "n=14 formula" in json.dumps(body)
    assert [m["role"] for m in body["messages"]] == ["user", "user"]


def test_ollama_encodes_a_compaction_boundary() -> None:
    body = _ollama_body(model="deepseek-r1", messages=_boundary_history())
    roles = [m["role"] for m in body["messages"]]
    assert "system" in roles
    assert "n=14 formula" in json.dumps(body)


def test_a_boundary_survives_the_system_hoist_on_anthropic() -> None:
    """With an explicit system= the hoist takes a different branch — it has to
    normalize too, or the boundary reaches the encoder and raises."""
    body = _anthropic_body_msgs(_boundary_history(), system="BASE PROMPT")
    assert body["system"] == "BASE PROMPT" or "BASE PROMPT" in json.dumps(body["system"])
    assert [m["role"] for m in body["messages"]] == ["user", "user"]
