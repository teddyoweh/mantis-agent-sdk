"""A search key the service refuses (401/403) is dead for the session: it used
to be tried first on every search — a failed round trip and a warning each
time before the DuckDuckGo fallback — in a real eval, on every web task."""

from __future__ import annotations

import anyio
import httpx
import pytest

from mantis_agent.builtin_tools import web


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(web, "_REJECTED_KEYS", set())
    monkeypatch.setenv("EXA_API_KEY", "dead-key")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def test_a_refused_exa_key_is_skipped_after_the_first_401(monkeypatch) -> None:
    calls = {"exa": 0, "ddg": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            calls["exa"] += 1
            return httpx.Response(401, json={"error": "Invalid API key"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(web, "_client", lambda: client)

    async def fake_ddg(query, **kw):
        calls["ddg"] += 1
        return "ddg results"
    monkeypatch.setattr(web, "_ddg_search", fake_ddg)

    assert web._resolve_search_provider() == "exa"
    out = anyio.run(lambda: web._exa_search("q", allowed=None, blocked=None))
    assert out == "ddg results" and calls == {"exa": 1, "ddg": 1}
    assert web._resolve_search_provider() == "ddg"        # never tried again this session


def test_the_next_configured_provider_takes_over(monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "good")
    web._REJECTED_KEYS.add("exa")
    assert web._resolve_search_provider() == "brave"
