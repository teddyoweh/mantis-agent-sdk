"""MCP JSON-RPC client.

The client speaks the v0-relevant subset of the MCP protocol:

* ``initialize`` (handshake; capture server capabilities + info)
* ``notifications/initialized`` (post-handshake notice)
* ``tools/list`` (discover tools)
* ``tools/call`` (invoke a tool, returning ``CallToolResult``)
* ``notifications/cancelled`` (best-effort cancellation of a request)
* ``resources/list`` + ``resources/read`` (list / read server resources)
* ``prompts/list`` + ``prompts/get`` (list / render server prompt templates)
* ``elicitation/create`` (server-initiated; client routes to handler)

Concurrency model
-----------------
A single background task pumps the transport's ``receive()`` loop. Each
incoming message is one of:

* a *response* to a request we sent (``id`` matches an entry in
  ``_pending``) — we stash the response and signal the awaiting task via
  an ``anyio.Event``.
* a *notification* from the server (no ``id``) — handled inline. v0
  drops everything except ``notifications/cancelled``; future work hooks
  ``notifications/progress`` and ``notifications/message`` into the agent's
  hook system.
* a *request* from the server (``id`` + ``method``) — currently we
  recognize ``elicitation/create`` and route it to the client-supplied
  ``elicitation_handler``. Any other server-initiated method gets a
  ``-32601 method not found`` response. We never block the read loop on
  the handler; it runs in the same task group as the read loop.

Request ids are monotonic ints. A response carrying ``error`` raises
``MCPError``. The legacy elicitation error code (-32042) is still
repackaged into ``MCPElicitationRequest`` for back-compat with old
servers that embed the prompt as an error rather than a real request.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

import anyio
import msgspec

from ..errors import AgentError
from .transports.base import Transport, TransportClosed
from .transports.http import HttpTransport
from .transports.in_process import InProcessTransport
from .transports.sse import SseTransport
from .transports.stdio import StdioTransport
from .types import (
    CallToolResult,
    ElicitationHandler,
    ElicitationRequest,
    ElicitationResult,
    HttpServerConfig,
    MCPPrompt,
    MCPResource,
    MCPTool,
    SamplingHandler,
    SamplingMessage,
    SamplingRequest,
    SamplingResult,
    SdkServerConfig,
    ServerConfig,
    SseServerConfig,
    StdioServerConfig,
)

_log = logging.getLogger(__name__)

# JSON-RPC error code reserved by MCP for elicitation requests. The server
# returns this code on tools/call when it needs the user to answer a
# question or visit a URL mid-call. Spec is still in flux; -32042 is the
# value the SDK currently uses.
_ELICITATION_ERROR_CODE = -32042

# MCP protocol version we negotiate. Servers advertise their version on
# initialize; mismatched but compatible versions are common.
_PROTOCOL_VERSION = "2025-03-26"

# Hard cap on pagination pages for tools/resources/prompts listing. Combined
# with the repeated-cursor guard, this bounds a buggy or hostile server that
# never stops paginating (which would hang connect-time discovery and grow
# memory without limit).
_MAX_LIST_PAGES = 1000

# Backpressure cap on concurrent server-initiated requests (elicitation /
# sampling). Without it a misbehaving server can stream requests faster than
# handlers complete, spawning unbounded handler tasks and exhausting memory.
# Requests beyond the cap are rejected with -32603 instead of queued.
_MAX_INFLIGHT_SERVER_REQUESTS = 16


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MCPError(AgentError):
    """A JSON-RPC error response from an MCP server."""

    __slots__ = ("code", "raw_data")

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"mcp error {code}: {message}")
        self.code = code
        self.raw_data = data


class MCPElicitationRequest(AgentError):
    """The MCP server is asking the user for input mid-tool-call.

    v0 just surfaces this exception; the agent loop should catch it and
    prompt the user (or surface to its caller) per the M3 elicitation
    handler. The ``params`` dict is the raw elicitation payload from the
    server — typically a ``message``, optional ``schema`` for structured
    answers, and an ``id`` for the response.
    """

    __slots__ = ("params",)

    def __init__(self, params: dict[str, Any]):
        message = params.get("message") or "mcp server requested user input"
        super().__init__(f"mcp elicitation: {message}")
        self.params = params


class MCPProtocolError(AgentError):
    """The server returned a structurally invalid message."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_DECODER = msgspec.json.Decoder()


