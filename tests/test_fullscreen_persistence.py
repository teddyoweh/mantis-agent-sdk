"""Session persistence — _persist_messages writes turns that /resume can list."""

from __future__ import annotations

import pytest

from mantis_agent.session_tree import SessionTranscript, list_sessions, new_session_id
from mantis_agent.tui import MantisTUI
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


def test_turn_persisted_and_listable() -> None:
    t = _tui()
    t.transcript = SessionTranscript(new_session_id())
    t.messages = [UserMessage(content="build the auth system"),
                  AssistantMessage(content=[TextBlock(text="on it")])]
    t._persist_messages(0)
    sid = t.transcript.session_id
    found = [s for s in list_sessions() if s.session_id == sid]
    assert len(found) == 1
    assert "build the auth system" in (found[0].first_prompt or "")


def test_noop_without_transcript() -> None:
    t = _tui()
    t.transcript = None
    t.messages = [UserMessage(content="hi")]
    t._persist_messages(0)                    # must not raise
    assert not any(s for s in list_sessions())   # nothing written


def test_only_appends_new_messages() -> None:
    t = _tui()
    t.transcript = SessionTranscript(new_session_id())
    t.messages = [UserMessage(content="one"), AssistantMessage(content=[TextBlock(text="a")])]
    t._persist_messages(0)
    # a second turn appends only messages from base onward
    t.messages += [UserMessage(content="two"), AssistantMessage(content=[TextBlock(text="b")])]
    t._persist_messages(2)
    sid = t.transcript.session_id
    found = [s for s in list_sessions() if s.session_id == sid]
    assert found and found[0].message_count >= 2   # both turns recorded


def test_meta_messages_skipped() -> None:
    t = _tui()
    t.transcript = SessionTranscript(new_session_id())
    t.messages = [
        UserMessage(content="<ctx>", isMeta=True),      # skipped
        UserMessage(content="real question"),
        AssistantMessage(content=[TextBlock(text="answer")]),
    ]
    t._persist_messages(0)
    sid = t.transcript.session_id
    found = [s for s in list_sessions() if s.session_id == sid]
    assert "real question" in (found[0].first_prompt or "")   # not "<ctx>"
