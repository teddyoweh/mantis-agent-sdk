"""Esc precedence — including the new 'idle + text → clear the input line'."""

from __future__ import annotations

from mantis_agent.tui import esc_action

_BASE = dict(awaiting_key=False, picking_model=False, pending_perm=False,
             question_open=False, question_typing=False, working=False, has_input=False)


def _esc(**over) -> str:
    return esc_action(**{**_BASE, **over})


def test_idle_with_text_clears_input() -> None:
    assert _esc(has_input=True) == "clear_input"        # the new behavior


def test_idle_empty_is_noop() -> None:
    assert _esc() == "none"


def test_working_interrupts() -> None:
    assert _esc(working=True) == "interrupt"
    assert _esc(working=True, has_input=True) == "interrupt"   # interrupt beats clear


def test_key_entry_highest_priority() -> None:
    assert _esc(awaiting_key=True, picking_model=True, pending_perm=True) == "cancel_key"


def test_picker_close() -> None:
    assert _esc(picking_model=True) == "close_picker"


def test_permission_deny_beats_working() -> None:
    assert _esc(pending_perm=True, working=True) == "deny"


def test_question_typing_vs_skip() -> None:
    assert _esc(question_open=True, question_typing=True) == "cancel_question_typing"
    assert _esc(question_open=True, question_typing=False) == "skip_question"


def test_full_precedence_order() -> None:
    # awaiting_key > picking_model > pending_perm > question > working > has_input
    assert _esc(awaiting_key=True, working=True, has_input=True) == "cancel_key"
    assert _esc(picking_model=True, pending_perm=True) == "close_picker"
    assert _esc(question_open=True, working=True) in ("skip_question",)
