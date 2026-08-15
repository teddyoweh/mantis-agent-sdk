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
    match_skills,
    render_skill_catalog,
)


@pytest.fixture
def skills_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)
    return tmp_path


def _make_skill(
    home: Path,
    slug: str,
    name: str,
    desc: str,
    body: str,
    *,
    search_hint: str | None = None,
) -> None:
    d = home / "skills" / slug
    d.mkdir(parents=True)
    frontmatter = f"---\nname: {name}\ndescription: {desc}\n"
    if search_hint:
        frontmatter += f"search_hint: {search_hint}\n"
    (d / "SKILL.md").write_text(f"{frontmatter}---\n{body}\n")


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
    agent = Agent(model="mock-7b", provider=MockProvider(), skills="auto",
                  include_env=False, include_memory=True)
    ctx = agent._build_user_context()
    assert "skills" in ctx
    assert "pdf-forms" in ctx["skills"]
    assert agent.tools.get("load_skill") is load_skill


def test_agent_skill_tool_wiring_does_not_duplicate(skills_home) -> None:
    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider

    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "body")
    agent = Agent(model="mock-7b", provider=MockProvider(), skills="auto", tools=[load_skill],
                  include_env=False, include_memory=True)
    ctx = agent._build_user_context()
    assert "skills" in ctx
    assert [t.name for t in agent.tools].count("load_skill") == 1


def test_tool_registered_in_terminal() -> None:
    from mantis_agent.tui import MantisTUI
    tui = MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                    max_tokens=1, temperature=None, max_turns=1)
    names = {t.name for t in tui._build_agent().tools}
    assert "load_skill" in names


def test_discover_parses_search_hint(skills_home) -> None:
    _make_skill(
        skills_home,
        "pdf",
        "pdf-forms",
        "Fill PDF forms",
        "body",
        search_hint="acroforms pdftk flatten",
    )
    skills = discover_skills()
    assert skills[0].search_hint == "acroforms pdftk flatten"


def test_match_skills_uses_name_description_and_search_hint(skills_home) -> None:
    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "PDF instructions")
    _make_skill(
        skills_home,
        "deploy",
        "cloud-deploy",
        "Provision hosted GPUs",
        "Deploy instructions",
        search_hint="runpod modal lambda",
    )

    assert [s.name for s in match_skills("please flatten these pdf forms", discover_skills())] == ["pdf-forms"]
    assert [s.name for s in match_skills("spin this up on runpod", discover_skills())] == ["cloud-deploy"]


def test_relevant_skill_body_auto_injected_for_matching_turn(skills_home) -> None:
    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import UserMessage

    _make_skill(
        skills_home,
        "pdf",
        "pdf-forms",
        "Fill PDF forms",
        "SKILL BODY: use pdftk fill_form then flatten.",
    )

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=MockProvider(), skills="auto",
            include_env=False,
            include_memory=True,
            include_recall=False,
        )
        messages = [UserMessage(content="Can you fill these PDF forms?")]
        try:
            async for _ in agent.run_iter(messages):
                pass
        finally:
            await agent.aclose()
        reminders = [
            m for m in messages
            if isinstance(m, UserMessage)
            and getattr(m, "isMeta", False)
            and isinstance(m.content, str)
        ]
        assert any("Available skills" in m.content for m in reminders)
        assert any("SKILL BODY: use pdftk" in m.content for m in reminders)

    anyio.run(main)


def test_relevant_skill_auto_injection_deduped_across_turns(skills_home) -> None:
    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import UserMessage

    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "SKILL BODY once")

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=MockProvider(), skills="auto",
            include_env=False,
            include_memory=True,
            include_recall=False,
        )
        messages = [UserMessage(content="fill PDF forms")]
        try:
            async for _ in agent.run_iter(messages):
                pass
            messages.append(UserMessage(content="more PDF forms please"))
            async for _ in agent.run_iter(messages):
                pass
        finally:
            await agent.aclose()
        bodies = [
            m for m in messages
            if isinstance(m, UserMessage)
            and getattr(m, "isMeta", False)
            and isinstance(m.content, str)
            and "SKILL BODY once" in m.content
        ]
        assert len(bodies) == 1

    anyio.run(main)


