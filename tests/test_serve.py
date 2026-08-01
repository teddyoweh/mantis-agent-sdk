"""Tests for ``mantis serve`` — the read-only web dashboard.

Exercises the data layer against a temp ``$MANTIS_AGENT_HOME`` seeded with a
real session, plus one end-to-end HTTP boot on an ephemeral port.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def seeded_home(tmp_path, monkeypatch):
    """A temp ~/.mantis-agent with one project holding one session."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    # Make sure any module-level cache of the home dir isn't in play.
    from mantis_agent import session_tree

    proj = tmp_path / "myproj"
    proj.mkdir()
    sid = session_tree.new_session_id()
    tx = session_tree.SessionTranscript(sid, cwd=str(proj))
    tx.append_message("user", "hello there")
    tx.record_last_prompt("hello there")
    tx.append_message("assistant", [
        {"type": "text", "text": "Hi! Let me check."},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "x.py"}},
    ])
    tx.append_message("user", [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents", "is_error": False},
    ])
    tx.append_message("assistant", [{"type": "text", "text": "Done."}])
    tx.set_title("Greeting and file read")
    return {"home": home, "cwd": str(proj), "session_id": sid}


def test_list_projects_finds_seeded(seeded_home):
    from mantis_agent import serve

    projects = serve.list_projects()
    assert len(projects) == 1
    p = projects[0]
    assert p["cwd"] == seeded_home["cwd"]
    assert p["name"] == "myproj"
    assert p["session_count"] == 1
    assert p["last_activity"] > 0


def test_sessions_and_detail(seeded_home):
    from mantis_agent import serve

    sessions = serve.sessions_for(seeded_home["cwd"])
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == seeded_home["session_id"]
    assert s["title"] == "Greeting and file read"

    detail = serve.session_detail(seeded_home["cwd"], seeded_home["session_id"])
    msgs = detail["messages"]
    # user, assistant(text+tool_use), user(tool_result), assistant(text)
    assert len(msgs) == 4
    assert msgs[0]["role"] == "user"
    # assistant turn carries a tool_use block
    kinds = [b["type"] for b in msgs[1]["content"]]
    assert "text" in kinds and "tool_use" in kinds
    # tool_result round-trips
    assert msgs[2]["content"][0]["type"] == "tool_result"


def test_models_and_config_shapes(seeded_home):
    from mantis_agent import serve

    m = serve.models_state()
    assert "providers" in m and isinstance(m["providers"], list)
    assert m["providers"], "catalog should list providers"
    assert all("enabled" in p and "id" in p for p in m["providers"])

    c = serve.config_state()
    assert "merged" in c and "layers" in c
    assert set(c["layers"]) == {"user", "project", "local"}


def test_provider_guides_attached(seeded_home):
    """Every provider carries a how-to-get-a-key guide; self-host guide present."""
    from mantis_agent import serve

    m = serve.models_state()
    for p in m["providers"]:
        g = p["guide"]
        assert g, f"{p['id']} missing guide"
        assert g["keys_url"].startswith("https://")
        assert g["env_var"] and g["steps"]
        assert p["docs_url"].startswith("https://")
    sh = m["selfhost_guide"]
    assert sh["runtimes"] and sh["steps"]
    assert any(r["name"] == "vLLM" for r in sh["runtimes"])


def test_overview(seeded_home):
    from mantis_agent import serve

    o = serve.overview()
    assert o["project_count"] == 1
    assert o["session_count"] == 1
    assert o["provider_count"] >= 1


