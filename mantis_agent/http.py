"""Shared HTTP client + streaming SSE parser.

Why one shared client
---------------------
Constructing ``httpx.AsyncClient`` per request costs a TLS handshake and a
fresh connection pool. The Anthropic API typically wants HTTP/2 + keep-alive;
re-establishing per call wastes 50–150 ms on every invocation.

We expose a *factory*, not a singleton — each ``Agent`` instance owns its
client so tests don't share state and event loops don't leak across agents.

SSE parser
----------
``httpx`` gives us ``aiter_lines()`` which is already line-framed. We don't
need to buffer the full body — just walk lines, accumulate event fields,
yield when a blank line closes the event. Memory cost is one event's worth
of bytes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import msgspec

from .errors import (
    AuthError,
    ProviderError,
    RateLimitError,
    StreamProtocolError,
)


DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=600.0,  # generous for long completions; per-stream read keeps connections alive
    write=10.0,
    pool=10.0,
)


def make_client(
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    http2: bool = True,
    max_connections: int = 100,
    max_keepalive_connections: int = 20,
    retries: bool = True,
) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient tuned for streaming model APIs.

    ``retries=True`` (default) wraps the transport in
    :class:`mantis_agent.retry.RetryTransport` so every provider gets
    exponential-backoff retries on 429/5xx + connect/read timeouts +
    ``Retry-After`` honoring. Set to ``False`` for tests where you want
    deterministic failures.
    """

    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=60.0,
    )
    transport: httpx.AsyncBaseTransport | None = None
    if retries:
        from .retry import RetryTransport  # local import: optional fast path

        transport = RetryTransport(
            httpx.AsyncHTTPTransport(http2=http2, retries=0, limits=limits),
        )

    kwargs: dict[str, Any] = dict(
        base_url=base_url,
        headers=headers or {},
        timeout=timeout,
        limits=limits,
    )
    if transport is not None:
        kwargs["transport"] = transport
    else:
        # No retry middleware — let httpx manage the transport itself.
        kwargs["http2"] = http2

    return httpx.AsyncClient(
        **kwargs,
    )




# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def raise_for_status(response: httpx.Response, *, body: dict[str, Any] | None = None) -> None:
    """Map an HTTP error response to a typed AgentError. ``body`` is the
    parsed JSON body if the caller already has it; otherwise we attempt
    one parse (best-effort)."""

    status = response.status_code
    if status < 400:
        return

    if body is None:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 — best-effort
            body = {"raw": response.text[:512]}

    msg = _extract_error_message(body) or response.reason_phrase or "provider error"

    if status == 429:
        retry_after = response.headers.get("retry-after")
        try:
            retry_after_s = float(retry_after) if retry_after else None
        except ValueError:
            retry_after_s = None
        raise RateLimitError(msg, status_code=status, retry_after_s=retry_after_s, raw=body)
    if status in (401, 403):
        raise AuthError(msg, status_code=status, raw=body)
    raise ProviderError(msg, status_code=status, raw=body)


def _extract_error_message(body: dict[str, Any]) -> str | None:
    """Anthropic, OpenAI, and Gemini all bury the message slightly
    differently. Try the common paths."""

    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("type")
    if isinstance(err, str):
        return err
    return body.get("message")


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------


_JSON_DECODER = msgspec.json.Decoder()


async def iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, data_dict)`` pairs from an SSE response.

    Implementation notes
    --------------------
    * ``httpx.aiter_lines`` handles the underlying TCP framing and gives us
      already-decoded UTF-8 strings, one logical SSE line per yield.
    * We accumulate ``event:`` and ``data:`` fields, yielding when we hit a
      blank line (the SSE event terminator).
    * Multi-line ``data:`` is concatenated with ``\\n`` per the spec.
    * Comments (lines starting with ``:``) are ignored.
    * The data payload is decoded with msgspec; bad JSON raises
      ``StreamProtocolError`` rather than silently dropping events.
    """

    event_name: str | None = None
    data_chunks: list[str] = []

    async for line in response.aiter_lines():
        if line == "":
            # End of event — emit if we have data.
            if data_chunks:
                payload = "\n".join(data_chunks)
                try:
                    data = _JSON_DECODER.decode(payload)
                except msgspec.DecodeError as e:
                    raise StreamProtocolError(
                        f"bad JSON in SSE event {event_name!r}: {payload[:200]}"
                    ) from e
                yield (event_name or "message", data)
            event_name = None
            data_chunks = []
            continue

        if line.startswith(":"):
            # SSE comment — keepalive ping etc. Ignore.
            continue

        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            # Spec says strip one leading space if present.
            chunk = line[5:]
            if chunk.startswith(" "):
                chunk = chunk[1:]
            data_chunks.append(chunk)
        # Other fields (id:, retry:) are ignored — model APIs don't use them.

    # Trailing event if the server didn't send a final blank line.
    if data_chunks:
        payload = "\n".join(data_chunks)
        try:
            data = _JSON_DECODER.decode(payload)
        except msgspec.DecodeError as e:
            raise StreamProtocolError(f"bad JSON in trailing SSE event: {payload[:200]}") from e
        yield (event_name or "message", data)
