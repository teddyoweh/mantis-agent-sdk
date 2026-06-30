"""Built-in WebSearch + WebFetch tools — drop-in compatible with the Claude
Agent SDK's tools of the same name.

Provider selection (auto, in order of preference):

    1. Exa     — if ``EXA_API_KEY`` is set. Semantic search, real URLs,
                 highlights, neural ranking. ~$0.005/query. Recommended.
    2. Brave   — if ``BRAVE_API_KEY`` is set. 2K free/month, clean JSON.
    3. Tavily  — if ``TAVILY_API_KEY`` is set. Agent-tuned.
    4. DuckDuckGo HTML — no key, free but limited (scraping, slow,
                 redirector URLs). Used as a fallback so the SDK never
                 silently fails when no key is configured.

The tool's *external* signature is the SAME as Claude SDK's WebSearch:

    web_search(query: str, allowed_domains: list[str] | None = None,
               blocked_domains: list[str] | None = None) -> str

Returns a plain-text result block, one result per line:
``TITLE — URL — SNIPPET``. The agent loop never sees which provider
served the request — that's an implementation detail.

WebFetch is the same idea — fetch a URL, return readable text. Optional
``prompt`` is a hint about what to extract (used by some providers; Exa's
``contents`` parameter can target highlights matching the prompt).
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote_plus

import httpx

from ..tools import Tool, tool

_LOG = logging.getLogger("mantis_agent.builtin_tools.web")


# ---------------------------------------------------------------------------
# Shared HTTP client — long-lived, HTTP/2, sane timeouts.
# ---------------------------------------------------------------------------


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": (
                "mantis-agent-sdk/0.1 (+https://github.com/teddyoweh/mantis-agent-sdk)"
            )
        },
        timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=10.0),
        follow_redirects=True,
        http2=True,
    )


_CLIENT: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = _make_client()
    return _CLIENT


async def aclose_builtin_clients() -> None:
    """Close the shared HTTP client. Called from ``Agent.aclose()`` so
    users don't have to remember."""
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
        _CLIENT = None


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------


def _resolve_search_provider() -> str:
    """Pick the best available search provider based on env."""

    if os.environ.get("EXA_API_KEY"):
        return "exa"
    if os.environ.get("BRAVE_API_KEY"):
        return "brave"
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    return "ddg"


def _domain_filter(host_url: str, allowed: list[str] | None, blocked: list[str] | None) -> bool:
    """Decide whether a result URL passes the allow/block filters.

    Both lists treat each entry as a domain or domain suffix. Empty / None
    lists are no-ops. Blocked wins over allowed.
    """

    if not (allowed or blocked):
        return True
    host = host_url.lower()
    if blocked:
        for b in blocked:
            b = b.lower().strip()
            if b and b in host:
                return False
    if allowed:
        for a in allowed:
            a = a.lower().strip()
            if a and a in host:
                return True
        return False  # allowed set is non-empty but nothing matched
    return True


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


async def _exa_search(
    query: str,
    *,
    allowed: list[str] | None,
    blocked: list[str] | None,
    num: int = 8,
) -> str:
    """Exa neural search. Returns ranked semantic results with highlights."""

    key = os.environ["EXA_API_KEY"]
    payload: dict[str, Any] = {
        "query": query,
        "type": "auto",
        "numResults": num,
        "contents": {
            "text": {"maxCharacters": 1500},
            "highlights": {"numSentences": 2},
        },
    }
    # Exa wants ``includeDomains`` / ``excludeDomains``.
    if allowed:
        payload["includeDomains"] = [_strip_scheme(d) for d in allowed]
    if blocked:
        payload["excludeDomains"] = [_strip_scheme(d) for d in blocked]

    r = await _client().post(
        "https://api.exa.ai/search",
        headers={"x-api-key": key, "content-type": "application/json"},
        json=payload,
    )
    if r.status_code >= 400:
        _LOG.warning("Exa search failed (status %s) — falling back to DDG", r.status_code)
        return await _ddg_search(query, allowed=allowed, blocked=blocked, num=num)
    data = r.json()

    rows: list[str] = []
    for res in data.get("results", [])[:num]:
        url = res.get("url", "")
        if not _domain_filter(url, allowed, blocked):
            continue
        title = res.get("title", "")
        # Prefer highlight; fall back to first chunk of text.
        snippet = ""
        highlights = res.get("highlights") or []
        if highlights:
            snippet = highlights[0]
        elif res.get("text"):
            snippet = res["text"][:300]
        rows.append(f"{title} — {url} — {snippet.strip()[:280]}")
    return "\n".join(rows) if rows else "no results"


