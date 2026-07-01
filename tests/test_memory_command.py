"""/memory — resolve_memory_target maps a target to the file to edit."""

from __future__ import annotations

from pathlib import Path

from mantis_agent.tui import SLASH_COMMANDS, resolve_memory_target


def test_project_default() -> None:
    assert resolve_memory_target(None, "/proj", "/home") == Path("/proj/MANTIS.md")
    assert resolve_memory_target("", "/proj", "/home") == Path("/proj/MANTIS.md")
    assert resolve_memory_target("project", "/proj", "/home") == Path("/proj/MANTIS.md")


def test_agents() -> None:
    assert resolve_memory_target("agents", "/proj", "/home") == Path("/proj/AGENTS.md")
    assert resolve_memory_target("AGENT", "/proj", "/home") == Path("/proj/AGENTS.md")


def test_user() -> None:
    assert resolve_memory_target("user", "/proj", "/home") == Path("/home/MANTIS.md")
    assert resolve_memory_target("global", "/proj", "/home") == Path("/home/MANTIS.md")


def test_unknown_falls_back_to_project() -> None:
    assert resolve_memory_target("nonsense", "/proj", "/home").name == "MANTIS.md"


def test_in_slash_menu() -> None:
    assert "/memory" in SLASH_COMMANDS
