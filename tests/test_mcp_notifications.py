from __future__ import annotations

import anyio

from mantis_agent.mcp.client import MCPClient
from mantis_agent.mcp.types import StdioServerConfig


def _client(handler=None) -> MCPClient:
    return MCPClient(
        StdioServerConfig(command="unused"),
        notification_handler=handler,
    )


def test_progress_notification_is_dispatched() -> None:
    received: list[tuple[str, dict]] = []

    async def handler(method: str, params: dict) -> None:
        received.append((method, params))

    async def main() -> None:
        client = _client(handler)
        async with anyio.create_task_group() as tg:
            client._task_group = tg
            client._dispatch({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": "build", "progress": 42, "total": 100},
            })
            for _ in range(10):
                if received:
                    break
                await anyio.sleep(0)
            tg.cancel_scope.cancel()

    anyio.run(main)
    assert received == [(
        "notifications/progress",
        {"progressToken": "build", "progress": 42, "total": 100},
    )]


def test_notification_handler_failure_does_not_break_response_dispatch() -> None:
    async def handler(method: str, params: dict) -> None:
        raise RuntimeError("broken UI")

    async def main() -> None:
        client = _client(handler)
        async with anyio.create_task_group() as tg:
            client._task_group = tg
            client._dispatch({
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "working"},
            })
            await anyio.sleep(0)
            tg.cancel_scope.cancel()

    anyio.run(main)


def test_notification_without_handler_is_ignored() -> None:
    client = _client()
    client._dispatch({
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {"progress": 1},
    })
