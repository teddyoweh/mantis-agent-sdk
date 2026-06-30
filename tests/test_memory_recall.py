"""Memory recall — scan (mtime prefilter), keyword relevance selection (≤5),
dedup against already-surfaced, and staleness caveats on injection."""

from __future__ import annotations

import os
import time

import pytest

from mantis_agent.memory import MemoryEntry, save_memory_entry
from mantis_agent.memory_recall import (
    find_relevant_memories,
    recall_block,
    render_recalled_memory,
    scan_memories,
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))


def _seed():
    save_memory_entry(MemoryEntry(
        slug="deploy", name="Deploy process",
        description="How to deploy the app to production with kubernetes",
        type="project", body="Run kubectl apply."))
    save_memory_entry(MemoryEntry(
        slug="testing", name="Testing prefs",
        description="User prefers pytest and TDD", type="feedback",
        body="Always write tests first."))
    save_memory_entry(MemoryEntry(
        slug="lunch", name="Lunch", description="User likes sushi",
        type="user", body="Order sushi."))


def test_relevance_picks_the_right_memory():
    _seed()
    hits = find_relevant_memories("how do I deploy to kubernetes?")
    assert hits and hits[0].entry.slug == "deploy"
    assert all(h.entry.slug != "lunch" for h in hits)  # irrelevant excluded


def test_no_relevant_returns_empty():
    _seed()
    assert find_relevant_memories("quantum chromodynamics recipe") == []


def test_limit_caps_results():
    for i in range(10):
        save_memory_entry(MemoryEntry(
            slug=f"m{i}", name=f"deploy thing {i}",
            description="deploy kubernetes production", type="project", body="x"))
    assert len(find_relevant_memories("deploy kubernetes", limit=5)) == 5


def test_already_surfaced_is_skipped():
    _seed()
    first = find_relevant_memories("deploy kubernetes")
    surfaced = frozenset(str(h.entry.path) for h in first)
    again = find_relevant_memories("deploy kubernetes", already_surfaced=surfaced)
    assert again == []


def test_scan_is_newest_first():
    save_memory_entry(MemoryEntry(slug="old", name="old", description="d", body="x"))
    time.sleep(0.01)
    save_memory_entry(MemoryEntry(slug="new", name="new", description="d", body="x"))
    scanned = scan_memories()
    slugs = [s.entry.slug for s in scanned]
    assert slugs.index("new") < slugs.index("old")


def test_staleness_caveat_only_for_old_memories(tmp_path):
    _seed()
    fresh = find_relevant_memories("deploy kubernetes")[0]
    assert "days old" not in render_recalled_memory(fresh)  # saved today
    # Age it 5 days and re-render.
    os.utime(fresh.entry.path, (time.time() - 5 * 86400,) * 2)
    aged = find_relevant_memories("deploy kubernetes")[0]
    block = render_recalled_memory(aged)
    assert "5 days old" in block
    assert "Verify against current code" in block


def test_recall_block_returns_text_and_paths():
    _seed()
    text, paths = recall_block("deploy to kubernetes")
    assert "kubectl apply" in text
    assert "<system-reminder>" in text
    assert any("deploy" in p for p in paths)
