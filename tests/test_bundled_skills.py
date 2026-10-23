"""mantis ships the playbooks a Claude Code agent gets from its bundled skills
— ``pdf``, ``xlsx`` and ``dataviz`` — so a model asked for a report or a
workbook gets the conventions and the check-your-own-output loop, not just a
blank page. A drop-in agent with file tools has them by default; a chat agent
doesn't; a user or project skill of the same name wins; and a turn that has
nothing to do with documents never pays for their bodies."""

from __future__ import annotations

from pathlib import Path

import pytest

from mantis_agent import MantisAgentOptions
from mantis_agent.compat_query import _build_agent, _normalize_options
from mantis_agent.permission_grammar import canonical_tool_name
from mantis_agent.skills import BUNDLED_SKILLS_DIR, discover_skills, load_skill_body, match_skills

BUNDLED = ("dataviz", "pdf", "xlsx")


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    return tmp_path


def _bundled() -> list:
    return [s for s in discover_skills() if s.source == "bundled"]


def test_the_package_ships_them(home) -> None:
    assert sorted(s.name for s in _bundled()) == list(BUNDLED)
    for name in BUNDLED:
        assert (BUNDLED_SKILLS_DIR / name / "SKILL.md").is_file()
        body = load_skill_body(name)
        assert body and "check" in body.lower()        # each one ends in a verify step
    assert discover_skills(bundled=False) == []


def test_a_user_skill_of_the_same_name_replaces_one(home) -> None:
    d = home / "skills" / "pdf"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pdf\ndescription: ours\n---\nOUR PDF RULES")
    (pdf,) = [s for s in discover_skills() if s.name == "pdf"]
    assert pdf.source == "user" and pdf.body == "OUR PDF RULES"


@pytest.mark.parametrize("prompt,expected", [
    ("Create `report.pdf` from `sales.csv` with three charts and page numbers", {"pdf", "dataviz"}),
    ("Create `summary.xlsx` with SUMIFS formulas over the Data sheet", {"xlsx"}),
    ("make a bar chart of revenue by region", {"dataviz"}),
    ("fix the failing test in the inventory package", set()),
    ("Read issue.md and fix the bug it describes in report_gen.py", set()),
    ("Bump the version in config.yaml and update the changelog", set()),
    ("update the page title in index.html", set()),
])
def test_only_document_turns_pull_them_in(home, prompt: str, expected: set) -> None:
    assert {s.name for s in match_skills(prompt, _bundled())} == expected


def _skills_of(opts: MantisAgentOptions):
    return _build_agent(_normalize_options(opts)).skills


def test_a_drop_in_agent_with_file_tools_gets_them() -> None:
    assert _skills_of(MantisAgentOptions(model="mock", allowed_tools=["Read", "Write", "Bash"])) == "bundled"
    assert _skills_of(MantisAgentOptions(model="mock", tools={"type": "preset", "preset": "claude_code"})) == "bundled"


def test_a_chat_agent_or_a_read_only_one_does_not() -> None:
    assert _skills_of(MantisAgentOptions(model="mock")) is None
    assert _skills_of(MantisAgentOptions(model="mock", allowed_tools=["Read", "Grep"])) is None


def test_the_caller_still_decides() -> None:
    assert _skills_of(MantisAgentOptions(model="mock", allowed_tools=["Bash"], skills=["pdf"])) == ["pdf"]
    assert _skills_of(MantisAgentOptions(model="mock", allowed_tools=["Bash"], disallowed_tools=["Skill"])) is None


def test_claude_s_skill_tool_name_is_load_skill() -> None:
    assert canonical_tool_name("Skill") == "load_skill"


def test_bundled_mode_puts_them_in_the_catalog_and_registers_load_skill(home, monkeypatch) -> None:
    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider

    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)
    user = home / "skills" / "mine"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("---\nname: mine\ndescription: a user skill\n---\nx")
    agent = Agent(model="mock", provider=MockProvider(), skills="bundled", include_env=False,
                  include_memory=False, include_recall=False)
    ctx = agent._build_user_context()
    for name in BUNDLED:
        assert f"- {name}:" in ctx["skills"]
    assert "mine" not in ctx["skills"]                  # bundled means bundled only
    assert agent.tools.get("load_skill") is not None
