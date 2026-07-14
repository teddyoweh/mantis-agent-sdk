"""max_tokens defaults to the model's output budget (so large writes don't truncate)."""

from __future__ import annotations

from mantis_agent.agent import Agent
from mantis_agent.capabilities import ModelCapability


def _cap(out: int) -> ModelCapability:
    return ModelCapability(name="m", family="x", context_window=32768, max_output_tokens=out)


def test_default_uses_model_output_budget() -> None:
    assert Agent(model="m", model_capability=_cap(4096)).max_tokens == 4096


def test_capped_at_8192() -> None:
    assert Agent(model="m", model_capability=_cap(100000)).max_tokens == 8192


def test_small_model_caps_down() -> None:
    assert Agent(model="m", model_capability=_cap(512)).max_tokens == 512


def test_explicit_higher_respected_within_context_budget() -> None:
    assert Agent(model="m", model_capability=_cap(4096), max_tokens=6000).max_tokens == 6000


def test_explicit_lower_respected() -> None:
    # an explicit non-default value is never overridden
    assert Agent(model="m", model_capability=_cap(4096), max_tokens=256).max_tokens == 256


def test_unknown_output_budget_keeps_default() -> None:
    assert Agent(model="m", model_capability=_cap(0)).max_tokens == 1024


def test_small_context_caps_output_reservation() -> None:
    assert Agent(model="m", model_capability=ModelCapability(
        name="m", family="x", context_window=16384, max_output_tokens=8192,
    )).max_tokens == 4096


def test_self_hosted_glm9b_context_and_output_budget() -> None:
    a = Agent(model="zai-org/GLM-4-9B-0414")
    assert a.model_capability.context_window == 16384
    assert a.max_tokens == 4096


def test_real_model_default() -> None:
    # a known model in the table gets its advertised budget
    assert Agent(model="qwen2.5-7b-instruct").max_tokens == 4096
