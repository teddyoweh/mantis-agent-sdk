"""`mantis run --tools` — one-shot mode gets the coding tools so it can act."""

from __future__ import annotations

import argparse

from mantis_agent import MantisAgentOptions
from mantis_agent.builtin_tools import CODING_TOOLS
from mantis_agent.cli import _build_options, _build_parser


def _args(tools: bool) -> argparse.Namespace:
    return argparse.Namespace(model="m", backend=None, max_tokens=1024, max_turns=10,
                              temperature=None, api_key=None, tools=tools)


def test_no_tools_by_default() -> None:
    assert "tools" not in _build_options(_args(False))


def test_tools_flag_adds_coding_kit() -> None:
    opts = _build_options(_args(True))
    names = {t.name for t in opts["tools"]}
    for n in ("read_file", "write_file", "edit_file", "bash", "grep", "glob", "lsp",
              "web_search", "web_fetch"):
        assert n in names


def test_tools_survive_query_normalization() -> None:
    opts = _build_options(_args(True))
    normalized = MantisAgentOptions(model="m", tools=opts["tools"]).to_query_options()
    assert len(normalized["tools"]) == len(opts["tools"])


def test_parser_accepts_tools_on_run_and_chat() -> None:
    p = _build_parser()
    assert p.parse_args(["run", "--model", "m", "--tools", "go"]).tools is True
    assert p.parse_args(["run", "--model", "m", "go"]).tools is False
    assert p.parse_args(["chat", "--model", "m", "--tools"]).tools is True


def test_skip_permissions_flag_sets_bypass() -> None:
    a = _args(True)
    a.skip_permissions = True
    assert _build_options(a).get("permission_mode") == "bypass"


def test_default_has_no_bypass() -> None:
    a = _args(True)
    a.skip_permissions = False
    assert "permission_mode" not in _build_options(a)


def test_both_flag_spellings_parse() -> None:
    p = _build_parser()
    assert p.parse_args(["run", "--model", "m", "--dangerously-skip-permissions", "go"]).skip_permissions
    assert p.parse_args(["run", "--model", "m", "--yes", "go"]).skip_permissions
    assert not p.parse_args(["run", "--model", "m", "go"]).skip_permissions


def test_bypass_allows_dangerous_headless() -> None:
    import anyio

    from mantis_agent.permissions import Allow, Deny, PermissionContext, check_permission
    from mantis_agent.tools import tool

    @tool(is_read_only=False)
    async def bash(command: str) -> str:
        return "x"

    dangerous = {"command": "rm -rf /tmp/x"}
    # bypass (the --yes path): dangerous shell runs
    allow = anyio.run(lambda: check_permission(bash, dangerous, PermissionContext(mode="bypass")))
    assert isinstance(allow, Allow)
    # default headless (no --yes): dangerous shell is refused (no approver)
    deny = anyio.run(lambda: check_permission(bash, dangerous, PermissionContext(mode="default")))
    assert isinstance(deny, Deny)
