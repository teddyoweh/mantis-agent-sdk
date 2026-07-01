"""Retry backoff honors the server's Retry-After header on rate limits."""

from __future__ import annotations

from mantis_agent.agent import _retry_delay
from mantis_agent.errors import ProviderError, RateLimitError


def test_honors_retry_after() -> None:
    assert _retry_delay(RateLimitError("slow", retry_after_s=3.0), 0) == 3.0
    assert _retry_delay(RateLimitError("slow", retry_after_s=7.5), 5) == 7.5   # attempt ignored


def test_retry_after_capped() -> None:
    # a hostile / huge Retry-After can't hang the agent
    assert _retry_delay(RateLimitError("slow", retry_after_s=99999), 0) == 60.0


def test_exponential_when_no_header() -> None:
    e = ProviderError("gateway", status_code=503)
    assert _retry_delay(e, 0) == 0.5
    assert _retry_delay(e, 1) == 1.0
    assert _retry_delay(e, 2) == 2.0


def test_exponential_capped() -> None:
    assert _retry_delay(ProviderError("x", status_code=500), 20) == 8.0


def test_rate_limit_without_header_uses_backoff() -> None:
    assert _retry_delay(RateLimitError("no header"), 1) == 1.0


def test_zero_or_negative_retry_after_ignored() -> None:
    # a bogus 0/negative header falls back to backoff, not an instant retry
    assert _retry_delay(RateLimitError("x", retry_after_s=0), 1) == 1.0
