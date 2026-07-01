"""web_fetch — dependency-free HTML→text extraction (no BeautifulSoup)."""

from __future__ import annotations

import anyio
import httpx

import mantis_agent.builtin_tools.web as web
from mantis_agent.builtin_tools.web import _html_to_text, web_fetch


def test_html_to_text_drops_script_style() -> None:
    html = ("<html><head><style>.x{color:red}</style></head><body>"
            "<script>var secret=1;</script><h1>Hi &amp; bye</h1>"
            "<p>Para one.</p><p>Para two.</p></body></html>")
    out = _html_to_text(html)
    assert "var secret" not in out and "color:red" not in out
    assert "Hi & bye" in out                      # entity decoded
    assert "Para one." in out and "Para two." in out


def test_html_to_text_block_newlines() -> None:
    out = _html_to_text("<p>a</p><p>b</p><br><div>c</div>")
    assert [l for l in out.splitlines() if l] == ["a", "b", "c"]  # markdown keeps blank breaks


def test_html_to_text_collapses_whitespace() -> None:
    assert _html_to_text("<p>lots     of    space</p>") == "lots of space"


def _mock_web(monkeypatch, *, body: str, content_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": content_type})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(web, "_CLIENT", client)
    monkeypatch.delenv("EXA_API_KEY", raising=False)   # force the raw path


def test_web_fetch_html_cleaned(monkeypatch) -> None:
    _mock_web(monkeypatch,
              body="<html><body><script>x</script><p>Clean me</p></body></html>",
              content_type="text/html; charset=utf-8")
    out = anyio.run(lambda: web_fetch.fn(url="http://x"))
    assert "Clean me" in out
    assert "<p>" not in out and "script" not in out


def test_web_fetch_json_verbatim(monkeypatch) -> None:
    _mock_web(monkeypatch, body='{"key": "value", "n": 1}',
              content_type="application/json")
    out = anyio.run(lambda: web_fetch.fn(url="http://x/api"))
    assert out == '{"key": "value", "n": 1}'         # NOT tag-stripped


def test_web_fetch_http_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(web, "_CLIENT", client)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    out = anyio.run(lambda: web_fetch.fn(url="http://x"))
    assert "fetch error" in out


def test_markdown_preserves_headings() -> None:
    from mantis_agent.builtin_tools.web import _html_to_markdown
    md = _html_to_markdown("<h1>Title</h1><p>body</p><h2>Sub</h2>")
    assert "# Title" in md and "## Sub" in md


def test_markdown_preserves_links() -> None:
    from mantis_agent.builtin_tools.web import _html_to_markdown
    md = _html_to_markdown('<p>see <a href="https://x.com/docs">the docs</a></p>')
    assert "[the docs](https://x.com/docs)" in md


def test_markdown_preserves_lists() -> None:
    from mantis_agent.builtin_tools.web import _html_to_markdown
    md = _html_to_markdown("<ul><li>one</li><li>two</li></ul>")
    assert "- one" in md and "- two" in md


def test_markdown_link_inside_list() -> None:
    from mantis_agent.builtin_tools.web import _html_to_markdown
    md = _html_to_markdown('<li>Read <a href="/g">guide</a></li>')
    assert "- Read [guide](/g)" in md


def test_markdown_still_drops_scripts() -> None:
    from mantis_agent.builtin_tools.web import _html_to_markdown
    md = _html_to_markdown("<script>bad()</script><h1>Safe</h1>")
    assert "bad()" not in md and "# Safe" in md
