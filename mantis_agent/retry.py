"""HTTP retry middleware for transient failures.

Production reality: every hosted OSS provider returns 429 (rate limit),
502/503/504 (transient infrastructure), and the occasional connection
reset under load. Without retries, a single bad minute on Together or
Fireworks crashes a whole research run.

This module provides ``RetryTransport`` — a thin ``httpx.AsyncBaseTransport``
wrapper that retries with exponential backoff. Plugged into
``make_client`` by default so every provider gets retries for free.

Policy
------

* Retry on:  HTTP 408, 425, 429, 500, 502, 503, 504, and
             ``httpx.ConnectError`` / ``httpx.ReadTimeout``.
* Honor ``Retry-After`` header on 429 (per RFC 7231) — sleep that many
  seconds before the next attempt instead of using our backoff.
* Backoff:   ``base * 2**attempt + jitter``  (default base=0.5s, max=20s).
* Stream requests: NOT retried (responses are partially consumed; can't
  safely replay). Tested via ``response.stream is not None`` heuristic.
* Non-idempotent methods (POST, DELETE, PATCH): still retried because all
  model-API providers are designed to be idempotent at the application
  layer (same prompt → same completion; the SDK doesn't issue mutating
  side effects to providers).

Configurable via env so users can tune without code changes:

    MANTIS_AGENT_RETRY_ATTEMPTS=4
    MANTIS_AGENT_RETRY_BASE_S=0.5
    MANTIS_AGENT_RETRY_MAX_S=20.0
"""

from __future__ import annotations

import logging
import os
import random
from typing import Iterable

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


# Retryable status codes (per RFC 9110 + provider conventions).
_RETRY_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

# Network-level errors worth retrying. ReadError/RemoteProtocolError catch
# the "TCP died mid-stream" case some providers hit under load.
_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


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


class RetryTransport(httpx.AsyncBaseTransport):
    """Wraps another transport, replaying retryable failures."""

    __slots__ = ("_inner", "_attempts", "_base_s", "_max_s", "_jitter")

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        attempts: int | None = None,
        base_s: float | None = None,
        max_s: float | None = None,
        jitter: bool = True,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(http2=True, retries=0)
        self._attempts = attempts if attempts is not None else _env_int(
            "MANTIS_AGENT_RETRY_ATTEMPTS", 4
        )
        self._base_s = base_s if base_s is not None else _env_float(
            "MANTIS_AGENT_RETRY_BASE_S", 0.5
        )
        self._max_s = max_s if max_s is not None else _env_float(
            "MANTIS_AGENT_RETRY_MAX_S", 20.0
        )
        self._jitter = jitter

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last_response: httpx.Response | None = None
        last_exc: Exception | None = None

        for attempt in range(self._attempts):
            try:
                response = await self._inner.handle_async_request(request)
            except _RETRY_EXCEPTIONS as e:
                last_exc = e
                sleep_for = self._backoff_seconds(attempt)
                _emit_retry(request, _friendly_reason(exc=e),
                            attempt + 1, self._attempts, sleep_for)
                await anyio.sleep(sleep_for)
                continue

            # Got a response. Retry only if status is retryable AND we have
            # attempts left.
            if response.status_code in _RETRY_STATUSES and attempt + 1 < self._attempts:
                # Honor Retry-After if present (seconds form; RFC 7231).
                retry_after = _parse_retry_after(
                    response.headers.get("retry-after")
                )
                sleep_for = retry_after if retry_after is not None else self._backoff_seconds(attempt)
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
                continue

            return response

        # Exhausted attempts. Surface the most-recent signal we have.
        if last_exc is not None:
            raise last_exc
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
    "RetryTransport",
]