class MCPClient:
    """Connect to one MCP server, list its tools, call them.

    Use as an async context manager::

        async with MCPClient(StdioServerConfig(command="my-mcp")) as client:
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "hi"})

    The client owns its transport and its read loop. Concurrent calls from
    multiple tasks are safe — every request gets a unique id and waits on
    its own event.
    """

    __slots__ = (
        "config",
        "server_id",
        "server_info",
        "server_capabilities",
        "elicitation_handler",
        "sampling_handler",
        "request_timeout_s",
        "_transport",
        "_id_counter",
        "_pending",
        "_task_group",
        "_closed",
        "_initialized",
        "_inflight_server_requests",
    )

    def __init__(
        self,
        config: ServerConfig,
        *,
        server_id: str | None = None,
        elicitation_handler: ElicitationHandler | None = None,
        sampling_handler: SamplingHandler | None = None,
        request_timeout_s: float | None = 60.0,
    ) -> None:
        self.config = config
        # Stable id used by MCPTool.server_id. Falls back to a synthetic
        # name based on transport type when the config doesn't carry one.
        self.server_id = server_id or _derive_server_id(config)
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        # Callback that produces an ``ElicitationResult`` for an inbound
        # ``elicitation/create`` request. ``None`` means the client doesn't
        # advertise the capability and will respond to such requests with
        # ``-32601 method not found``.
        self.elicitation_handler: ElicitationHandler | None = elicitation_handler
        # Callback that produces a ``SamplingResult`` for an inbound
        # ``sampling/createMessage`` request. Same advertise-only-if-
        # registered rule as elicitation: a polite server won't ask
        # unless we said we could answer.
        self.sampling_handler: SamplingHandler | None = sampling_handler
        # Cap on how long ONE request may wait for its response — covers the
        # initialize handshake, tools/list, and every tools/call. ``None`` waits
        # forever (pre-existing behavior). Scoped around the response event only,
        # so it never crosses the client's task-group boundary.
        self.request_timeout_s = request_timeout_s
        self._transport: Transport | None = None
        self._id_counter = itertools.count(1)
        self._pending: dict[int, _PendingRequest] = {}
        self._task_group: anyio.abc.TaskGroup | None = None
        self._closed = False
        self._initialized = False
        # Outstanding server-initiated request handlers (bounded backpressure).
        self._inflight_server_requests = 0

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "MCPClient":
        self._transport = await _open_transport(self.config)
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._read_loop)
        await self._initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Fail any in-flight waiters cleanly.
        for pending in list(self._pending.values()):
            pending.error = TransportClosed("MCP client closing")
            pending.event.set()
        self._pending.clear()
        if self._transport is not None:
            try:
                await self._transport.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass
        if self._task_group is not None:
            try:
                self._task_group.cancel_scope.cancel()
                await self._task_group.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # -- public API ---------------------------------------------------------

    async def list_tools(self) -> list[MCPTool]:
        """Call ``tools/list`` and return the parsed tool definitions.

        Pagination (``cursor``) is honored: we loop until the server stops
        sending a ``nextCursor``. Most servers fit in a single response.
        """

        tools: list[MCPTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._request("tools/list", params)
            for raw in result.get("tools", []):
                if not isinstance(raw, dict):
                    continue
                tools.append(
                    MCPTool(
                        name=str(raw.get("name", "")),
                        description=str(raw.get("description", "")),
                        # MCP uses ``inputSchema``; we normalize to snake_case.
                        input_schema=raw.get("inputSchema") or {},
                        server_id=self.server_id,
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
            # Guard against a server that returns a non-advancing cursor
            # forever (unbounded loop / memory growth).
            if cursor in seen_cursors:
                raise MCPProtocolError(
                    "MCP server returned a repeating tools/list cursor"
                )
            seen_cursors.add(cursor)
        else:
            raise MCPProtocolError("MCP server exceeded tools/list pagination limit")
        return tools

    async def list_resources(self) -> list["MCPResource"]:
        """Call ``resources/list`` (paginated) — the readable blobs the server
        exposes (files, rows, API responses), addressed by URI."""
        out: list[MCPResource] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self._request("resources/list", params)
            for raw in result.get("resources", []):
                if isinstance(raw, dict) and raw.get("uri"):
                    out.append(MCPResource(
                        uri=str(raw["uri"]),
                        name=str(raw.get("name", "")),
                        description=str(raw.get("description", "")),
                        mime_type=raw.get("mimeType"),
                        server_id=self.server_id,
                    ))
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise MCPProtocolError(
                    "MCP server returned a repeating resources/list cursor"
                )
            seen_cursors.add(cursor)
        else:
            raise MCPProtocolError("MCP server exceeded resources/list pagination limit")
        return out

    async def read_resource(self, uri: str) -> str:
        """Call ``resources/read`` for ``uri`` and return the text contents.
        Binary parts are noted (not decoded) so the model isn't handed base64."""
        result = await self._request("resources/read", {"uri": uri})
        parts: list[str] = []
        for c in result.get("contents", []):
            if not isinstance(c, dict):
                continue
            if isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif "blob" in c:
                mt = c.get("mimeType", "application/octet-stream")
                parts.append(f"[binary resource {mt} — not shown]")
        return "\n".join(parts)

    async def list_prompts(self) -> list["MCPPrompt"]:
        """Call ``prompts/list`` (paginated) — the reusable named prompt
        templates the server offers."""
        out: list[MCPPrompt] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self._request("prompts/list", params)
            for raw in result.get("prompts", []):
                if isinstance(raw, dict) and raw.get("name"):
                    out.append(MCPPrompt(
                        name=str(raw["name"]),
                        description=str(raw.get("description", "")),
                        arguments=list(raw.get("arguments", []) or []),
                        server_id=self.server_id,
                    ))
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise MCPProtocolError(
                    "MCP server returned a repeating prompts/list cursor"
                )
            seen_cursors.add(cursor)
        else:
            raise MCPProtocolError("MCP server exceeded prompts/list pagination limit")
        return out

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call ``prompts/get`` and render the template's messages to text
        (``[role] text`` lines)."""
        result = await self._request(
            "prompts/get", {"name": name, "arguments": arguments or {}}
        )
        parts: list[str] = []
        for m in result.get("messages", []):
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            content = m.get("content", {})
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(f"[{role}] {content['text']}")
            elif isinstance(content, str):
                parts.append(f"[{role}] {content}")
        return "\n".join(parts)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> CallToolResult:
        """Invoke ``tools/call``. Returns the parsed ``CallToolResult``.

        Honors ``timeout_s`` via ``anyio.fail_after``; on timeout the client
        sends ``notifications/cancelled`` to the server (best-effort) and
        raises ``TimeoutError``. ``MCPElicitationRequest`` bubbles when the
        server returns elicitation error code -32042.

        ``timeout_s=None`` (the default) falls back to ``self.request_timeout_s``
        — the same budget the manager raises for mid-session tool calls — so
        that setting actually governs tool-call duration. A ``None`` effective
        timeout waits forever.
        """

        effective_timeout = timeout_s if timeout_s is not None else self.request_timeout_s

        request_id = next(self._id_counter)
        pending = _PendingRequest()
        self._pending[request_id] = pending

        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

        try:
            if effective_timeout is not None:
                with anyio.fail_after(effective_timeout):
                    await pending.event.wait()
            else:
                await pending.event.wait()
        except TimeoutError:
            # Best-effort cancellation notice. Server may or may not honor.
            self._pending.pop(request_id, None)
            await self._notify(
                "notifications/cancelled",
                {"requestId": request_id, "reason": "client timeout"},
            )
            raise

        self._pending.pop(request_id, None)
        if pending.error is not None:
            raise pending.error
        if pending.response is None:
            raise MCPProtocolError("MCP server returned no response payload")

        if "error" in pending.response:
            err = pending.response["error"]
            code = int(err.get("code", 0))
            message = str(err.get("message", "")) or "unknown error"
            data = err.get("data")
            if code == _ELICITATION_ERROR_CODE:
                params = data if isinstance(data, dict) else {"message": message}
                raise MCPElicitationRequest(params)
            raise MCPError(code, message, data)

        result = pending.response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError("tools/call result was not an object")
        return CallToolResult(
            content=list(result.get("content") or []),
            is_error=bool(result.get("isError", False)),
        )

    # -- handshake ----------------------------------------------------------

    async def _initialize(self) -> None:
        # Advertise client capabilities. ``elicitation: {}`` tells the
        # server "feel free to send me elicitation/create requests" —
        # only set when a handler is registered, otherwise a server that
        # honors our advertised capabilities and then sends us a prompt
        # would deadlock waiting for an answer we can't produce.
        # ``sampling: {}`` follows the same advertise-iff-registered rule.
        capabilities: dict[str, Any] = {}
        if self.elicitation_handler is not None:
            capabilities["elicitation"] = {}
        if self.sampling_handler is not None:
            capabilities["sampling"] = {}

        result = await self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": capabilities,
                "clientInfo": {
                    "name": "mantis-agent-sdk",
                    "version": "0.1.0",
                },
            },
        )
        self.server_info = result.get("serverInfo") or {}
        self.server_capabilities = result.get("capabilities") or {}
        # Post-handshake notice. Required by the spec before any other call.
        await self._notify("notifications/initialized", {})
        self._initialized = True

    # -- wire helpers -------------------------------------------------------

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request and wait for its response. Raises ``MCPError`` on
        error responses."""

        request_id = next(self._id_counter)
        pending = _PendingRequest()
        self._pending[request_id] = pending
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            if self.request_timeout_s is not None:
                with anyio.fail_after(self.request_timeout_s):
                    await pending.event.wait()
            else:
                await pending.event.wait()
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise MCPProtocolError(
                f"MCP server did not answer {method!r} within "
                f"{self.request_timeout_s:.0f}s"
            ) from None
        self._pending.pop(request_id, None)
        if pending.error is not None:
            raise pending.error
        if pending.response is None:
            raise MCPProtocolError(f"MCP server returned no response for {method!r}")
        if "error" in pending.response:
            err = pending.response["error"]
            raise MCPError(
                int(err.get("code", 0)),
                str(err.get("message", "")) or "unknown error",
                err.get("data"),
            )
        return pending.response.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Fire-and-forget JSON-RPC notification (no ``id``)."""

        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        if self._transport is None:
            raise TransportClosed("MCP transport not open")
        await self._transport.send(message)

    async def _read_loop(self) -> None:
        assert self._transport is not None
        try:
            while True:
                try:
                    message = await self._transport.receive()
                except TransportClosed:
                    break
                self._dispatch(message)
        finally:
            # Unblock any pending requests on shutdown.
            for pending in list(self._pending.values()):
                if not pending.event.is_set():
                    pending.error = TransportClosed("MCP transport closed")
                    pending.event.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        mid = message.get("id")
        method = message.get("method")
        if mid is not None and method is None:
            # Response to one of our requests. JSON-RPC ids may be strings or
            # numbers; correlate on the raw id first (covers string ids), then
            # fall back to an int-coerced key for servers that echo our
            # integer id back as a numeric string ("1" vs 1).
            pending = self._pending.get(mid)
            if pending is None and isinstance(mid, str):
                try:
                    pending = self._pending.get(int(mid))
                except ValueError:
                    pending = None
            if pending is None:
                return
            pending.response = message
            pending.event.set()
            return
        if method is not None and mid is not None:
            # Server-initiated REQUEST. Spawn the handler in our task
            # group so the read loop stays responsive — handlers may
            # take human time (CLI prompt, GUI dialog).
            if self._task_group is None:
                # No task group means we're closing; drop silently.
                return
            if self._inflight_server_requests >= _MAX_INFLIGHT_SERVER_REQUESTS:
                # Too many concurrent server-initiated requests — reject this
                # one instead of spawning an unbounded handler task.
                self._task_group.start_soon(self._reject_overflow, message)
                return
            self._inflight_server_requests += 1
            self._task_group.start_soon(self._handle_server_request, message)
            return
        # Server-initiated NOTIFICATION (no id, has method). v0: log and
        # ignore everything except notifications/cancelled, which the
        # spec lets either side send. Future hook integration goes here.
        if method == "notifications/cancelled":
            return
        _log.debug("mcp: dropping server notification %r", method)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        """Dispatch a server-initiated request to the right handler.

        Currently supports ``elicitation/create``. Anything else gets a
        ``-32601 method not found`` reply so the server isn't left
        hanging on a request id it owns.
        """

        mid = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        try:
            if method == "elicitation/create":
                await self._handle_elicitation(mid, params)
            elif method == "sampling/createMessage":
                await self._handle_sampling(mid, params)
            else:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {
                            "code": -32601,
                            "message": f"method not found: {method!r}",
                        },
                    }
                )
        except TransportClosed:
            # Client is shutting down; nothing more to do.
            pass
        except Exception as exc:  # noqa: BLE001 — never crash the read loop
            _log.warning("mcp: server request handler raised: %r", exc)
            try:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {
                            "code": -32603,
                            "message": f"client handler error: {exc!r}",
                        },
                    }
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            if self._inflight_server_requests > 0:
                self._inflight_server_requests -= 1

    async def _reject_overflow(self, message: dict[str, Any]) -> None:
        """Reject a server-initiated request that exceeds the concurrency cap."""
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {
                        "code": -32603,
                        "message": "too many concurrent server-initiated requests",
                    },
                }
            )
        except Exception:  # noqa: BLE001 — best-effort; never crash the read loop
            pass

    async def _handle_elicitation(self, mid: Any, params: dict[str, Any]) -> None:
        """Route an ``elicitation/create`` request to the client's handler.

        If no handler is registered we return a ``-32601`` error: the spec
        says the server should respect the client's advertised capabilities,
        but unprincipled servers exist and we don't want to silently hang.
        """

        if self.elicitation_handler is None:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {
                        "code": -32601,
                        "message": "client did not register an elicitation_handler",
                    },
                }
            )
            return

        request = ElicitationRequest(
            message=str(params.get("message", "")),
            requested_schema=params.get("requestedSchema") or {},
            raw=dict(params),
        )
        result = await self.elicitation_handler(request)
        if not isinstance(result, ElicitationResult):
            # Lenient: accept a plain dict if the handler returned one.
            if isinstance(result, dict):
                result = ElicitationResult(
                    action=result.get("action", "accept"),  # type: ignore[arg-type]
                    content=result.get("content") or {},
                )
            else:
                raise TypeError(
                    "elicitation_handler must return ElicitationResult, "
                    f"got {type(result).__name__}"
                )
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"action": result.action, "content": result.content},
            }
        )

    async def _handle_sampling(self, mid: Any, params: dict[str, Any]) -> None:
        """Route a ``sampling/createMessage`` request to the client's handler.

        Mirrors :meth:`_handle_elicitation` — if no handler is registered
        we return ``-32601`` instead of hanging. We're defensive about
        the params shape because sampling has the largest field set in
        the MCP spec and servers in the wild get some of it wrong
        (missing ``maxTokens``, integer ``temperature``, etc.).
        """

        if self.sampling_handler is None:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {
                        "code": -32601,
                        "message": "client did not register a sampling_handler",
                    },
                }
            )
            return

        # Parse messages. Malformed entries are skipped rather than
        # crashing — the handler still sees the well-formed ones.
        messages: list[SamplingMessage] = []
        for raw_msg in params.get("messages") or []:
            if not isinstance(raw_msg, dict):
                continue
            content = raw_msg.get("content")
            if not isinstance(content, dict):
                continue
            messages.append(
                SamplingMessage(
                    role=str(raw_msg.get("role") or "user"),
                    content=content,
                )
            )

        try:
            max_tokens = int(params.get("maxTokens") or 1024)
        except (TypeError, ValueError):
            max_tokens = 1024

        # System prompt / temperature / stop_sequences are all optional;
        # tolerate wrong types by coercing or skipping. This keeps the
        # handler from seeing a half-broken request.
        system_prompt = params.get("systemPrompt")
        if not isinstance(system_prompt, str):
            system_prompt = None

        raw_temp = params.get("temperature")
        if isinstance(raw_temp, (int, float)):
            temperature: float | None = float(raw_temp)
        else:
            temperature = None

        raw_stops = params.get("stopSequences")
        stop_sequences = list(raw_stops) if isinstance(raw_stops, list) else []

        raw_meta = params.get("metadata")
        metadata = dict(raw_meta) if isinstance(raw_meta, dict) else {}

        raw_prefs = params.get("modelPreferences")
        model_preferences = dict(raw_prefs) if isinstance(raw_prefs, dict) else {}

        raw_ctx = params.get("includeContext")
        include_context = raw_ctx if isinstance(raw_ctx, str) else None

        request = SamplingRequest(
            messages=messages,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            temperature=temperature,
            stop_sequences=stop_sequences,
            metadata=metadata,
            model_preferences=model_preferences,
            include_context=include_context,
            raw=dict(params),
        )
        result = await self.sampling_handler(request)
        if not isinstance(result, SamplingResult):
            # Lenient: accept a plain dict shaped like a result.
            if isinstance(result, dict):
                result = SamplingResult(
                    role=str(result.get("role") or "assistant"),
                    content=result.get("content") or {},
                    model=str(result.get("model") or ""),
                    stop_reason=result.get("stopReason"),
                )
            else:
                raise TypeError(
                    "sampling_handler must return SamplingResult, "
                    f"got {type(result).__name__}"
                )

        wire: dict[str, Any] = {
            "role": result.role,
            "content": result.content,
            "model": result.model,
        }
        if result.stop_reason is not None:
            wire["stopReason"] = result.stop_reason
        await self._send({"jsonrpc": "2.0", "id": mid, "result": wire})


# ---------------------------------------------------------------------------
# Pending-request bookkeeping
# ---------------------------------------------------------------------------


class _PendingRequest:
    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = anyio.Event()
        self.response: dict[str, Any] | None = None
        self.error: BaseException | None = None


# ---------------------------------------------------------------------------
# Transport factory
# ---------------------------------------------------------------------------


async def _open_transport(config: ServerConfig) -> Transport:
    if isinstance(config, StdioServerConfig):
        t: Transport = StdioTransport(config.command, config.args, config.env)
    elif isinstance(config, SseServerConfig):
        t = SseTransport(config.url, config.headers)
    elif isinstance(config, HttpServerConfig):
        t = HttpTransport(config.url, config.headers)
    elif isinstance(config, SdkServerConfig):
        if config.server is None:
            raise ValueError(
                "SdkServerConfig must carry an SdkServer; use create_sdk_server()"
            )
        t = InProcessTransport(config.server)
    else:
        raise TypeError(f"unknown server config: {type(config).__name__}")
    await t.__aenter__()
    return t


def _derive_server_id(config: ServerConfig) -> str:
    if isinstance(config, StdioServerConfig):
        return f"stdio:{config.command}"
    if isinstance(config, SseServerConfig):
        return f"sse:{config.url}"
    if isinstance(config, HttpServerConfig):
        return f"http:{config.url}"
    if isinstance(config, SdkServerConfig):
        return f"sdk:{config.name}"
    return "mcp:unknown"
