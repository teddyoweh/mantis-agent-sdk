"""The general-purpose `task` tool — delegate a read-only investigation to a
fresh subagent that returns just its findings."""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.builtin_tools import CODING_TOOLS
from mantis_agent.providers.mock import MockProvider
from mantis_agent.subagent import make_task_tool


def _explore() -> list:
    return [t for t in CODING_TOOLS if getattr(t, "is_read_only", False)]


def _task(default_text: str = "done") -> Any:
    prov = MockProvider(default_text=default_text)
    return make_task_tool(model="mock", provider=prov, tools=_explore())


def test_tool_shape() -> None:
    t = _task()
    assert t.name == "task"
    assert t.is_read_only is True                 # safe: read-only exploration
    props = t.input_schema["properties"]
    assert "prompt" in props and "description" in props
    assert t.input_schema["required"] == ["prompt"]


def test_returns_child_final_text() -> None:
    t = _task("The auth check lives in security/gate.py:88.")
    out = anyio.run(lambda: t.fn(prompt="where is auth enforced?", description="find auth"))
    assert out == "The auth check lives in security/gate.py:88."


def test_empty_prompt_guarded() -> None:
    t = _task()
    assert "required" in anyio.run(lambda: t.fn(prompt="   "))
    assert "required" in anyio.run(lambda: t.fn(description="no prompt"))


def test_subagent_has_no_write_or_recursion() -> None:
    # The kit handed to the subagent is read-only and never includes `task`
    # itself (no infinite recursion) or edit/write/bash.
    names = {t.name for t in _explore()}
    assert "task" not in names
    assert "write_file" not in names and "edit_file" not in names and "bash" not in names
    assert "read_file" in names and "grep" in names


def test_subagent_runs_isolated_context() -> None:
    # The child gets a fresh single-turn message list — the parent's prompt only.
    prov = MockProvider(default_text="ok")
    t = make_task_tool(model="mock", provider=prov, tools=_explore())
    anyio.run(lambda: t.fn(prompt="investigate X"))
    # the child's very first call carried exactly the delegated prompt as the
    # sole user message (no parent history leaked in)
    first = prov.calls[0]["messages"]
    user_texts = [m.content for m in first if getattr(m, "role", "") == "user"
                  and isinstance(m.content, str)]
    assert any("investigate X" in tx for tx in user_texts)


# -- agent types (Claude Code's subagent_type) --------------------------------


def test_builtin_agent_types_present() -> None:
    from mantis_agent.subagent import BUILTIN_AGENT_TYPES
    names = [t.name for t in BUILTIN_AGENT_TYPES]
    assert names == ["explore", "plan", "general-purpose"]
    by = {t.name: t for t in BUILTIN_AGENT_TYPES}
    assert by["explore"].tools == "read-only"
    assert by["plan"].tools == "read-only"
    assert by["general-purpose"].tools == "all"


def test_tool_policy_resolution() -> None:
    from mantis_agent.subagent import BUILTIN_AGENT_TYPES, resolve_agent_tools
    by = {t.name: t for t in BUILTIN_AGENT_TYPES}
    kit = list(CODING_TOOLS)
    gp = {t.name for t in resolve_agent_tools(by["general-purpose"], kit)}
    ex = {t.name for t in resolve_agent_tools(by["explore"], kit)}
    # general-purpose gets write+shell; explore is strictly read-only
    assert {"bash", "write_file", "edit_file"} <= gp
    assert not ({"bash", "write_file", "edit_file"} & ex)
    assert {"read_file", "grep", "glob"} <= ex


def test_interactive_and_recursive_tools_always_excluded() -> None:
    # Even under the "all" policy a subagent must never get task (recursion),
    # ask_user_question (no user channel), exit_plan_mode, or todo_write.
    from mantis_agent.subagent import BUILTIN_AGENT_TYPES, resolve_agent_tools
    from mantis_agent.tools import tool as _tool

    @_tool(name="task")
    async def fake_task() -> str:
        return ""

    @_tool(name="ask_user_question")
    async def fake_ask() -> str:
        return ""

    gp = next(t for t in BUILTIN_AGENT_TYPES if t.name == "general-purpose")
    names = {t.name for t in resolve_agent_tools(gp, [*CODING_TOOLS, fake_task, fake_ask])}
    assert "task" not in names and "ask_user_question" not in names


def test_task_schema_and_description_list_types() -> None:
    from mantis_agent.subagent import BUILTIN_AGENT_TYPES
    t = make_task_tool(model="mock", provider=MockProvider(default_text="x"),
                       tools=_explore(), agent_types=list(BUILTIN_AGENT_TYPES))
    enum = t.input_schema["properties"]["subagent_type"]["enum"]
    assert enum == ["explore", "plan", "general-purpose"]
    for name in enum:
        assert name in t.description  # the model reads the menu from here
    assert t.input_schema["required"] == ["prompt"]  # type stays optional