def test_write_connect_and_use(seeded_home):
    """Self-host connect + set-current write to models.json (no network)."""
    from mantis_agent import catalog, serve

    r = serve.connect_selfhost("http://box:8000/v1", "zai-org/GLM-4-9B-0414")
    assert r["ok"] and r["backend"] == "http://box:8000/v1"
    last = catalog.get_last_model()
    assert last["model"] == "zai-org/GLM-4-9B-0414"
    assert last["backend"] == "http://box:8000/v1"

    # bad backend rejected
    assert serve.connect_selfhost("not-a-url", "m")["ok"] is False
    assert serve.connect_selfhost("http://x/v1", "")["ok"] is False

    r = serve.set_current("gpt-5.5", "https://api.openai.com/v1")
    assert r["ok"]
    assert catalog.get_last_model()["model"] == "gpt-5.5"


def test_write_key_set_and_clear(seeded_home):
    """Saving then clearing a provider key (clear needs no network)."""
    from mantis_agent import catalog, serve

    catalog.set_key("openai", "sk-testkey-abcd1234")
    assert catalog.saved_key("openai") == "sk-testkey-abcd1234"

    r = serve.set_provider_key("openai", "")  # empty → clear
    assert r["ok"] and r.get("cleared")
    assert catalog.saved_key("openai") is None

    assert serve.set_provider_key("nope", "x")["ok"] is False


def test_post_requires_token(seeded_home):
    """POST always needs the token, even on a loopback (token=None) bind."""
    import urllib.error

    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = None  # loopback reads open, but writes must still be blocked
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/use",
            data=json.dumps({"model": "x"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_skills_crud(seeded_home, tmp_path, monkeypatch):
    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)  # project scope resolves under tmp
    assert serve.skills_state()["global"] == []

    r = serve.add_skill("global", "Deploy Checklist", "steps to deploy", "1. build\n2. ship")
    assert r["ok"] and r["slug"] == "deploy-checklist"
    st = serve.skills_state()
    assert [s["name"] for s in st["global"]] == ["Deploy Checklist"]
    assert st["global"][0]["body"].startswith("1. build")

    serve.add_skill("project", "Proj Skill", "d", "b")
    assert any(s["slug"] == "proj-skill" for s in serve.skills_state()["project"])

    assert serve.delete_skill("global", "deploy-checklist")["ok"]
    assert serve.skills_state()["global"] == []
    assert serve.delete_skill("global", "nope")["ok"] is False
    assert serve.add_skill("global", "", "d", "b")["ok"] is False


def test_mcp_crud(seeded_home, tmp_path, monkeypatch):
    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)
    assert serve.mcp_state()["servers"] == []

    assert serve.add_mcp("global", "github", {"command": "npx", "args": ["-y", "srv"]})["ok"]
    m = serve.mcp_state()
    assert [(s["name"], s["scope"], s["transport"]) for s in m["servers"]] == [("github", "global", "stdio")]

    serve.add_mcp("project", "remote", {"type": "http", "url": "https://x/mcp"})
    names = {(s["name"], s["scope"]) for s in serve.mcp_state()["servers"]}
    assert ("remote", "project") in names

    assert serve.add_mcp("global", "x", {})["ok"] is False  # no command/url
    assert serve.delete_mcp("global", "github")["ok"]
    assert serve.delete_mcp("global", "github")["ok"] is False
    assert serve.delete_mcp("settings", "x")["ok"] is False  # settings not editable here


