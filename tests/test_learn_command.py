"""/learn — expands into a prompt that has the agent save durable memories."""

from __future__ import annotations

from mantis_agent.tui import LEARN_PROMPT, SLASH_COMMANDS, expand_slash_prompt


def test_learn_expands() -> None:
    assert expand_slash_prompt("/learn") == LEARN_PROMPT
    assert expand_slash_prompt("  /learn  ") == LEARN_PROMPT


def test_learn_with_focus() -> None:
    p = expand_slash_prompt("/learn the deployment process")
    assert p is not None
    assert LEARN_PROMPT in p
    assert "Focus especially on: the deployment process" in p


def test_not_confused_with_similar() -> None:
    assert expand_slash_prompt("/learned something") is None
    assert expand_slash_prompt("learn this") is None


def test_prompt_is_actionable() -> None:
    low = LEARN_PROMPT.lower()
    assert "remember" in low                 # tells the agent the tool to use
    assert "durable" in low                   # emphasis on durability
    assert "not" in low and "transient" in low  # anti-noise guardrail


def test_init_unaffected() -> None:
    from mantis_agent.tui import INIT_PROMPT
    assert expand_slash_prompt("/init") == INIT_PROMPT


def test_in_slash_menu() -> None:
    assert "/learn" in SLASH_COMMANDS
