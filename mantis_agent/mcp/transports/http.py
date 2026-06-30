"""Streamable-HTTP transport — the modern MCP transport (2025 spec).

Single URL handles both directions. Each *client → server* JSON-RPC message
is sent as the body of an HTTP POST. The server responds with either:

* ``Content-Type: application/json`` and a single JSON-RPC response in the
  body — the synchronous one-shot case, used for ``tools/list`` etc.
* ``Content-Type: text/event-stream`` and a stream of SSE events, each one
  a JSON-RPC message. The stream remains open for the duration of the
  request and can deliver mid-stream notifications (progress updates,
  elicitations, etc.).

The session is identified by an ``Mcp-Session-Id`` header the server hands
back on ``initialize``; subsequent requests echo it.

Implementation notes
--------------------
* We keep a single in-process queue of ``receive()``-able messages. The POST
  task is responsible for parsing the response — sync or streaming — and
  feeding messages into the queue.
* For v0 we don't keep a long-lived ``GET`` channel open for server-initiated
  notifications. The spec allows the server to push via a dangling GET, but
  most servers in the wild rely on the POST-response stream. Adding the
  long-lived GET is a known follow-up.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import msgspec

from ...http import iter_sse, make_client
from .base import TransportClosed


_DECODER = msgspec.json.Decoder()
_ENCODER = msgspec.json.Encoder()


class HttpTransport:
    """Streamable-HTTP MCP transport (single URL, JSON or SSE responses)."""

    __slots__ = (
        "url",
        "headers",
        "_client",
        "_owns_client",
        "_session_id",
        "_inbox_tx",
        "_inbox_rx",
        "_task_group",
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
        self.headers.setdefault(
            "Accept", "application/json, text/event-stream"
        )
        self._client = client
        self._owns_client = client is None
        self._session_id: str | None = None
        self._inbox_tx, self._inbox_rx = anyio.create_memory_object_stream[
            dict[str, Any] | BaseException
        ](max_buffer_size=1024)
        self._task_group: anyio.abc.TaskGroup | None = None
        self._closed = False

    async def __aenter__(self) -> "HttpTransport":
        if self._client is None:
            self._client = make_client(headers=self.headers)
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def send(self, message: dict[str, Any]) -> None:
        """Fire-and-forget — kick off a POST task and return immediately.

        The task drains the response (json or sse) onto the inbox. Order of
        responses is preserved by the JSON-RPC ``id`` field that ``client.py``
        already correlates on.
        """

        if self._closed or self._client is None:
            raise TransportClosed("http transport is closed")
        assert self._task_group is not None
        self._task_group.start_soon(self._post_one, message)

    async def _post_one(self, message: dict[str, Any]) -> None:
        assert self._client is not None
        payload = _ENCODER.encode(message)
        headers = {**self.headers, "Content-Type": "application/json"}
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id

        is_notification = "id" not in message or message.get("id") is None

        try:
            async with self._client.stream(
                "POST", self.url, content=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    await self._inbox_tx.send(
                        TransportClosed(
                            f"http POST failed: {response.status_code}"
                        )
                    )
                    return
                # Capture session id from initialize response.
                sid = response.headers.get("Mcp-Session-Id") or response.headers.get(
                    "mcp-session-id"
                )
                if sid and self._session_id is None:
                    self._session_id = sid

                # Notifications get 202 Accepted with no body.
                if is_notification and response.status_code == 202:
                    return

                content_type = (
                    response.headers.get("Content-Type")
                    or response.headers.get("content-type")
                    or ""
                ).lower()

                if "text/event-stream" in content_type:
                    async for _name, data in iter_sse(response):
                        if isinstance(data, dict):
                            await self._inbox_tx.send(data)
                else:
                    body = await response.aread()
                    if not body:
                        return
                    decoded = _DECODER.decode(body)
                    # A response may be a single message or a batch.
                    if isinstance(decoded, list):
                        for item in decoded:
                            if isinstance(item, dict):
                                await self._inbox_tx.send(item)
                    elif isinstance(decoded, dict):
                        await self._inbox_tx.send(decoded)
        except Exception as exc:  # noqa: BLE001 — surface to receiver
            try:
                await self._inbox_tx.send(exc)
            except Exception:  # noqa: BLE001
                pass

    async def receive(self) -> dict[str, Any]:
        if self._closed:
            raise TransportClosed("http transport is closed")
        try:
            item = await self._inbox_rx.receive()
        except anyio.EndOfStream as e:
            raise TransportClosed("http transport ended") from e
        if isinstance(item, BaseException):
            raise TransportClosed(f"http read error: {item}") from item
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task_group is not None:
            try:
                self._task_group.cancel_scope.cancel()
                await self._task_group.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._inbox_tx.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._inbox_rx.aclose()
        except Exception:  # noqa: BLE001
            pass
        if self._owns_client and self._client is not None:
            await self._client.aclose()
