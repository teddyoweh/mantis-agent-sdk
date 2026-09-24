"""The TUI's permission wiring: read-only shell commands don't prompt, rules
from settings use the structured grammar, session approvals survive an agent
rebuild, the "don't ask again for <prefix>" offer persists a rule, and file
edits preview the proposed change as a diff."""

from __future__ import annotations

import asyncio

import anyio
import pytest

from mantis_agent.builtin_tools.fs import bash, edit_file, multi_edit, write_file
from mantis_agent.permissions import (
    Allow,
    Ask,
    Deny,
    PermissionContext,
    PermissionRule,
    PermissionRuleSet,
    check_permission,
    rules_from_settings,
)
from mantis_agent.term_caps import strip_ansi
from mantis_agent.tui import MODES, MantisTUI, proposed_change_diff
from mantis_agent.tui_fullscreen import perm_diff_visible, perm_options, perm_payload


def _tui() -> MantisTUI:
    return MantisTUI(
        model="qwen2.5-coder:7b",
        backend="http://localhost:11434",
        api_key=None,
        system=None,
        max_tokens=1,
        temperature=None,
        max_turns=1,
    )


def _default(tui: MantisTUI) -> MantisTUI:
    tui.mode_idx = [m[0] for m in MODES].index("default")
    return tui


def _ctx(tui: MantisTUI, rules: PermissionRuleSet | None = None,
         answer: str = "deny") -> PermissionContext:
    async def asker(tool, inp, prompt):  # noqa: ANN001, ANN202
        return answer
    return PermissionContext(can_use_tool=tui._permit, asker=asker, rules=rules)


def _check(tool, inp: dict, ctx: PermissionContext):  # noqa: ANN001, ANN202
    return anyio.run(check_permission, tool, inp, ctx)


# -- A: the already-wired parts ----------------------------------------------


@pytest.mark.parametrize("cmd", ["ls", "git status", "ls -la src"])
def test_read_only_shell_commands_are_allowed(cmd: str) -> None:
    tui = _default(_tui())
    assert isinstance(anyio.run(tui._permit, bash, {"command": cmd}, None), Allow)


def test_mutating_shell_command_asks() -> None:
    tui = _default(_tui())
    assert isinstance(anyio.run(tui._permit, bash, {"command": "rm x"}, None), Ask)


def test_explicit_deny_rule_beats_read_only() -> None:
    tui = _default(_tui())
    ctx = _ctx(tui, rules_from_settings({"deny": ["Bash(git status:*)"]}), answer="allow_once")
    assert isinstance(_check(bash, {"command": "git status"}, ctx), Deny)
    # Without the rule the same read runs unprompted.
    assert isinstance(_check(bash, {"command": "git status"}, _ctx(tui)), Allow)


def test_settings_prefix_rule_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    import mantis_agent.settings as settings

    monkeypatch.setattr(
        settings, "load_settings",
        lambda *a, **k: {"permissions": {"allow": ["Bash(npm run test:*)"]}})
    tui = _default(_tui())
    rules = tui._load_permission_rules()
    assert rules is not None
    # The asker would deny — so an Allow can only come from the rule.
    ctx = _ctx(tui, rules)
    assert isinstance(_check(bash, {"command": "npm run test -- -k x"}, ctx), Allow)
    assert isinstance(_check(bash, {"command": "npm run build"}, ctx), Deny)


def test_git_status_settings_rule_is_word_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import mantis_agent.settings as settings

    monkeypatch.setattr(
        settings, "load_settings",
        lambda *a, **k: {"permissions": {"allow": ["Bash(git status:*)"]}})
    rules = _tui()._load_permission_rules()
    assert rules.match("bash", {"command": "git status --short"}, tool=bash) is not None
    assert rules.match("bash", {"command": "git statusx"}, tool=bash) is None


def test_session_allows_survive_agent_rebuild() -> None:
    tui = _default(_tui())
    tui.agent = tui._build_agent()
    ctx = tui.agent.permissions
    ctx.asker = lambda *a: _answer("allow_session")
    inp = {"command": "touch made.txt"}
    assert isinstance(_check(bash, inp, ctx), Allow)
    assert ctx.session_allows

    tui.agent = tui._build_agent()  # model switch / MCP reload / resume
    new = tui.agent.permissions
    assert new is not ctx
    new.asker = lambda *a: _answer("deny")
    assert isinstance(_check(bash, inp, new), Allow)


