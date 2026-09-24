"""MCPManager connects servers concurrently (startup ≈ max handshake, not the
sum), isolates a failing server, keeps config order, caches ``tools/list``
for its lifetime and refreshes it on ``notifications/tools/list_changed``."""

from __future__ import annotations

import time

import anyio

from mantis_agent.mcp import manager as manager_mod
from mantis_agent.mcp.manager import MCPManager
from mantis_agent.mcp.types import MCPTool, StdioServerConfig

_LIST_CALLS: dict[str, int] = {}


class _FakeClient:
    """Stands in for MCPClient: the handshake delay is taken from the config's
    ``args`` (``["0.3"]``), ``command == "boom"`` fails the handshake."""

    def __init__(self, cfg, *, server_id, request_timeout_s=None, notification_handler=None):
        self.cfg = cfg
        self.server_id = server_id
        self.request_timeout_s = request_timeout_s
        self.notification_handler = notification_handler
        self.closed = False
        self.tool_names = [f"t_{server_id}"]

    async def __aenter__(self):
        await anyio.sleep(float(self.cfg.args[0]) if self.cfg.args else 0)
        if self.cfg.command == "boom":
            raise ConnectionRefusedError("refused")
        return self

    async def list_tools(self):
        _LIST_CALLS[self.server_id] = _LIST_CALLS.get(self.server_id, 0) + 1
        return [MCPTool(name=n, description="", input_schema={"type": "object"},
                        server_id=self.server_id) for n in self.tool_names]

    async def close(self):
        self.closed = True


def _cfg(delay: float, command: str = "fake") -> StdioServerConfig:
    return StdioServerConfig(command=command, args=[str(delay)])


def test_connect_all_runs_handshakes_concurrently(monkeypatch) -> None:
    monkeypatch.setattr(manager_mod, "MCPClient", _FakeClient)
    # Deliberately slowest-first so completion order != config order.
    mgr = MCPManager({"a": _cfg(0.4), "b": _cfg(0.3), "c": _cfg(0.2), "d": _cfg(0.1)})

    async def go():
        t0 = time.perf_counter()
        tools = await mgr.connect_all(timeout_s=5.0)
        elapsed = time.perf_counter() - t0
        assert [t.name for t in tools] == [
            "mcp__a__t_a", "mcp__b__t_b", "mcp__c__t_c", "mcp__d__t_d"]
        assert list(mgr.tools) == ["a", "b", "c", "d"]
        # Serial would be 1.0s; concurrent is ≈ the slowest (0.4s).
        assert elapsed < 0.8, elapsed
        clients = list(mgr.clients.values())
        await mgr.aclose()
        assert all(c.closed for c in clients)

    anyio.run(go)


def test_one_failing_server_does_not_block_the_rest(monkeypatch) -> None:
    monkeypatch.setattr(manager_mod, "MCPClient", _FakeClient)
    mgr = MCPManager({"bad": _cfg(0.05, "boom"), "slow": _cfg(0.3), "fast": _cfg(0.0)})

    async def go():
        tools = await mgr.connect_all(timeout_s=5.0)
        assert [t.name for t in tools] == ["mcp__slow__t_slow", "mcp__fast__t_fast"]
        assert mgr.errors == {"bad": "ConnectionRefusedError: refused"}
        states = {r["name"]: r["state"] for r in mgr.status_rows()}
        assert states == {"bad": "failed", "slow": "connected", "fast": "connected"}
        await mgr.aclose()

    anyio.run(go)


def test_wedged_server_is_abandoned_without_holding_others(monkeypatch) -> None:
    monkeypatch.setattr(manager_mod, "MCPClient", _FakeClient)
    # timeout_s=0.05 → overall bound 5.1s; the wedged server sleeps longer.
    mgr = MCPManager({"wedged": _cfg(30), "ok": _cfg(0.0)})

    async def go():
        t0 = time.perf_counter()
        tools = await mgr.connect_all(timeout_s=0.05)
        assert time.perf_counter() - t0 < 8
        assert [t.name for t in tools] == ["mcp__ok__t_ok"]
        assert "wedged" in mgr.errors
        await mgr.aclose()

    anyio.run(go)


def test_close_from_a_different_task_than_connect(monkeypatch) -> None:
    """Each client lives in its own host task, so connect in one task and
    aclose() in another is safe (no misnested cancel scopes)."""
    mgr = MCPManager({"echoes": _echo_server_config()})

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mgr.connect_all)
        assert list(mgr.clients) == ["echoes"]
        out = await mgr.tools["echoes"][0].fn(text="x")
        assert out == "echo:x"
        await mgr.aclose()
        assert mgr.clients == {}

    anyio.run(go)


def test_tools_list_cached_and_refreshed_on_list_changed(monkeypatch) -> None:
    monkeypatch.setattr(manager_mod, "MCPClient", _FakeClient)
    _LIST_CALLS.clear()
    mgr = MCPManager({"s": _cfg(0.0)})
    changed: list[str] = []
    mgr.on_tools_changed = changed.append

    async def go():
        await mgr.connect_all()
        assert _LIST_CALLS == {"s": 1}
        # A second connect_all reuses the live client — no re-list, no respawn.
        tools = await mgr.connect_all()
        assert [t.name for t in tools] == ["mcp__s__t_s"]
        assert _LIST_CALLS == {"s": 1}
        client = mgr.clients["s"]
        client.tool_names = ["t_s", "extra"]
        # Unrelated notifications don't re-list.
        await client.notification_handler("notifications/progress", {})
        assert _LIST_CALLS == {"s": 1}
        await client.notification_handler("notifications/tools/list_changed", {})
        assert _LIST_CALLS == {"s": 2}
        assert [t.name for t in mgr.tools["s"]] == ["mcp__s__t_s", "mcp__s__extra"]
        assert changed == ["s"]
        await mgr.aclose()

    anyio.run(go)


