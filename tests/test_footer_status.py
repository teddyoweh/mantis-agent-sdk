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
    # One ramp everywhere (footer, /context, /dash): green < 60, yellow < 85, red after.
    assert "38;5;113m" in format_ctx_status(5000, 32000)   # 16% green
    assert "38;5;113m" in format_ctx_status(19000, 32000)  # 59% still green
    assert "33m" in format_ctx_status(20000, 32000)        # 63% yellow
    assert "33m" in format_ctx_status(27000, 32000)        # 84% yellow
    assert "31m" in format_ctx_status(27500, 32000)        # 86% red
    assert "31m" in format_ctx_status(31000, 32000)        # 97% red


def test_small_token_count_not_k() -> None:
    assert _plain(format_ctx_status(500, 0)) == "500 tok"


# -- the full status line -------------------------------------------------------


def _footer(**kw):
    from mantis_agent.tui_fullscreen import _footer_line
    return _footer_line(**kw)


def test_footer_shows_family_glyph_and_name_next_to_the_model() -> None:
    line = _plain(_footer(mode_idx=0, model="claude-sonnet-5", family="✦ Claude"))
    assert "✦ Claude claude-sonnet-5" in line


def test_footer_without_family_is_unchanged() -> None:
    assert _plain(_footer(mode_idx=2, model="claude-opus-5")) == \
        "⏸ plan mode on (shift+tab to cycle)   claude-opus-5"


def test_footer_at_80_columns_shows_mode_family_model_context_and_cost() -> None:
    ctx = format_ctx_status(12000, 200000, 0.03)
    line = _footer(mode_idx=1, model="claude-sonnet-5", knobs=["effort=high"], ctx=ctx,
                   live="1 monitor", family="✦ Claude", width=80)
    plain = _plain(line)
    assert len(plain) <= 80
    assert plain == ("⏵⏵ accept edits on   ✦ Claude claude-sonnet-5   "
                     "12k/200k 6% · $0.03 · 1 monitor")
    assert "38;5;113m" in line          # green context fill


def test_footer_degrades_in_order_and_never_exceeds_width() -> None:
    ctx = format_ctx_status(180000, 200000, 1.5)
    kw = dict(mode_idx=1, model="claude-sonnet-5", knobs=["effort=high", "verb=low"],
              ctx=ctx, live="2 agents", family="✦ Claude")
    full = _plain(_footer(**kw, width=200))
    assert "(shift+tab to cycle)" in full and "↓ to manage" in full and "effort=high" in full
    at100 = _plain(_footer(**kw, width=100))
    assert "(shift+tab" not in at100 and len(at100) <= 100
    for w in (90, 80, 70, 60, 48, 30):
        p = _plain(_footer(**kw, width=w))
        assert len(p) <= w, (w, p)
        assert "accept edits on" in p          # the mode is never dropped
    at70 = _plain(_footer(**kw, width=70))
    assert "effort=high" not in at70 and "✦ claude-sonnet-5" in at70   # glyph survives the name
    assert "31m" in _footer(**kw, width=80)   # 90% → red


def test_footer_cut_closes_colour_escapes() -> None:
    kw = dict(mode_idx=3, model="accounts/fireworks/models/gpt-oss-120b", family="◈ Hosted OSS",
              ctx=format_ctx_status(1000, 32000), width=40)
    line = _footer(**kw)
    assert len(_plain(line)) <= 40
    assert line.endswith("\x1b[0m") or "\x1b" not in line
