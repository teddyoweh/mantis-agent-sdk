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


def test_old_body_only_match_beyond_scan_cap():
    path = save_memory_entry(MemoryEntry(
        slug="ancient", name="Old note", description="General notes",
        body="The zephyrquartz workaround fixes this."))
    os.utime(path, (1, 1))
    for i in range(205):
        save_memory_entry(MemoryEntry(
            slug=f"new-{i}", name="Recent note", description="Unrelated", body="Nothing."))
    scanned = scan_memories()
    assert len(scanned) == 200
    assert all(s.entry.slug != "ancient" and not s.entry.body for s in scanned)
    hits = find_relevant_memories("zephyrquartz")
    assert [h.entry.slug for h in hits] == ["ancient"]
    assert "workaround" in hits[0].entry.body


def test_metadata_outweighs_repeated_body_matches():
    for slug, name, desc, body in [
        ("title", "zephyrquartz", "", "note"),
        ("description", "Note", "zephyrquartz", "note"),
        ("body", "Note", "", "zephyrquartz " * 1000),
    ]:
        save_memory_entry(MemoryEntry(slug=slug, name=name, description=desc, body=body))
    assert [h.entry.slug for h in find_relevant_memories("zephyrquartz")] == [
        "title", "description", "body"]


def test_excerpt_includes_distant_query_evidence_and_caveat():
    path = save_memory_entry(MemoryEntry(
        slug="notes", name="Notes", description="General",
        body="irrelevant preface " * 2000 + "zephyrquartz repairneedle fix" + " filler" * 2000))
    os.utime(path, (time.time() - 5 * 86400,) * 2)
    text, paths = recall_block("zephyrquartz repairneedle", max_chars=650)
    assert len(text) <= 650
    assert "zephyrquartz repairneedle fix" in text
    assert str(path) in text and paths == [str(path)]
    assert "Verify against current code" in text
    assert text.endswith("</system-reminder>")


def test_total_bound_and_omitted_entries_not_surfaced():
    for i in range(3):
        save_memory_entry(MemoryEntry(
            slug=f"note-{i}", name="zephyrquartz", description="", body="zephyrquartz " * 2000))
    text, paths = recall_block("zephyrquartz", max_chars=700)
    assert len(text) <= 700
    assert len(paths) == 1
    remaining = find_relevant_memories("zephyrquartz", already_surfaced=frozenset(paths))
    assert len(remaining) == 2
    default_text, _ = recall_block("zephyrquartz")
    assert len(default_text) <= 12_000


@pytest.mark.parametrize("bound", [-100, -1, 0, 1, 20])
def test_tiny_bounds_do_not_surface(bound):
    _seed()
    assert recall_block("deploy", max_chars=bound) == ("", [])


@pytest.mark.parametrize("limit", [-10, -1, 0])
def test_nonpositive_limits(limit):
    _seed()
    assert scan_memories(limit=limit) == []
    assert find_relevant_memories("deploy", limit=limit) == []
    assert recall_block("deploy", limit=limit) == ("", [])


def test_cache_reuses_bodies_and_invalidates_edits(monkeypatch):
    import mantis_agent.memory_recall as recall

    path = save_memory_entry(MemoryEntry(
        slug="cached", name="Notes", description="", body="zephyrquartz"))
    original = recall.load_memory_entry
    calls = []

    def counting_load(slug):
        calls.append(slug)
        return original(slug)

    monkeypatch.setattr(recall, "load_memory_entry", counting_load)
    assert find_relevant_memories("zephyrquartz")
    assert find_relevant_memories("zephyrquartz")
    assert calls == ["cached"]
    path.write_text("Updated repairneedle", encoding="utf-8")
    assert find_relevant_memories("zephyrquartz") == []
    assert find_relevant_memories("repairneedle")
    assert calls == ["cached", "cached"]


def test_cache_is_bounded(monkeypatch):
    from collections import OrderedDict
    import mantis_agent.memory_recall as recall

    monkeypatch.setattr(recall, "_BODY_CACHE", OrderedDict())
    monkeypatch.setattr(recall, "_cache_chars", 0)
    monkeypatch.setattr(recall, "_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(recall, "_CACHE_MAX_CHARS", 1000)
    for i in range(4):
        save_memory_entry(MemoryEntry(
            slug=f"cached-{i}", name="Notes", description="", body="zephyrquartz"))
    assert len(find_relevant_memories("zephyrquartz")) == 4
    assert len(recall._BODY_CACHE) <= 2
    assert recall._cache_chars <= 1000
    save_memory_entry(MemoryEntry(
        slug="oversized", name="Notes", description="", body="repairneedle " * 200))
    assert find_relevant_memories("repairneedle")
    assert recall._cache_chars <= 1000


def test_excerpt_prefers_multiple_distinct_terms():
    save_memory_entry(MemoryEntry(
        slug="windows", name="Notes", description="",
        body="zephyrquartz " + "filler " * 1000
        + "zephyrquartz repairneedle jointly" + " filler" * 1000))
    text, _ = recall_block("zephyrquartz repairneedle", max_chars=500)
    assert "zephyrquartz repairneedle jointly" in text
    assert len(text) <= 500


def test_custom_selector_keeps_order_and_hydrates():
    _seed()
    hits = find_relevant_memories(
        "unmatched", limit=1,
        selector=lambda query, candidates: [s for s in candidates if s.entry.slug == "lunch"],
    )
    assert len(hits) == 1 and hits[0].entry.body == "Order sushi."
