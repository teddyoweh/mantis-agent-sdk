"""HTTP retry middleware for transient failures.

Production reality: every hosted OSS provider returns 429 (rate limit),
502/503/504 (transient infrastructure), and the occasional connection
reset under load. Without retries, a single bad minute on Together or
Fireworks crashes a whole research run.

This module provides ``RetryTransport`` — a thin ``httpx.AsyncBaseTransport``
wrapper that retries with exponential backoff. Plugged into
``make_client`` by default so every provider gets retries for free.

One retry authority per error class
-----------------------------------

The transport owns every failure that happens *before the first response-body
byte*; the engine (``Agent._stream_with_fallback``) owns stream-level failures
after that, plus the fallback-model switch. Nothing is retried by both layers:

* **Transport retries** HTTP 408, 425, 429, 500, 502, 503, 504, 529 and
  connect-shaped errors (``ConnectError`` / ``ConnectTimeout`` /
  ``WriteTimeout`` / ``PoolTimeout``, and ``RemoteProtocolError`` before the
  status line — usually a stale keep-alive socket). When it gives up, the
  surfaced error carries ``retried_by_transport = True`` and the engine goes
  straight to the fallback model (or raises) instead of retrying again.
* **Nobody retries a first-byte timeout.** A server that hasn't produced a
  byte within the window is wedged or drowning in prefill; sending the same
  prompt again only queues a second copy behind the first. It surfaces as
  :class:`FirstByteTimeout` (tagged, so the engine won't retry it either).
* **Engine retries** stream-level failures only: an empty / cut-off body
  (cold start), a connection dropped or a :class:`StreamIdleTimeout` mid-body.
  Before any content block that's a plain re-stream (``Agent.max_retries``,
  default 2); after one, it's the truncation-recovery path.

``Retry-After`` is honoured by the transport alone, once per attempt, up to
``MANTIS_AGENT_RETRY_AFTER_MAX_S`` (default 60s). A longer ask (``Retry-After:
3600``) is not slept on silently — the response surfaces at once: a 429 as a
``RateLimitError`` (with ``retry_after_s``), a 503/529 as a ``ProviderError``,
both naming the wait (:func:`status_error`).

Worst case for one model call with the defaults (4 attempts): 4 HTTP requests
for a connect / 429 / 5xx outage (~3.5s of backoff, or up to 3 x 60s when the
server sends ``Retry-After``); 1 request and ``FIRST_BYTE`` seconds for a
wedged server; at most 1 + ``max_retries`` streams for mid-body drops before
content.

Timeouts
--------

httpx's ``read`` timeout applies per socket read, so on a streamed response it
already *is* an idle timeout. What one number can't express is "the first byte
may take minutes (CPU prefill of a 30k-token prompt), but once tokens flow, a
three-minute silence means the server is stuck". The transport splits the two
for **streaming** requests (``"stream": true`` in the JSON body, an
``event-stream`` Accept header, a ``…stream…`` endpoint, or the
``mantis_streaming`` request extension):

* ``MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S`` (default 300) — from sending the request
  to the first response-body byte. It spans the header wait AND the first body
  read, so it holds both for servers that answer headers only after prefill
  (llama.cpp) and for those that send them at once (vLLM).
* ``MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S`` (default 900, or the value above
  when that is set explicitly) — the same bound for loopback servers (Ollama,
  llama.cpp, a local vLLM), where the first request also pays a cold model load
  from disk that can outlast five minutes on its own.
* ``MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S`` (default 180) — the longest silence
  between body chunks once the first one arrived. SSE keep-alive comments count
  as liveness. 180s rather than 120s leaves room for backends that reason
  silently between visible chunks. ``0`` disables any of the bounds.

A **non-streaming** request (``POST /responses`` for a high-effort reasoning
model, ``/api/show`` probes) has no separate first byte — its first byte IS the
whole generation — so the transport applies no first-byte bound to it; httpx's
own read timeout governs. The client default for that is generous
(``max(600, first-byte budgets)``). A per-request ``timeout=`` the caller set
explicitly always wins: a larger ``read`` raises the streaming first-byte bound
to match, ``timeout=None`` lifts it, and a short probe timeout surfaces as
httpx's own ``ReadTimeout``, not a :class:`FirstByteTimeout`.

A transport deadline that fires while the connection is still being opened
(nothing sent yet) is a connect failure — retried like ``ConnectTimeout``.

All requests are retried, including non-idempotent methods (POST): model-API
providers are idempotent at the application layer (the SDK issues no mutating
side effects), and a retry only fires before any body byte was consumed, so
there is no partially-read response to "replay".

Configurable via env so users can tune without code changes:

    MANTIS_AGENT_RETRY_ATTEMPTS=4
    MANTIS_AGENT_RETRY_BASE_S=0.5
    MANTIS_AGENT_RETRY_MAX_S=20.0
    MANTIS_AGENT_RETRY_AFTER_MAX_S=60
    MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S=300
    MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S=900
    MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S=180
"""
from __future__ import annotations

