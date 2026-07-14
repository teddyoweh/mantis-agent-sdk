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

import asyncio
import html as html_mod
import ipaddress
import logging
import os
import re
import socket
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

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
    """Close the shared HTTP client and terminate any background shells. Called
    from ``Agent.aclose()`` so users don't have to remember — a detached dev
    server started by the agent shouldn't outlive the session."""
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
        _CLIENT = None
    try:
        from .fs import terminate_background_shells  # noqa: PLC0415
        terminate_background_shells()
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        pass


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


def _host_matches(host: str, entry: str) -> bool:
    """True if ``host`` equals ``entry`` or is a subdomain of it.

    Matches on real DNS-label boundaries (host == entry or
    host.endswith('.' + entry)) so an allow/block entry can never be
    satisfied by an unrelated host that merely *contains* the string
    (e.g. ``corp.com`` must not match ``corp.com.evil.net``).
    """

    entry = _strip_scheme(entry.lower().strip())
    if not entry:
        return False
    return host == entry or host.endswith("." + entry)


def _domain_filter(host_url: str, allowed: list[str] | None, blocked: list[str] | None) -> bool:
    """Decide whether a result URL passes the allow/block filters.

    Both lists treat each entry as a domain or domain suffix. Empty / None
    lists are no-ops. Blocked wins over allowed. Matching is on the parsed
    host with proper label boundaries — never a raw substring of the URL.
    """

    if not (allowed or blocked):
        return True
    # Callers pass a full URL; match on its host only, not path/query.
    host = (urlparse(host_url).hostname or host_url).lower()
    if blocked:
        for b in blocked:
            if _host_matches(host, b):
                return False
    if allowed:
        for a in allowed:
            if _host_matches(host, a):
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


_DDG_LINK_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE
)
_DDG_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")


def _ddg_text(fragment: str) -> str:
    return html_mod.unescape(_TAG_RE.sub("", fragment)).strip()


