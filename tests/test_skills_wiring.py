"""Skills progressive disclosure (T1.3): discover SKILL.md files, inject only
the frontmatter catalog, load bodies on demand via the load_skill tool."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from mantis_agent.builtin_tools.skill_tool import load_skill
from mantis_agent.skills import (
    discover_skills,
    load_skill_body,
    render_skill_catalog,
)


@pytest.fixture
def skills_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)
    return tmp_path


def _make_skill(home: Path, slug: str, name: str, desc: str, body: str) -> None:
    d = home / "skills" / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")


def test_discover_parses_frontmatter_and_body(skills_home) -> None:
    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "Use pdftk fill_form.")
    skills = discover_skills()
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "pdf-forms"
    assert s.description == "Fill PDF forms"
    assert "pdftk" in s.body


def test_catalog_is_frontmatter_only(skills_home) -> None:
    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "SECRET BODY DETAIL")
    catalog = render_skill_catalog(discover_skills())
    assert "pdf-forms" in catalog
    assert "Fill PDF forms" in catalog
    assert "SECRET BODY DETAIL" not in catalog   # body NOT in the catalog


def test_load_skill_body(skills_home) -> None:
    _make_skill(skills_home, "pdf", "pdf-forms", "d", "the full instructions here")
    assert "full instructions" in (load_skill_body("pdf-forms") or "")
    assert load_skill_body("missing") is None


def test_load_skill_tool(skills_home) -> None:
    _make_skill(skills_home, "pdf", "pdf-forms", "d", "STEP ONE do this")
    out = anyio.run(lambda: load_skill.fn(name="pdf-forms"))
    assert "STEP ONE" in out
    miss = anyio.run(lambda: load_skill.fn(name="nope"))
    assert "no skill named" in miss


def test_empty_catalog_when_no_skills(skills_home) -> None:
    assert discover_skills() == []
    assert render_skill_catalog([]) == ""


def test_injected_into_agent_context(skills_home, monkeypatch) -> None:
    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider

    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "body")
    agent = Agent(model="mock-7b", provider=MockProvider(),
                  include_env=False, include_memory=True)
    ctx = agent._build_user_context()
    assert "skills" in ctx
    assert "pdf-forms" in ctx["skills"]


def test_tool_registered_in_terminal() -> None:
    from mantis_agent.tui import MantisTUI
    tui = MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                    max_tokens=1, temperature=None, max_turns=1)
    names = {t.name for t in tui._build_agent().tools}
    assert "load_skill" in names
