"""The session-cost display sums ``estimate_cost(usage, model, hint)`` per turn.
These lock the two shapes callers depend on: a real number for a priced model,
and ``None`` (never a crash) for an unpriced/local one so the caller's ``if c:``
guard works.
"""

from __future__ import annotations

from mantis_agent.budget import estimate_cost
from mantis_agent.tui import format_cost
from mantis_agent.types import Usage

_ONE_M = Usage(input_tokens=1_000_000, output_tokens=1_000_000)


def test_priced_model_returns_positive_cost() -> None:
    c = estimate_cost(_ONE_M, "deepseek-v3", "deepseek")
    assert c is not None and c > 0


def test_unpriced_and_local_return_none() -> None:
    # Guarded by the caller's `if c:` — must be None, not a bogus number or crash.
    assert estimate_cost(_ONE_M, "gpt-5.5", "openai") is None
    assert estimate_cost(_ONE_M, "qwen2.5-coder:7b", None) is None


def test_format_cost_renders_without_crashing() -> None:
    assert isinstance(format_cost(0.0), str)
    assert "$" in format_cost(0.0123)
    assert isinstance(format_cost(None), str)
