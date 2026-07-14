from __future__ import annotations

from mantis_agent.claude_compat import MantisAgentOptions


def test_claude_code_preset_append_is_preserved() -> None:
    opts = MantisAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code", "append": "extra rules"}
    ).to_query_options()

    assert opts["system"] == "extra rules"


def test_claude_code_preset_without_append_adds_no_noise() -> None:
    opts = MantisAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code"}
    ).to_query_options()

    assert "system" not in opts
