"""``query()`` reuses a loop-scoped pooled httpx client instead of building
(and handshaking) a fresh one per call; plain ``make_client`` is unchanged."""

from __future__ import annotations

import asyncio

from mantis_agent import http as http_mod
from mantis_agent.http import aclose_shared_clients, make_client, sharing_http_clients


def test_make_client_is_private_outside_the_sharing_scope() -> None:
    async def go():
        a, b = make_client(base_url="http://x"), make_client(base_url="http://x")
        assert a is not b
        await a.aclose()
        assert a.is_closed
        await b.aclose()

    asyncio.run(go())


def test_pooled_client_is_reused_per_loop_and_key() -> None:
    async def go():
        with sharing_http_clients():
            a = make_client(base_url="http://x", headers={"A": "1"})
            b = make_client(base_url="http://x", headers={"A": "1"})
            other_headers = make_client(base_url="http://x", headers={"A": "2"})
            other_url = make_client(base_url="http://y", headers={"A": "1"})
        assert a is b
        assert a is not other_headers and a is not other_url
        # A borrower closing it (provider.aclose / async with) doesn't close it.
        await a.aclose()
        async with b:
            pass
        assert not a.is_closed
        await aclose_shared_clients()
        assert a.is_closed and other_url.is_closed
        return a

    first = asyncio.run(go())

    async def again():
        with sharing_http_clients():
            return make_client(base_url="http://x", headers={"A": "1"})

    # A new event loop never inherits another loop's pool.
    second = asyncio.run(again())
    assert second is not first


def test_sharing_needs_a_running_loop() -> None:
    with sharing_http_clients():
        a = make_client(base_url="http://x")
        b = make_client(base_url="http://x")
    assert a is not b


def test_query_reuses_the_provider_client_across_calls(monkeypatch) -> None:
    import mantis_agent.compat_query as cq
    from mantis_agent.claude_compat import MantisAgentOptions

    seen = []
    real_build = cq._build_agent

    def spy(opts):
        agent = real_build(opts)
        seen.append(agent.provider.client)
        return agent

    monkeypatch.setattr(cq, "_build_agent", spy)
    opts = MantisAgentOptions(model="qwen3:8b", base_url="http://127.0.0.1:9/v1")

    async def one_call():
        gen = cq.query(prompt="hi", options=opts)
        async for _ in gen:
            break   # the init banner; the agent (and its client) now exist
        await gen.aclose()

    async def go():
        await one_call()
        await one_call()
        client = seen[0]
        assert seen[1] is client
        assert not client.is_closed   # agent.aclose() didn't close the pooled client
        await aclose_shared_clients()
        assert client.is_closed

    asyncio.run(go())
    assert len(seen) == 2
    assert not http_mod._SHARE.get()   # scope fully unwound


def test_asyncio_run_teardown_closes_the_loops_pooled_clients() -> None:
    """No explicit aclose_shared_clients(): the loop-shutdown sentinel closes
    the pool when asyncio.run() finalizes async generators, and the loop's
    entry is gone — a loop + client + sockets don't leak per asyncio.run()."""
    import gc
    import weakref

    held: dict = {}

    async def go():
        with sharing_http_clients():
            held["client"] = make_client(base_url="http://leak")
        held["loop"] = weakref.ref(asyncio.get_running_loop())

    asyncio.run(go())
    assert held["client"].is_closed
    gc.collect()
    assert held["loop"]() is None


def test_closed_loop_pools_are_pruned() -> None:
    """A loop closed by hand (no shutdown_asyncgens) is pruned on the next
    pool lookup instead of being pinned forever by its clients."""
    import gc
    import weakref

    loop = asyncio.new_event_loop()

    async def pool_one():
        with sharing_http_clients():
            return make_client(base_url="http://prune")

    loop.run_until_complete(pool_one())
    loop_ref = weakref.ref(loop)
    loop.close()
    del loop

    async def again():
        with sharing_http_clients():
            c = make_client(base_url="http://prune")
        await aclose_shared_clients()
        return c

    asyncio.run(again())
    assert not any(lp.is_closed() for lp in list(http_mod._POOLS.keys()))
    assert not any(lp.is_closed() for lp in list(http_mod._SENTINELS.keys()))
    gc.collect()
    assert loop_ref() is None


def test_retry_env_is_part_of_the_pool_key(monkeypatch) -> None:
    async def go():
        with sharing_http_clients():
            monkeypatch.setenv("MANTIS_AGENT_RETRY_ATTEMPTS", "2")
            a = make_client(base_url="http://env")
            b = make_client(base_url="http://env")
            monkeypatch.setenv("MANTIS_AGENT_RETRY_ATTEMPTS", "7")
            c = make_client(base_url="http://env")
        assert a is b and a is not c
        assert c._transport._attempts == 7
        await aclose_shared_clients()

    asyncio.run(go())
