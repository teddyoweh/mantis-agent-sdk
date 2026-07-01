"""MantisAgentOptions is the options class name (ClaudeAgentOptions removed)."""

from __future__ import annotations

import pytest


def test_mantis_agent_options_is_the_name() -> None:
    from mantis_agent import MantisAgentOptions
    o = MantisAgentOptions(model="qwen2.5:7b", max_turns=3)
    assert o.model == "qwen2.5:7b"
    assert MantisAgentOptions.__name__ == "MantisAgentOptions"
    assert "model" in o.to_query_options()


def test_claude_agent_options_removed() -> None:
    with pytest.raises(ImportError):
        from mantis_agent import ClaudeAgentOptions  # noqa: F401


def test_query_accepts_mantis_options() -> None:
    from mantis_agent.compat_query import _normalize_options
    from mantis_agent import MantisAgentOptions
    opts = _normalize_options(MantisAgentOptions(model="qwen2.5:7b"))
    assert opts["model"] == "qwen2.5:7b"
