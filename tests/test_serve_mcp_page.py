"""The MCP page as a board of connections: a card per server with its real
status and tools, and a sheet with the tools in full and the configuration."""

from __future__ import annotations


def _js():
    from mantis_agent.serve_ui import INDEX_HTML

    return INDEX_HTML, INDEX_HTML.split("<script>")[1]


def test_the_page_is_a_board_with_real_status():
    page, js = _js()
    for fn in ("async function loadMcp(", "function paintMcpCards(", "function mcpCard(", "async function testMcp(",
               "function openMcpSheet(", "function mcpToolsTab(", "function mcpConfigTab(", "function mcpEditor(",
               "function openMcpAdd("):
        assert fn in js, fn
    for gone in ("function mcpDetail(", "function openMcpEditor(", "function paintMcpDoc("):
        assert gone not in js, gone
    body = js[js.index("async function loadMcp("):js.index("const mcpWithheld")]
    # every server is connected once on open, three at a time, never twice
    assert "Promise.all([next(), next(), next()])" in body and "!MCPW.tests[s.name]" in body
    # an untrusted project's stdio server is never connected (it would run its command)
    assert "!mcpWithheld(s)" in body
    assert 'emptyState("mcp", "No MCP servers configured"' in js and 'pageHead(pad, "MCP servers"' in js
    assert ".mcx-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));" in page


def test_failures_explain_themselves_and_secrets_stay_masked():
    page, js = _js()
    hint = js[js.index("function mcpHint("):js.index("function mcpPill(")]
    for h in ("refused the credentials", "isn't installed on this machine", "Nothing is listening there"):
        assert h in hint, h
    cfg = js[js.index("function mcpConfigTab("):js.index("async function toggleMcpReveal(")]
    assert 'el("span","mcp-v" + (raw ? "" : " secret")' in cfg
    ed = js[js.index("function mcpEditor("):js.index("function openMcpAdd(")]
    assert 'api("/api/mcp/entry?"' in ed and "JSON.parse(ta.value)" in ed


def test_tools_show_their_parameters():
    page, js = _js()
    tab = js[js.index("function mcpToolsTab("):js.index("function mcpConfigTab(")]
    assert '"mcs-p" + (p.required ? " req" : "")' in tab


def test_probe_returns_parameters_from_a_real_stdio_server(tmp_path, monkeypatch):
    """End to end: a tiny stdio MCP server, a real handshake, and the tool's
    parameters (required marked) come back — a probe that crashes on the
    schema would turn every server red."""
    import json
    import sys

    srv = tmp_path / "srv.py"
    srv.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line); i = m.get('id')\n"
        "    if i is None: continue\n"
        "    if m['method'] == 'initialize':\n"
        "        r = {'protocolVersion': m['params'].get('protocolVersion'), 'capabilities': {'tools': {}}, 'serverInfo': {'name': 't', 'version': '1'}}\n"
        "    elif m['method'] == 'tools/list':\n"
        "        r = {'tools': [{'name': 'search', 'description': 'find', 'inputSchema': {'type': 'object',\n"
        "             'properties': {'q': {'type': 'string'}, 'n': {'type': 'integer'}}, 'required': ['q']}}]}\n"
        "    else: r = {}\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': i, 'result': r}) + '\\n'); sys.stdout.flush()\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"t": {"command": sys.executable, "args": [str(srv)]}}}))
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    from mantis_agent import serve

    r = serve.test_mcp("t")
    assert r["ok"], r
    (tool,) = r["tools"]
    assert tool["params"] == [{"name": "q", "type": "string", "required": True},
                              {"name": "n", "type": "integer", "required": False}]
