"""Regression coverage for the three bugs the CLI shake-out uncovered:

1. CLI ``run`` crashed with ``AttributeError`` because it referenced
   ``msg.content_blocks`` — that attribute doesn't exist on
   ``SDKAssistantMessage``; the right path is ``msg.message.content``.
2. ``compat_query._build_agent`` ignored ``mcp_servers``, so in-process
   MCP server tools never reached the agent's wire-format tool list.
   The model couldn't see them, so it just answered in prose.
3. The Ollama provider passed Anthropic-shape tools (``{name,
   description, input_schema}``) on the wire. Ollama silently accepted
   them but produced ``tool_calls`` with ``name=""``, which the SDK
   then dropped — leaving an empty AssistantMessage.

These tests pin the fixes in place and document the shapes so a future
refactor doesn't regress.
"""

from __future__ import annotations

import inspect
from typing import Any


from mantis_agent import (
    MantisAgentOptions,
    create_sdk_mcp_server,
    tool,
)
from mantis_agent.compat_query import _build_agent
from mantis_agent.providers.ollama import _to_openai_tool


# ---------------------------------------------------------------------------
# Tool-shape transformer (bug 3)
# ---------------------------------------------------------------------------


def test_to_openai_tool_translates_anthropic_shape() -> None:
    """Anthropic shape ``{name, description, input_schema}`` must become
    OpenAI shape ``{type: function, function: {name, description, parameters}}``.

    If we leave the Anthropic shape on the wire, Ollama silently emits
    tool_calls with ``name=""`` and the agent loop drops them.
    """

    out = _to_openai_tool({
        "name": "add",
        "description": "Add two numbers",
        "input_schema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    })
    assert out["type"] == "function"
    fn = out["function"]
    assert fn["name"] == "add"
    assert fn["description"] == "Add two numbers"
    # The schema MUST be under `parameters`, not `input_schema` — that's the
    # whole point of this transformer.
    assert "parameters" in fn
    assert "input_schema" not in fn
    assert fn["parameters"]["required"] == ["a", "b"]


def test_to_openai_tool_passes_through_openai_shape() -> None:
    """If something is already OpenAI-shape, don't re-wrap it."""

    already = {
        "type": "function",
        "function": {"name": "x", "description": "y", "parameters": {}},
    }
    assert _to_openai_tool(already) is already


def test_to_openai_tool_handles_missing_fields() -> None:
    """Be lenient about partial shapes — copy what's present."""

    out = _to_openai_tool({"name": "bare"})
    assert out == {"type": "function", "function": {"name": "bare"}}


# ---------------------------------------------------------------------------
# MCP server → agent registry bridge (bug 2)
# ---------------------------------------------------------------------------


@tool("add", "Add two numbers", {"a": float, "b": float})
async def _add(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}


@tool("multiply", "Multiply two numbers", {"a": float, "b": float})
async def _multiply(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(args["a"] * args["b"])}]}


def test_mcp_server_tools_reach_the_agent_registry() -> None:
    """Passing ``mcp_servers={"calc": create_sdk_mcp_server(...)}`` MUST
    put the server's tools into the agent's registry — renamed to the
    Claude-SDK-style namespace ``mcp__{server}__{tool}``."""

    calc = create_sdk_mcp_server(name="calc", version="1.0", tools=[_add, _multiply])
    opts = MantisAgentOptions(
        model="qwen2.5:1.5b",
        backend="http://localhost:11434",
        mcp_servers={"calc": calc},
    )

    agent = _build_agent(opts.to_query_options())
    names = {t.name for t in agent.tools}
    assert "mcp__calc__add" in names
    assert "mcp__calc__multiply" in names


def test_mcp_server_tools_preserve_input_schema_and_fn() -> None:
    """The bridged tool must keep the original schema and function — the
    agent loop dispatches to ``fn`` when the model calls the tool."""

    calc = create_sdk_mcp_server(name="calc", version="1.0", tools=[_add])
    opts = MantisAgentOptions(
        model="qwen2.5:1.5b",
        backend="http://localhost:11434",
        mcp_servers={"calc": calc},
    )
    agent = _build_agent(opts.to_query_options())

    bridged = agent.tools.get("mcp__calc__add")
    assert bridged is not None
    assert bridged.description == "Add two numbers"
    assert bridged.input_schema["properties"]["a"]["type"] == "number"
    # fn must be a callable that the agent loop can await.
    assert callable(bridged.fn)


def test_allowed_tools_filters_registry() -> None:
    """``allowed_tools`` on the options must restrict which tools the
    model actually sees — same semantics as the Claude SDK."""

    calc = create_sdk_mcp_server(name="calc", version="1.0", tools=[_add, _multiply])
    opts = MantisAgentOptions(
        model="qwen2.5:1.5b",
        backend="http://localhost:11434",
        mcp_servers={"calc": calc},
        allowed_tools=["mcp__calc__add"],  # multiply excluded
    )

    agent = _build_agent(opts.to_query_options())
    names = {t.name for t in agent.tools}
    assert names == {"mcp__calc__add"}


def test_mcp_servers_dict_form_is_supported() -> None:
    """Whether the user passes ``mcp_servers`` as a dict or a list of
    ``(name, config)`` tuples, the bridge must work."""

    calc = create_sdk_mcp_server(name="calc", version="1.0", tools=[_add])

    # Dict form (what MantisAgentOptions stores)
    opts_dict_form: dict[str, Any] = {
        "model": "qwen2.5:1.5b",
        "backend": "http://localhost:11434",
        "mcp_servers": {"calc": calc},
    }
    agent1 = _build_agent(opts_dict_form)
    assert "mcp__calc__add" in {t.name for t in agent1.tools}

    # List-of-tuples form (what to_query_options() normalizes to)
    opts_tuple_form: dict[str, Any] = {
        "model": "qwen2.5:1.5b",
        "backend": "http://localhost:11434",
        "mcp_servers": [("calc", calc)],
    }
    agent2 = _build_agent(opts_tuple_form)
    assert "mcp__calc__add" in {t.name for t in agent2.tools}


# ---------------------------------------------------------------------------
# CLI `_cmd_run_async` no longer reaches for the wrong attribute (bug 1)
# ---------------------------------------------------------------------------


def test_cli_run_does_not_reference_removed_content_blocks_attr() -> None:
    """``SDKAssistantMessage`` exposes blocks via ``msg.message.content``,
    not ``msg.content_blocks``. Static grep on the CLI source catches
    regressions cheaper than booting a real run."""

    from mantis_agent import cli

    src = inspect.getsource(cli._cmd_run_async)
    assert "msg.message.content" in src, (
        "_cmd_run_async should iterate `msg.message.content` (the actual "
        "shape of SDKAssistantMessage), not the non-existent "
        "`msg.content_blocks`."
    )
    # The attribute access we don't want: `msg.content_blocks` (with the
    # dot). Mentioning `content_blocks` in a comment is fine.
    assert "msg.content_blocks" not in src, (
        "Found a stale reference to `msg.content_blocks` in _cmd_run_async — "
        "that attribute doesn't exist on SDKAssistantMessage, the CLI "
        "would crash with AttributeError on every run."
    )
