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

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin

import anyio
import httpx
import msgspec

from ...http import make_client
from .base import TransportClosed


# MCP servers are interactive infrastructure, not a model API. ``make_client``'s
# defaults (10s connect, four backed-off retries) turn one wrong URL into a
# ~40s stall before /mcp can say "failed", so the MCP transports connect fast
# and fail fast instead. The read budget stays generous: a live session may sit
# idle between server events.
_MCP_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=10.0, pool=5.0)


_DECODER = msgspec.json.Decoder()
_ENCODER = msgspec.json.Encoder()


async def _iter_sse_frames(
    response: httpx.Response,
) -> AsyncIterator[tuple[str, str]]:
    """Yield ``(event_name, raw_data)`` SSE frames without decoding the data.

    The shared model-API ``iter_sse`` JSON-decodes every event's data and
    raises on non-JSON — but the standard MCP ``endpoint`` event carries a
    RAW URL path (``data: /messages/?session_id=...``), which is not JSON.
    Parsing frames here keeps ``endpoint`` a plain string; only ``message``
    events are JSON-decoded by the caller.
    """

    event_name: str | None = None
    data_chunks: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_chunks:
                yield (event_name or "message", "\n".join(data_chunks))
            event_name = None
            data_chunks = []
            continue
        if line.startswith(":"):
            # SSE comment / keepalive — ignore.
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            chunk = line[5:]
            if chunk.startswith(" "):
                chunk = chunk[1:]
            data_chunks.append(chunk)
        # Other fields (id:, retry:) are ignored.
    # Trailing event if the server didn't send a final blank line.
    if data_chunks:
        yield (event_name or "message", "\n".join(data_chunks))


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
            self._client = make_client(headers=self.headers, timeout=_MCP_TIMEOUT,
                                       retries=False)
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
            async for event_name, data in _iter_sse_frames(self._response):
                if event_name == "endpoint":
                    # MCP SSE spec: the endpoint event's *data* is the path
                    # (relative or absolute) to POST to. The reference server
                    # sends it as a RAW string; some servers JSON-encode it as
                    # a quoted string or an object with a uri/url field.
                    endpoint = self._parse_endpoint(data)
                    self._post_url = urljoin(self.url, endpoint) if endpoint else self.url
                    self._endpoint_ready.set()
                elif event_name in ("message", "default"):
                    try:
                        msg = _DECODER.decode(data)
                    except msgspec.DecodeError:
                        continue
                    if isinstance(msg, dict):
                        await self._inbox_tx.send(msg)
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

    @staticmethod
    def _parse_endpoint(data: str) -> str:
        """Resolve the ``endpoint`` event's data to a POST path.

        The reference MCP SSE server sends a raw path; be lenient about servers
        that JSON-quote the string or wrap it in an object. Only attempt JSON
        when the payload looks like JSON, so a bare path is never decode-failed.
        """
        text = data.strip()
        if not text:
            return ""
        if text[0] in "{\"":
            try:
                decoded = _DECODER.decode(text)
            except msgspec.DecodeError:
                return text
            if isinstance(decoded, str):
                return decoded
            if isinstance(decoded, dict):
                uri = decoded.get("uri") or decoded.get("url")
                return str(uri) if uri else ""
            return ""
        return text

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
