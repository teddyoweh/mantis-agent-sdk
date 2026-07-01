"""Vim editing mode config (T2 near-free). The live VI keybindings + external
editor need a real terminal; here we test the toggle + env default + menu entry."""

from __future__ import annotations

from mantis_agent.tui import SLASH_COMMANDS, MantisTUI


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


def test_vim_mode_defaults_off() -> None:
    assert _tui().vim_mode is False


def test_vim_mode_env_default(monkeypatch) -> None:
    monkeypatch.setenv("MANTIS_VIM", "1")
    assert _tui().vim_mode is True


def test_vim_command_in_menu() -> None:
    assert "/vim" in SLASH_COMMANDS
