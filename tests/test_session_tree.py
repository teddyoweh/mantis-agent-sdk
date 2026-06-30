"""Claude-Code-faithful transcript tree: append-only JSONL with parent_uuid
linking, resume (chain walk), branch (independent fork), rewind (truncate), and
cheap head/tail session listing."""

from __future__ import annotations

import pytest

from mantis_agent.session_tree import (
    SessionTranscript,
    branch_session,
    build_chain,
    latest_leaf,
    list_sessions,
    load_entries,
    load_for_resume,
    new_session_id,
    rewind_chain,
)
from mantis_agent.types import AssistantMessage, UserMessage


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MANTIS_AGENT_PROJECT_ROOT", str(tmp_path / "proj"))


def _seed(sid: str) -> SessionTranscript:
    t = SessionTranscript(sid)
    t.set_title("Session one")
    t.append_message("user", "whats the time")
    t.append_message("assistant", "It is 10:30.")
    t.append_message("user", "and the date?")
    t.append_message("assistant", "June 30.")
    t.record_last_prompt("and the date?")
    return t


def test_append_and_resume_roundtrip() -> None:
    sid = new_session_id()
    _seed(sid)
    msgs = load_for_resume(sid)
    assert [type(m).__name__ for m in msgs] == [
        "UserMessage", "AssistantMessage", "UserMessage", "AssistantMessage"
    ]
    assert msgs[0].content == "whats the time"


def test_parent_uuid_chain_is_linked() -> None:
    sid = new_session_id()
    _seed(sid)
    entries = load_entries(SessionTranscript(sid).path)
    chain = build_chain(entries, latest_leaf(entries).uuid)
    # Each non-root entry parents off the previous one.
    assert chain[0].parent_uuid is None
    for prev, cur in zip(chain, chain[1:]):
        assert cur.parent_uuid == prev.uuid


def test_resume_reopens_at_tip_and_continues() -> None:
    sid = new_session_id()
    _seed(sid)
    # Reopen (as resume would) and append — must thread off the existing tip.
    t2 = SessionTranscript(sid)
    t2.append_message("user", "third question")
    msgs = load_for_resume(sid)
    assert len(msgs) == 5
    assert msgs[-1].content == "third question"


def test_branch_is_independent_fork() -> None:
    sid = new_session_id()
    _seed(sid)
    fork = branch_session(sid)
    assert fork != sid
    # Fork carries the full copied thread + a forked_from backpointer.
    fork_entries = load_entries(SessionTranscript(fork).path)
    assert len(fork_entries) == 4
    assert all(e.forked_from and e.forked_from["session_id"] == sid for e in fork_entries)
    # Appending to the fork must NOT touch the original.
    SessionTranscript(fork).append_message("user", "fork-only")
    assert len(load_for_resume(sid)) == 4
    assert len(load_for_resume(fork)) == 5


def test_branch_relinks_chain_from_scratch() -> None:
    sid = new_session_id()
    _seed(sid)
    fork = branch_session(sid)
    src = load_entries(SessionTranscript(sid).path)
    dst = load_entries(SessionTranscript(fork).path)
    # New uuids, fresh parent chain (root has no parent).
    assert {e.uuid for e in src}.isdisjoint({e.uuid for e in dst})
    assert dst[0].parent_uuid is None
    for prev, cur in zip(dst, dst[1:]):
        assert cur.parent_uuid == prev.uuid


def test_branch_empty_session_raises() -> None:
    sid = new_session_id()
    SessionTranscript(sid)  # never appends a message
    with pytest.raises(ValueError):
        branch_session(sid)


def test_rewind_truncates_inclusive() -> None:
    sid = new_session_id()
    _seed(sid)
    entries = load_entries(SessionTranscript(sid).path)
    chain = build_chain(entries, latest_leaf(entries).uuid)
    target = chain[1]  # first assistant message
    rw = rewind_chain(entries, target.uuid)
    assert len(rw) == 2
    assert rw[-1].uuid == target.uuid


def test_list_sessions_is_cheap_and_sorted() -> None:
    a = new_session_id()
    _seed(a)
    b = new_session_id()
    tb = SessionTranscript(b)
    tb.set_title("Session two")
    tb.append_message("user", "hello there")
    sessions = list_sessions()
    assert {s.session_id for s in sessions} == {a, b}
    by_id = {s.session_id: s for s in sessions}
    assert by_id[a].title == "Session one"
    assert by_id[a].first_prompt == "whats the time"
    assert by_id[a].message_count == 2  # two user turns
    assert by_id[b].first_prompt == "hello there"


def test_dangling_tool_use_is_dropped_on_resume() -> None:
    from mantis_agent.types import ToolUseBlock

    sid = new_session_id()
    t = SessionTranscript(sid)
    t.append_message("user", "do a thing")
    # Assistant ended on a tool_use that never got a result (crash mid-turn).
    t.append_message("assistant", [ToolUseBlock(id="tu1", name="bash", input={})])
    msgs = load_for_resume(sid)
    # The unanswered assistant turn is dropped so the array stays API-valid.
    assert [type(m).__name__ for m in msgs] == ["UserMessage"]