import logging
import os
import random
import re
from typing import Any

import anyio
import httpx

_LOG = logging.getLogger("mantis_agent.retry")

# UI status hook. When a TUI is running, raw log lines would tear through the
# prompt frame (and four wrapped WARNINGs per outage read as a crash), so the
# TUI sets ``notify`` to render retries as a single in-place status note
# instead. With a hook installed the log drops to DEBUG; headless keeps the
# WARNING lines. Payload: {host, reason, attempt, attempts, sleep_s}.
notify = None  # Callable[[dict], None] | None


def _friendly_reason(exc: Exception | None = None, status: int | None = None) -> str:
    if status is not None:
        return "rate limited (429)" if status == 429 else f"HTTP {status}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connection failed"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "timed out"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "connection dropped"
    return type(exc).__name__ if exc is not None else "error"


def _emit_retry(request: httpx.Request, reason: str, attempt: int,
                attempts: int, sleep_s: float) -> None:
    cb = notify
    if cb is not None:
        try:
            cb({"host": request.url.host, "reason": reason, "attempt": attempt,
                "attempts": attempts, "sleep_s": sleep_s})
        except Exception:  # noqa: BLE001 — a UI hook must never break retries
            pass
        _LOG.debug("request %s %s: %s; retry %d/%d in %.2fs",
                   request.method, request.url, reason, attempt, attempts, sleep_s)
    else:
        _LOG.warning("request %s %s failed (%s); retry %d/%d in %.2fs",
                     request.method, request.url, reason, attempt, attempts, sleep_s)


# Retryable status codes (per RFC 9110 + provider conventions; 529 is
# Anthropic's "overloaded").
_RETRY_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

# Network-level errors worth retrying: all of them happen before the server
# has started answering. ``ReadTimeout`` is deliberately absent — see
# FirstByteTimeout. (Mid-body errors never reach this loop at all: the
# transport returns once headers arrive and the body streams afterwards.)
_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

#: Response extension keys the transport sets on a retryable-status response
#: it hands back (``http.raise_for_status`` copies them onto the error).
RETRIED_KEY = "mantis_retried_by_transport"
RETRY_AFTER_REFUSED_KEY = "mantis_retry_after_refused_s"


class FirstByteTimeout(httpx.ReadTimeout):
    """No response-body byte within ``first_byte_s`` of sending the request.

    Never retried (by either layer): the server is wedged or still prefilling,
    and a second copy of the prompt would only queue behind the first."""


class StreamIdleTimeout(httpx.ReadTimeout):
    """The body stream went silent for ``idle_s`` after it had started.

    A stream-level failure — the engine re-streams it before any content block
    and treats it as a truncation after one."""


def mark_retried(err: BaseException) -> BaseException:
    """Tag ``err`` as already handled by the transport's retry policy, so the
    engine doesn't retry it a second time."""

    try:
        err.retried_by_transport = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a frozen/slotted exception: best-effort
        pass
    return err


def retried_by_transport(err: BaseException) -> bool:
    return bool(getattr(err, "retried_by_transport", False))


