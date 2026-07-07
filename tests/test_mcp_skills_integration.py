"""MCP + skills terminal integration: config discovery (.mcp.json), the
MCPManager lifecycle, mcp__server__tool namespacing into the agent registry,
/mcp status, /skills listing, and direct /skill-name invocation."""

from __future__ import annotations

import json

import anyio
import pytest

from mantis_agent.mcp.manager import (
    MCPManager,
    load_mcp_server_configs,
    parse_server_entry,
)
from mantis_agent.mcp.types import (
    HttpServerConfig,
    SseServerConfig,
    StdioServerConfig,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)  # keep .mcp.json discovery off the real repo
    yield


# -- config parsing ------------------------------------------------------------


def test_parse_stdio_entry_infers_type() -> None:
    cfg = parse_server_entry({"command": "npx", "args": ["-y", "server-github"],
                              "env": {"TOKEN": "x"}})
    assert isinstance(cfg, StdioServerConfig)
    assert cfg.command == "npx" and cfg.args == ["-y", "server-github"]
    assert cfg.env == {"TOKEN": "x"}


def test_parse_url_entries() -> None:
    assert isinstance(parse_server_entry({"url": "https://x/api"}), HttpServerConfig)
    assert isinstance(parse_server_entry({"type": "sse", "url": "https://x/sse"}),
                      SseServerConfig)
    http = parse_server_entry({"type": "http", "url": "https://x", "headers": {"A": "b"}})
    assert isinstance(http, HttpServerConfig) and http.headers == {"A": "b"}


def test_parse_garbage_entries_are_none() -> None:
    assert parse_server_entry({}) is None
    assert parse_server_entry({"type": "http"}) is None  # no url
    assert parse_server_entry("not a dict") is None
    assert parse_server_entry(None) is None


def test_load_configs_merges_user_and_project(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp"},
        "shared": {"command": "user-version"},
    }}))
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "db": {"url": "https://db.example/api"},
        "shared": {"command": "project-version"},  # project wins
    }}))
    cfgs = load_mcp_server_configs(tmp_path)
    assert set(cfgs) == {"github", "db", "shared"}
    assert cfgs["shared"].command == "project-version"


def test_load_configs_reads_settings_json(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"mcpServers": {
        "from-settings": {"command": "x"}}}))
    assert "from-settings" in load_mcp_server_configs(tmp_path)


def test_load_configs_skips_malformed_file(tmp_path) -> None:
    (tmp_path / ".mcp.json").write_text("{not json")
    assert load_mcp_server_configs(tmp_path) == {}


# -- manager lifecycle (real in-process MCP server) ------------------------------


def _echo_server_config():
    from mantis_agent.mcp import create_sdk_server
    from mantis_agent.tools import tool

    @tool(name="echo")
    async def echo(text: str) -> str:
        """Echo text back.

        Args:
            text: What to echo.
        """
        return f"echo:{text}"

    return create_sdk_server("echoes", tools=[echo])


def test_manager_connects_and_namespaces_tools() -> None:
    async def go():
        mgr = MCPManager({"echoes": _echo_server_config()})
        tools = await mgr.connect_all()
        assert [t.name for t in tools] == ["mcp__echoes__echo"]
        out = await tools[0].fn(text="hi")
        assert out == "echo:hi"
        rows = mgr.status_rows()
        assert rows[0]["state"] == "connected" and "1 tools" in rows[0]["detail"]
        assert "echoes (1 tools)" in mgr.summary()
        await mgr.aclose()
    anyio.run(go)


def test_manager_isolates_a_failing_server() -> None:
    async def go():
        bad = StdioServerConfig(command="definitely-not-a-real-binary-xyz")
        mgr = MCPManager({"bad": bad, "good": _echo_server_config()})
        tools = await mgr.connect_all(timeout_s=5.0)
        # the good server's tools still arrive; the bad one is reported
        assert [t.name for t in tools] == ["mcp__good__echo"]
        states = {r["name"]: r["state"] for r in mgr.status_rows()}
        assert states == {"bad": "failed", "good": "connected"}
        assert "bad ✗" in mgr.summary()
        await mgr.aclose()
    anyio.run(go)


# -- TUI integration ---------------------------------------------------------------


def _tui():
    from mantis_agent.tui import MantisTUI
    return MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                     system=None, max_tokens=1, temperature=None, max_turns=1)


class _Rec:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.width = 80
    def print(self, *a, **k) -> None:
        self.lines.append(" ".join(str(x) for x in a))
    def text(self) -> str:
        return "\n".join(self.lines)


def test_connect_mcp_folds_tools_into_live_agent_and_rebuilds(tmp_path, monkeypatch) -> None:
    async def go():
        t = _tui()
        # patch discovery to return the in-process server (no file/subprocess)
        import mantis_agent.mcp.manager as mm
        monkeypatch.setattr(mm, "load_mcp_server_configs",
                            lambda cwd=None: {"echoes": _echo_server_config()})
        # also patch the symbol the TUI imports
        t.agent = t._build_agent()
        before = {x.name for x in t.agent.tools}
        assert not any(n.startswith("mcp__") for n in before)
        summary = await t._connect_mcp()
        assert "echoes (1 tools)" in summary
        live = {x.name for x in t.agent.tools}
        assert "mcp__echoes__echo" in live          # live agent got it
        rebuilt = t._build_agent()
        assert "mcp__echoes__echo" in {x.name for x in rebuilt.tools}  # rebuilds keep it
        # second call is idempotent (no reconnect)
        assert await t._connect_mcp() == summary
        await t._close_mcp()
    anyio.run(go)


