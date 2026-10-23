"""A claude-agent-sdk project keeps its skills in ``.claude/skills`` and loads
them with ``setting_sources=["project"]``. mantis read only ``.mantis/skills``
(and looked relative to the process, not the agent's cwd), so a migrated
project's skills vanished. ``load_skill`` also returned only the body; Claude's
Skill tool says where the skill lives, so a skill can ship its own scripts."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from mantis_agent import MantisAgentOptions
from mantis_agent.builtin_tools.fs import AGENT_CWD
from mantis_agent.builtin_tools.skill_tool import load_skill
from mantis_agent.compat_query import _build_agent, _normalize_options
from mantis_agent.skills import SKILL_SOURCES, discover_skills


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.delenv("MANTIS_SKILLS_TRUST_PROJECT", raising=False)
    proj = tmp_path / "proj"
    d = proj / ".claude" / "skills" / "release-notes"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: release-notes\ndescription: How we write release notes\n---\n"
                                "Run scripts/check.py on the result.")
    (d / "scripts" / "check.py").write_text("print('ok')")
    u = tmp_path / "claude-home" / "skills" / "tone"
    u.mkdir(parents=True)
    (u / "SKILL.md").write_text("---\nname: tone\ndescription: Our writing tone\n---\nBe plain.")
    return proj


def _names(proj: Path, sources) -> set[str]:
    return {s.name for s in discover_skills(proj, sources=sources, bundled=False)}


def test_claude_folders_need_setting_sources(project: Path) -> None:
    assert _names(project, ()) == set()
    assert _names(project, ["project"]) == {"release-notes"}
    assert _names(project, ["user", "project"]) == {"release-notes", "tone"}


def test_the_agent_cwd_is_where_project_skills_are_found(project: Path) -> None:
    tok, src = AGENT_CWD.set(str(project)), SKILL_SOURCES.set(("project",))
    try:
        assert "release-notes" in {s.name for s in discover_skills(bundled=False)}
    finally:
        AGENT_CWD.reset(tok)
        SKILL_SOURCES.reset(src)


def test_load_skill_says_where_the_skill_lives(project: Path) -> None:
    tok, src = AGENT_CWD.set(str(project)), SKILL_SOURCES.set(("project",))
    try:
        out = anyio.run(lambda: load_skill.fn(name="release-notes"))
    finally:
        AGENT_CWD.reset(tok)
        SKILL_SOURCES.reset(src)
    base = project / ".claude" / "skills" / "release-notes"
    assert out.startswith(f"Base directory for this skill: {base}")
    assert "Run scripts/check.py" in out


def test_a_drop_in_agent_with_project_sources_lists_them(project: Path, monkeypatch) -> None:
    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)
    agent = _build_agent(_normalize_options(MantisAgentOptions(
        model="mock", cwd=str(project), allowed_tools=["Bash", "Write"], setting_sources=["project"])))
    assert agent.skills == "bundled" and agent.setting_sources == ["project"]
    catalog = agent._build_user_context().get("skills", "")
    assert "- release-notes:" in catalog and "- pdf:" in catalog
    assert "- tone:" not in catalog                       # "user" wasn't asked for
