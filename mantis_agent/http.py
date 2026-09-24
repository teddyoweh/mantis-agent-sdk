"""Shared HTTP client + streaming SSE parser.

Why one shared client
---------------------
Constructing ``httpx.AsyncClient`` per request costs a TLS handshake and a
fresh connection pool. The Anthropic API typically wants HTTP/2 + keep-alive;
re-establishing per call wastes 50–150 ms on every invocation.

We expose a *factory*, not a singleton — each ``Agent`` instance owns its
client so tests don't share state and event loops don't leak across agents.
The exception is ``query()``: inside :func:`sharing_http_clients` the factory
returns a loop-scoped pooled client, so repeated ``query()`` calls on one
event loop keep their warm connections (see "Shared clients" below).

SSE parser
----------
``httpx`` gives us ``aiter_lines()`` which is already line-framed. We don't
need to buffer the full body — just walk lines, accumulate event fields,
yield when a blank line closes the event. Memory cost is one event's worth
of bytes.
"""

from __future__ import annotations

import atexit
import weakref
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx
import msgspec

from .errors import (  # noqa: F401 — error types kept importable from here
    AuthError,
    ProviderError,
    RateLimitError,
    StreamProtocolError,
)


DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,
    # httpx's read timeout is per socket read — on a stream, an idle bound; on a
    # non-streaming call, the whole generation. It is at least every first-byte
    # budget (so CPU prefill / a cold local model load isn't killed) and at
    # least 600s for non-streaming calls; RetryTransport enforces the tighter
    # first-byte and inter-chunk idle bounds on streams. See mantis_agent.retry
    # ("Timeouts").
    read=900.0,
    write=10.0,
    pool=10.0,
)


def default_timeout() -> httpx.Timeout:
    """``DEFAULT_TIMEOUT`` with ``read`` derived from the first-byte env budgets
    (:func:`mantis_agent.retry.default_read_timeout_s`; a ``0`` budget = no read
    bound). Read at client-build time so env overrides apply."""

    from .retry import default_read_timeout_s  # noqa: PLC0415

    return httpx.Timeout(connect=10.0, read=default_read_timeout_s(), write=10.0, pool=10.0)


def make_client(
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    http2: bool = True,
    max_connections: int = 100,
    max_keepalive_connections: int = 20,
    retries: bool = True,
) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient tuned for streaming model APIs.

    ``retries=True`` (default) wraps the transport in
    :class:`mantis_agent.retry.RetryTransport`: backoff retries on 429/5xx and
    connect errors (``Retry-After`` honoured up to a cap), a time-to-first-byte
    bound and an inter-chunk idle watchdog. Set to ``False`` for tests where
    you want deterministic failures — then only httpx's own per-read timeout
    (the first-byte budget) applies.
    """

    if timeout is None:
        timeout = default_timeout()
    share_key: tuple[Any, ...] | None = None
    if _SHARE.get():
        share_key = (
            base_url, tuple(sorted((headers or {}).items())),
            (timeout.connect, timeout.read, timeout.write, timeout.pool),
            http2, max_connections, max_keepalive_connections, retries,
            _retry_env_key() if retries else (),
        )
        pooled = _pooled(share_key)
        if pooled is not None:
            return pooled
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
            # So a per-request ``timeout=`` can be told from the client default.
            default_read_s=timeout.read,
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

    if share_key is not None:
        client: httpx.AsyncClient = _SharedAsyncClient(**kwargs)
        _remember(share_key, client)
        return client
    return httpx.AsyncClient(
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Shared clients for query()
# ---------------------------------------------------------------------------
#
# Every ``query()`` builds a fresh Agent → provider → AsyncClient, so each call
# used to pay a new TCP (+TLS) handshake and an empty keep-alive pool. Inside
# :func:`sharing_http_clients` ``make_client`` instead hands out one pooled
# client per (event loop, base_url, headers, timeout, limits): back-to-back
# ``query()`` calls on the same loop reuse the warm connections. Keyed by loop
# because an httpx pool is bound to the loop that opened its sockets — a new
# ``asyncio.run()`` gets new clients, and a loop's clients drop with it.
# Borrowers can't close a pooled client (``aclose``/``__aexit__`` are no-ops);
# :func:`aclose_shared_clients`, the loop-shutdown sentinel and the
# interpreter-exit hook do.
#
# A pooled client (via its sockets) strongly refs its loop, so the weak loop
# key alone would never drop: every ``asyncio.run(query(...))`` would leak a
# loop + client + sockets. Two things prevent that: a per-loop sentinel async
# generator that ``asyncio.run()``'s ``shutdown_asyncgens()`` finalizes — it
# closes that loop's clients while the loop can still run them — and a prune
# of closed loops' entries on every pool lookup (a loop closed by hand).
# The retry/first-byte env knobs are part of the key, so changing them between
# queries takes effect instead of reusing a transport built with the old ones.

_RETRY_ENV_VARS = (
    "MANTIS_AGENT_RETRY_ATTEMPTS", "MANTIS_AGENT_RETRY_BASE_S",
    "MANTIS_AGENT_RETRY_MAX_S", "MANTIS_AGENT_RETRY_AFTER_MAX_S",
    "MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S", "MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S",
    "MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S",
)


def _retry_env_key() -> tuple[str | None, ...]:
    import os  # noqa: PLC0415

    return tuple(os.environ.get(k) for k in _RETRY_ENV_VARS)

_SHARE: ContextVar[bool] = ContextVar("mantis_share_http_clients", default=False)
_POOLS: "weakref.WeakKeyDictionary[Any, dict[tuple[Any, ...], httpx.AsyncClient]]" = (
    weakref.WeakKeyDictionary()
)
_SENTINELS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


class _SharedAsyncClient(httpx.AsyncClient):
    """A pooled client: borrowers' ``aclose()`` / ``async with`` don't close it."""

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> "_SharedAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def _close_for_real(self) -> None:
        await httpx.AsyncClient.aclose(self)


@contextmanager
def sharing_http_clients() -> Iterator[None]:
    """While active, :func:`make_client` returns loop-scoped pooled clients."""

    token = _SHARE.set(True)
    try:
        yield
    finally:
        _SHARE.reset(token)


def _current_loop() -> Any:
    import asyncio  # noqa: PLC0415

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _prune_closed_loops() -> None:
    """Drop pools whose loop is closed. Their sockets' transports died with
    the loop; what's left is references, and dropping them frees the loop."""
    for loop in [lp for lp in list(_POOLS.keys()) if lp.is_closed()]:
        pool = _POOLS.pop(loop, None)
        if pool:
            pool.clear()
        _finish_sentinel(_SENTINELS.pop(loop, None))


