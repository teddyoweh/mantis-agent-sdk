"""Friendly tool-call labels — the tools shipped this run render with a proper
(verb, target) instead of a bare name + empty target."""

from __future__ import annotations

from mantis_agent.tui import TOOL_VERBS, MantisTUI


def _tui() -> MantisTUI:
    return MantisTUI(model="x", backend="http://y", api_key=None, system=None,
                     max_tokens=1, temperature=None, max_turns=1)


def test_task_label() -> None:
    tui = _tui()
    verb, target = tui._tool_label("task", {"description": "find the auth bug", "prompt": "…"})
    assert verb == "Delegate" and target == "find the auth bug"
    # falls back to prompt when no description
    _v, t2 = tui._tool_label("task", {"prompt": "investigate flaky test"})
    assert t2 == "investigate flaky test"


def test_lsp_and_notebook_and_remember() -> None:
    tui = _tui()
    assert tui._tool_label("lsp", {"operation": "definition", "symbol": "foo"}) == ("Look up", "foo")
    assert tui._tool_label("notebook_edit", {"path": "a.ipynb"}) == ("Edit cell", "a.ipynb")
    assert tui._tool_label("remember", {"name": "cache TTL"}) == ("Remember", "cache TTL")


def test_no_target_tools_have_verbs() -> None:
    tui = _tui()
    assert tui._tool_label("ask_user_question", {})[0] == "Ask"
    assert tui._tool_label("exit_plan_mode", {"plan": "x"})[0] == "Present plan"


def test_all_registered_verbs_are_curated() -> None:
    # every entry maps to a human verb distinct from the raw tool name
    for name, (verb, _keys) in TOOL_VERBS.items():
        assert verb and verb != name


def test_unknown_tool_falls_back_gracefully() -> None:
    tui = _tui()
    verb, _t = tui._tool_label("some_custom_tool", {"path": "x"})
    assert verb == "some_custom_tool"      # graceful: raw name, no crash
