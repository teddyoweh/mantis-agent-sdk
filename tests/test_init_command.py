"""/init — expands into a canned prompt that has the agent write MANTIS.md."""

from __future__ import annotations

from mantis_agent.tui import INIT_PROMPT, SLASH_COMMANDS, expand_slash_prompt


def test_init_expands_to_prompt() -> None:
    assert expand_slash_prompt("/init") == INIT_PROMPT
    assert expand_slash_prompt("  /init  ") == INIT_PROMPT   # trimmed


def test_other_commands_not_expanded() -> None:
    assert expand_slash_prompt("/diff") is None
    assert expand_slash_prompt("/compact") is None
    assert expand_slash_prompt("just a message") is None


def test_prompt_is_actionable() -> None:
    p = INIT_PROMPT.lower()
    assert "mantis.md" in p
    assert "write_file" in p                 # tells the agent how to persist it
    assert "build" in p and "test" in p      # asks for the load-bearing commands
    assert "already exists" in p             # respects existing content (read-first)


def test_in_slash_menu() -> None:
    assert "/init" in SLASH_COMMANDS
