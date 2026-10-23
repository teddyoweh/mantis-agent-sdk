"""A drop-in agent with tools works under mantis's agent prompt, the
counterpart of Claude Code's ``claude_code`` preset. It used to run with no
system prompt at all: an open model got tools and a task and nothing about how
to work. A caller's own string still replaces it, as in the Claude SDK."""

from __future__ import annotations

from mantis_agent import MantisAgentOptions
from mantis_agent.agent_prompt import AGENT_SYSTEM_PROMPT
from mantis_agent.compat_query import _build_agent, _normalize_options


def _system(**kw):
    return _build_agent(_normalize_options(MantisAgentOptions(model="mock", **kw))).system


def test_an_agent_with_tools_gets_the_agent_prompt() -> None:
    assert _system(allowed_tools=["Bash", "Read"]) == AGENT_SYSTEM_PROMPT
    assert _system(mcp_servers={"inv": {"type": "stdio", "command": "true"}}) == AGENT_SYSTEM_PROMPT


def test_a_string_system_prompt_replaces_it() -> None:
    assert _system(allowed_tools=["Bash"], system_prompt="You are a pirate.") == "You are a pirate."


def test_the_claude_code_preset_keeps_it_and_appends() -> None:
    got = _system(allowed_tools=["Bash"],
                  system_prompt={"type": "preset", "preset": "claude_code", "append": "Answer in French."})
    assert got.startswith(AGENT_SYSTEM_PROMPT.strip()[:60]) and got.endswith("Answer in French.")


def test_a_chat_agent_without_tools_is_left_alone() -> None:
    assert _system() is None
    assert _system(system_prompt="Be brief.") == "Be brief."


def test_the_marker_never_reaches_the_provider() -> None:
    agent = _build_agent(_normalize_options(MantisAgentOptions(model="mock", allowed_tools=["Bash"],
                                                                system_prompt="x")))
    assert "system_replaces" not in (agent.extra or {})