def test_options_skills_preload_explicit_skill_body(skills_home) -> None:
    from mantis_agent import MantisAgentOptions, query
    from mantis_agent.providers.base import register
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import UserMessage

    _make_skill(skills_home, "front", "frontend-polish", "Polish UI", "SKILL BODY explicit")
    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "SKILL BODY should not load")
    register("mock", lambda: MockProvider(default_text="done"))

    async def main():
        opts = MantisAgentOptions(
            model="mock-7b",
            backend="mock",
            skills=["frontend-polish"],
            include_memory=True,
        )
        assert opts.to_query_options()["skills"] == ["frontend-polish"]
        messages = [m async for m in query(prompt="build a page", options=opts)]
        reminders = [
            m.content
            for m in messages
            if isinstance(m, UserMessage)
            and getattr(m, "isMeta", False)
            and isinstance(m.content, str)
        ]
        joined = "\n".join(reminders)
        assert "frontend-polish" in joined
        assert "SKILL BODY explicit" in joined
        assert "pdf-forms" not in joined
        assert "SKILL BODY should not load" not in joined

    anyio.run(main)


def test_options_skills_all_preloads_all_skill_bodies(skills_home) -> None:
    from mantis_agent import MantisAgentOptions, query
    from mantis_agent.providers.base import register
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.types import UserMessage

    _make_skill(skills_home, "front", "frontend-polish", "Polish UI", "FRONT BODY")
    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "PDF BODY")
    register("mock", lambda: MockProvider(default_text="done"))

    async def main():
        messages = [
            m
            async for m in query(
                prompt="general task",
                options=MantisAgentOptions(
                    model="mock-7b",
                    backend="mock",
                    skills="all",
                    include_memory=True,
                ),
            )
        ]
        reminders = [
            m.content
            for m in messages
            if isinstance(m, UserMessage)
            and getattr(m, "isMeta", False)
            and isinstance(m.content, str)
        ]
        joined = "\n".join(reminders)
        assert "FRONT BODY" in joined
        assert "PDF BODY" in joined

    anyio.run(main)


# ---------------------------------------------------------------------------
# The default: off for library callers
# ---------------------------------------------------------------------------


def test_skills_are_off_by_default_for_sdk_callers(skills_home) -> None:
    """An SDK caller must not inherit skills from a home directory.

    Dogfooded: an agent built in a temp directory with a three-tool belt called
    ``check-internet`` and pinged google.com — a skill sitting in
    ``~/.mantis-agent/skills`` on the developer's machine, injected into a
    library caller's conversation. Whether an agent works then depends on whose
    laptop it ran on, which is not a property a library should have.

    ``skills="auto"`` (what the terminal passes) restores the old behavior.
    """

    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider

    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "body")

    off = Agent(model="mock-7b", provider=MockProvider(),
                include_env=False, include_memory=True)
    assert "skills" not in off._build_user_context()
    assert off.tools.get("load_skill") is None

    on = Agent(model="mock-7b", provider=MockProvider(), skills="auto",
               include_env=False, include_memory=True)
    assert "pdf-forms" in on._build_user_context()["skills"]


def test_explicit_skill_list_still_selects(skills_home) -> None:
    from mantis_agent.agent import Agent
    from mantis_agent.providers.mock import MockProvider

    _make_skill(skills_home, "pdf", "pdf-forms", "Fill PDF forms", "body")
    _make_skill(skills_home, "csv", "csv-clean", "Clean CSVs", "body")

    agent = Agent(model="mock-7b", provider=MockProvider(), skills=["pdf-forms"],
                  include_env=False, include_memory=True)
    ctx = agent._build_user_context()
    assert "pdf-forms" in ctx["skills"]
    assert "csv-clean" not in ctx["skills"]
