"""MCP resources + prompts client methods (T2)."""

from __future__ import annotations

import anyio

from mantis_agent.mcp.client import MCPClient
from mantis_agent.mcp.types import StdioServerConfig


def _client() -> MCPClient:
    return MCPClient(StdioServerConfig(command="x"), server_id="srv")


def _patch_request(monkeypatch, responses: dict) -> None:
    async def fake(self, method, params):  # noqa: ANN001
        return responses.get(method, {})
    monkeypatch.setattr(MCPClient, "_request", fake)


def test_list_resources(monkeypatch) -> None:
    _patch_request(monkeypatch, {"resources/list": {"resources": [
        {"uri": "file:///a.txt", "name": "a", "description": "the a file",
         "mimeType": "text/plain"},
        {"uri": "db://rows/1"},
        {"no_uri": "skipped"},
    ]}})
    res = anyio.run(_client().list_resources)
    assert [r.uri for r in res] == ["file:///a.txt", "db://rows/1"]
    assert res[0].name == "a" and res[0].mime_type == "text/plain"
    assert res[0].server_id == "srv"


def test_read_resource_text_and_binary(monkeypatch) -> None:
    _patch_request(monkeypatch, {"resources/read": {"contents": [
        {"uri": "file:///a.txt", "text": "line one"},
        {"uri": "img://x", "blob": "AAAA", "mimeType": "image/png"},
    ]}})
    out = anyio.run(lambda: _client().read_resource("file:///a.txt"))
    assert "line one" in out
    assert "binary resource image/png" in out       # blob noted, not decoded


def test_list_prompts(monkeypatch) -> None:
    _patch_request(monkeypatch, {"prompts/list": {"prompts": [
        {"name": "summarize", "description": "Summarize text",
         "arguments": [{"name": "text", "required": True}]},
    ]}})
    ps = anyio.run(_client().list_prompts)
    assert ps[0].name == "summarize"
    assert ps[0].arguments == [{"name": "text", "required": True}]


def test_get_prompt_renders_messages(monkeypatch) -> None:
    _patch_request(monkeypatch, {"prompts/get": {"messages": [
        {"role": "user", "content": {"type": "text", "text": "Summarize: hello"}},
        {"role": "assistant", "content": {"type": "text", "text": "ok"}},
    ]}})
    out = anyio.run(lambda: _client().get_prompt("summarize", {"text": "hello"}))
    assert "[user] Summarize: hello" in out
    assert "[assistant] ok" in out


def test_pagination(monkeypatch) -> None:
    pages = [
        {"resources": [{"uri": "a"}], "nextCursor": "c1"},
        {"resources": [{"uri": "b"}]},
    ]
    calls = {"n": 0}

    async def fake(self, method, params):  # noqa: ANN001
        p = pages[calls["n"]]
        calls["n"] += 1
        return p
    monkeypatch.setattr(MCPClient, "_request", fake)
    res = anyio.run(_client().list_resources)
    assert [r.uri for r in res] == ["a", "b"]        # both pages collected
