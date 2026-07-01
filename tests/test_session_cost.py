"""Session cost tracking — estimate_cost + the /context readout formatter."""

from __future__ import annotations

from mantis_agent.budget import estimate_cost
from mantis_agent.tui import format_cost
from mantis_agent.types import Usage


def test_estimate_cost_priced_model() -> None:
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    # deepseek-v3: 0.27 prompt + 1.10 completion per million
    assert estimate_cost(u, "deepseek-v3", "deepseek") == 0.27 + 1.10


def test_estimate_cost_scales_with_tokens() -> None:
    half = Usage(input_tokens=500_000, output_tokens=0)
    assert estimate_cost(half, "deepseek-v3", "deepseek") == 0.135


def test_estimate_cost_local_is_free() -> None:
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost(u, "qwen2.5-1.5b-instruct", "ollama") == 0.0   # free wildcard


def test_estimate_cost_unknown_is_none() -> None:
    u = Usage(input_tokens=1000, output_tokens=1000)
    assert estimate_cost(u, "totally-made-up-model-xyz", "unknownprovider") is None


def test_cumulative_cost_sums_per_turn() -> None:
    # cost is summed per turn (each API call re-bills the whole prompt)
    turns = [Usage(input_tokens=100_000, output_tokens=10_000) for _ in range(3)]
    total = sum(estimate_cost(u, "deepseek-v3", "deepseek") for u in turns)
    one = estimate_cost(turns[0], "deepseek-v3", "deepseek")
    assert total == one * 3


def test_format_cost() -> None:
    assert "local" in format_cost(0.0)
    assert "local" in format_cost(None)
    assert format_cost(0.0012) == "$0.0012"
    assert format_cost(0.42) == "$0.42"
    assert format_cost(3.5) == "$3.50"
