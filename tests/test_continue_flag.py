"""--continue / resume_most_recent — reload the newest conversation on launch."""

from __future__ import annotations

import time

import pytest

from mantis_agent.session_tree import SessionTranscript, new_session_id
from mantis_agent.tui import MantisTUI
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


def _persist(prompt: str) -> str:
    t = _tui()
    t.transcript = SessionTranscript(new_session_id())
    t.messages = [UserMessage(content=prompt), AssistantMessage(content=[TextBlock(text="ok")])]
    t._persist_messages(0)
    return t.transcript.session_id


def test_no_sessions_returns_none() -> None:
    assert _tui().resume_most_recent() is None


def test_resumes_a_session() -> None:
    sid = _persist("fix the parser bug")
    t = _tui()
    label = t.resume_most_recent()
    assert label == "fix the parser bug"
    assert len(t.messages) > 0
    assert t.transcript.session_id == sid          # continues the SAME session


def test_picks_most_recent_of_several() -> None:
    _persist("older task")
    time.sleep(1.05)                               # ensure a distinct mtime
    newer = _persist("newer task")
    t = _tui()
    label = t.resume_most_recent()
    assert label == "newer task"
    assert t.transcript.session_id == newer


def test_continue_flag_parses() -> None:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--continue", "-c", dest="continue_session", action="store_true")
    assert p.parse_args(["--continue"]).continue_session is True
    assert p.parse_args(["-c"]).continue_session is True
    assert p.parse_args([]).continue_session is False
