"""Deferred tool schemas — the mechanism that keeps a dozen MCP servers from
eating the context window on every single turn."""

from __future__ import annotations

import json

import pytest

from mantis_agent.builtin_tools.tool_search import (
    deferred_prompt_section,
    make_tool_search,
    search_deferred,
)
from mantis_agent.tools import Tool, ToolRegistry

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def _t(name: str, desc: str = "") -> Tool:
    async def fn(**kw):
        return f"ran {name}"

    return Tool(name=name, description=desc or f"{name} does a thing",
                input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
                fn=fn)


@pytest.fixture()
def registry() -> ToolRegistry:
    r = ToolRegistry()
    r.add(
        _t("read_file", "Read a file from disk"),
        _t("bash", "Run a shell command"),
        _t("mcp__github__create_issue", "Open an issue on a GitHub repository"),
        _t("mcp__github__list_pulls", "List pull requests on a GitHub repository"),
        _t("mcp__slack__send_message", "Post a message to a Slack channel"),
        _t("mcp__linear__search", "Search Linear issues"),
    )
    r.defer("mcp__github__create_issue", "mcp__github__list_pulls",
            "mcp__slack__send_message", "mcp__linear__search")
    return r


# -- the registry contract --------------------------------------------------


def test_deferred_tools_stay_off_the_wire(registry) -> None:
    """The whole point: their schemas are not in the request."""
    names = [t["name"] for t in registry.to_wire()]
    assert names == ["read_file", "bash"]
    assert len(registry) == 6                 # …but the registry still knows them
    assert len(registry.deferred_tools()) == 4


def test_surfacing_puts_a_tool_back_on_the_wire(registry) -> None:
    registry.surface("mcp__slack__send_message")
    names = [t["name"] for t in registry.to_wire()]
    assert "mcp__slack__send_message" in names
    assert "mcp__github__create_issue" not in names   # only what was asked for


def test_a_deferred_tool_is_still_callable_if_the_model_guesses(registry) -> None:
    """Deferring hides the schema, it does not disable the tool — a blind call
    with correct arguments should work rather than burning a turn."""
    assert registry.resolve("mcp__linear__search") is not None


def test_defer_reports_what_changed(registry) -> None:
    assert registry.defer("mcp__linear__search") == 0      # already deferred
    assert registry.defer("read_file") == 1
    assert registry.defer("nope") == 0


# -- search ------------------------------------------------------------------


def test_select_fetches_exact_names_in_order(registry) -> None:
    found = search_deferred(registry, "select:mcp__slack__send_message,mcp__linear__search")
    assert [t.name for t in found] == ["mcp__slack__send_message", "mcp__linear__search"]


def test_select_tolerates_drifted_spelling(registry) -> None:
    found = search_deferred(registry, "select:MCP-SLACK-SEND-MESSAGE")
    assert [t.name for t in found] == ["mcp__slack__send_message"]


def test_keyword_search_ranks_name_matches_first(registry) -> None:
    found = search_deferred(registry, "github")
    assert [t.name for t in found] == [
        "mcp__github__create_issue", "mcp__github__list_pulls"]


def test_search_matches_descriptions_too(registry) -> None:
    found = search_deferred(registry, "channel")
    assert [t.name for t in found] == ["mcp__slack__send_message"]


def test_required_term_narrows_before_ranking(registry) -> None:
    found = search_deferred(registry, "+github issue")
    assert all(t.name.startswith("mcp__github__") for t in found)
    assert found[0].name == "mcp__github__create_issue"


def test_max_results_is_honoured(registry) -> None:
    assert len(search_deferred(registry, "mcp", max_results=2)) == 2


def test_search_never_returns_live_tools(registry) -> None:
    assert search_deferred(registry, "read_file") == []
    assert search_deferred(registry, "select:read_file") == []


# -- the tool itself ---------------------------------------------------------


async def test_tool_search_loads_schemas_and_makes_them_callable(registry) -> None:
    ts = make_tool_search(registry)
    out = await ts.fn(query="select:mcp__github__create_issue")

    assert "Loaded 1 tool schema" in out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["name"] == "mcp__github__create_issue"
    assert payload["input_schema"]["type"] == "object"    # enough to call it
    assert "mcp__github__create_issue" in [t["name"] for t in registry.to_wire()]


async def test_tool_search_explains_a_miss_and_lists_what_exists(registry) -> None:
    ts = make_tool_search(registry)
    out = await ts.fn(query="kubernetes")
    assert "No deferred tool matches" in out
    assert "mcp__github__create_issue" in out       # so the next call can succeed


async def test_tool_search_says_so_when_nothing_is_deferred() -> None:
    r = ToolRegistry()
    r.add(_t("read_file"))
    out = await make_tool_search(r).fn(query="anything")
    assert "No deferred tools" in out


async def test_tool_search_clamps_absurd_max_results(registry) -> None:
    out = await make_tool_search(registry).fn(query="mcp", max_results=9999)
    assert out.count('"input_schema"') <= 20


async def test_tool_search_survives_a_junk_max_results(registry) -> None:
    """Small models pass strings where ints are expected all the time."""
    out = await make_tool_search(registry).fn(query="mcp", max_results="not a number")
    assert "Loaded" in out


# -- the prompt side ---------------------------------------------------------


def test_prompt_section_names_every_deferred_tool(registry) -> None:
    section = deferred_prompt_section(registry)
    assert "tool_search" in section
    for name in ("mcp__github__create_issue", "mcp__slack__send_message"):
        assert name in section
    # the schemas themselves must NOT be in there — that's the point
    assert "input_schema" not in section


def test_prompt_section_is_empty_when_nothing_is_deferred() -> None:
    r = ToolRegistry()
    r.add(_t("read_file"))
    assert deferred_prompt_section(r) == ""


def test_prompt_section_truncates_a_huge_catalogue() -> None:
    r = ToolRegistry()
    for i in range(80):
        r.add(_t(f"mcp__srv__tool_{i}"))
    r.defer(*[t.name for t in r])
    section = deferred_prompt_section(r, limit=10)
    assert "…and 70 more." in section


# -- the saving, measured ----------------------------------------------------


def test_deferring_actually_shrinks_the_request() -> None:
    """A regression guard on the reason this exists."""
    r = ToolRegistry()
    for i in range(30):
        r.add(_t(f"mcp__server__operation_{i}",
                 "A reasonably wordy description of what this operation does, "
                 "the kind every MCP server ships with. " * 3))
    full = len(json.dumps(r.to_wire()))
    r.defer(*[t.name for t in r])
    deferred = len(json.dumps(r.to_wire())) + len(deferred_prompt_section(r))
    assert deferred < full / 2, f"expected a big saving, got {full} → {deferred}"
