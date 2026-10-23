"""A claude-agent-sdk project names Claude's built-in tools — ``"Read"``,
``"Bash"``, ``"WebSearch"`` — in ``allowed_tools`` or ``tools``. Under mantis
those names must become mantis's own implementations, or the agent runs with
no tools at all (it used to: the names were recorded and never registered).
"""

from __future__ import annotations

from mantis_agent import MantisAgentOptions
from mantis_agent.compat_query import _build_agent, _normalize_options


def _names(opts: MantisAgentOptions) -> list[str]:
    agent = _build_agent(_normalize_options(opts))
    return sorted(t.name for t in agent.tools)


def test_allowed_tools_alone_gives_the_agent_those_tools():
    got = _names(MantisAgentOptions(model="mock", allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"]))
    assert got == ["bash", "edit_file", "glob", "grep", "read_file", "write_file"]


def test_web_tools_and_claude_spellings():
    got = _names(MantisAgentOptions(model="mock", allowed_tools=["WebSearch", "WebFetch", "MultiEdit", "LS"]))
    assert got == ["ls", "multi_edit", "web_fetch", "web_search"]


def test_tools_list_and_the_claude_code_preset():
    assert _names(MantisAgentOptions(model="mock", tools=["Bash", "Read"])) == ["bash", "read_file"]
    preset = _names(MantisAgentOptions(model="mock", tools={"type": "preset", "preset": "claude_code"}))
    for name in ("bash", "read_file", "write_file", "edit_file", "glob", "grep", "web_search", "web_fetch"):
        assert name in preset, name


def test_allowed_tools_still_narrows_an_explicit_tools_list():
    got = _names(MantisAgentOptions(model="mock", tools=["Bash", "Read", "Write"], allowed_tools=["Read"]))
    assert got == ["read_file"]


def test_disallowed_tools_takes_tools_away():
    got = _names(MantisAgentOptions(model="mock", tools={"type": "preset", "preset": "claude_code"},
                                    disallowed_tools=["Bash", "Write"]))
    assert "bash" not in got and "write_file" not in got and "read_file" in got


def test_nothing_named_means_no_file_or_shell_tools():
    # a plain chat agent does not silently gain bash
    assert _names(MantisAgentOptions(model="mock")) == []


def test_names_mantis_has_no_tool_for_are_skipped():
    assert _names(MantisAgentOptions(model="mock", allowed_tools=["TodoWrite", "Task", "Read"])) == ["read_file"]


def _calc_server():
    from mantis_agent import create_sdk_mcp_server, tool

    @tool("add", "Add two numbers", {"a": float, "b": float})
    async def add(args):
        return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}

    @tool("sub", "Subtract", {"a": float, "b": float})
    async def sub(args):
        return {"content": [{"type": "text", "text": str(args["a"] - args["b"])}]}

    return create_sdk_mcp_server(name="calc", version="1.0", tools=[add, sub])


def test_listing_built_ins_keeps_a_configured_mcp_server() -> None:
    # Claude's allowed_tools pre-approves; it never hides the server the caller wired up
    got = _names(MantisAgentOptions(model="mock", allowed_tools=["Bash"], mcp_servers={"calc": _calc_server()}))
    assert got == ["bash", "mcp__calc__add", "mcp__calc__sub"]


def test_disallowed_tools_reach_mcp_tools_and_whole_servers() -> None:
    calc = _calc_server()
    one = _names(MantisAgentOptions(model="mock", mcp_servers={"calc": calc}, disallowed_tools=["mcp__calc__sub"]))
    assert one == ["mcp__calc__add"]
    none = _names(MantisAgentOptions(model="mock", mcp_servers={"calc": calc}, disallowed_tools=["mcp__calc"]))
    assert none == []
