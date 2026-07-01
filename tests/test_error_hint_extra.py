"""error_hint covers context-length, tool-unsupported, and OOM errors."""

from __future__ import annotations

import pytest

from mantis_agent.errors import ProviderError
from mantis_agent.tui import error_hint


@pytest.mark.parametrize("msg", [
    "This model's maximum context length is 8192 tokens",
    "context_length_exceeded",
    "prompt is too long: 9000 tokens",
    "reduce the length of the messages",
])
def test_context_length(msg: str) -> None:
    h = error_hint(ProviderError(msg), None)
    assert h and "/compact" in h and "too long" in h


@pytest.mark.parametrize("msg", [
    "Tools are not supported by this model",
    "function calling is not available",
    "this model has no tool support",
])
def test_tool_unsupported(msg: str) -> None:
    h = error_hint(ProviderError(msg), None)
    assert h and "tool" in h.lower() and "/models" in h


def test_oom() -> None:
    h = error_hint(ProviderError("CUDA out of memory"), None)
    assert h and "memory" in h and "/models" in h


def test_tool_error_not_confused_with_model_not_found() -> None:
    # "not supported" appears in both; the tool hint must win (comes first)
    h = error_hint(ProviderError("tools are not supported"), None)
    assert "tool calling" in h and "isn't available on this backend" not in h


def test_unknown_still_none() -> None:
    assert error_hint(ProviderError("something inexplicable"), None) is None


def test_existing_hints_intact() -> None:
    from mantis_agent.errors import AuthError
    assert "API key" in error_hint(AuthError("bad key"), None)
    assert "ollama serve" in error_hint(ProviderError("connection refused"), "http://localhost:11434")
