"""Argument-name aliases + teaching errors at dispatch.

A model trained on Claude Code calls ``edit_file(file_path=...)``; that key used
to be silently dropped and the call died with Python's "missing 1 required
positional argument: 'path'" — which never mentioned the dropped key, so the
model retried identically.
"""

from __future__ import annotations

from typing import Any

import anyio

from mantis_agent.builtin_tools.fs import edit_file, grep, read_file, write_file
from mantis_agent.streaming.executor import StreamingToolExecutor, _apply_arg_aliases
from mantis_agent.tools import ToolRegistry, tool
from mantis_agent.types import ToolUseBlock


def _dispatch(tools: list[Any], name: str, **input: Any):
    reg = ToolRegistry()
    reg.add(*tools)

    async def main():
        async with StreamingToolExecutor(reg) as ex:
            ex.add_tool_call(ToolUseBlock(id="t0", name=name, input=input))
            results = await ex.wait_all()
            return results[0], ex.executed_calls

    return anyio.run(main)


def test_file_path_aliases_to_path_for_read_write_edit(tmp_path) -> None:
    p = tmp_path / "a.txt"
    res, _ = _dispatch([write_file], "write_file", file_path=str(p), content="hello\n")
    assert not res.is_error, res.content
    assert p.read_text() == "hello\n"

    res, _ = _dispatch([read_file], "read_file", file_path=str(p))
    assert not res.is_error, res.content
    assert "hello" in str(res.content)

    res, executed = _dispatch(
        [edit_file], "edit_file", file_path=str(p), old_str="hello", new_str="bye",
    )
    assert not res.is_error, res.content
    assert p.read_text() == "bye\n"
    # evidence records the canonical call actually made
    assert executed[0].input == {"path": str(p), "old_string": "hello", "new_string": "bye"}


def test_present_canonical_key_is_never_overwritten() -> None:
    out, fired = _apply_arg_aliases(
        "edit_file", edit_file.fn,
        {"path": "real.py", "file_path": "decoy.py", "old_string": "a", "new_string": "b"},
    )
    assert out["path"] == "real.py"
    assert fired == []


def test_claude_grep_flags_map() -> None:
    out, fired = _apply_arg_aliases(
        "grep", grep.fn, {"pattern": "x", "-i": True, "-C": 2, "type": "py"},
    )
    assert out == {"pattern": "x", "ignore_case": True, "context_lines": 2, "file_type": "py"}
    assert ("-i", "ignore_case") in fired


def test_bash_timeout_ms_converts_to_seconds() -> None:
    from mantis_agent.builtin_tools.fs import bash

    out, _ = _apply_arg_aliases("bash", bash.fn, {"cmd": "ls", "timeout_ms": 30000})
    assert out == {"command": "ls", "timeout": 30}
    # a bare ``timeout`` is canonical — untouched
    out, fired = _apply_arg_aliases("bash", bash.fn, {"command": "ls", "timeout": 5})
    assert out == {"command": "ls", "timeout": 5} and not fired


def test_alias_never_applies_when_it_is_a_real_param() -> None:
    @tool
    async def search(query: str, path: str = ".") -> str:
        return f"{query}@{path}"

    # ``query`` is search's own param — must not become ``pattern`` (not accepted
    # anyway) nor be touched.
    out, fired = _apply_arg_aliases("search", search.fn, {"query": "q", "file": "f.py"})
    assert out == {"query": "q", "path": "f.py"}
    assert fired == [("file", "path")]


def test_kwargs_and_mcp_style_tools_unaffected() -> None:
    @tool(name="mcp__srv__read", input_schema={
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    })
    async def mcp_read(args: dict) -> str:
        return repr(sorted(args))

    inp = {"file_path": "x"}
    out, fired = _apply_arg_aliases(mcp_read.name, mcp_read.fn, inp)
    assert out is inp and not fired
    res, _ = _dispatch([mcp_read], "mcp__srv__read", file_path="x")
    assert not res.is_error
    assert res.content == "['file_path']"


def test_unknown_key_and_missing_required_gives_teaching_error() -> None:
    res, executed = _dispatch(
        [edit_file], "edit_file", target="a.py", old_string="x", new_string="y",
    )
    assert res.is_error
    msg = str(res.content)
    assert "edit_file: missing required argument 'path'" in msg
    assert "target (ignored — unknown)" in msg
    assert "path (string, required)" in msg
    assert "replace_all (boolean, optional)" in msg
    assert executed == []  # not executed → no evidence


def test_typeerror_mentions_dropped_keys() -> None:
    @tool
    async def picky(path: str) -> str:
        raise TypeError("bad shape")

    res, _ = _dispatch([picky], "picky", path="a", junk=1)
    assert res.is_error
    msg = str(res.content)
    assert "bad shape" in msg
    assert "junk (ignored — unknown)" in msg
    assert "Expected: path (string, required)" in msg


