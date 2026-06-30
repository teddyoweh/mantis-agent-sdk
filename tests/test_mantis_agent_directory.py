"""Tests for the .mantis-agent/ directory: paths, transcripts, memory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from mantis_agent import (
    APIAssistantMessage,
    APIUserMessage,
    JsonlTranscript,
    MemoryEntry,
    SDKAssistantMessage,
    SDKResultMessage,
    SDKSystemMessage,
    SDKUserMessage,
    get_mantis_agent_dir,
    get_memory_dir,
    get_session_path,
    get_sessions_dir,
    iter_transcripts,
    list_memory_entries,
    load_memory_entry,
    load_memory_index,
    read_transcript,
    save_memory_entry,
    update_memory_index,
)
from mantis_agent.paths import sanitize_session_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_mantis_agent_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Force MANTIS_AGENT_HOME to a tmpdir for the test's lifetime."""
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# paths.py
# ---------------------------------------------------------------------------


def test_mantis_agent_home_respects_env(tmp_mantis_agent_home: Path) -> None:
    assert get_mantis_agent_dir() == tmp_mantis_agent_home


def test_subdirs_use_root(tmp_mantis_agent_home: Path) -> None:
    assert get_sessions_dir() == tmp_mantis_agent_home / "sessions"
    assert get_memory_dir() == tmp_mantis_agent_home / "memory"


def test_session_path_sanitizes(tmp_mantis_agent_home: Path) -> None:
    # Slashes / colons must not escape the sessions dir. Underscores are fine.
    p = get_session_path("malicious/../../etc/passwd")
    assert str(p).startswith(str(tmp_mantis_agent_home / "sessions"))
    assert "/" not in p.name
    # Resolves to a real child of sessions/ (no parent traversal).
    assert p.parent.resolve() == (tmp_mantis_agent_home / "sessions").resolve()


def test_sanitize_session_id_keeps_uuids() -> None:
    assert sanitize_session_id("abc-123_def.ghi") == "abc-123_def.ghi"
    # Non-empty fallback when input is all stripped
    assert sanitize_session_id("") == "_"


# ---------------------------------------------------------------------------
# transcripts.py
# ---------------------------------------------------------------------------


def test_jsonl_transcript_roundtrip(tmp_mantis_agent_home: Path) -> None:
    sid = "session-001"
    msgs = [
        SDKSystemMessage(model="qwen2.5-7b-instruct", tools=["foo"]),
        SDKUserMessage(message=APIUserMessage(content="hello")),
        SDKAssistantMessage(message=APIAssistantMessage(id="msg_1")),
        SDKResultMessage(subtype="success", num_turns=1, result="hi"),
    ]
    with JsonlTranscript(sid) as t:
        for m in msgs:
            t.write(m)

    # File exists at the canonical path
    assert get_session_path(sid).exists()

    # Round-trip parse
    lines = list(read_transcript(sid))
    assert len(lines) == 4
    assert lines[0]["type"] == "system" and lines[0]["subtype"] == "init"
    assert lines[1]["type"] == "user"
    assert lines[1]["message"]["content"] == "hello"
    assert lines[2]["type"] == "assistant"
    assert lines[3]["type"] == "result" and lines[3]["subtype"] == "success"


def test_jsonl_transcript_tolerates_partial_line(tmp_mantis_agent_home: Path) -> None:
    """A crash mid-write leaves a partial trailing line. Reader must skip it."""

    sid = "session-002"
    with JsonlTranscript(sid) as t:
        t.write(SDKSystemMessage(model="m"))
    # Append a garbage half-line without a trailing newline.
    with open(get_session_path(sid), "ab") as f:
        f.write(b'{"type":"resu')

    lines = list(read_transcript(sid))
    assert len(lines) == 1  # only the complete first line


def test_iter_transcripts_lists_files(tmp_mantis_agent_home: Path) -> None:
    for sid in ("a", "b", "c"):
        with JsonlTranscript(sid) as t:
            t.write(SDKSystemMessage())
    found = sorted(s for s, _ in iter_transcripts())
    assert found == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# memory.py
# ---------------------------------------------------------------------------


def test_memory_entry_roundtrip(tmp_mantis_agent_home: Path) -> None:
    e = MemoryEntry(
        slug="sf_housing",
        name="SF housing search",
        description="Apartment search in SF, May 2026",
        type="project",
        body="Looking in Mission, Hayes Valley. $3500 max.",
    )
    p = save_memory_entry(e)
    assert p.exists()
    assert p.read_text().startswith("---\n")

    loaded = load_memory_entry("sf_housing")
    assert loaded is not None
    assert loaded.name == "SF housing search"
    assert loaded.type == "project"
    assert "Mission" in loaded.body


def test_memory_supports_nested_slugs(tmp_mantis_agent_home: Path) -> None:
    e = MemoryEntry(
        slug="stripe/onboarding",
        name="Stripe onboarding notes",
        description="What we learned setting up Stripe Connect",
        type="reference",
        body="Webhook signature verification uses…",
    )
    p = save_memory_entry(e)
    assert p == get_memory_dir() / "stripe" / "onboarding.md"
    entries = list_memory_entries(recursive=True)
    slugs = {e.slug for e in entries}
    assert "stripe/onboarding" in slugs


def test_update_memory_index_renders_lines(tmp_mantis_agent_home: Path) -> None:
    save_memory_entry(
        MemoryEntry(
            slug="a",
            name="Alpha",
            description="alpha hook",
            type="project",
            body="body",
        )
    )
    save_memory_entry(
        MemoryEntry(
            slug="b",
            name="Beta",
            description="beta hook",
            type="user",
            body="body",
        )
    )
    update_memory_index()
    idx = load_memory_index()
    assert "Alpha" in idx
    assert "Beta" in idx
    assert "memory/a.md" in idx
    assert "memory/b.md" in idx


def test_unknown_memory_returns_none(tmp_mantis_agent_home: Path) -> None:
    assert load_memory_entry("does_not_exist") is None


def test_memory_index_empty_when_dir_missing(tmp_mantis_agent_home: Path) -> None:
    # No memory/ dir yet; load_memory_index returns ""
    assert load_memory_index() == ""
    assert list_memory_entries() == []
