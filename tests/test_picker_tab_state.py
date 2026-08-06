"""The picker's tab bar has to show which providers you can actually reach.

Every unselected chip rendered identically dim, so a provider holding a working
key looked exactly like one that had never been set up — you had to open each
tab to find out. Providers are also not all-or-nothing: the local/open group is
partly usable whenever only some models are installed.
"""

from __future__ import annotations

import pytest

from mantis_agent.tui_fullscreen import _tab_chip, tab_state, tab_text


def _tab(tab="claude", label="claude", count=11, avail=11):
    return {"tab": tab, "label": label, "count": count, "avail": avail}


@pytest.mark.parametrize(
    ("tab", "want"),
    [
        (_tab(count=11, avail=11), "live"),      # keyed: everything reachable
        (_tab(count=15, avail=8), "partial"),    # e.g. some local models pulled
        (_tab(count=5, avail=0), "locked"),      # no credential
        (_tab(tab="all", label="all"), "plain"),
        (_tab(tab="available", label="available"), "plain"),
        (_tab(count=0, avail=0), "locked"),      # empty group
    ],
)
def test_tab_state(tab: dict, want: str) -> None:
    assert tab_state(tab) == want


def test_live_tabs_are_starred_so_colour_is_not_the_only_signal() -> None:
    # Under NO_COLOR every escape in the module collapses to "", so a
    # colour-only cue would leave live and locked chips identical.
    assert tab_text(_tab(count=11, avail=11)) == "✦claude 11"


def test_partial_tabs_show_the_ratio() -> None:
    assert tab_text(_tab(tab="open", label="open", count=15, avail=8)) == "open 8/15"


def test_locked_tabs_are_unchanged() -> None:
    assert tab_text(_tab(tab="groq", label="groq", count=5, avail=0)) == "groq 5"


def test_aggregate_tabs_get_no_marker() -> None:
    assert tab_text({"tab": "all", "label": "all", "count": 129, "avail": 80}) == "all 129"
    assert tab_text({"tab": "available", "label": "available",
                     "count": 80, "avail": 80}) == "available 80"


def test_reported_width_matches_the_printable_text() -> None:
    # The bar wraps on these widths and shares them with the height calc; a
    # mismatch pushes the panel's last row off screen.
    for tab in (_tab(), _tab(count=15, avail=8), _tab(count=5, avail=0),
                {"tab": "all", "label": "all", "count": 129, "avail": 80}):
        for selected in (False, True):
            text, chip, width = _tab_chip(tab, selected=selected)
            assert width == len(text) + (2 if selected else 0)
            assert text.replace("✦", "") in chip.replace("✦", "") or "/" in text


def test_partial_chip_highlights_the_reachable_count() -> None:
    _text, chip, _w = _tab_chip(_tab(tab="open", label="open", count=15, avail=8),
                                selected=False)
    assert "8" in chip and "/15" in chip


def test_selected_chip_still_wins_over_state_styling() -> None:
    for tab in (_tab(), _tab(count=5, avail=0)):
        _t, chip, _w = _tab_chip(tab, selected=True)
        assert chip.startswith("\x1b[") or chip.startswith(" "), chip
