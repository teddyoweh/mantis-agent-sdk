"""Pressing `/` must reach every command, not just the first eight.

The menu rendered ``opts[:8]`` unconditionally. With 47 registered slash
commands that showed the first 8 and hid the other 39: arrowing down moved the
selection out of the rendered slice, so the highlight vanished and the list
appeared frozen.
"""

from __future__ import annotations

from mantis_agent.tui import all_slash_commands
from mantis_agent.tui_fullscreen import MENU_ROWS, menu_window


def _visible(total: int, sel: int) -> range:
    start = menu_window(total, sel)
    return range(start, min(start + MENU_ROWS, total))


def test_short_lists_never_scroll() -> None:
    for sel in range(MENU_ROWS):
        assert menu_window(MENU_ROWS, sel) == 0


def test_selection_is_always_visible_walking_the_whole_list() -> None:
    total = 47
    for sel in range(total):
        assert sel in _visible(total, sel), f"selection {sel} scrolled out of view"


def test_the_last_item_is_reachable() -> None:
    total = 47
    assert total - 1 in _visible(total, total - 1)
    # and the window stops at the end rather than running past it
    assert menu_window(total, total - 1) == total - MENU_ROWS


def test_window_never_runs_off_either_end() -> None:
    for total in (1, 8, 9, 47, 200):
        for sel in (-5, 0, total // 2, total - 1, total + 5):
            start = menu_window(total, sel)
            assert 0 <= start
            assert start + MENU_ROWS <= max(total, MENU_ROWS)


def test_every_registered_command_can_be_reached() -> None:
    cmds = list(all_slash_commands())
    assert len(cmds) > MENU_ROWS, "regression guard assumes more commands than rows"
    reachable = {i for sel in range(len(cmds)) for i in _visible(len(cmds), sel)}
    assert reachable == set(range(len(cmds)))
