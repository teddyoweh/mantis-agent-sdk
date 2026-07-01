"""Footer usage indicator — tokens + fill% + live session cost."""

from __future__ import annotations

import re

from mantis_agent.tui import format_ctx_status


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_empty_until_usage() -> None:
    assert format_ctx_status(0, 32000) == ""
    assert format_ctx_status(0, 32000, 5.0) == ""


def test_tokens_and_percent() -> None:
    assert _plain(format_ctx_status(12000, 32000)) == "12k/32k 38%"


def test_cost_tail_only_when_nonzero() -> None:
    assert "$" not in format_ctx_status(12000, 32000, 0.0)     # local/free — no clutter
    assert _plain(format_ctx_status(12000, 32000, 0.034)) == "12k/32k 38% · $0.03"   # >=1c → 2dp
    assert _plain(format_ctx_status(12000, 32000, 0.0034)) == "12k/32k 38% · $0.0034"  # <1c → 4dp
    assert _plain(format_ctx_status(12000, 32000, 1.5)) == "12k/32k 38% · $1.50"


def test_no_window_fallback() -> None:
    assert _plain(format_ctx_status(5000, 0)) == "5k tok"


def test_fill_colour_thresholds() -> None:
    assert "90m" in format_ctx_status(5000, 32000)      # <75% grey
    assert "33m" in format_ctx_status(25000, 32000)     # >=75% yellow
    assert "31m" in format_ctx_status(31000, 32000)     # >=90% red


def test_small_token_count_not_k() -> None:
    assert _plain(format_ctx_status(500, 0)) == "500 tok"
