"""In-process transport — server runs in the same asyncio task group.

No serialization to bytes, no network, no subprocess. Used by
``create_sdk_server`` to expose local ``@tool``-decorated functions through
the same MCP protocol path that remote servers use. This is how upstream's
Claude Code lets a sub-agent run with its own ``McpServer`` that surfaces a
curated tool subset.

Wire shape is identical to the network transports: both sides exchange
dict-shaped JSON-RPC messages over two ``anyio`` memory streams. The
``SdkServer`` is responsible for its own run loop that consumes inbound
requests and produces responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio

from .base import TransportClosed

if TYPE_CHECKING:
    from ..server import SdkServer


class InProcessTransport:
    """Bidirectional channel between an ``MCPClient`` and an ``SdkServer``.

    Two unbounded memory streams act as the wire. The server takes its half
    on ``__aenter__``; the client uses the other half via ``send`` /
    ``receive``. Closing the transport cancels the server's run task.
    """

    __slots__ = (
        "server",
        "_to_server_tx",
        "_to_server_rx",
        "_to_client_tx",
        "_to_client_rx",
        "_task_group",
        "_closed",
    )

    def __init__(self, server: "SdkServer") -> None:
        self.server = server
        self._to_server_tx, self._to_server_rx = anyio.create_memory_object_stream[
            dict[str, Any]
        ](max_buffer_size=1024)
        self._to_client_tx, self._to_client_rx = anyio.create_memory_object_stream[
            dict[str, Any]
        ](max_buffer_size=1024)
        self._task_group: anyio.abc.TaskGroup | None = None
        self._closed = False

    async def __aenter__(self) -> "InProcessTransport":
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        # Server reads from to_server, writes to to_client.
        self._task_group.start_soon(
            self.server.run, self._to_server_rx, self._to_client_tx
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise TransportClosed("in-process transport is closed")
        try:
            await self._to_server_tx.send(message)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as e:
            raise TransportClosed("in-process server closed") from e

    async def receive(self) -> dict[str, Any]:
        if self._closed:
            raise TransportClosed("in-process transport is closed")
        try:
            return await self._to_client_rx.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError) as e:
            raise TransportClosed("in-process server closed") from e

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Closing the to_server stream signals the server to wind down.
        try:
            await self._to_server_tx.aclose()
        except Exception:  # noqa: BLE001
            pass
        if self._task_group is not None:
            try:
                self._task_group.cancel_scope.cancel()
                await self._task_group.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._to_client_rx.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._to_server_rx.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._to_client_tx.aclose()
        except Exception:  # noqa: BLE001
            pass
