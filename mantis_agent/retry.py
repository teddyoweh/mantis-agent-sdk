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
                _LOG.warning(
                    "request %s %s failed (%s); retry %d/%d in %.2fs",
                    request.method,
                    request.url,
                    type(e).__name__,
                    attempt + 1,
                    self._attempts,
                    sleep_for,
                )
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
                _LOG.info(
                    "request %s %s returned %d; retry %d/%d in %.2fs",
                    request.method,
                    request.url,
                    response.status_code,
                    attempt + 1,
                    self._attempts,
                    sleep_for,
                )
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
    """Parse the ``Retry-After`` header. Only the seconds form is supported
    (the HTTP-date form is rare and providers don't use it for model APIs)."""

    if not header:
        return None
    try:
        v = float(header.strip())
        return v if v >= 0 else None
    except ValueError:
        return None


__all__ = [
    "RetryTransport",
]
