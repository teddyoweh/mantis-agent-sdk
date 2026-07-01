"""/export transcript rendering + /copy clipboard helper (T2)."""

from __future__ import annotations

import shutil
import subprocess

from mantis_agent import AssistantMessage, TextBlock, ToolUseBlock, UserMessage
from mantis_agent.clipboard import copy_to_clipboard
from mantis_agent.tui import render_transcript


def test_render_transcript_basic() -> None:
    msgs = [
        UserMessage(content="how do I read a file?"),
        AssistantMessage(content=[TextBlock(text="Use the read_file tool.")]),
    ]
    out = render_transcript(msgs)
    assert "# mantis conversation" in out
    assert "## User" in out and "how do I read a file?" in out
    assert "## Assistant" in out and "Use the read_file tool." in out


def test_render_transcript_skips_meta_and_notes_tools() -> None:
    msgs = [
        UserMessage(content="ctx", isMeta=True),                 # skipped
        UserMessage(content="do it"),
        AssistantMessage(content=[
            TextBlock(text="running"),
            ToolUseBlock(id="c1", name="bash", input={"command": "ls"}),
        ]),
    ]
    out = render_transcript(msgs)
    assert "ctx" not in out                 # isMeta head excluded
    assert "do it" in out
    assert "running" in out
    assert "bash" in out                    # tool call noted


def test_render_transcript_empty() -> None:
    out = render_transcript([])
    assert out.strip() == "# mantis conversation"


def test_copy_to_clipboard_returns_bool() -> None:
    # Never raises; returns whether a clipboard tool was available.
    assert isinstance(copy_to_clipboard("hello"), bool)


def test_copy_roundtrip_if_available() -> None:
    # On macOS, pbcopy+pbpaste round-trips.
    if shutil.which("pbcopy") and shutil.which("pbpaste"):
        assert copy_to_clipboard("mantis-clip-test") is True
        got = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
        assert got == "mantis-clip-test"
