"""SSE transport — legacy MCP transport (pre-2025-spec).

Dual-channel:
* ``GET <url>`` opens an SSE stream that delivers *server → client* messages.
* The server's first SSE event is an ``endpoint`` event whose ``data`` is
  the URL the client should ``POST`` *client → server* messages to.
* Subsequent SSE events of type ``message`` are JSON-RPC messages from the
  server.

This transport is still widely deployed (especially in self-hosted MCP
servers built before the Streamable-HTTP spec landed). New servers prefer
the http transport; we support both.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import anyio
import httpx
import msgspec

from ...http import iter_sse, make_client
from .base import TransportClosed


_DECODER = msgspec.json.Decoder()
_ENCODER = msgspec.json.Encoder()


class SseTransport:
    """SSE inbound + HTTP POST outbound."""

    __slots__ = (
        "url",
        "headers",
        "_client",
        "_owns_client",
        "_post_url",
        "_endpoint_ready",
        "_inbox_tx",
        "_inbox_rx",
        "_task_group",
        "_response",
        "_response_cm",
        "_closed",
    )

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        # Force SSE-friendly accept header.
        self.headers.setdefault("Accept", "text/event-stream")
        self._client = client
        self._owns_client = client is None
        self._post_url: str | None = None
        self._endpoint_ready = anyio.Event()
        # Unbounded internal queue: server may push at any rate.
        self._inbox_tx, self._inbox_rx = anyio.create_memory_object_stream[
            dict[str, Any] | BaseException
        ](max_buffer_size=1024)
        self._task_group: anyio.abc.TaskGroup | None = None
        self._response: httpx.Response | None = None
        self._response_cm: Any = None
        self._closed = False

    async def __aenter__(self) -> "SseTransport":
        if self._client is None:
            self._client = make_client(headers=self.headers)
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._read_loop)
        # Wait for the server to send us its POST endpoint.
        with anyio.fail_after(30.0):
            await self._endpoint_ready.wait()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _read_loop(self) -> None:
        assert self._client is not None
        try:
            self._response_cm = self._client.stream("GET", self.url, headers=self.headers)
            self._response = await self._response_cm.__aenter__()
            self._response.raise_for_status()
            async for event_name, data in iter_sse(self._response):
                if event_name == "endpoint":
                    # MCP SSE spec: the endpoint event's *data* is the path
                    # (relative or absolute) to POST to. Servers vary on
                    # whether they JSON-encode it as a string or send it
                    # raw. ``iter_sse`` already parsed via msgspec, so a
                    # string payload arrives as ``str`` here.
                    if isinstance(data, str):
                        endpoint = data
                    elif isinstance(data, dict):
                        endpoint = data.get("uri") or data.get("url") or ""
                    else:
                        endpoint = ""
                    self._post_url = urljoin(self.url, endpoint) if endpoint else self.url
                    self._endpoint_ready.set()
                elif event_name in ("message", "default"):
                    if isinstance(data, dict):
                        await self._inbox_tx.send(data)
                # Other event types (ping, etc.) are ignored.
        except Exception as exc:  # noqa: BLE001 — surface to the receiver
            try:
                await self._inbox_tx.send(exc)
            except Exception:  # noqa: BLE001
                pass
            # Unblock initialize even on early failure.
            if not self._endpoint_ready.is_set():
                self._endpoint_ready.set()
        finally:
            try:
                await self._inbox_tx.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed or self._client is None or self._post_url is None:
            raise TransportClosed("sse transport is closed or not ready")
        payload = _ENCODER.encode(message)
        headers = {**self.headers, "Content-Type": "application/json"}
        resp = await self._client.post(self._post_url, content=payload, headers=headers)
        if resp.status_code >= 400:
            raise TransportClosed(
                f"sse POST failed: {resp.status_code} {resp.text[:200]}"
            )

    async def receive(self) -> dict[str, Any]:
        if self._closed:
            raise TransportClosed("sse transport is closed")
        try:
            item = await self._inbox_rx.receive()
        except anyio.EndOfStream as e:
            raise TransportClosed("sse stream ended") from e
        if isinstance(item, BaseException):
            raise TransportClosed(f"sse read error: {item}") from item
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._response_cm is not None:
            try:
                await self._response_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if self._task_group is not None:
            try:
                self._task_group.cancel_scope.cancel()
                await self._task_group.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._inbox_rx.aclose()
        except Exception:  # noqa: BLE001
            pass
        if self._owns_client and self._client is not None:
            await self._client.aclose()
