"""Session commands (/resume, /branch, /rewind) — wired in the fullscreen TUI and
their underlying MantisTUI logic."""

from __future__ import annotations

import contextlib
import io

import anyio

from mantis_agent.tui import SLASH_COMMANDS, MantisTUI
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


def _convo() -> list:
    return [
        UserMessage(content="first task"),
        AssistantMessage(content=[TextBlock(text="did first")]),
        UserMessage(content="second task"),
        AssistantMessage(content=[TextBlock(text="did second")]),
    ]


def _silent(fn) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        fn()


def test_rewind_truncates_to_prompt() -> None:
    tui = _tui()
    tui.messages = _convo()
    _silent(lambda: tui._cmd_rewind("2"))     # rewind to the 2nd user prompt
    assert len(tui.messages) == 2             # kept: first task + its reply
    assert tui.messages[-1].content[0].text == "did first"


def test_rewind_to_first_empties() -> None:
    tui = _tui()
    tui.messages = _convo()
    _silent(lambda: tui._cmd_rewind("1"))
    assert tui.messages == []


def test_rewind_out_of_range_is_noop() -> None:
    tui = _tui()
    tui.messages = _convo()
    _silent(lambda: tui._cmd_rewind("9"))     # only 2 prompts
    assert len(tui.messages) == 4             # unchanged


def test_rewind_list_mode_keeps_messages() -> None:
    tui = _tui()
    tui.messages = _convo()
    _silent(lambda: tui._cmd_rewind(""))      # no arg → just lists
    assert len(tui.messages) == 4


def test_resume_with_no_sessions_is_safe() -> None:
    tui = _tui()
    # no transcript / fresh env — should not raise, just report nothing to resume
    _silent(lambda: anyio.run(lambda: tui._cmd_resume("")))


def test_commands_are_advertised() -> None:
    for c in ("/resume", "/branch", "/rewind"):
        assert c in SLASH_COMMANDS