def test_mcp_state_shows_the_whole_entry_with_secrets_masked(seeded_home, tmp_path, monkeypatch):
    """The dashboard's MCP page is an inspector: it needs each server's real
    configuration, but a screenshot of it must not leak a token."""
    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)
    serve.add_mcp("global", "exa", {"command": "npx", "args": ["-y", "exa-mcp"],
                                    "env": {"EXA_API_KEY": "sk-live-secret"}})
    serve.add_mcp("global", "linear", {"type": "http", "url": "https://m.co/mcp?apiKey=tok-secret",
                                       "headers": {"Authorization": "Bearer hush"}})
    by_name = {s["name"]: s for s in serve.mcp_state()["servers"]}

    exa = by_name["exa"]
    assert exa["entry"]["command"] == "npx" and exa["entry"]["args"] == ["-y", "exa-mcp"]
    assert "sk-live-secret" not in json.dumps(exa)          # masked on the wire
    assert "EXA_API_KEY" in json.dumps(exa["entry"])        # …but the key name shows
    assert exa["secrets"] == ["env"] and exa["editable"] is True

    lin = by_name["linear"]
    blob = json.dumps(lin)
    assert "tok-secret" not in blob and "hush" not in blob
    assert lin["transport"] == "http" and set(lin["secrets"]) == {"headers", "url"}

    # The editor asks for the raw entry explicitly; that one is unmasked.
    raw = serve.mcp_entry_raw("exa", "global")
    assert raw["ok"] and raw["entry"]["env"]["EXA_API_KEY"] == "sk-live-secret"
    assert serve.mcp_entry_raw("exa", "settings")["ok"] is False   # not editable here
    assert serve.mcp_entry_raw("nope", "global")["ok"] is False


def test_mcp_paste_accepts_a_config_blob(seeded_home, tmp_path, monkeypatch):
    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)
    r = serve.add_mcp_paste("global",
        '{"mcpServers": {"a": {"command": "x"}, "b": {"url": "https://b/mcp"}}}')
    assert r["ok"] and sorted(r["added"]) == ["a", "b"]
    assert {s["name"] for s in serve.mcp_state()["servers"]} == {"a", "b"}

    # A bare command has no name in it — the UI is told to ask for one.
    bare = serve.add_mcp_paste("global", "npx -y thing")
    assert bare["ok"] is False and bare["needs_name"] is True
    assert serve.add_mcp_paste("global", "{oops")["ok"] is False


def test_mcp_trust_gate_is_reported_and_settable(seeded_home, tmp_path, monkeypatch):
    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANTIS_MCP_TRUST_PROJECT", raising=False)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"repo": {"command": "./run.sh"}}}), encoding="utf-8")

    st = serve.mcp_state()
    assert st["project_exists"] and st["project_trusted"] is False
    assert st["withheld"] == ["repo"]           # stdio, untrusted → won't start

    assert serve.trust_project_mcp_file()["ok"]
    st = serve.mcp_state()
    assert st["project_trusted"] and st["withheld"] == []


def test_mcp_test_endpoint_connects_and_reports(seeded_home, tmp_path, monkeypatch):
    """'Does this server actually work' is the question a config page can't
    answer by reading JSON — so the dashboard runs a real handshake."""
    import sys

    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)
    server = tmp_path / "srv.py"
    server.write_text(
        "import json, sys\n"
        "def send(m): sys.stdout.write(json.dumps(m) + '\\n'); sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line: continue\n"
        "    m = json.loads(line); mid, meth = m.get('id'), m.get('method')\n"
        "    if meth == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05',"
        "'capabilities':{'tools':{}},'serverInfo':{'name':'t','version':'1'}}})\n"
        "    elif meth == 'tools/list':\n"
        "        send({'jsonrpc':'2.0','id':mid,'result':{'tools':[{'name':'ping',"
        "'description':'Ping.','inputSchema':{'type':'object'}}]}})\n"
        "    elif mid is not None:\n"
        "        send({'jsonrpc':'2.0','id':mid,'error':{'code':-32601,'message':'no'}})\n",
        encoding="utf-8")
    serve.add_mcp("global", "live", {"command": sys.executable, "args": [str(server)]})
    serve.add_mcp("global", "dead", {"command": "echo", "args": ["bye"]})

    ok = serve.test_mcp("live")
    assert ok["ok"] and [t["name"] for t in ok["tools"]] == ["ping"] and ok["ms"] >= 0

    bad = serve.test_mcp("dead")
    assert bad["ok"] is False and bad["error"]
    assert serve.test_mcp("missing")["ok"] is False