def _finish_sentinel(agen: Any) -> None:
    """Run a sentinel's (now pool-less, so await-free) finally synchronously,
    so the GC finalizer never tries to schedule it on a closed loop."""
    if agen is None:
        return
    try:
        agen.aclose().send(None)
    except BaseException:  # noqa: BLE001 — StopIteration is the normal exit
        pass


async def _pool_sentinel(loop_ref: Any) -> AsyncIterator[None]:
    """Parked at its yield for the loop's lifetime; ``shutdown_asyncgens()``
    (``asyncio.run`` / ``asyncio.Runner`` teardown) closes it, and that closes
    the loop's pooled clients while the loop can still run their teardown."""
    try:
        yield
    finally:
        loop = loop_ref()
        pool = _POOLS.pop(loop, None) if loop is not None else None
        if loop is not None:
            _SENTINELS.pop(loop, None)
        for client in list((pool or {}).values()):
            try:
                await client._close_for_real()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass


def _arm_sentinel(loop: Any) -> None:
    if loop in _SENTINELS:
        return
    agen = _pool_sentinel(weakref.ref(loop))
    try:
        # asend() registers it with the running loop's asyncgen hooks; one
        # send() drives it to its yield (no awaits before it).
        agen.asend(None).send(None)
    except StopIteration:
        pass
    except Exception:  # noqa: BLE001 — no sentinel: prune/atexit still cover it
        return
    _SENTINELS[loop] = agen


def _pooled(key: tuple[Any, ...]) -> httpx.AsyncClient | None:
    _prune_closed_loops()
    loop = _current_loop()
    if loop is None:
        return None
    client = _POOLS.get(loop, {}).get(key)
    if client is None or client.is_closed:
        return None
    return client


def _remember(key: tuple[Any, ...], client: httpx.AsyncClient) -> None:
    loop = _current_loop()
    if loop is None:
        return  # no loop to scope it to — it behaves as a private client
    _prune_closed_loops()
    try:
        _POOLS.setdefault(loop, {})[key] = client
        _arm_sentinel(loop)
    except TypeError:  # pragma: no cover — a loop type without weakref support
        pass


async def aclose_shared_clients() -> None:
    """Close every pooled client that belongs to the running loop."""

    loop = _current_loop()
    pool = _POOLS.pop(loop, None) if loop is not None else None
    for client in list((pool or {}).values()):
        try:
            await client._close_for_real()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


def _close_pools_at_exit() -> None:
    """Interpreter exit: close pooled clients whose loop can still run. A loop
    that's already closed took its sockets' transports down with it."""

    for loop, pool in list(_POOLS.items()):
        clients = list(pool.values())
        pool.clear()
        if not clients or loop.is_closed() or loop.is_running():
            continue

        async def _close_all(cs: list[Any] = clients) -> None:
            for c in cs:
                try:
                    await c._close_for_real()
                except Exception:  # noqa: BLE001
                    pass

        try:
            loop.run_until_complete(_close_all())
        except Exception:  # noqa: BLE001 — never raise during interpreter exit
            pass


atexit.register(_close_pools_at_exit)




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
    msg = f"{msg}{_where(response, status)}"

    # Typed by status (AuthError / RateLimitError / ProviderError) and carrying
    # the transport's signals: retry_after_s, the refused-Retry-After note, and
    # the retried_by_transport tag.
    from .retry import status_error  # noqa: PLC0415

    raise status_error(response, msg, raw=body)


#: Ports that mean "you're pointed at the wrong local server", with the thing
#: to check. A bare ``Not Found`` against localhost is the single most
#: time-consuming error in this SDK: it's what you get when a bare model name
#: falls through to the openai_compat default and nothing is listening, and
#: nothing in the message says which door was knocked on.
_LOCAL_HINTS: dict[str, str] = {
    "8000": "the vLLM default — nothing is serving there unless you started it",
    "11434": "Ollama — is the daemon running, and is the model pulled? (`ollama list`)",
    "8080": "llama.cpp — is `llama-server` running?",
    "3000": "TGI — is the server running?",
}


def _where(response: httpx.Response, status: int) -> str:
    """`` (404 from http://host/path)`` — plus a hint for the local cases.

    The query string is dropped: some providers (Gemini among them) carry the
    API key there, and an error message is exactly the string that ends up in
    a log, a bug report, or a screenshot.
    """

    try:
        url = response.request.url
    except (AttributeError, RuntimeError):  # pragma: no cover — defensive
        return f" ({status})"
    shown = f"{url.scheme}://{url.netloc.decode('ascii', 'replace')}{url.path}"
    out = f" ({status} from {shown})"

    host = url.host or ""
    if status == 404 and host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        hint = _LOCAL_HINTS.get(str(url.port or ""))
        if hint:
            out += f" — port {url.port} is {hint}"
    return out


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