def test_show_mcp_renders_status_and_recipe(monkeypatch) -> None:
    async def go():
        t = _tui()
        t.console = _Rec()
        t._show_mcp()  # nothing configured
        out = t.console.text()
        assert "none configured" in out and ".mcp.json" in out
        import mantis_agent.mcp.manager as mm
        monkeypatch.setattr(mm, "load_mcp_server_configs",
                            lambda cwd=None: {"echoes": _echo_server_config()})
        await t._connect_mcp()
        t.console = _Rec()
        t._show_mcp()
        out = t.console.text()
        assert "echoes" in out and "connected" in out and "mcp__echoes__echo" in out
        await t._close_mcp()
    anyio.run(go)


# -- skills: /skills + direct /<name> invocation --------------------------------------


def _write_skill(root, name, body, desc="A test skill.") -> None:
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}")


def test_skill_slash_invocation(tmp_path) -> None:
    from mantis_agent.tui import expand_skill_command, expand_slash_prompt
    _write_skill(tmp_path / "home", "deploy-checklist", "Run through the deploy list.")
    assert expand_skill_command("/deploy-checklist") == "Run through the deploy list."
    got = expand_skill_command("/deploy-checklist staging")
    assert got == "Run through the deploy list.\n\nTask: staging"
    # full chain: expand_slash_prompt routes to the skill
    assert expand_slash_prompt("/deploy-checklist go") is not None


def test_skill_cannot_shadow_builtin_or_command(tmp_path) -> None:
    from mantis_agent.tui import all_slash_commands, expand_skill_command
    _write_skill(tmp_path / "home", "model", "Should not shadow /model.")
    assert expand_skill_command("/model") is None
    # custom command beats a same-named skill
    _write_skill(tmp_path / "home", "ship", "skill body")
    cmd_dir = tmp_path / "home" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / "ship.md").write_text("---\ndescription: cmd\n---\ncommand body")
    from mantis_agent.tui import expand_slash_prompt
    assert expand_slash_prompt("/ship") == "command body"
    merged = all_slash_commands()
    assert merged["/ship"].endswith("(custom)")


def test_all_slash_commands_includes_skills(tmp_path) -> None:
    from mantis_agent.tui import all_slash_commands
    _write_skill(tmp_path / "home", "audit-deps", "x", desc="Audit dependencies")
    merged = all_slash_commands()
    assert merged["/audit-deps"] == "Audit dependencies (skill)"


def test_show_skills_lists_discovered(tmp_path) -> None:
    t = _tui()
    t.console = _Rec()
    _write_skill(tmp_path / "home", "audit-deps", "x", desc="Audit dependencies")
    t._show_skills()
    out = t.console.text()
    assert "audit-deps" in out and "Audit dependencies" in out
    assert "SKILL.md" in out  # the add-your-own recipe


# -- real stdio transport (regression: anyio.subprocess AttributeError) ----------

_TINY_SERVER = '''
import json, sys
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); mid, meth = m.get("id"), m.get("method")
    if meth == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{"tools":{}},"serverInfo":{"name":"tiny","version":"1"}}})
    elif meth == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":"add",
              "description":"Add.","inputSchema":{"type":"object","properties":
              {"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}}]}})
    elif meth == "tools/call":
        a = m["params"].get("arguments") or {}
        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text",
              "text":str(a.get("a",0)+a.get("b",0))}],"isError":False}})
    elif mid is not None:
        send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"nope"}})
'''


def test_stdio_transport_end_to_end(tmp_path) -> None:
    # A REAL subprocess speaking MCP over stdio — covers the spawn path that
    # shipped broken (anyio.subprocess.PIPE doesn't exist) because nothing
    # exercised it. If this hangs, the per-request timeout trips it to failed.
    import sys as _sys
    server = tmp_path / "tiny_server.py"
    server.write_text(_TINY_SERVER)
    cfg = StdioServerConfig(command=_sys.executable, args=[str(server)])

    async def go():
        mgr = MCPManager({"tiny": cfg})
        tools = await mgr.connect_all(timeout_s=15.0)
        assert mgr.errors == {}
        assert [t.name for t in tools] == ["mcp__tiny__add"]
        assert await tools[0].fn(a=2, b=40) == "42"
        await mgr.aclose()
    anyio.run(go)


def test_request_timeout_marks_unresponsive_server_failed(tmp_path) -> None:
    # A server that connects but never answers must fail fast (per-request
    # timeout), not hang the terminal forever.
    import sys as _sys
    server = tmp_path / "mute_server.py"
    server.write_text("import time\nwhile True: time.sleep(1)\n")
    cfg = StdioServerConfig(command=_sys.executable, args=[str(server)])

    async def go():
        mgr = MCPManager({"mute": cfg})
        tools = await mgr.connect_all(timeout_s=1.5)
        assert tools == []
        assert "mute" in mgr.errors
        await mgr.aclose()
    anyio.run(go)
