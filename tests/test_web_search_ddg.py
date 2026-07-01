"""DuckDuckGo web search fallback — dependency-free (no bs4), redirect-decoded.

The previous fallback required beautifulsoup4 (not a dependency), so keyless web
search returned an error. This covers the stdlib parser + redirect unwrap.
"""

from __future__ import annotations

import anyio

import mantis_agent.builtin_tools.web as web
from mantis_agent.builtin_tools.web import _ddg_real_url, _ddg_text


def test_redirect_url_decoded() -> None:
    assert _ddg_real_url(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&rut=x"
    ) == "https://docs.python.org/3/"


def test_plain_url_passthrough() -> None:
    assert _ddg_real_url("https://example.com/page") == "https://example.com/page"
    assert _ddg_real_url("//example.com/x") == "https://example.com/x"


def test_text_strips_tags_and_unescapes() -> None:
    assert _ddg_text("<b>Hello</b> &amp; bye") == "Hello & bye"


_FAKE_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fasync-io-python%2F">
    Async IO in Python
  </a>
  <a class="result__snippet">A hands-on walkthrough of asyncio.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">
    asyncio docs
  </a>
  <a class="result__snippet">The official reference.</a>
</div>
"""


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    async def get(self, url: str):  # noqa: ANN201
        return _FakeResp(_FAKE_HTML)


def test_ddg_search_parses_results(monkeypatch) -> None:
    monkeypatch.setattr(web, "_client", lambda: _FakeClient())
    out = anyio.run(lambda: web._ddg_search("asyncio", allowed=None, blocked=None))
    lines = out.splitlines()
    assert "realpython.com/async-io-python" in lines[0]
    assert "Async IO in Python" in lines[0]
    assert "hands-on walkthrough" in lines[0]
    assert "docs.python.org/3/library/asyncio.html" in out
    # No leftover DDG redirector URLs.
    assert "duckduckgo.com/l/" not in out


def test_ddg_search_empty_gives_helpful_note(monkeypatch) -> None:
    class _Empty(_FakeClient):
        async def get(self, url: str):  # noqa: ANN201
            return _FakeResp("<html>nothing here</html>")

    monkeypatch.setattr(web, "_client", lambda: _Empty())
    out = anyio.run(lambda: web._ddg_search("q", allowed=None, blocked=None))
    assert "no results" in out
    assert "EXA_API_KEY" in out              # points to the reliable path


def test_ddg_domain_filter(monkeypatch) -> None:
    monkeypatch.setattr(web, "_client", lambda: _FakeClient())
    out = anyio.run(
        lambda: web._ddg_search("asyncio", allowed=["realpython.com"], blocked=None)
    )
    assert "realpython.com" in out
    assert "docs.python.org" not in out