def status_error(response: httpx.Response, message: str, *, raw: Any = None) -> Exception:
    """The typed error for an HTTP error ``response``, carrying what the
    transport knows about it: ``status_code``, a :class:`RateLimitError` with
    ``retry_after_s`` for a 429, the "not waiting" note when a ``Retry-After``
    beyond ``MANTIS_AGENT_RETRY_AFTER_MAX_S`` was refused (429 or 5xx alike),
    and the ``retried_by_transport`` tag. Every provider's error mapping goes
    through here so the engine sees the same signals whatever the wire."""

    from .errors import AuthError, ProviderError, RateLimitError  # noqa: PLC0415

    status = response.status_code
    ext = getattr(response, "extensions", None) or {}
    refused = ext.get(RETRY_AFTER_REFUSED_KEY)
    if refused is not None:
        message += (f" — server asked to retry after {refused:.0f}s, longer than "
                    "MANTIS_AGENT_RETRY_AFTER_MAX_S allows; not waiting")
    err: ProviderError
    if status in (401, 403):
        return AuthError(message, status_code=status, raw=raw)
    if status == 429:
        err = RateLimitError(
            message, status_code=status,
            retry_after_s=_parse_retry_after(response.headers.get("retry-after")),
            raw=raw,
        )
    else:
        err = ProviderError(message, status_code=status, raw=raw)
    # The transport already retried (or deliberately declined to retry) this
    # status — tell the engine not to retry it again.
    if ext.get(RETRIED_KEY):
        mark_retried(err)
    return err


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def first_byte_timeout_s() -> float | None:
    """``MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S`` (default 300); ``<= 0`` → None (off)."""
    v = _env_float("MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S", 300.0)
    return v if v > 0 else None


def local_first_byte_timeout_s() -> float | None:
    """First-byte budget for loopback servers: ``MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S``
    if set, else an explicitly set ``MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S``, else 900
    (a cold Ollama / llama.cpp model load from disk comes before the first
    token). ``<= 0`` → None (off)."""
    if os.environ.get("MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S") is not None:
        v = _env_float("MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S", 900.0)
    elif os.environ.get("MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S") is not None:
        return first_byte_timeout_s()
    else:
        v = 900.0
    return v if v > 0 else None


def stream_idle_timeout_s() -> float | None:
    """``MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S`` (default 180); ``<= 0`` → None (off)."""
    v = _env_float("MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S", 180.0)
    return v if v > 0 else None


#: Floor for httpx's per-read timeout on a client built by ``make_client``:
#: what a non-streaming call (whose first byte is the whole generation) gets.
NON_STREAMING_READ_S = 600.0


def default_read_timeout_s() -> float | None:
    """The client-wide httpx ``read`` timeout: at least every first-byte budget
    and at least :data:`NON_STREAMING_READ_S`; None when any budget is off."""
    budgets = (first_byte_timeout_s(), local_first_byte_timeout_s())
    if any(b is None for b in budgets):
        return None
    return max(NON_STREAMING_READ_S, *budgets)  # type: ignore[type-var]


#: Request extension a provider may set to say "this request streams" (True) or
#: "it doesn't" (False), overriding the body / header / path sniffing below.
STREAMING_EXTENSION = "mantis_streaming"

# ``"stream": true`` as a JSON key — the lookbehind skips the escaped form a
# user message quoting it would carry.
_STREAM_FLAG = re.compile(rb'(?<!\\)"stream"\s*:\s*true')

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_streaming_request(request: httpx.Request) -> bool:
    """Whether ``request`` asks for a streamed response — the only kind with a
    first byte distinct from the whole answer."""

    flag = request.extensions.get(STREAMING_EXTENSION)
    if flag is not None:
        return bool(flag)
    accept = request.headers.get("accept", "").lower()
    if "event-stream" in accept or "eventstream" in accept:
        return True
    # Bedrock ``invoke-with-response-stream``, Vertex ``:streamRawPredict``.
    if "stream" in request.url.path.rsplit("/", 1)[-1].lower():
        return True
    try:
        body = request.content
    except httpx.RequestNotRead:  # a streamed upload — can't sniff; assume not
        return False
    return bool(body) and _STREAM_FLAG.search(body) is not None


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


