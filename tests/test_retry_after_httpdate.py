"""``Retry-After`` may be an HTTP-date (RFC 7231), not just a seconds count —
some proxies/gateways (Bedrock/Vertex fronting) use it. The retry transport must
honor it so it doesn't retry too early and burn the budget.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from mantis_agent.retry import _parse_retry_after


def test_seconds_form_still_works() -> None:
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("  15  ") == 15.0
    assert _parse_retry_after("garbage") is None
    assert _parse_retry_after(None) is None


def test_http_date_future_returns_seconds_until() -> None:
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
    v = _parse_retry_after(future)
    assert v is not None
    assert 110 <= v <= 121  # ~120s, allowing test-runtime slack


def test_http_date_past_clamps_to_zero() -> None:
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=45))
    assert _parse_retry_after(past) == 0.0