async def _answer(value: str) -> str:
    return value


# -- B: "don't ask again for <prefix> in this project" -------------------------


def test_prefix_rule_offered_only_for_safe_shell_commands() -> None:
    tui = _tui()
    assert tui._prefix_rule_for(bash, {"command": "npm run test -- -k x"}) == "Bash(npm run test:*)"
    assert tui._prefix_rule_for(bash, {"command": "rm -rf build"}) is None
    assert tui._prefix_rule_for(write_file, {"path": "a", "content": "b"}) is None


def test_perm_options_include_prefix_only_when_offered() -> None:
    assert [v for _l, v in perm_options(None)] == ["allow_once", "allow_session", "deny"]
    opts = perm_options("Bash(pytest:*)")
    # Fixed digit mapping: 3 is ALWAYS deny; the permanent rule is 4 (last).
    assert [v for _l, v in opts] == ["allow_once", "allow_session", "deny", "allow_prefix"]
    assert "Bash(pytest:*)" in opts[3][0]


def test_remember_prefix_rule_persists_and_goes_live(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mantis_agent.settings import load_setting_source

    monkeypatch.chdir(tmp_path)
    tui = _default(_tui())
    tui.agent = tui._build_agent()
    tui.agent.permissions.rules = None  # no settings rules: must create a set
    tui._remember_prefix_rule("Bash(pytest:*)")
    tui._remember_prefix_rule("Bash(pytest:*)")  # idempotent

    data = load_setting_source("local", cwd=tmp_path)
    assert data["permissions"]["allow"] == ["Bash(pytest:*)"]
    rules = tui.agent.permissions.rules
    assert rules.allow == [PermissionRule("Bash(pytest:*)", "allow")]
    ctx = tui.agent.permissions
    ctx.asker = lambda *a: _answer("deny")
    assert isinstance(_check(bash, {"command": "pytest -q tests"}, ctx), Allow)


def test_classic_prompt_prefix_answer(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import sys

    monkeypatch.chdir(tmp_path)
    tui = _default(_tui())
    tui.agent = tui._build_agent()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda *_a: "p")
    ans = anyio.run(tui._ask_permission, bash, {"command": "pytest -q"}, "bash: pytest -q")
    assert ans == "allow_once"
    assert PermissionRule("Bash(pytest:*)", "allow") in tui.agent.permissions.rules.allow


def test_fullscreen_payload_carries_prefix_and_diff(tmp_path) -> None:
    tui = _tui()
    fut = asyncio.new_event_loop().create_future()
    p = perm_payload(tui, bash, {"command": "pytest -q"}, "bash: pytest -q", fut)
    assert p["prefix_rule"] == "Bash(pytest:*)"
    assert p["opts"][2][1] == "deny" and p["opts"][3][1] == "allow_prefix"
    assert p["diff_rows"] == []

    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    p = perm_payload(tui, edit_file, {"path": str(f), "old_string": "x = 1",
                                      "new_string": "x = 2"}, "edit", fut)
    assert p["prefix_rule"] is None
    plain = [strip_ansi(r) for r in p["diff_rows"]]
    assert any("x = 1" in r and "-" in r for r in plain)
    assert any("x = 2" in r and "+" in r for r in plain)


# -- C: diff preview -----------------------------------------------------------


def test_proposed_diff_edit_and_multi_edit(tmp_path) -> None:
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    path, lines = proposed_change_diff(
        "edit_file", {"path": str(f), "old_string": "b = 2", "new_string": "b = 20"})
    assert path == str(f)
    assert "-b = 2" in lines and "+b = 20" in lines
    assert lines[0].startswith("@@")

    _p, lines = proposed_change_diff("multi_edit", {"path": str(f), "edits": [
        {"old_string": "a = 1", "new_string": "a = 10"},
        {"old_string": "c = 3", "new_string": "c = 30"}]})
    assert {"-a = 1", "+a = 10", "-c = 3", "+c = 30"} <= set(lines)
    assert f.read_text() == "a = 1\nb = 2\nc = 3\n"  # a preview never writes


def test_proposed_diff_write_file_new_vs_existing(tmp_path) -> None:
    new = tmp_path / "new.txt"
    _p, lines = proposed_change_diff("write_file", {"path": str(new), "content": "one\ntwo\n"})
    assert [ln for ln in lines if not ln.startswith("@@")] == ["+one", "+two"]
    assert not new.exists()

    old = tmp_path / "old.txt"
    old.write_text("one\ntwo\n")
    _p, lines = proposed_change_diff("write_file", {"path": str(old), "content": "one\n2\n"})
    assert "-two" in lines and "+2" in lines and " one" in lines


def test_proposed_diff_other_tools_and_bad_input() -> None:
    assert proposed_change_diff("bash", {"command": "ls"}) is None
    assert proposed_change_diff("write_file", {"path": "x"}) is None
    assert proposed_change_diff("multi_edit", {"path": "x", "edits": []}) is None


def test_perm_diff_rows_truncate_with_marker(tmp_path) -> None:
    tui = _tui()
    content = "".join(f"line {i}\n" for i in range(100))
    rows = tui._perm_diff_rows(
        write_file, {"path": str(tmp_path / "big.txt"), "content": content}, max_rows=10)
    assert len(rows) == 10
    assert strip_ansi(rows[-1]).strip() == "… +91 lines"
    assert "line 0" in strip_ansi(rows[0])
    # Not an edit tool → no preview.
    assert tui._perm_diff_rows(multi_edit, {"path": "x", "edits": []}) == []


def test_perm_diff_visible_caps_to_screen_budget() -> None:
    rows = [f"r{i}" for i in range(30)]
    assert perm_diff_visible(rows, 0) == []
    assert perm_diff_visible(rows, 40) == rows
    out = perm_diff_visible(rows, 5)
    assert len(out) == 5 and out[:4] == rows[:4]
    assert strip_ansi(out[-1]).strip() == "… +26 lines"
    # A renderer marker already on the last row folds into the new count.
    out = perm_diff_visible(rows[:9] + ["  … +50 lines"], 5)
    assert strip_ansi(out[-1]).strip() == "… +55 lines"


def test_fullscreen_overlay_renders_capped_diff_and_prefix_key(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand up the real app: the overlay grows for a diff but never past half
    the screen, the layout still fits, and ``p`` answers ``allow_prefix``."""
    pytest.importorskip("prompt_toolkit")
    import mantis_agent.tui_fullscreen as mod
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.layout.controls import FormattedTextControl

    from tests.test_term_caps import _stand_up_app

    app, _tui_mock, controls = _stand_up_app(monkeypatch, mod)
    perm = next(c.text for c in controls if c.text.__name__ == "perm_ft")
    state = perm.__closure__[perm.__code__.co_freevars.index("state")].cell_contents
    window = next(w for w in app.layout.find_all_windows()
                  if isinstance(w.content, FormattedTextControl)
                  and w.content.text is perm)
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    rows = [f"\x1b[32m  {i:>4}  + line {i}" for i in range(200)]
    state["pending_perm"] = {"future": fut, "prompt": "write big.txt", "sel": 0,
                             "prefix_rule": "Bash(pytest:*)",
                             "opts": perm_options("Bash(pytest:*)"), "diff_rows": rows}
    with set_app(app):
        size = app.output.get_size()
        height = window.height().preferred
        text = strip_ansi(perm().value)
        assert 2 < height <= size.rows // 2 + 2
        assert "… +" in text and "don't ask again for Bash(pytest:*)" in text
        assert text.count("\n") + 1 == height
        binding = next(b for b in app.key_bindings.bindings
                       if [k.value if hasattr(k, "value") else k for k in b.keys] == ["p"]
                       and b.filter())
        binding.handler(None)
    assert fut.result() == "allow_prefix"
    loop.close()


def _perm_binding(app, key: str):  # noqa: ANN001, ANN202
    return next(b for b in app.key_bindings.bindings
                if [k.value if hasattr(k, "value") else k for k in b.keys] == [key]
                and b.filter())


@pytest.mark.parametrize("prefix", [None, "Bash(pytest:*)"])
def test_fullscreen_digits_have_fixed_meaning(
        monkeypatch: pytest.MonkeyPatch, prefix: str | None) -> None:
    """3 is deny whether or not the "don't ask again" option is shown — it
    must never grant a permanent rule — and 4 is that rule when offered."""
    pytest.importorskip("prompt_toolkit")
    import mantis_agent.tui_fullscreen as mod
    from prompt_toolkit.application.current import set_app

    from tests.test_term_caps import _stand_up_app

    app, _tui_mock, controls = _stand_up_app(monkeypatch, mod)
    perm = next(c.text for c in controls if c.text.__name__ == "perm_ft")
    state = perm.__closure__[perm.__code__.co_freevars.index("state")].cell_contents
    loop = asyncio.new_event_loop()
    try:
        for key, want in [("1", "allow_once"), ("2", "allow_session"), ("3", "deny"),
                          ("4", "allow_prefix" if prefix else None)]:
            fut = loop.create_future()
            state["pending_perm"] = {"future": fut, "prompt": "bash: pytest", "sel": 0,
                                     "prefix_rule": prefix, "opts": perm_options(prefix),
                                     "diff_rows": []}
            with set_app(app):
                text = strip_ansi(perm().value)
                _perm_binding(app, key).handler(None)
            assert (fut.result() if fut.done() else None) == want, key
            if want:  # the number shown next to the label is the key that picks it
                label = next(lbl for lbl, v in perm_options(prefix) if v == want)
                assert f"{key} {label}" in text
    finally:
        loop.close()


def test_proposed_diff_big_or_binary_file_is_a_note(tmp_path) -> None:
    import mantis_agent.tui as tui_mod

    big = tmp_path / "big.txt"
    big.write_text("x\n" * (tui_mod._DIFF_PREVIEW_MAX_BYTES // 2 + 10))
    note = [tui_mod._DIFF_TOO_BIG]
    assert proposed_change_diff("edit_file", {"path": str(big), "old_string": "x",
                                              "new_string": "y"}) == (str(big), note)
    binf = tmp_path / "a.bin"
    binf.write_bytes(b"abc\x00def\n")
    assert proposed_change_diff("write_file", {"path": str(binf),
                                               "content": "z\n"}) == (str(binf), note)
    huge = "y" * (tui_mod._DIFF_PREVIEW_MAX_BYTES + 1)
    assert proposed_change_diff("write_file", {"path": str(tmp_path / "n"),
                                               "content": huge})[1] == note
    assert proposed_change_diff("multi_edit", {"path": str(tmp_path / "n"), "edits": [
        {"old_string": huge, "new_string": "a"}]})[1] == note
    rows = _tui()._perm_diff_rows(write_file, {"path": str(binf), "content": "z"})
    assert [strip_ansi(r).strip() for r in rows] == note


def test_proposed_diff_ambiguous_old_string_is_a_note(tmp_path) -> None:
    f = tmp_path / "d.py"
    f.write_text("x = 1\nx = 1\nx = 1\n")
    _p, lines = proposed_change_diff(
        "edit_file", {"path": str(f), "old_string": "x = 1", "new_string": "x = 2"})
    assert lines == ["(old_string matches 3 places — the edit will be rejected)"]
    _p, lines = proposed_change_diff("edit_file", {
        "path": str(f), "old_string": "x = 1", "new_string": "x = 2", "replace_all": True})
    assert lines.count("+x = 2") == 3
    _p, lines = proposed_change_diff("multi_edit", {"path": str(f), "edits": [
        {"old_string": "x = 1", "new_string": "x = 2"}]})
    assert "matches 3 places" in lines[0]


def test_perm_diff_resolves_relative_path_against_agent_cwd(tmp_path) -> None:
    from mantis_agent.builtin_tools.fs import AGENT_CWD

    (tmp_path / "rel.py").write_text("a = 1\n")
    token = AGENT_CWD.set(str(tmp_path))
    try:
        rows = anyio.run(_tui()._perm_diff_rows_async, edit_file,
                         {"path": "rel.py", "old_string": "a = 1", "new_string": "a = 2"})
    finally:
        AGENT_CWD.reset(token)
    plain = [strip_ansi(r) for r in rows]
    assert any("a = 1" in r and "-" in r for r in plain)
    assert any("a = 2" in r and "+" in r for r in plain)