class _WatchdogStream(httpx.AsyncByteStream):
    """Response body wrapper enforcing the first-byte deadline and, after the
    first chunk, the inter-chunk idle bound. Each ``fail_after`` scope wraps a
    single ``__anext__`` (never a ``yield``), so it is safe inside the
    generator; a cancelled read makes httpcore drop the connection."""

    def __init__(self, inner: httpx.AsyncByteStream, request: httpx.Request,
                 deadline: float | None, idle_s: float | None,
                 first_byte_s: float | None) -> None:
        self._inner = inner
        self._request = request
        self._deadline = deadline
        self._idle_s = idle_s
        self._first_byte_s = first_byte_s

    async def __aiter__(self):  # type: ignore[override]
        it = self._inner.__aiter__()
        started = False
        while True:
            if started:
                limit = self._idle_s
            elif self._deadline is not None:
                # Headers are in; a sliver of grace so headers landing right on
                # the deadline don't turn into a fail_after(0) on the first read.
                grace = min(5.0, 0.05 * (self._first_byte_s or 0.0))
                limit = max(grace, self._deadline - anyio.current_time())
            else:
                limit = None
            try:
                with anyio.fail_after(limit):
                    chunk = await it.__anext__()
            except StopAsyncIteration:
                return
            except httpx.ReadTimeout:
                # httpx's per-read bound is the caller's and propagates
                # untouched — unless it merely beat our own deadline to it.
                if (started or self._deadline is None
                        or anyio.current_time() < self._deadline - 1.0):
                    raise
                raise _first_byte_error(self._request, self._first_byte_s) from None
            except TimeoutError:
                # Our own deadline (anyio's TimeoutError).
                if started:
                    raise StreamIdleTimeout(
                        f"stream went silent for {self._idle_s:g}s mid-response "
                        "(MANTIS_AGENT_STREAM_IDLE_TIMEOUT_S)",
                        request=self._request,
                    ) from None
                raise _first_byte_error(self._request, self._first_byte_s) from None
            if chunk:
                started = True
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


def _first_byte_error(request: httpx.Request, first_byte_s: float | None) -> FirstByteTimeout:
    shown = f"{first_byte_s:g}s" if first_byte_s else "the read timeout"
    host = request.url.host or ""
    if _is_loopback(host):
        why = ("the server is stuck, still loading the model from disk (a cold "
               "load can take minutes), or still processing the prompt (raise "
               "MANTIS_AGENT_LOCAL_FIRST_BYTE_TIMEOUT_S)")
    else:
        why = ("the server is stuck, cold-starting the model, or still processing "
               "the prompt (raise MANTIS_AGENT_FIRST_BYTE_TIMEOUT_S for slow "
               "prefill or cold starts)")
    return mark_retried(FirstByteTimeout(  # type: ignore[return-value]
        f"no response from {host} within {shown} — {why}",
        request=request,
    ))


class _Phase:
    """Tracks, via httpcore's ``trace`` extension, whether the request left the
    client — so a transport deadline can tell "never connected" (retryable)
    from "sent, no answer" (a first-byte timeout). Chains any caller trace."""

    __slots__ = ("seen", "sent", "_chained")

    def __init__(self, chained: Any) -> None:
        self.seen = False
        self.sent = False
        self._chained = chained

    async def __call__(self, name: str, info: dict[str, Any]) -> None:
        self.seen = True
        if name.endswith("send_request_body.complete"):
            self.sent = True
        if self._chained is not None:
            await self._chained(name, info)

    @property
    def connecting(self) -> bool:
        # No events at all = a transport that doesn't trace (or a pool wait):
        # stay conservative and call it a first-byte timeout.
        return self.seen and not self.sent


_UNSET: Any = object()


