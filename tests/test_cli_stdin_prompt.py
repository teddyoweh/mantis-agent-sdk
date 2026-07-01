"""`mantis run -` reads the prompt from stdin (piping a spec/file into the agent)."""

from __future__ import annotations

from mantis_agent.cli import _resolve_prompt


def test_dash_reads_stdin() -> None:
    assert _resolve_prompt("-", lambda: "piped feature spec\n") == "piped feature spec"


def test_literal_prompt_kept() -> None:
    # a real prompt must NOT touch stdin
    called = {"n": 0}

    def reader() -> str:
        called["n"] += 1
        return "should not be read"

    assert _resolve_prompt("fix the parser bug", reader) == "fix the parser bug"
    assert called["n"] == 0


def test_whitespace_dash_still_stdin() -> None:
    assert _resolve_prompt("  -  ", lambda: "content") == "content"


def test_stdin_content_is_stripped() -> None:
    assert _resolve_prompt("-", lambda: "\n  hello world  \n\n") == "hello world"


def test_empty_prompt_passthrough() -> None:
    assert _resolve_prompt("", lambda: "x") == ""