def test_skill_edit_preserves_metadata_and_slug(seeded_home, tmp_path, monkeypatch):
    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)
    serve.add_skill("global", "Deploy", "ship it", "1. build", "ops", True)
    s = serve.skills_state()["global"][0]
    assert s["category"] == "ops" and s["always_load"] is True

    # Saving from the editor renames in place — the directory (and every
    # reference to it) stays put.
    r = serve.add_skill("global", "Deploy v2", "ship it twice", "1. build\n2. ship",
                        "ops", False, slug="deploy")
    assert r["ok"] and r["slug"] == "deploy"
    s = serve.skills_state()["global"][0]
    assert s["name"] == "Deploy v2" and s["always_load"] is False
    assert s["body"].endswith("2. ship")
    assert serve.add_skill("global", "x", "d", "b", slug="ghost")["ok"] is False


def test_every_provider_has_a_mark() -> None:
    """Each catalogued provider ships its own logo, so the setup list is
    scannable by mark. A new provider without one falls back to a letter tile —
    this test is the nudge to add it."""
    from mantis_agent import catalog
    from mantis_agent.serve_logos import PROVIDER_LOGOS

    missing = [p.id for p in catalog.CATALOG if p.id not in PROVIDER_LOGOS]
    assert not missing, f"no logo for: {missing} — see tools/gen_provider_logos.py"
    for pid, mark in PROVIDER_LOGOS.items():
        assert mark["svg"].startswith("<svg"), pid
        assert "<script" not in mark["svg"].lower(), pid


def test_model_probe_reports_unreachable_endpoints(seeded_home) -> None:
    from mantis_agent import serve

    r = serve.test_provider(None, "http://127.0.0.1:9/v1")   # nothing listening
    assert r["ok"] is False and r["error"] and r["ms"] >= 0
    assert serve.test_provider("not-a-provider")["ok"] is False
    assert serve.test_provider(None, "")["ok"] is False


def test_models_state_carries_capability_info(seeded_home) -> None:
    from mantis_agent import serve

    m = serve.models_state()
    info = m["model_info"]
    assert info, "expected per-model capability info"
    # every listed model resolves to something the table can compare on
    for p in m["providers"]:
        for mid in p["models"]:
            assert mid in info
            assert info[mid].get("ctx", 0) > 0


def test_overview_counts_skills_and_servers(seeded_home, tmp_path, monkeypatch):
    from mantis_agent import serve

    monkeypatch.chdir(tmp_path)
    serve.add_mcp("global", "a", {"command": "x"})
    serve.add_skill("global", "S", "d", "b")
    o = serve.overview()
    assert o["mcp_count"] == 1 and o["skill_count"] == 1
    assert o["cwd"] and "hosting" in o


def test_http_boot_and_endpoints(seeded_home):
    """Boot the real handler on an ephemeral port and hit the API + index."""
    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = None  # loopback: no auth
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read()

        code, body = get("/api/overview")
        assert code == 200
        assert json.loads(body)["session_count"] == 1

        code, body = get("/api/projects")
        assert json.loads(body)["projects"][0]["name"] == "myproj"

        code, body = get("/")
        assert code == 200
        assert b"<title>mantis" in body
        # Provider logos ship inside the page: a local dashboard must not fetch
        # third-party assets (it would leak which providers you look at, and
        # break offline), so no placeholder may survive and the marks must be
        # real inline SVG.
        assert b"__LOGOS__" not in body and b"__TOKEN__" not in body
        assert body.count(b"<svg") > 10

        code, body = get("/api/models")
        assert "providers" in json.loads(body)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_token_guard_rejects_without_token(seeded_home):
    """When a token is set (LAN mode), API calls without it are 401."""
    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = "secret123"
    httpd.enforce_get = True  # LAN mode: reads require the token too
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # No token → 401
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/api/overview", timeout=5)
        assert ei.value.code == 401
        # With token as query param → 200
        with urllib.request.urlopen(base + "/api/overview?k=secret123", timeout=5) as r:
            assert r.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
