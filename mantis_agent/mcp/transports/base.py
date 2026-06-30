"""Transport protocol — the seam between MCP-the-protocol and MCP-the-wire.

Every JSON-RPC message — request, response, notification — flows through one
of these transports. The protocol is intentionally symmetric and dict-based:
the ``client.py`` JSON-RPC layer is unaware of which transport it's talking
to. Concrete transports (stdio, sse, http, in-process) each handle their
quirks (line framing, dual-channel SSE, HTTP request/response demux, async
queues) and expose the same dict-in / dict-out surface.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Bidirectional dict-message transport.

    Implementations are responsible for whatever framing the underlying
    channel requires (newline-delimited JSON for stdio, SSE frames for sse,
    HTTP request/response for streamable-http, an asyncio queue for
    in-process). Order of ``send`` calls is preserved end-to-end.
    """

    async def send(self, message: dict[str, Any]) -> None:
        """Send one JSON-RPC message to the peer."""
        ...

    async def receive(self) -> dict[str, Any]:
        """Wait for and return the next JSON-RPC message from the peer.

        Raises ``ConnectionError`` (or subclass) when the channel closes.
        """
        ...

    async def close(self) -> None:
        """Tear down the channel. Idempotent."""
        ...

    async def __aenter__(self) -> "Transport":
        ...

    async def __aexit__(self, exc_type, exc, tb) -> None:
        ...


class TransportClosed(ConnectionError):
    """Raised by ``Transport.receive`` when the channel has been closed."""