def test_grep_after_and_before_context_take_the_wider_window() -> None:
    out, _ = _apply_arg_aliases("grep", grep.fn, {"pattern": "x", "-A": 2, "-B": 5})
    assert out == {"pattern": "x", "context_lines": 5}


def test_unconvertible_timeout_ms_is_left_for_the_filter() -> None:
    from mantis_agent.builtin_tools.fs import bash

    out, fired = _apply_arg_aliases("bash", bash.fn, {"command": "ls", "timeout_ms": "inf"})
    assert out == {"command": "ls", "timeout_ms": "inf"} and not fired


def test_web_search_query_is_not_renamed() -> None:
    from mantis_agent.builtin_tools import web_search

    inp = {"query": "q", "file": "x"}
    out, fired = _apply_arg_aliases("web_search", web_search.fn, inp)
    assert out is inp and not fired  # no ``pattern``/``path`` param to alias onto


def test_normalize_is_idempotent() -> None:
    from mantis_agent.streaming.executor import normalize_tool_input

    once, fired = normalize_tool_input(edit_file, {"file_path": "a", "old_str": "x", "new_str": "y"})
    assert fired
    twice, fired2 = normalize_tool_input(edit_file, once)
    assert twice is once and not fired2


# -- Security: aliases are canonicalized BEFORE hooks + permissions -----------
#
# Structured rules bind to the canonical param (``Edit(...)`` → ``path``,
# ``Bash(...)`` → ``command``). If preflight saw the raw ``file_path`` / ``cmd``
# the rule would miss, and the executor would then alias the key and run it.


def _preflight_agent(tools: list[Any], *, rules=None, hooks=None):
    from mantis_agent.agent import Agent
    from mantis_agent.permissions import PermissionContext
    from mantis_agent.providers.mock import MockProvider

    reg = ToolRegistry()
    reg.add(*tools)
    kw: dict[str, Any] = {}
    if rules is not None:
        kw["permissions"] = PermissionContext(mode="bypassPermissions", rules=rules)
    if hooks is not None:
        kw["hooks"] = hooks
    return Agent(
        model="mock", provider=MockProvider(), tools=reg,
        include_memory=False, include_env=False, **kw,
    )


def _preflight(ag, name: str, input: dict[str, Any]):
    async def go():
        call = ToolUseBlock(id="c1", name=name, input=input)
        out = await ag._preflight_call(call, [])
        await ag.aclose()
        return out

    return anyio.run(go)


def _deny(*patterns: str):
    from mantis_agent.permissions import PermissionRule, PermissionRuleSet

    return PermissionRuleSet(deny=[PermissionRule(pattern=p, action="deny") for p in patterns])


def test_path_deny_rule_blocks_aliased_file_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets").mkdir()
    secret = tmp_path / "secrets" / "k.txt"
    secret.write_text("token=a\n")

    ag = _preflight_agent([edit_file], rules=_deny("Edit(secrets/**)"))
    _, sc = _preflight(ag, "edit_file", {
        "file_path": "secrets/k.txt", "old_str": "token=a", "new_str": "token=b",
    })
    assert sc is not None and sc.is_error
    assert "permission denied" in str(sc.content)
    assert secret.read_text() == "token=a\n"


def test_command_deny_rule_blocks_aliased_cmd() -> None:
    from mantis_agent.builtin_tools.fs import bash

    ag = _preflight_agent([bash], rules=_deny("Bash(git push:*)"))
    _, sc = _preflight(ag, "bash", {"cmd": "git push origin main"})
    assert sc is not None and "permission denied" in str(sc.content)


def test_hook_sees_and_approved_call_carries_canonical_input(tmp_path) -> None:
    from mantis_agent.hooks import HookContext, HookResult, Hooks

    seen: list[dict[str, Any]] = []

    async def spy(ctx: HookContext) -> HookResult | None:
        seen.append(dict(ctx.input))
        return None

    ag = _preflight_agent([read_file], hooks=Hooks(pre_tool_use=spy))
    approved, sc = _preflight(ag, "read_file", {"file_path": str(tmp_path / "a")})
    assert sc is None
    assert seen == [{"path": str(tmp_path / "a")}]
    assert approved.input == {"path": str(tmp_path / "a")}


def test_hook_rewrite_using_an_alias_is_permission_checked(tmp_path, monkeypatch) -> None:
    from mantis_agent.hooks import HookContext, HookResult, Hooks

    monkeypatch.chdir(tmp_path)

    async def rewrite(ctx: HookContext) -> HookResult | None:
        return HookResult(mutated_input={
            "file_path": "secrets/k.txt", "old_string": "a", "new_string": "b",
        })

    ag = _preflight_agent(
        [edit_file], rules=_deny("Edit(secrets/**)"), hooks=Hooks(pre_tool_use=rewrite),
    )
    _, sc = _preflight(ag, "edit_file", {"path": "ok.txt", "old_string": "a", "new_string": "b"})
    assert sc is not None and "permission denied" in str(sc.content)
