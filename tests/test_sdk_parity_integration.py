"""SDK-library integration: the Claude-SDK-shaped query()/options path must
carry the session's new machinery — options.agents (was silently ignored) and
external MCP servers in options.mcp_servers (were silently dropped)."""

from __future__ import annotations

import sys

import anyio
import pytest

from mantis_agent import AgentDefinition, MantisAgentOptions
from mantis_agent.compat_query import (
    _build_agent,
    _connect_external_mcp,
    _normalize_options,
    _register_agent_definitions,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    yield


def _opts(**kw) -> dict:
    o = MantisAgentOptions(model="qwen2.5-7b-instruct",
                           backend="http://localhost:11434", **kw)
    return _normalize_options(o)


# -- options.agents ---------------------------------------------------------------


def test_agent_definitions_become_tools() -> None:
    from mantis_agent.tools import tool

    @tool(name="lint")
    async def lint(path: str) -> str:
        """Lint a path.

        Args:
            path: what to lint.
        """
        return "clean"

    opts = _opts(agents={
        "reviewer": AgentDefinition(description="Reviews diffs for bugs.",
                                    prompt="You are a reviewer.",
                                    tools=["lint"]),
        "planner": AgentDefinition(description="Plans work.",
                                   prompt="You are a planner."),
    })
    opts["tools"] = [lint]
    agent = _build_agent(opts)
    _register_agent_definitions(agent, opts)
    names = {t.name for t in agent.tools}
    assert {"reviewer", "planner"} <= names
    rev = agent.tools.get("reviewer")
    assert rev.description == "Reviews diffs for bugs."
    # narrowing: reviewer's child kit was restricted to lint (spec captured it)
    assert [t.name for t in rev._spec.tools] == ["lint"]
    # planner got the full kit minus the agents themselves (no recursion)
    plan = agent.tools.get("planner")
    assert "reviewer" not in {t.name for t in plan._spec.tools}


def test_no_agents_is_noop() -> None:
    opts = _opts()
    agent = _build_agent(opts)
    before = {t.name for t in agent.tools}
    _register_agent_definitions(agent, opts)
    assert {t.name for t in agent.tools} == before


# -- options.mcp_servers external transports ------------------------------------


_TINY = '''
import json, sys
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); mid, meth = m.get("id"), m.get("method")
    if meth == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{"tools":{}},"serverInfo":{"name":"t","version":"1"}}})
    elif meth == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":"ping",
              "description":"Ping.","inputSchema":{"type":"object","properties":{}}}]}})
    elif meth == "tools/call":
        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text",
              "text":"pong"}],"isError":False}})
    elif mid is not None:
        send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"nope"}})
'''


def test_external_stdio_mcp_connects_through_options(tmp_path) -> None:
    server = tmp_path / "srv.py"
    server.write_text(_TINY)
    # Claude Code config-dict shape, exactly as users pass it
    opts = _opts(mcp_servers={"tiny": {"command": sys.executable,
                                       "args": [str(server)]}})

    async def go():
        agent = _build_agent(opts)
        mgr = await _connect_external_mcp(agent, opts)
        assert mgr is not None
        t = agent.tools.get("mcp__tiny__ping")
        assert t is not None
        assert await t.fn() == "pong"
        await mgr.aclose()
    anyio.run(go)


def test_sdk_inprocess_server_not_double_connected() -> None:
    from mantis_agent import create_sdk_mcp_server
    from mantis_agent.tools import tool

    @tool(name="echo")
    async def echo(text: str) -> str:
        """Echo.

        Args:
            text: t.
        """
        return text

    srv = create_sdk_mcp_server("calc", tools=[echo])
    opts = _opts(mcp_servers={"calc": srv})

    async def go():
        agent = _build_agent(opts)                       # bridges in-process
        assert agent.tools.get("mcp__calc__echo") is not None
        mgr = await _connect_external_mcp(agent, opts)   # must skip it
        assert mgr is None
    anyio.run(go)