async def _brave_search(
    query: str,
    *,
    allowed: list[str] | None,
    blocked: list[str] | None,
    num: int = 8,
) -> str:
    key = os.environ["BRAVE_API_KEY"]
    params = {"q": query, "count": num}
    r = await _client().get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params=params,
    )
    if r.status_code >= 400:
        _LOG.warning("Brave search failed (%s) — falling back to DDG", r.status_code)
        return await _ddg_search(query, allowed=allowed, blocked=blocked, num=num)
    data = r.json()
    rows: list[str] = []
    for res in (data.get("web") or {}).get("results", [])[:num]:
        url = res.get("url", "")
        if not _domain_filter(url, allowed, blocked):
            continue
        rows.append(
            f"{res.get('title','')} — {url} — {(res.get('description') or '').strip()[:280]}"
        )
    return "\n".join(rows) if rows else "no results"


async def _tavily_search(
    query: str,
    *,
    allowed: list[str] | None,
    blocked: list[str] | None,
    num: int = 8,
) -> str:
    key = os.environ["TAVILY_API_KEY"]
    payload: dict[str, Any] = {
        "api_key": key,
        "query": query,
        "max_results": num,
        "search_depth": "basic",
    }
    if allowed:
        payload["include_domains"] = [_strip_scheme(d) for d in allowed]
    if blocked:
        payload["exclude_domains"] = [_strip_scheme(d) for d in blocked]
    r = await _client().post("https://api.tavily.com/search", json=payload)
    if r.status_code >= 400:
        _LOG.warning("Tavily search failed (%s) — falling back to DDG", r.status_code)
        return await _ddg_search(query, allowed=allowed, blocked=blocked, num=num)
    data = r.json()
    rows: list[str] = []
    for res in data.get("results", [])[:num]:
        url = res.get("url", "")
        if not _domain_filter(url, allowed, blocked):
            continue
        rows.append(
            f"{res.get('title','')} — {url} — {(res.get('content') or '').strip()[:280]}"
        )
    return "\n".join(rows) if rows else "no results"


async def _ddg_search(
    query: str,
    *,
    allowed: list[str] | None,
    blocked: list[str] | None,
    num: int = 8,
) -> str:
    """DuckDuckGo HTML scraping. No key, free, lower quality, redirector URLs."""

    # bs4 is imported lazily so users who don't use this fallback don't pay.
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    except ImportError:
        return (
            "DDG fallback requires 'beautifulsoup4'. "
            "Either install it or set EXA_API_KEY for proper search."
        )

    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        r = await _client().get(url)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return f"search error: {e!r}"

    soup = BeautifulSoup(r.text, "html.parser")
    rows: list[str] = []
    for result in soup.select("div.result")[: num * 2]:  # over-fetch for filter
        title_el = result.select_one("a.result__a")
        if title_el is None:
            continue
        href = title_el.get("href", "")
        if not _domain_filter(href, allowed, blocked):
            continue
        title = title_el.get_text(strip=True)
        snip_el = result.select_one(".result__snippet")
        snip = snip_el.get_text(strip=True) if snip_el else ""
        rows.append(f"{title} — {href} — {snip[:280]}")
        if len(rows) >= num:
            break
    return "\n".join(rows) if rows else "no results"


# ---------------------------------------------------------------------------
# Public tools — Claude SDK-compatible names + signatures
# ---------------------------------------------------------------------------