class RetryTransport(httpx.AsyncBaseTransport):
    """Wraps another transport, replaying retryable failures that happen before
    the response body, and bounding time-to-first-byte and stream idleness."""

    __slots__ = ("_inner", "_attempts", "_base_s", "_max_s", "_jitter",
                 "_retry_after_max_s", "_first_byte_s", "_local_first_byte_s",
                 "_idle_s", "_default_read_s")

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        attempts: int | None = None,
        base_s: float | None = None,
        max_s: float | None = None,
        jitter: bool = True,
        retry_after_max_s: float | None = None,
        first_byte_s: float | None = -1.0,
        idle_s: float | None = -1.0,
        local_first_byte_s: float | None = -1.0,
        default_read_s: float | None = _UNSET,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(http2=True, retries=0)
        self._attempts = max(1, attempts if attempts is not None else _env_int(
            "MANTIS_AGENT_RETRY_ATTEMPTS", 4
        ))
        self._base_s = base_s if base_s is not None else _env_float(
            "MANTIS_AGENT_RETRY_BASE_S", 0.5
        )
        self._max_s = max_s if max_s is not None else _env_float(
            "MANTIS_AGENT_RETRY_MAX_S", 20.0
        )
        self._retry_after_max_s = (
            retry_after_max_s if retry_after_max_s is not None
            else _env_float("MANTIS_AGENT_RETRY_AFTER_MAX_S", 60.0)
        )
        # -1 = "read the env"; None / <= 0 = off. An explicit first_byte_s with
        # no explicit local budget applies to loopback hosts too.
        if local_first_byte_s is not None and local_first_byte_s < 0:
            local_first_byte_s = (
                local_first_byte_timeout_s()
                if first_byte_s is None or first_byte_s < 0 else first_byte_s
            )
        if first_byte_s is not None and first_byte_s < 0:
            first_byte_s = first_byte_timeout_s()
        if idle_s is not None and idle_s < 0:
            idle_s = stream_idle_timeout_s()
        self._first_byte_s = first_byte_s or None
        self._local_first_byte_s = local_first_byte_s or None
        self._idle_s = idle_s or None
        # The client-wide httpx read timeout, so a per-request ``timeout=`` the
        # caller set can be told apart from it (``_UNSET``: unknown — ignore
        # per-request reads).
        self._default_read_s = default_read_s
        self._jitter = jitter

    def _first_byte_budget(self, request: httpx.Request) -> float | None:
        """The first-byte bound for this request, or None for no bound."""

        if not is_streaming_request(request):
            return None  # its first byte is the whole answer — httpx's read governs
        host = request.url.host or ""
        budget = self._local_first_byte_s if _is_loopback(host) else self._first_byte_s
        if budget is None or self._default_read_s is _UNSET:
            return budget
        timeout = request.extensions.get("timeout")
        if not isinstance(timeout, dict) or "read" not in timeout:
            return budget
        read = timeout["read"]
        if read == self._default_read_s:
            return budget
        # The caller set a read timeout for this request: None lifts the bound,
        # a larger one stretches it (a smaller one fires in httpx on its own).
        return None if read is None else max(budget, read)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last_response: httpx.Response | None = None
        last_exc: Exception | None = None
        first_byte_s = self._first_byte_budget(request)
        caller_trace = request.extensions.get("trace")

        for attempt in range(self._attempts):
            deadline = (anyio.current_time() + first_byte_s
                        if first_byte_s else None)
            phase = _Phase(caller_trace)
            if first_byte_s:
                request.extensions["trace"] = phase
            try:
                try:
                    with anyio.fail_after(first_byte_s):
                        response = await self._inner.handle_async_request(request)
                except httpx.ReadTimeout:
                    # httpx's own per-read bound. Only ours if it fired on (or
                    # past) our deadline; a caller's short probe timeout is theirs.
                    if deadline is None or anyio.current_time() < deadline - 1.0:
                        raise
                    raise _first_byte_error(request, first_byte_s) from None
                except TimeoutError:
                    if not phase.connecting:
                        # Sent, headers never came. Not retried — see FirstByteTimeout.
                        raise _first_byte_error(request, first_byte_s) from None
                    # Our deadline fired while still connecting: nothing was
                    # sent, so it's a connect failure like any other.
                    raise httpx.ConnectTimeout(
                        f"could not connect to {request.url.host} within "
                        f"{first_byte_s:g}s", request=request,
                    ) from None
            except _RETRY_EXCEPTIONS as e:
                last_exc = e
                # On the final attempt there is no retry to wait for, so skip
                # the backoff sleep entirely — otherwise we'd burn up to max_s
                # of dead latency before surfacing the failure.
                last_attempt = attempt + 1 >= self._attempts
                sleep_for = 0.0 if last_attempt else self._backoff_seconds(attempt)
                _emit_retry(request, _friendly_reason(exc=e),
                            attempt + 1, self._attempts, sleep_for)
                if last_attempt:
                    break
                await anyio.sleep(sleep_for)
                continue
            finally:
                if first_byte_s:
                    if caller_trace is None:
                        request.extensions.pop("trace", None)
                    else:
                        request.extensions["trace"] = caller_trace

            response.stream = _WatchdogStream(
                response.stream, request, deadline, self._idle_s, first_byte_s,
            )
            if response.status_code not in _RETRY_STATUSES:
                return response
            # A retryable status. Whatever happens next, the transport's policy
            # has been applied to it — the engine must not retry it again.
            response.extensions[RETRIED_KEY] = True
            if attempt + 1 >= self._attempts:
                return response
            # Honor Retry-After if present (seconds or HTTP-date; RFC 7231) —
            # but only up to retry_after_max_s. A longer ask is surfaced as an
            # error now rather than hanging the turn silently for an hour.
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if retry_after is not None and retry_after > self._retry_after_max_s:
                response.extensions[RETRY_AFTER_REFUSED_KEY] = retry_after
                return response
            sleep_for = (
                retry_after if retry_after is not None
                else self._backoff_seconds(attempt)
            )
            _emit_retry(request, _friendly_reason(status=response.status_code),
                        attempt + 1, self._attempts, sleep_for)
            # Drain the response body before retrying so the connection
            # can be reused (httpx pools won't release otherwise).
            try:
                await response.aread()
                await response.aclose()
            except Exception:  # noqa: BLE001
                pass
            last_response = response
            await anyio.sleep(sleep_for)

        # Exhausted attempts. Surface the most-recent signal we have.
        if last_exc is not None:
            raise mark_retried(last_exc)
        assert last_response is not None  # at least one iteration ran
        return last_response

    async def aclose(self) -> None:
        await self._inner.aclose()

    # ------------------------------------------------------------------

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with optional jitter."""

        base = self._base_s * (2 ** attempt)
        capped = min(base, self._max_s)
        if self._jitter:
            # Decorrelated jitter — small random fraction so swarms of
            # callers don't thunder past the same retry-after window.
            capped += random.uniform(0, capped * 0.25)
        return capped


def _parse_retry_after(header: str | None) -> float | None:
    """Parse the ``Retry-After`` header (RFC 7231): a delay in seconds, or an
    HTTP-date to wait until. Returns the number of seconds to sleep, or None if
    absent/unparseable. Most model APIs use the seconds form; the HTTP-date form
    shows up behind some proxies/gateways."""

    if not header:
        return None
    h = header.strip()
    try:
        v = float(h)
        return v if v >= 0 else None
    except ValueError:
        pass
    # HTTP-date form → seconds from now (clamped at 0, never negative).
    try:
        from datetime import datetime, timezone  # noqa: PLC0415
        from email.utils import parsedate_to_datetime  # noqa: PLC0415

        when = parsedate_to_datetime(h)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


__all__ = [
    "FirstByteTimeout",
    "RetryTransport",
    "STREAMING_EXTENSION",
    "StreamIdleTimeout",
    "default_read_timeout_s",
    "first_byte_timeout_s",
    "is_streaming_request",
    "local_first_byte_timeout_s",
    "mark_retried",
    "retried_by_transport",
    "status_error",
    "stream_idle_timeout_s",
]
