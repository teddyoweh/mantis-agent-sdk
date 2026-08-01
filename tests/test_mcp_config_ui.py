"""The config layer behind the ``/mcp`` inspector: what the UI can paste,
what it shows you about a server, and what it must never show in plaintext."""

from __future__ import annotations

import json

import pytest

from mantis_agent.mcp.manager import (
    load_mcp_server_configs,
    mask_secret,
    mcp_config_layers,
    mcp_entry_transport,
    mcp_raw_entries,
    mcp_server_origin,
    parse_mcp_paste,
    redact_mcp_entry,
    remove_user_mcp_server,
    save_user_mcp_server,
    save_user_mcp_servers,
    user_mcp_config_path,
)


@pytest.fixture()
def mcp_home(tmp_path, monkeypatch):
    """An isolated mantis home + project dir, both empty."""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.chdir(proj)
    return home, proj


def _write(path, servers):
    path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")


# -- paste parsing ----------------------------------------------------------


def test_paste_accepts_the_readme_mcpservers_blob() -> None:
    servers, err = parse_mcp_paste(
        '{"mcpServers": {"github": {"command": "npx", "args": ["-y", "gh-mcp"]}}}')
    assert err is None
    assert servers == {"github": {"command": "npx", "args": ["-y", "gh-mcp"]}}


def test_paste_accepts_bare_name_to_entry_map() -> None:
    servers, err = parse_mcp_paste('{"a": {"url": "https://a.co/mcp"}}')
    assert err is None
    assert list(servers) == ["a"]


def test_paste_accepts_a_lone_entry_and_leaves_naming_to_the_caller() -> None:
    servers, err = parse_mcp_paste('{"command": "npx", "args": ["-y", "x"]}')
    assert err is None
    assert servers == {"": {"command": "npx", "args": ["-y", "x"]}}


def test_paste_uses_an_inline_name_field_when_the_entry_carries_one() -> None:
    servers, err = parse_mcp_paste('{"name": "gh", "type": "sse", "url": "https://x/sse"}')
    assert err is None
    assert servers == {"gh": {"type": "sse", "url": "https://x/sse"}}


def test_paste_tolerates_comments_and_trailing_commas() -> None:
    servers, err = parse_mcp_paste(
        '{\n  // from the docs\n  "mcpServers": {\n'
        '    "a": {"url": "https://a.co/mcp", /* inline */},\n  },\n}')
    assert err is None
    assert servers == {"a": {"url": "https://a.co/mcp"}}


def test_paste_never_mistakes_a_url_for_a_comment() -> None:
    servers, err = parse_mcp_paste('{"a": {"url": "https://a.co/mcp"},}')
    assert err is None
    assert servers["a"]["url"] == "https://a.co/mcp"


def test_paste_still_takes_a_shell_command_or_a_url() -> None:
    assert parse_mcp_paste("npx -y pkg")[0] == {"": {"command": "npx", "args": ["-y", "pkg"]}}
    assert parse_mcp_paste("https://x.co/sse")[0] == {"": {"type": "sse", "url": "https://x.co/sse"}}
    assert parse_mcp_paste("https://x.co/mcp")[0] == {"": {"type": "http", "url": "https://x.co/mcp"}}


@pytest.mark.parametrize(("text", "needle"), [
    ("", "nothing"),
    ("{oops", "invalid JSON"),
    ('{"a": {"nope": 1}}', "command"),
    ('{"mcpServers": {}}', "no MCP servers"),
    ("[1, 2]", "expected a JSON object"),
])
def test_paste_errors_are_sentences_not_exceptions(text: str, needle: str) -> None:
    servers, err = parse_mcp_paste(text)
    assert servers == {}
    assert err and needle in err


# -- redaction --------------------------------------------------------------


def test_redaction_masks_every_credential_but_keeps_the_keys() -> None:
    entry = {
        "command": "npx",
        "args": ["-y", "srv", "--api-key", "sk-live-1", "--token=zzz"],
        "env": {"EXA_API_KEY": "sk-live-2", "DEBUG": "1"},
        "url": "https://m.co/mcp?apiKey=sk-live-3&region=us",
        "headers": {"Authorization": "Bearer t"},
    }
    red = redact_mcp_entry(entry)
    flat = json.dumps(red)
    for secret in ("sk-live-1", "sk-live-2", "sk-live-3", "zzz", "Bearer t"):
        assert secret not in flat
    assert "EXA_API_KEY" in flat and "DEBUG" in flat      # names survive
    assert "region=us" in red["url"]                      # harmless params survive
    assert red["command"] == "npx"
    assert entry["env"]["EXA_API_KEY"] == "sk-live-2"     # original untouched