def test_unknown_subagent_type_reports_available() -> None:
    t = _task()
    out = anyio.run(lambda: t.fn(prompt="x", subagent_type="bogus"))
    assert "unknown subagent_type" in out and "explore" in out


def test_default_type_is_explore_and_model_inherits(monkeypatch) -> None:
    # Capture the child-Agent construction to assert wiring without a live run.
    import mantis_agent.subagent as sub
    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured.update(kw)
        async def run(self, messages):
            return None

    monkeypatch.setattr(sub, "Agent", FakeAgent)
    perms = object()
    t = sub.make_task_tool(model="parent-model", provider=MockProvider(),
                           tools=_explore(), permissions=perms)
    anyio.run(lambda: t.fn(prompt="investigate"))
    assert "read-only exploration subagent" in captured["system"]
    assert captured["model"] == "parent-model"      # inherit
    assert captured["permissions"] is perms          # parent's gate flows down
    assert captured["include_recall"] is False and captured["include_env"] is False


def test_type_model_override_and_step_budget(monkeypatch) -> None:
    import mantis_agent.subagent as sub
    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured.update(kw)
        async def run(self, messages):
            return None

    monkeypatch.setattr(sub, "Agent", FakeAgent)
    at = sub.AgentType(name="cheap", description="d", system_prompt="S",
                       tools="read-only", model="mini-model", max_steps=33)
    t = sub.make_task_tool(model="parent-model", provider=MockProvider(),
                           tools=_explore(), agent_types=[at])
    anyio.run(lambda: t.fn(prompt="x", subagent_type="cheap"))
    assert captured["model"] == "mini-model"
    assert captured["max_steps"] == 33  # type budget wins over the default floor
    assert captured["system"] == "S"


# -- user-defined agents (~/.mantis-agent/agents/*.md) ------------------------


def test_discover_user_and_project_agents(monkeypatch, tmp_path) -> None:
    from mantis_agent.subagent import discover_agent_types
    home = tmp_path / "home"; proj = tmp_path / "proj"
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "docs-writer.md").write_text(
        "---\ndescription: Writes docs.\ntools: read_file, write_file\n---\nYou write docs.")
    (proj / ".mantis" / "agents").mkdir(parents=True)
    (proj / ".mantis" / "agents" / "explore.md").write_text(
        "---\nname: explore\ndescription: Project explorer.\n---\nCustom prompt.")
    d = {t.name: t for t in discover_agent_types(proj)}
    # user agent discovered, name falls back to the file stem
    assert d["docs-writer"].source == "user"
    assert d["docs-writer"].tools == ("read_file", "write_file")
    assert d["docs-writer"].system_prompt == "You write docs."
    # project agent OVERRIDES the builtin explore
    assert d["explore"].source == "project"
    assert d["explore"].system_prompt == "Custom prompt."
    # untouched builtins survive
    assert d["general-purpose"].source == "builtin"


def test_malformed_agent_md_skipped(monkeypatch, tmp_path) -> None:
    from mantis_agent.subagent import discover_agent_types
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    (tmp_path / "agents").mkdir(parents=True)
    (tmp_path / "agents" / "empty.md").write_text("---\nname: empty\n---\n")  # no body
    (tmp_path / "agents" / "blank.md").write_text("")
    names = {t.name for t in discover_agent_types(tmp_path)}
    assert "empty" not in names and "blank" not in names
    assert {"explore", "plan", "general-purpose"} <= names  # builtins intact


def test_agent_md_frontmatter_edge_cases() -> None:
    from mantis_agent.subagent import _parse_agent_md
    # model: inherit → None; max_steps clamped; tools keyword forms
    at = _parse_agent_md(
        "---\nmodel: inherit\nmax_steps: 999\ntools: ALL\n---\nBody.", "x")
    assert at.model is None and at.max_steps == 100 and at.tools == "all"
    at2 = _parse_agent_md("---\ntools: read-only\nmax_steps: junk\n---\nBody.", "y")
    assert at2.tools == "read-only" and at2.max_steps == 20
    at3 = _parse_agent_md("no frontmatter, just a prompt", "z")
    assert at3.name == "z" and at3.tools == "all"


def test_user_agent_launches_end_to_end(monkeypatch, tmp_path) -> None:
    from mantis_agent.subagent import discover_agent_types
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    (tmp_path / "agents").mkdir(parents=True)
    (tmp_path / "agents" / "greeter.md").write_text(
        "---\ndescription: Greets.\ntools: read_file\n---\nYou greet.")
    prov = MockProvider(default_text="hello from greeter")
    t = make_task_tool(model="mock", provider=prov, tools=_explore(),
                       agent_types=discover_agent_types(tmp_path))
    out = anyio.run(lambda: t.fn(prompt="greet", subagent_type="greeter"))
    assert out == "hello from greeter"