def _echo_server_config():
    from mantis_agent.mcp import create_sdk_server
    from mantis_agent.tools import tool

    @tool(name="echo")
    async def echo(text: str) -> str:
        """Echo text back.

        Args:
            text: What to echo.
        """
        return f"echo:{text}"

    return create_sdk_server("echoes", tools=[echo])


# -- teardown / retry / list_changed races -----------------------------------


class _HangingClient(_FakeClient):
    """A transport that never finishes opening."""

    instances: list["_HangingClient"] = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        _HangingClient.instances.append(self)

    async def __aenter__(self):
        await anyio.sleep_forever()


def test_stop_mid_handshake_returns_promptly(monkeypatch) -> None:
    """Quitting while a server is still handshaking must not wait out the
    handshake: stop(timeout_s=0.5) cancels the runner, and the runner's
    teardown cancels the host that isn't listening for its stop event yet."""
    import asyncio

    monkeypatch.setattr(manager_mod, "MCPClient", _HangingClient)
    _HangingClient.instances.clear()
    mgr = MCPManager({"hang": _cfg(0.0)})

    async def go():
        starter = asyncio.ensure_future(mgr.start())
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        await mgr.stop(timeout_s=0.5)
        assert time.perf_counter() - t0 < 1.5
        assert await asyncio.wait_for(starter, 1.0) == []
        assert all(c.closed for c in _HangingClient.instances)

    asyncio.run(go())


def test_aclose_cancels_host_still_handshaking(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(manager_mod, "MCPClient", _HangingClient)
    mgr = MCPManager({"hang": _cfg(0.0)})

    async def go():
        connecting = asyncio.ensure_future(mgr.connect_all(timeout_s=30))
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        await mgr.aclose()
        assert time.perf_counter() - t0 < 1.0
        outcome = await asyncio.wait_for(connecting, 1.0)
        assert outcome == []
        assert "hang" in mgr.errors

    asyncio.run(go())


def test_retry_keeps_superseded_host_until_aclose(monkeypatch) -> None:
    """A retried server's previous host (still closing) is held and awaited by
    aclose(), not orphaned to the GC."""
    import asyncio

    release: dict[str, asyncio.Event] = {}

    class _SlowClose(_FakeClient):
        async def close(self):
            await release["ev"].wait()
            self.closed = True

    monkeypatch.setattr(manager_mod, "MCPClient", _SlowClose)
    mgr = MCPManager({"s": _cfg(30)})

    async def go():
        release["ev"] = asyncio.Event()
        await mgr.connect_all(timeout_s=0.05)     # wedged → abandoned, closing
        assert "s" in mgr.errors
        first = mgr._hosts["s"]
        assert not first.done()                   # blocked in close()
        mgr.configs["s"] = _cfg(0.0)
        await mgr.connect_all(timeout_s=1.0)      # retry succeeds
        assert "s" in mgr.clients
        assert first in mgr._retired_hosts
        release["ev"].set()
        await mgr.aclose()
        assert first.done()
        assert mgr._retired_hosts == []

    asyncio.run(go())


def test_list_changed_during_connect_is_not_lost(monkeypatch) -> None:
    """A list_changed that lands while other servers are still connecting is
    applied (the server is registered at handshake) and not overwritten by the
    handshake's older tools/list when the gather finishes."""
    import asyncio

    monkeypatch.setattr(manager_mod, "MCPClient", _FakeClient)
    mgr = MCPManager({"fast": _cfg(0.0), "slow": _cfg(0.3)})

    async def go():
        connecting = asyncio.ensure_future(mgr.connect_all(timeout_s=5.0))
        await asyncio.sleep(0.1)
        client = mgr.clients["fast"]        # live before the gather finishes
        client.tool_names = ["t_fast", "late"]
        await client.notification_handler("notifications/tools/list_changed", {})
        await connecting
        assert [t.name for t in mgr.tools["fast"]] == ["mcp__fast__t_fast", "mcp__fast__late"]
        await mgr.aclose()

    asyncio.run(go())


def test_older_refetch_cannot_land_last(monkeypatch) -> None:
    import asyncio

    gates: list[asyncio.Event] = []

    class _Gated(_FakeClient):
        async def list_tools(self):
            names = list(self.tool_names)
            if gates:
                await gates.pop(0).wait()
            return [MCPTool(name=n, description="", input_schema={"type": "object"},
                            server_id=self.server_id) for n in names]

    monkeypatch.setattr(manager_mod, "MCPClient", _Gated)
    mgr = MCPManager({"s": _cfg(0.0)})

    async def go():
        await mgr.connect_all()
        client = mgr.clients["s"]
        slow, fast = asyncio.Event(), asyncio.Event()
        gates.extend([slow, fast])
        client.tool_names = ["old"]
        first = asyncio.ensure_future(
            client.notification_handler("notifications/tools/list_changed", {}))
        await asyncio.sleep(0)
        client.tool_names = ["new"]
        second = asyncio.ensure_future(
            client.notification_handler("notifications/tools/list_changed", {}))
        await asyncio.sleep(0)
        fast.set()
        await second
        slow.set()
        await first
        assert [t.name for t in mgr.tools["s"]] == ["mcp__s__new"]
        await mgr.aclose()

    asyncio.run(go())
