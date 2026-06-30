"""MANTIS.md instruction-memory hierarchy — the mantis analogue of CLAUDE.md:
managed → user → project (root→cwd, incl. .mantis/ + rules) → local, with
recursive @import expansion."""

from __future__ import annotations

from mantis_agent.project_memory import (
    load_memory_files,
    render_memory_prompt,
)


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_precedence_order_user_project_local(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    _write(home / "MANTIS.md", "User-level instructions.")

    proj = tmp_path / "proj"
    _write(proj / "MANTIS.md", "Project root.")
    sub = proj / "sub"
    _write(sub / "MANTIS.md", "Deepest dir.")
    _write(sub / "MANTIS.local.md", "Local private.")

    files = load_memory_files(sub)
    tiers = [f.tier for f in files]
    # user comes first, local last; project files in root→cwd order between.
    assert tiers[0] == "user"
    assert tiers[-1] == "local"
    contents = [f.content for f in files]
    assert contents.index("Project root.") < contents.index("Deepest dir.")


def test_workspace_dir_and_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    _write(proj / ".mantis" / "MANTIS.md", "Workspace file.")
    _write(proj / ".mantis" / "rules" / "a.md", "Rule A.")
    _write(proj / ".mantis" / "rules" / "b.md", "Rule B.")

    contents = [f.content for f in load_memory_files(proj)]
    assert "Workspace file." in contents
    assert "Rule A." in contents and "Rule B." in contents


def test_at_import_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    _write(proj / "MANTIS.md", "Main file.\n@./shared.md\n")
    _write(proj / "shared.md", "Imported content.")

    files = load_memory_files(proj)
    paths = [f.path.name for f in files]
    # import emitted right after its parent, tagged with the parent.
    assert paths == ["MANTIS.md", "shared.md"]
    imported = next(f for f in files if f.path.name == "shared.md")
    assert imported.parent.name == "MANTIS.md"


def test_import_skipped_inside_code_fence(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    _write(proj / "MANTIS.md", "Text.\n```\n@./should_not_load.md\n```\n")
    _write(proj / "should_not_load.md", "NOPE")
    contents = [f.content for f in load_memory_files(proj)]
    assert "NOPE" not in contents


def test_import_cycle_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    _write(proj / "MANTIS.md", "A.\n@./b.md\n")
    _write(proj / "b.md", "B.\n@./MANTIS.md\n")  # cycle back
    files = load_memory_files(proj)
    # Deduped: each file appears once, no infinite loop.
    names = sorted(f.path.name for f in files)
    assert names == ["MANTIS.md", "b.md"]


def test_render_is_labeled_and_empty_when_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert render_memory_prompt(empty) == ""

    proj = tmp_path / "proj"
    _write(proj / "MANTIS.md", "Hello rules.")
    out = render_memory_prompt(proj)
    assert "Hello rules." in out
    assert "MANTIS.md" in out
    assert "project instructions" in out  # tier label
