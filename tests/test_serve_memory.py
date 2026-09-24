"""The Memory page: instruction files (the CLAUDE.md-style hierarchy) and the
agent's own memory (MEMORY.md + memory/*.md), read and edited in place."""

from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.chdir(proj)
    (proj / "AGENTS.md").write_text("# Rules\n\nBe terse.\n", encoding="utf-8")
    return home, proj


def test_state_lists_every_standard_file_and_what_is_loaded(env):
    from mantis_agent import serve

    m = serve.memory_state()
    ids = {x["id"]: x for x in m["instructions"]}
    assert {"user", "agents", "mantis", "local"} <= set(ids)
    assert ids["agents"]["exists"] and ids["agents"]["loaded"] and "Be terse" in ids["agents"]["content"]
    assert not ids["user"]["exists"] and not ids["user"]["loaded"]
    # the absolute path never leaves the server
    assert all("_abs" not in x for x in m["instructions"])


def test_writes_go_only_to_known_slots(env):
    home, proj = env
    from mantis_agent import serve

    assert serve.save_instruction("local", "private note")["ok"]
    assert (proj / "MANTIS.local.md").read_text() == "private note\n"
    assert serve.save_instruction("user", "my rules")["ok"]
    assert (home / "MANTIS.md").read_text() == "my rules\n"
    # an id the page invents, or a path, is refused
    for bad in ("../../etc/passwd", "/tmp/x", "loaded:999", None):
        assert serve.save_instruction(bad, "x")["ok"] is False
    # and now it is loaded
    ids = {x["id"]: x for x in serve.memory_state()["instructions"]}
    assert ids["local"]["loaded"] and ids["user"]["loaded"]


def test_a_new_memory_is_indexed_never_overwrites_and_forgets_cleanly(env):
    home, _ = env
    from mantis_agent import serve

    r = serve.save_memory(None, "Prefers terse replies", "short answers", "feedback", "Keep it short.")
    assert r["ok"] and r["slug"] == "prefers-terse-replies"
    text = (home / "memory" / "prefers-terse-replies.md").read_text()
    assert "type: feedback" in text and "Keep it short." in text
    assert "(memory/prefers-terse-replies.md) — short answers" in (home / "MEMORY.md").read_text()
    again = serve.save_memory(None, "Prefers terse replies", "x", "user", "clobber")
    assert again["ok"] is False and again["exists"] is True
    assert "Keep it short." in (home / "memory" / "prefers-terse-replies.md").read_text()
    # an edit keeps the slug and does not add a second index line
    assert serve.save_memory("prefers-terse-replies", "Terse", "d", "user", "b")["ok"]
    assert (home / "MEMORY.md").read_text().count("prefers-terse-replies") == 1
    assert serve.save_memory("../escape", "x", "d", "user", "b")["ok"] is False
    assert serve.delete_memory("prefers-terse-replies")["ok"]
    assert not (home / "memory" / "prefers-terse-replies.md").exists()
    assert "prefers-terse-replies" not in (home / "MEMORY.md").read_text()


def test_the_page_edits_both_kinds_on_the_skills_surface():
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    assert '<button data-v="memory">' in INDEX_HTML and 'id="memorypad"' in INDEX_HTML
    for fn in ("function loadMemory(", "function paintMemList(", "function paintMemDoc(", "function memProps(",
               "async function saveMemNow(", "function confirmMemDelete("):
        assert fn in js, fn
    # one editor: a skill save and a memory save share the block engine
    assert 'if (SKW.kind !== "skill") return saveMemNow();' in js
    for route in ('"/api/memory/file"', '"/api/memory/index"', '"/api/memory/entry"', '"/api/memory/entry/delete"'):
        assert route in js, route
    # the two document pages never keep each other's DOM (shared element ids)
    assert 'const DOCS = ["skills", "memory"];' in js and "DOCS.filter(v => v !== name)" in js
