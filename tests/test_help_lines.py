"""/help is generated from SLASH_COMMANDS so it never drifts out of sync."""

from __future__ import annotations

from mantis_agent.tui import SLASH_COMMANDS, MantisTUI, build_help_lines, search_help_lines


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


def test_help_search_matches_command_category_and_description() -> None:
    # "resume" hits /resume (the command) and /workflows (which resumes a past
    # run) — search matches descriptions too, and both are genuinely relevant.
    assert [c for _cat, c, _desc in search_help_lines(SLASH_COMMANDS, "resume")] == [
        "/resume", "/workflows"]
    assert "/resume" in {c for _cat, c, _desc in search_help_lines(SLASH_COMMANDS, "/resume")}
    session = {c for _cat, c, _desc in search_help_lines(SLASH_COMMANDS, "session")}
    assert {"/resume", "/branch", "/rewind"}.issubset(session)
    assert search_help_lines(SLASH_COMMANDS, "definitely-not-a-command") == []


def test_classic_tui_help_uses_generated_commands() -> None:
    import asyncio
    import io

    from rich.console import Console

    tui = MantisTUI(
        model="x",
        backend="http://y",
        api_key=None,
        system=None,
        max_tokens=1,
        temperature=None,
        max_turns=1,
    )
    buf = io.StringIO()
    tui.console = Console(file=buf, force_terminal=False, width=120)

    assert asyncio.run(tui._handle_slash("/help")) is True
    out = buf.getvalue()
    assert "/compact" in out
    assert "/init" in out
    assert "/learn" in out
    assert SLASH_COMMANDS["/compact"] in out


def test_classic_tui_help_search_filters_commands() -> None:
    import asyncio
    import io

    from rich.console import Console

    tui = MantisTUI(
        model="x",
        backend="http://y",
        api_key=None,
        system=None,
        max_tokens=1,
        temperature=None,
        max_turns=1,
    )
    buf = io.StringIO()
    tui.console = Console(file=buf, force_terminal=False, width=120)

    assert asyncio.run(tui._handle_slash("/help resume")) is True
    out = buf.getvalue()
    assert "commands matching 'resume'" in out
    assert "/resume" in out
    assert "/models" not in out
    assert "/help <term>" in out
