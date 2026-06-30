"""``bash`` must run non-interactively and return clean text — a model that
reaches for ``nano``/``vim`` or a paging command shouldn't hang the agent or
pollute its context with terminal-control sequences (Claude-Code parity)."""

from __future__ import annotations

import anyio

from mantis_agent.builtin_tools import bash
from mantis_agent.builtin_tools.fs import _strip_terminal_controls


def test_interactive_editor_does_not_hang(tmp_path) -> None:
    # nano with TERM=dumb + closed stdin exits immediately instead of launching
    # a full-screen UI that blocks on the (closed) stdin until the timeout.
    out = anyio.run(bash.fn, f"nano {tmp_path / 'x.txt'}", 8)
    assert "\x1b" not in out  # no escape codes leaked
    assert "PICO" not in out and "New Buffer" not in out


def test_ansi_color_codes_are_stripped() -> None:
    out = anyio.run(bash.fn, r'printf "\033[31mRED\033[0m \033[1mBOLD\033[0m"', 10)
    assert out == "RED BOLD"


def test_carriage_return_progress_collapses_to_final() -> None:
    out = anyio.run(bash.fn, r'printf "10%%\r50%%\r100%% done"', 10)
    assert out == "100% done"


def test_strip_terminal_controls_unit() -> None:
    assert _strip_terminal_controls("\x1b[2J\x1b[Hhello\x1b[0m") == "hello"
    assert _strip_terminal_controls("a\x1b]0;title\x07b") == "ab"
    assert _strip_terminal_controls("plain text") == "plain text"  # fast path
    assert _strip_terminal_controls("one\rtwo\rthree") == "three"


def test_stdin_is_fed_to_command() -> None:
    # A command that reads stdin gets exactly what the model passes.
    out = anyio.run(bash.fn, 'read -p "Name: " n; echo "Hi $n"', 10, "Teddy\n")
    assert out == "Hi Teddy"


def test_stdin_closes_after_input_so_no_hang() -> None:
    # `cat` with no stdin must hit EOF and exit fast, not block until timeout.
    out = anyio.run(bash.fn, "cat", 8)
    assert "exit code 0" in out or out.strip() == ""


def test_bash_exposes_stdin_parameter() -> None:
    assert "stdin" in bash.input_schema["properties"]