def _ddg_real_url(href: str) -> str:
    """DDG wraps result links in a ``/l/?uddg=<encoded real url>`` redirector —
    unwrap it so callers get the actual destination."""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        q = parse_qs(urlparse(href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href


async def _ddg_search(
    query: str,
    *,
    allowed: list[str] | None,
    blocked: list[str] | None,
    num: int = 8,
) -> str:
    """DuckDuckGo — no key, free, dependency-free (stdlib HTML parsing). Tries
    the html endpoint, then the lite endpoint if it comes back empty (DDG rate-
    limits / A-B-tests markup, so a second surface improves reliability)."""

    for base in (
        "https://html.duckduckgo.com/html/?q=",
        "https://lite.duckduckgo.com/lite/?q=",
    ):
        try:
            r = await _client().get(base + quote_plus(query))
            r.raise_for_status()
        except httpx.HTTPError:
            continue

        rows: list[str] = []
        titles = _DDG_LINK_RE.findall(r.text)
        snippets = _DDG_SNIPPET_RE.findall(r.text)
        for i, (href, title_html) in enumerate(titles):
            real = _ddg_real_url(href)
            if not real.startswith("http") or "duckduckgo.com" in real:
                continue
            if not _domain_filter(real, allowed, blocked):
                continue
            title = _ddg_text(title_html)
            snip = _ddg_text(snippets[i]) if i < len(snippets) else ""
            rows.append(f"{title} — {real} — {snip[:280]}")
            if len(rows) >= num:
                break
        if rows:
            return "\n".join(rows)

    return (
        "no results (DuckDuckGo returned nothing — it may be rate-limiting). "
        "For reliable search set EXA_API_KEY, BRAVE_API_KEY, or TAVILY_API_KEY."
    )


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


_DROP_BLOCK_RE = re.compile(
    r"<(script|style|noscript|head|svg|template|iframe)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_BLOCK_CLOSE_RE = re.compile(
    r"</(p|div|section|article|h[1-6]|li|tr|table|ul|ol|header|footer|nav|blockquote|pre)\s*>",
    re.IGNORECASE,
)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a\s*>',
                      re.DOTALL | re.IGNORECASE)
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li\s*>", re.DOTALL | re.IGNORECASE)


def _strip_inner(t: str) -> str:
    return re.sub(r"[ \t]+", " ", _TAG_RE.sub("", t)).strip()


def _html_to_markdown(html: str) -> str:
    """HTML → readable MARKDOWN, stdlib only (no BeautifulSoup). Preserves the
    structure a model can navigate — headings (``#``), links (``[text](url)``),
    and list items (``- ``) — then drops remaining tags, unescapes entities, and
    collapses blank runs. Far more useful to the model than flat text."""
    s = _DROP_BLOCK_RE.sub(" ", html)
    # Links first, so a link nested in a heading/list becomes [text](url) there.
    s = _LINK_RE.sub(lambda m: f"[{_strip_inner(m.group(2))}]({m.group(1).strip()})", s)
    s = _HEADING_RE.sub(
        lambda m: f"\n\n{'#' * int(m.group(1))} {_strip_inner(m.group(2))}\n\n", s)
    s = _LI_RE.sub(lambda m: f"\n- {_strip_inner(m.group(1))}", s)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_CLOSE_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# Back-compat alias (the pre-markdown name).
_html_to_text = _html_to_markdown


# ---------------------------------------------------------------------------
# SSRF protection — refuse to fetch loopback / link-local / private / reserved
# addresses (cloud metadata at 169.254.169.254, internal admin services, etc.).
# This SDK is built to drive untrusted models, so a prompt-injected page must
# not be able to make web_fetch reach into the host's private network. Fails
# CLOSED: anything we can't confirm is a public address is rejected. Set
# ``MANTIS_WEB_ALLOW_LOCAL=1`` to opt into fetching non-public addresses.
# ---------------------------------------------------------------------------

_ALLOW_LOCAL_ENV = "MANTIS_WEB_ALLOW_LOCAL"
_MAX_REDIRECTS = 5


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that must not be reachable from a web_fetch."""

    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # IPv4-mapped IPv6 (::ffff:169.254.169.254) — unwrap and re-check.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_is_blocked(mapped)
    return False


async def _vet_url(url: str) -> tuple[str | None, str | None]:
    """Validate ``url`` and pin a vetted address.

    Returns ``(error, pinned_ip)``:

    * ``error`` is a non-``None`` string when the URL is unsafe (non-http(s)
      scheme, or a host that resolves / is a literal for a loopback /
      link-local / private / reserved address).
    * ``pinned_ip`` is the concrete public IP the caller MUST connect to.
      Pinning it into the connection closes the TOCTOU / DNS-rebinding gap:
      without it httpx would re-resolve the host independently at connect
      time, so a low-TTL record that flips public→private after this check
      would slip past the denylist.

    ``pinned_ip`` is ``None`` when ``MANTIS_WEB_ALLOW_LOCAL`` is set (the guard
    is disabled and the caller should fetch the URL as-is) or when every
    resolved address is rejected (in which case ``error`` is also set).
    """

    if os.environ.get(_ALLOW_LOCAL_ENV):
        return None, None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"fetch error: refusing non-http(s) URL scheme {parsed.scheme!r}", None
    host = parsed.hostname
    if not host:
        return "fetch error: URL has no host", None

    # Host given as an IP literal — check it directly and pin it.
    try:
        literal = ipaddress.ip_address(host)
        if _ip_is_blocked(literal):
            return "fetch error: refusing to fetch non-public address", None
        return None, str(literal)
    except ValueError:
        pass  # not a literal — resolve below

    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, parsed.port or 0)
    except (OSError, socket.gaierror) as e:
        return f"fetch error: cannot resolve host ({e!r})", None
    if not infos:
        return "fetch error: cannot resolve host", None
    pinned: str | None = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return "fetch error: refusing to fetch non-public address", None
        if _ip_is_blocked(ip):
            return "fetch error: refusing to fetch non-public address", None
        if pinned is None:
            pinned = str(ip)
    return None, pinned


async def _pinned_get(
    client: httpx.AsyncClient, url: str, pinned_ip: str | None
) -> httpx.Response:
    """GET ``url`` but connect to ``pinned_ip`` (the address ``_vet_url``
    validated) instead of letting httpx re-resolve the hostname.

    When ``pinned_ip`` is ``None`` (``MANTIS_WEB_ALLOW_LOCAL`` set) the URL is
    fetched as-is. Otherwise the request targets the IP literal while the
    original ``Host`` header and TLS SNI/cert hostname are preserved, so name-
    based virtual hosting and certificate verification still work."""

    if pinned_ip is None:
        return await client.get(url, follow_redirects=False)

    parsed = httpx.URL(url)
    hostpart = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    ip_url = parsed.copy_with(host=hostpart)
    host_header = parsed.host if parsed.port is None else f"{parsed.host}:{parsed.port}"
    return await client.get(
        ip_url,
        follow_redirects=False,
        headers={"Host": host_header},
        extensions={"sni_hostname": parsed.host},
    )


async def _raw_fetch(url: str) -> str:
    """Direct HTTP fetch + dependency-free text extraction (the default path when
    Exa isn't configured). HTML is cleaned to readable text; non-HTML bodies
    (JSON, plain text, markdown, source) are returned verbatim so nothing useful
    is mangled.

    SSRF-hardened: the target and every redirect hop is validated against the
    non-public-address denylist before the request is issued, redirects are
    followed manually (not by httpx) so a public URL can't bounce to an
    internal one, and the request is connected to the exact IP that was vetted
    (not the hostname) so a DNS record can't flip public→private between the
    check and the connect (TOCTOU / DNS rebinding)."""

    err, pinned_ip = await _vet_url(url)
    if err:
        return err

    client = _client()
    current = url          # logical URL (keeps the hostname for redirects/Host)
    r: httpx.Response | None = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            r = await _pinned_get(client, current, pinned_ip)
            if not r.is_redirect:
                break
            loc = r.headers.get("location")
            if not loc:
                break
            # Join relative redirects against the logical hostname URL, not the
            # IP we happened to connect to.
            current = str(httpx.URL(current).join(loc))
            hop_err, pinned_ip = await _vet_url(current)
            if hop_err:
                return hop_err
        else:
            return "fetch error: too many redirects"
        r.raise_for_status()
    except httpx.HTTPError as e:
        return f"fetch error: {e!r}"

    ctype = r.headers.get("content-type", "").lower()
    body = r.text
    is_html = "html" in ctype or (not ctype and "<html" in body[:2000].lower())
    if is_html:
        text = _html_to_markdown(body)
        return text[:5000] if text else "(empty page)"
    return body[:5000] if body else "(empty)"


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
