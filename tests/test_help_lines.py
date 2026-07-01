"""/help is generated from SLASH_COMMANDS so it never drifts out of sync."""

from __future__ import annotations

from mantis_agent.tui import SLASH_COMMANDS, build_help_lines


def test_every_command_is_covered() -> None:
    rows = build_help_lines(SLASH_COMMANDS)
    covered = {c for _cat, c, _d in rows}
    for c in SLASH_COMMANDS:
        if c in ("/help", "/exit", "/quit"):
            continue
        assert c in covered, f"/help drifted: {c} missing"


def test_previously_missing_commands_present() -> None:
    covered = {c for _cat, c, _d in build_help_lines(SLASH_COMMANDS)}
    for c in ("/compact", "/init", "/learn", "/resume", "/branch", "/rewind", "/vim"):
        assert c in covered


def test_descriptions_come_from_source() -> None:
    for _cat, c, desc in build_help_lines(SLASH_COMMANDS):
        assert desc == SLASH_COMMANDS[c]           # not a hardcoded copy


def test_self_evident_commands_skipped() -> None:
    covered = {c for _cat, c, _d in build_help_lines(SLASH_COMMANDS)}
    assert "/help" not in covered and "/exit" not in covered and "/quit" not in covered


def test_uncategorized_command_falls_into_more() -> None:
    cmds = {**SLASH_COMMANDS, "/brandnew": "a shiny new command"}
    rows = build_help_lines(cmds)
    assert ("more", "/brandnew", "a shiny new command") in rows


def test_category_order_stable() -> None:
    rows = build_help_lines(SLASH_COMMANDS)
    cats = [cat for cat, _c, _d in rows]
    # model rows precede session rows precede project rows
    assert cats.index("model") < cats.index("session") < cats.index("project")