def test_mask_secret_leaves_an_unset_value_visibly_unset() -> None:
    assert mask_secret("") == ""
    assert mask_secret(None) == ""
    assert set(mask_secret("anything")) == {"•"}


def test_transport_label_covers_every_shape() -> None:
    assert mcp_entry_transport({"command": "x"}) == "stdio"
    assert mcp_entry_transport({"url": "https://x"}) == "http"
    assert mcp_entry_transport({"type": "sse", "url": "https://x"}) == "sse"
    assert mcp_entry_transport({"junk": 1}) == "?"


# -- what the inspector reads ----------------------------------------------


def test_raw_entries_report_the_winning_layer_and_its_file(mcp_home) -> None:
    home, proj = mcp_home
    _write(home / "mcp.json", {"shared": {"command": "user-cmd"},
                               "mine": {"command": "only-user"}})
    _write(proj / ".mcp.json", {"shared": {"command": "project-cmd"},
                                "theirs": {"url": "https://t.co/mcp"}})

    entries = mcp_raw_entries()
    assert entries["shared"]["entry"] == {"command": "project-cmd"}   # project wins
    assert entries["shared"]["origin"] == "project"
    assert entries["shared"]["path"].endswith(".mcp.json")
    assert entries["mine"]["origin"] == "user"
    assert entries["mine"]["path"] == str(home / "mcp.json")
    assert set(entries) == {"shared", "mine", "theirs"}
    # …and it agrees with both the typed loader and the origin helper.
    assert set(load_mcp_server_configs()) == set(entries)
    assert mcp_server_origin("mine") == "user"
    assert mcp_server_origin("theirs") == "project"


def test_config_layers_are_listed_lowest_priority_first(mcp_home) -> None:
    assert [lay["origin"] for lay in mcp_config_layers()] == ["settings", "user", "project"]


def test_bulk_save_then_remove_round_trips_through_the_user_file(mcp_home) -> None:
    home, _ = mcp_home
    save_user_mcp_servers({"a": {"command": "a"}, "b": {"url": "https://b.co/mcp"}})
    save_user_mcp_server("c", {"command": "c"})
    assert set(mcp_raw_entries()) == {"a", "b", "c"}

    assert remove_user_mcp_server("b") is True
    assert remove_user_mcp_server("b") is False          # already gone
    assert set(mcp_raw_entries()) == {"a", "c"}
    # The file stays valid JSON with nothing else clobbered.
    data = json.loads(user_mcp_config_path().read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"a", "c"}


def test_a_project_entry_cannot_be_removed_by_the_user_level_writer(mcp_home) -> None:
    _, proj = mcp_home
    _write(proj / ".mcp.json", {"theirs": {"command": "x"}})
    assert remove_user_mcp_server("theirs") is False
    assert set(mcp_raw_entries()) == {"theirs"}


# -- connect isolation ------------------------------------------------------

_TINY_SERVER = '''
import json, sys
def send(m): sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line); mid, meth = m.get("id"), m.get("method")
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05",
              "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "ping",
              "description": "Ping.", "inputSchema": {"type": "object"}}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
'''


def test_one_broken_server_never_takes_down_the_ones_after_it(tmp_path) -> None:
    """A server that dies around the handshake used to cancel the task running
    the connect loop, silently aborting every server queued behind it — no
    tools, no errors, no status rows. Each failure has to stay local."""
    import sys

    import anyio

    from mantis_agent.mcp.manager import MCPManager, parse_server_entry

    server = tmp_path / "srv.py"
    server.write_text(_TINY_SERVER, encoding="utf-8")
    good = {"command": sys.executable, "args": [str(server)]}
    configs = {
        # exits instantly: stdio transport dies mid-handshake
        "dead": parse_server_entry({"command": "echo", "args": ["hi"]}),
        "live1": parse_server_entry(good),
        # nothing listening: the remote transport fails its connect
        "refused": parse_server_entry({"type": "http", "url": "http://127.0.0.1:9/mcp"}),
        "live2": parse_server_entry(good),
    }

    async def go():
        mgr = MCPManager(dict(configs))
        try:
            tools = await mgr.start()
            assert sorted(t.name for t in tools) == [
                "mcp__live1__ping", "mcp__live2__ping"]
            states = {r["name"]: r["state"] for r in mgr.status_rows()}
            assert states == {"dead": "failed", "live1": "connected",
                              "refused": "failed", "live2": "connected"}
            assert mgr.errors["dead"] and mgr.errors["refused"]
        finally:
            await mgr.stop()

    anyio.run(go)
