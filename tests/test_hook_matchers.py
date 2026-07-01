"""Multiple hooks per event + tool-name matchers (T2)."""

from __future__ import annotations

import anyio

from mantis_agent.hooks import (
    HookContext,
    HookDispatcher,
    HookMatcher,
    HookResult,
    Hooks,
)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _ctx(tool: str | None = None, inp: dict | None = None) -> HookContext:
    return HookContext(event="PreToolUse",
                       tool=_Tool(tool) if tool else None,
                       input=inp or {})


def _run(dispatcher: HookDispatcher, ctx: HookContext) -> HookResult:
    return anyio.run(lambda: dispatcher.dispatch("PreToolUse", ctx))


def test_backward_compat_single_callable() -> None:
    calls = []

    async def h(ctx):
        calls.append(ctx.tool.name)
        return None

    d = HookDispatcher(Hooks(pre_tool_use=h))
    _run(d, _ctx("bash"))
    assert calls == ["bash"]


def test_multiple_hooks_all_fire() -> None:
    order = []

    async def a(ctx):
        order.append("a")

    async def b(ctx):
        order.append("b")

    d = HookDispatcher(Hooks(pre_tool_use=[a, b]))
    _run(d, _ctx("bash"))
    assert order == ["a", "b"]


def test_matcher_scopes_to_tool() -> None:
    hit = []

    async def h(ctx):
        hit.append(ctx.tool.name)

    d = HookDispatcher(Hooks(pre_tool_use=HookMatcher(hook=h, matcher="bash")))
    _run(d, _ctx("write_file"))   # doesn't match → skipped
    assert hit == []
    _run(d, _ctx("bash"))         # matches
    assert hit == ["bash"]


def test_matcher_glob() -> None:
    hit = []

    async def h(ctx):
        hit.append(ctx.tool.name)

    d = HookDispatcher(Hooks(pre_tool_use=HookMatcher(hook=h, matcher="*_file")))
    _run(d, _ctx("write_file"))
    _run(d, _ctx("bash"))
    assert hit == ["write_file"]  # only the *_file tool matched


def test_first_block_short_circuits() -> None:
    ran = []

    async def blocker(ctx):
        ran.append("block")
        return HookResult(block=True, note="nope")

    async def after(ctx):
        ran.append("after")

    d = HookDispatcher(Hooks(pre_tool_use=[blocker, after]))
    res = _run(d, _ctx("bash"))
    assert res.block is True
    assert ran == ["block"]        # 'after' never ran


def test_mutations_chain() -> None:
    async def add_x(ctx):
        return HookResult(mutated_input={**(ctx.input or {}), "x": 1})

    async def add_y(ctx):
        # sees x from the previous hook
        assert ctx.input.get("x") == 1
        return HookResult(mutated_input={**ctx.input, "y": 2})

    d = HookDispatcher(Hooks(pre_tool_use=[add_x, add_y]))
    res = _run(d, _ctx("bash", {"cmd": "ls"}))
    assert res.mutated_input == {"cmd": "ls", "x": 1, "y": 2}


def test_non_tool_event_ignores_matcher() -> None:
    fired = []

    async def h(ctx):
        fired.append(ctx.event)

    # A tool-scoped matcher on a lifecycle event (no tool) still fires.
    d = HookDispatcher(Hooks(session_start=HookMatcher(hook=h, matcher="bash")))
    anyio.run(lambda: d.dispatch("SessionStart", HookContext(event="SessionStart")))
    assert fired == ["SessionStart"]


def test_has_true_for_list_and_matcher() -> None:
    async def h(ctx):
        return None

    assert HookDispatcher(Hooks(pre_tool_use=[h])).has("PreToolUse")
    assert HookDispatcher(Hooks(pre_tool_use=HookMatcher(hook=h))).has("PreToolUse")
    assert not HookDispatcher(Hooks()).has("PreToolUse")
