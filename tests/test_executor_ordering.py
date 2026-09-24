"""Ordering barriers in ``StreamingToolExecutor`` (Claude Code semantics).

An unsafe (writing) call waits for every earlier call; every later call waits
for the most recent earlier unsafe call; consecutive safe calls overlap. Plus
the flag defaults that feed it: ``is_concurrency_safe`` follows
``is_read_only`` unless set, and MCP tools are unsafe unless the server
annotates ``readOnlyHint``.
"""

from __future__ import annotations

import anyio

from mantis_agent.mcp.client import _read_only_hint
from mantis_agent.mcp.types import MCPTool
from mantis_agent.streaming.executor import StreamingToolExecutor
from mantis_agent.tools import Tool, ToolRegistry, tool
from mantis_agent.types import ToolUseBlock


def _call(i: int, name: str, **inp) -> ToolUseBlock:
    return ToolUseBlock(id=f"c{i}", name=name, input=inp)


def _reg(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.add(t)
    return reg


def _run(coro_fn):
    return anyio.run(coro_fn)


def _fs_tools(state: dict, log: list[str]) -> ToolRegistry:
    @tool(name="write", is_read_only=False)
    async def write(value: str) -> str:
        log.append("write:start")
        await anyio.sleep(0.05)
        state["v"] = value
        log.append("write:end")
        return "ok"

    @tool(name="read", is_read_only=True)
    async def read() -> str:
        log.append("read:start")
        await anyio.sleep(0.02)
        log.append("read:end")
        return state.get("v", "<unset>")

    @tool(name="boom", is_read_only=False)
    async def boom() -> str:
        await anyio.sleep(0.02)
        raise RuntimeError("kaboom")

    return _reg(write, read, boom)


def test_read_after_write_observes_the_write():
    state: dict = {}
    log: list[str] = []
    reg = _fs_tools(state, log)

    async def main():
        async with StreamingToolExecutor(reg) as ex:
            ex.add_tool_call(_call(0, "write", value="new"))
            ex.add_tool_call(_call(1, "read"))
            return await ex.wait_all()

    results = _run(main)
    assert [r.content for r in results] == ["ok", "new"]
    assert log.index("write:end") < log.index("read:start")


def test_write_waits_for_all_earlier_reads():
    state = {"v": "old"}
    log: list[str] = []
    reg = _fs_tools(state, log)

    async def main():
        async with StreamingToolExecutor(reg) as ex:
            ex.add_tool_call(_call(0, "read"))
            ex.add_tool_call(_call(1, "read"))
            ex.add_tool_call(_call(2, "write", value="new"))
            return await ex.wait_all()

    results = _run(main)
    # Both reads saw the pre-write state; the write started after both ended.
    assert [r.content for r in results] == ["old", "old", "ok"]
    last_read_end = max(i for i, e in enumerate(log) if e == "read:end")
    assert last_read_end < log.index("write:start")


def test_consecutive_safe_calls_still_overlap():
    starts: list[float] = []
    ends: list[float] = []

    @tool(name="slow_read", is_read_only=True)
    async def slow_read() -> str:
        starts.append(anyio.current_time())
        await anyio.sleep(0.1)
        ends.append(anyio.current_time())
        return "ok"

    reg = _reg(slow_read)

    async def main():
        async with StreamingToolExecutor(reg) as ex:
            for i in range(3):
                ex.add_tool_call(_call(i, "slow_read"))
            await ex.wait_all()

    _run(main)
    # All three started before the first one finished.
    assert max(starts) < min(ends)


def test_safe_calls_after_a_write_overlap_each_other_but_not_the_write():
    state: dict = {}
    log: list[str] = []
    reg = _fs_tools(state, log)

    async def main():
        async with StreamingToolExecutor(reg) as ex:
            ex.add_tool_call(_call(0, "write", value="x"))
            ex.add_tool_call(_call(1, "read"))
            ex.add_tool_call(_call(2, "read"))
            return await ex.wait_all()

    results = _run(main)
    assert [r.content for r in results] == ["ok", "x", "x"]
    assert log[:2] == ["write:start", "write:end"]
    # Both reads started before either ended (parallel after the barrier).
    assert log[2:4] == ["read:start", "read:start"]


def test_erroring_unsafe_call_releases_its_barrier():
    state = {"v": "old"}
    log: list[str] = []
    reg = _fs_tools(state, log)

    async def main():
        async with StreamingToolExecutor(reg) as ex:
            ex.add_tool_call(_call(0, "boom"))
            ex.add_tool_call(_call(1, "read"))
            ex.add_tool_call(_call(2, "write", value="new"))
            with anyio.fail_after(2):
                return await ex.wait_all()

    results = _run(main)
    assert results[0].is_error and "kaboom" in str(results[0].content)
    assert results[1].content == "old"
    assert results[2].content == "ok"


def test_denied_unsafe_call_does_not_block_later_calls():
    state = {"v": "old"}
    log: list[str] = []
    reg = _fs_tools(state, log)

    async def deny_writes(t, _inp, _ctx):
        return (t.name != "write", "no writes")

    async def main():
        async with StreamingToolExecutor(reg, can_use_tool=deny_writes) as ex:
            ex.add_tool_call(_call(0, "write", value="new"))
            ex.add_tool_call(_call(1, "read"))
            with anyio.fail_after(2):
                return await ex.wait_all()

    results = _run(main)
    assert results[0].is_error and "permission denied" in results[0].content
    assert results[1].content == "old"


def test_cancelled_run_does_not_hang_waiters():
    signal = anyio.Event()

    @tool(name="hang", is_read_only=False)
    async def hang() -> str:
        await anyio.sleep(30)
        return "never"

    @tool(name="peek", is_read_only=True)
    async def peek() -> str:
        return "peeked"

    reg = _reg(hang, peek)

    async def main():
        async with StreamingToolExecutor(reg, cancellation_signal=signal) as ex:
            ex.add_tool_call(_call(0, "hang"))
            ex.add_tool_call(_call(1, "peek"))
            await anyio.sleep(0.02)
            signal.set()
            with anyio.fail_after(2):
                return await ex.wait_all()

    results = _run(main)
    assert all(r.is_error for r in results)
    assert [r.content for r in results] == ["cancelled by signal"] * 2


def test_results_keep_insertion_order_across_barriers():
    state: dict = {}
    log: list[str] = []
    reg = _fs_tools(state, log)

    async def main():
        async with StreamingToolExecutor(reg) as ex:
            ex.add_tool_call(_call(0, "read"))
            ex.add_tool_call(_call(1, "write", value="a"))
            ex.add_tool_call(_call(2, "read"))
            return await ex.wait_all()

    results = _run(main)
    assert [r.tool_use_id for r in results] == ["c0", "c1", "c2"]
    assert [r.content for r in results] == ["<unset>", "ok", "a"]


# ---------------------------------------------------------------------------
# Flag defaults
# ---------------------------------------------------------------------------


async def _noop(**_kw):
    return ""


def test_concurrency_safe_defaults_to_read_only():
    assert Tool("a", "", {}, _noop).is_concurrency_safe is False
    assert Tool("a", "", {}, _noop, is_read_only=True).is_concurrency_safe is True
    # Explicit values win either way.
    assert Tool("a", "", {}, _noop, is_concurrency_safe=True).is_concurrency_safe is True
    assert Tool(
        "a", "", {}, _noop, is_read_only=True, is_concurrency_safe=False
    ).is_concurrency_safe is False


def test_decorator_defaults_follow_read_only_and_parallel_safe_alias():
    @tool(name="w")
    async def w() -> str:
        return ""

    @tool(name="r", is_read_only=True)
    async def r() -> str:
        return ""

    @tool(name="p", parallel_safe=True)
    async def p() -> str:
        return ""

    assert w.is_concurrency_safe is False
    assert r.is_concurrency_safe is True
    assert p.is_concurrency_safe is True


def test_builtin_writers_are_not_concurrency_safe():
    from mantis_agent.builtin_tools.fs import notebook_edit
    from mantis_agent.builtin_tools.memory_tool import remember

    assert notebook_edit.is_concurrency_safe is False
    assert remember.is_concurrency_safe is False


def test_mcp_tools_default_unsafe_unless_read_only_hint():
    base = dict(name="t", description="", input_schema={}, server_id="s")
    assert MCPTool(**base).to_mantis_agent_tool(None).is_concurrency_safe is False
    hinted = MCPTool(**base, read_only_hint=True).to_mantis_agent_tool(None)
    assert hinted.is_concurrency_safe is True
    # The hint alone never grants read-only permission treatment.
    assert hinted.is_read_only is False


def test_read_only_hint_parsing_is_strict():
    assert _read_only_hint({"annotations": {"readOnlyHint": True}}) is True
    assert _read_only_hint({"annotations": {"readOnlyHint": "true"}}) is False
    assert _read_only_hint({"annotations": {"readOnlyHint": False}}) is False
    assert _read_only_hint({"annotations": "junk"}) is False
    assert _read_only_hint({}) is False