@tool(is_read_only=True, timeout_s=30.0)
async def web_search(
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    """Search the web. Returns ranked results as ``TITLE — URL — SNIPPET`` lines.

    Args:
        query: The search query.
        allowed_domains: Only include results whose URL contains one of these.
        blocked_domains: Exclude results whose URL contains any of these.

    Uses Exa when ``EXA_API_KEY`` is set (best quality, semantic ranking,
    real URLs). Falls back to Brave / Tavily / DuckDuckGo based on what's
    available in env.
    """

    # Sanitize: drop wildcards / empty strings that small models often emit.
    allowed_domains = _sanitize_domains(allowed_domains)
    blocked_domains = _sanitize_domains(blocked_domains)

    provider = _resolve_search_provider()
    if provider == "exa":
        return await _exa_search(query, allowed=allowed_domains, blocked=blocked_domains)
    if provider == "brave":
        return await _brave_search(query, allowed=allowed_domains, blocked=blocked_domains)
    if provider == "tavily":
        return await _tavily_search(query, allowed=allowed_domains, blocked=blocked_domains)
    return await _ddg_search(query, allowed=allowed_domains, blocked=blocked_domains)


@tool(is_read_only=True, timeout_s=30.0)
async def web_fetch(url: str, prompt: str | None = None) -> str:
    """Fetch a URL's readable text content. Truncated to ~5000 chars.

    Args:
        url: The URL to fetch.
        prompt: Optional hint about what information you're looking for.
            When using Exa as the search backend, this can target the
            page's highlights for better precision. Otherwise informational.
    """

    # If Exa is available, prefer its ``/contents`` endpoint — same URL,
    # but pre-processes the page to extract clean text + highlights tuned
    # to ``prompt``. Fall through to raw HTTP if Exa errors OR returns
    # an empty/useless response (which happens for pages it hasn't indexed).
    if os.environ.get("EXA_API_KEY"):
        try:
            exa_out = await _exa_fetch(url, prompt=prompt)
            if exa_out and exa_out != "(empty)" and exa_out != "(no content)":
                return exa_out
            _LOG.info("Exa fetch returned empty for %s — falling back to raw HTTP", url)
        except Exception as e:  # noqa: BLE001 — degraded fallback
            _LOG.warning("Exa fetch failed (%r) — falling back to raw HTTP", e)

    return await _raw_fetch(url)


async def _exa_fetch(url: str, *, prompt: str | None) -> str:
    """Use Exa's /contents to get clean text + optional prompt-targeted highlights."""

    key = os.environ["EXA_API_KEY"]
    payload: dict[str, Any] = {
        "ids": [url],
        "text": {"maxCharacters": 5000},
    }
    if prompt:
        payload["highlights"] = {"query": prompt, "numSentences": 4}

    r = await _client().post(
        "https://api.exa.ai/contents",
        headers={"x-api-key": key, "content-type": "application/json"},
        json=payload,
    )
    r.raise_for_status()
    data = r.json()
    results = data.get("results", [])
    if not results:
        return "(no content)"
    res = results[0]
    parts: list[str] = []
    if res.get("title"):
        parts.append(f"Title: {res['title']}")
    if res.get("highlights"):
        parts.append("Highlights:\n- " + "\n- ".join(res["highlights"]))
    text = res.get("text") or ""
    if text:
        parts.append(f"Text:\n{text[:5000]}")
    return "\n\n".join(parts) if parts else "(empty)"


async def _raw_fetch(url: str) -> str:
    """Direct HTTP fetch + BeautifulSoup text extraction. Fallback path."""

    try:
        r = await _client().get(url)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return f"fetch error: {e!r}"

    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    except ImportError:
        # No bs4 — return raw text truncated.
        return r.text[:5000]

    soup = BeautifulSoup(r.text, "html.parser")
    for el in soup(["script", "style", "noscript"]):
        el.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    return text[:5000] if text else "(empty page)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_scheme(s: str) -> str:
    return s.replace("https://", "").replace("http://", "").strip("/")


def _sanitize_domains(domains: list[str] | None) -> list[str] | None:
    """Drop wildcards and empty strings — small models love to pass things
    like ``["*.com", "*.org"]`` which break exact-match domain filters."""

    if not domains:
        return None
    cleaned = [
        _strip_scheme(d).lstrip("*.")
        for d in domains
        if d and "*" not in d.replace("*.", "")
    ]
    cleaned = [c for c in cleaned if c]
    return cleaned or None


# ---------------------------------------------------------------------------
# Claude Agent SDK parity — `WebSearch` / `WebFetch` are the canonical names
# in the upstream SDK. Alias them so:
#
#   from mantis_agent import WebSearch, WebFetch
#
# works as a drop-in replacement for:
#
#   from claude_agent_sdk import WebSearch, WebFetch
# ---------------------------------------------------------------------------

WebSearch: Tool = web_search
WebFetch: Tool = web_fetch


__all__ = [
    "WebFetch",
    "WebSearch",
    "aclose_builtin_clients",
    "web_fetch",
    "web_search",
]
