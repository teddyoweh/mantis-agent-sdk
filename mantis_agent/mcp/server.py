"""In-process MCP server — surface local ``Tool``s through the MCP protocol.

Used to expose a curated set of tools to a sub-agent (or to anything that
speaks MCP) without spinning up a subprocess. Speaks the same JSON-RPC
methods a real MCP server would over stdio/sse/http, but the wire is just
a pair of ``anyio`` memory streams owned by ``InProcessTransport``.

We implement the v0-relevant subset:

* ``initialize`` — capability negotiation
* ``notifications/initialized`` — handshake completion notice
* ``tools/list`` — emit each tool's name + description + input schema
* ``tools/call`` — dispatch through the underlying ``Tool.fn``
* ``notifications/cancelled`` — recorded but not actionable in v0
* server-initiated ``elicitation/create`` — tools can ask the client
  (and the user behind it) for input mid-call via ``ctx.elicit(...)``

Anything else gets a ``-32601 Method not found`` error response.

Public entry point is ``create_sdk_server(name, tools)`` which returns an
``SdkServerConfig`` ready to drop into ``MCPClient``.

Elicitation flow
----------------
A tool function whose signature includes ``ctx`` (or ``context``) receives
a ``ServerContext`` bound to the current request. The tool calls
``await ctx.elicit(message, schema)`` which:

1. Allocates a fresh JSON-RPC id from the server's monotonic counter.
2. Sends an ``elicitation/create`` request on the outbox.
3. Awaits an ``anyio.Event`` keyed by that id.
4. When the client responds (routed through ``_handle`` below), unblocks
   and returns the parsed ``ElicitationResult`` to the tool.

The server only sends elicitation requests to clients that advertised
the ``elicitation`` capability in initialize — calling ``ctx.elicit`` on
a non-elicitation-capable client raises ``ElicitationNotSupportedError``
so tool authors don't accidentally hang.
"""

from __future__ import annotations

import inspect
import itertools
from typing import Any

import anyio

from ..errors import AgentError
from ..tools import Tool, ToolRegistry, _stringify_result
from .types import (
    ElicitationRequest,
    ElicitationResult,
    SamplingMessage,
    SamplingResult,
    SdkServerConfig,
)


_PROTOCOL_VERSION = "2025-03-26"


class ElicitationNotSupportedError(AgentError):
    """The connected client did not advertise the ``elicitation`` capability.

    Raised when a tool calls ``ctx.elicit(...)`` but the handshake came
    back without ``capabilities.elicitation``. Tool authors should either
    handle this and fall back to a deterministic default, or document
    elicitation as a hard requirement.
    """


class SamplingNotSupportedError(AgentError):
    """The connected client did not advertise the ``sampling`` capability.

    Raised when a tool calls ``ctx.sample(...)`` but the handshake came
    back without ``capabilities.sampling``. Tool authors that need to
    call back into the agent's model must either handle this and fall
    back, or document sampling as a hard requirement.
    """


class ServerContext:
    """Per-request handle passed to tool functions that declare a ``ctx`` arg.

    Tool authors use this to interact with the connected client mid-call:

    * ``await ctx.elicit(message, schema)`` — ask the user a question;
      returns an ``ElicitationResult`` describing what they answered (if
      anything).

    Other server→client interactions (progress notifications, sampling,
    logging) will hang off this same context as they're implemented.
    """

    __slots__ = ("_server", "_outbox", "request_id")

    def __init__(
        self,
        server: "SdkServer",
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
        request_id: Any,
    ) -> None:
        self._server = server
        self._outbox = outbox
        # The id of the tools/call request this tool was invoked under.
        # Stored for future use (progress notifications, cancellation tying).
        self.request_id = request_id

    @property
    def client_capabilities(self) -> dict[str, Any]:
        """Capabilities the connected client advertised on initialize."""
        return self._server.client_capabilities

    async def elicit(
        self,
        message: str,
        requested_schema: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> ElicitationResult:
        """Ask the client (via the user) a question and wait for an answer.

        ``requested_schema`` is an optional JSON Schema the client may use
        to render a structured form. Headless clients can ignore it and
        return free-form ``content``.

        Returns an ``ElicitationResult``. Tool code should branch on
        ``result.action``: ``"accept"`` means the user submitted a real
        answer (``result.content`` is meaningful); ``"decline"`` / ``"cancel"``
        mean the tool should usually return an error or sensible default.

        Raises:
            ElicitationNotSupportedError: client never advertised the
                capability — calling ``elicit`` would deadlock.
            TimeoutError: ``timeout_s`` elapsed before the client replied.
                The server sends ``notifications/cancelled`` on the way out
                so polite clients can clean up their UI.
        """

        if "elicitation" not in self._server.client_capabilities:
            raise ElicitationNotSupportedError(
                "connected MCP client did not advertise the 'elicitation' "
                "capability — cannot prompt the user"
            )
        return await self._server.send_elicitation(
            outbox=self._outbox,
            message=message,
            requested_schema=requested_schema or {},
            timeout_s=timeout_s,
        )

    async def sample(
        self,
        messages: list[SamplingMessage] | list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
        system_prompt: str | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        model_preferences: dict[str, Any] | None = None,
        include_context: str | None = None,
        timeout_s: float | None = None,
    ) -> SamplingResult:
        """Ask the client to run an LLM call on this server's behalf.

        Sends a ``sampling/createMessage`` request and awaits the
        client's reply. The client typically routes the call through
        the agent's own provider — that's the whole point of sampling:
        servers borrow the agent's model instead of carrying their own
        API keys.

        ``messages`` accepts either a list of :class:`SamplingMessage`
        or a list of dicts (``{"role": ..., "content": ...}``) for
        convenience — the dict form is what most tool authors will
        write inline.

        Returns a :class:`SamplingResult` whose ``content`` field holds
        the assistant's reply (typically ``{"type": "text", "text":
        "..."}``).

        Raises:
            SamplingNotSupportedError: client never advertised the
                capability — calling ``sample`` would deadlock.
            TimeoutError: ``timeout_s`` elapsed before the client replied.
                The server sends ``notifications/cancelled`` on the way
                out so polite clients can abort their model call.
        """

        if "sampling" not in self._server.client_capabilities:
            raise SamplingNotSupportedError(
                "connected MCP client did not advertise the 'sampling' "
                "capability — cannot call the agent's model"
            )
        # Normalize dict-form messages into SamplingMessage so the wire
        # encoder doesn't have to branch.
        normalized: list[SamplingMessage] = []
        for m in messages:
            if isinstance(m, SamplingMessage):
                normalized.append(m)
            elif isinstance(m, dict):
                content = m.get("content")
                if not isinstance(content, dict):
                    raise ValueError(
                        f"sampling message content must be a dict, got "
                        f"{type(content).__name__}"
                    )
                normalized.append(
                    SamplingMessage(
                        role=str(m.get("role") or "user"),
                        content=content,
                    )
                )
            else:
                raise TypeError(
                    f"sampling messages must be SamplingMessage or dict, "
                    f"got {type(m).__name__}"
                )
        return await self._server.send_sampling(
            outbox=self._outbox,
            messages=normalized,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            temperature=temperature,
            stop_sequences=stop_sequences,
            metadata=metadata,
            model_preferences=model_preferences,
            include_context=include_context,
            timeout_s=timeout_s,
        )


# Parameter names a tool can use to receive the per-call ``ServerContext``.
# Both are common in upstream MCP servers; we accept either.
_CTX_PARAM_NAMES = ("ctx", "context")


class SdkServer:
    """JSON-RPC dispatcher that exposes a ``ToolRegistry`` over MCP wire format.

    Run is driven externally by ``InProcessTransport``: it hands us a pair
    of memory streams (``inbox`` for client→server, ``outbox`` for
    server→client) and we loop until the inbox closes.
    """

    __slots__ = (
        "name",
        "registry",
        "client_info",
        "client_capabilities",
        "_id_counter",
        "_pending_server_requests",
    )

    def __init__(self, name: str, tools: list[Tool]) -> None:
        self.name = name
        self.registry = ToolRegistry()
        self.registry.add(*tools)
        # Populated on initialize. Tools use this to gate ctx.elicit().
        self.client_info: dict[str, Any] = {}
        self.client_capabilities: dict[str, Any] = {}
        # Monotonic id space for server→client requests. Distinct from
        # the client's id space; collisions don't matter because each
        # side only awaits ids it owns.
        self._id_counter = itertools.count(1)
        self._pending_server_requests: dict[int, _PendingServerRequest] = {}

    # -- runtime ------------------------------------------------------------

    async def run(
        self,
        inbox: anyio.streams.memory.MemoryObjectReceiveStream[dict[str, Any]],
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
    ) -> None:
        """Drain ``inbox``, dispatch, write responses to ``outbox``.

        Each request is handled in its own task so a slow tool doesn't
        block the next request. Notifications produce no response.
        """

        async with anyio.create_task_group() as tg:
            try:
                async for message in inbox:
                    if not isinstance(message, dict):
                        continue
                    # Responses from the client to server-initiated
                    # requests (e.g. elicitation/create answers) arrive
                    # here too. Route them synchronously — no need to
                    # spawn a task — then continue.
                    if "method" not in message and message.get("id") is not None:
                        self._route_server_response(message)
                        continue
                    tg.start_soon(self._handle, message, outbox)
            except anyio.EndOfStream:
                pass
            except anyio.ClosedResourceError:
                pass
            finally:
                # Unblock any tools still waiting on elicitation answers.
                for pending in list(self._pending_server_requests.values()):
                    if not pending.event.is_set():
                        pending.error = ConnectionError(
                            "MCP client closed before elicitation reply"
                        )
                        pending.event.set()
                self._pending_server_requests.clear()

    async def _handle(
        self,
        message: dict[str, Any],
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
    ) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        mid = message.get("id")
        is_notification = mid is None

        # Notifications: no response, just side effects.
        if is_notification:
            # notifications/initialized, notifications/cancelled — nothing
            # to do in v0. Future hook integration goes here.
            return

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "tools/list":
                result = self._tools_list(params)
            elif method == "tools/call":
                result = await self._tools_call(params, outbox, mid)
            else:
                await self._send_error(
                    outbox, mid, -32601, f"method not found: {method!r}"
                )
                return
        except Exception as exc:  # noqa: BLE001 — server errors must not crash
            await self._send_error(outbox, mid, -32603, f"internal error: {exc!r}")
            return

        await self._send_result(outbox, mid, result)

    # -- methods ------------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Capture what the client advertised so tools can introspect
        # before calling ctx.elicit / ctx.sample / etc.
        self.client_info = dict(params.get("clientInfo") or {})
        self.client_capabilities = dict(params.get("capabilities") or {})
        return {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {
                # We support tools (listChanged not yet implemented).
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": self.name,
                "version": "0.1.0",
            },
        }

    def _tools_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        tools = [
            {
                "name": t.name,
                "description": t.description,
                # MCP wire shape uses ``inputSchema`` (camelCase).
                "inputSchema": t.input_schema,
            }
            for t in self.registry
        ]
        return {"tools": tools}

    async def _tools_call(
        self,
        params: dict[str, Any],
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
        request_id: Any,
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return {
                "content": [{"type": "text", "text": "missing tool name"}],
                "isError": True,
            }
        tool = self.registry.get(name)
        if tool is None:
            return {
                "content": [{"type": "text", "text": f"tool {name!r} not found"}],
                "isError": True,
            }
        # Inject a ServerContext for tools that ask for one (param named
        # ``ctx`` or ``context``). Detected lazily — most tools don't take
        # a context and just see their normal kwargs.
        call_kwargs = dict(arguments)
        ctx_param = _detect_ctx_param(tool.fn)
        if ctx_param is not None:
            call_kwargs[ctx_param] = ServerContext(self, outbox, request_id)
        try:
            out = await tool.fn(**call_kwargs)
        except Exception as exc:  # noqa: BLE001 — tool errors are isError, not protocol errors
            return {
                "content": [{"type": "text", "text": f"tool error: {exc!r}"}],
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": _stringify_result(out)}],
            "isError": False,
        }

    # -- server-initiated requests -----------------------------------------

    async def send_elicitation(
        self,
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
        message: str,
        requested_schema: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> ElicitationResult:
        """Send ``elicitation/create`` and wait for the client's reply.

        Internal: tool authors call this through ``ServerContext.elicit``.
        """

        rid = next(self._id_counter)
        pending = _PendingServerRequest()
        self._pending_server_requests[rid] = pending

        params: dict[str, Any] = {"message": message}
        if requested_schema:
            params["requestedSchema"] = requested_schema

        request = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "elicitation/create",
            "params": params,
        }
        try:
            await outbox.send(request)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
            self._pending_server_requests.pop(rid, None)
            raise ConnectionError("MCP transport closed before elicitation send") from exc

        try:
            if timeout_s is None:
                await pending.event.wait()
            else:
                with anyio.fail_after(timeout_s):
                    await pending.event.wait()
        except TimeoutError:
            # Best-effort cancellation notice so polite clients clean up UI.
            self._pending_server_requests.pop(rid, None)
            try:
                await outbox.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": rid, "reason": "server timeout"},
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            raise

        self._pending_server_requests.pop(rid, None)
        if pending.error is not None:
            raise pending.error
        response = pending.response or {}
        if "error" in response:
            err = response["error"]
            raise AgentError(
                f"elicitation/create error {err.get('code')}: {err.get('message', '')}"
            )
        result = response.get("result") or {}
        action = result.get("action", "cancel")
        if action not in ("accept", "decline", "cancel"):
            # Be lenient with malformed clients; treat unknown as cancel.
            action = "cancel"
        content = result.get("content") or {}
        if not isinstance(content, dict):
            content = {}
        return ElicitationResult(action=action, content=content)

    async def send_sampling(
        self,
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
        messages: list[SamplingMessage],
        *,
        max_tokens: int,
        system_prompt: str | None,
        temperature: float | None,
        stop_sequences: list[str] | None,
        metadata: dict[str, Any] | None,
        model_preferences: dict[str, Any] | None,
        include_context: str | None,
        timeout_s: float | None = None,
    ) -> SamplingResult:
        """Send ``sampling/createMessage`` and wait for the client's reply.

        Internal: tool authors call this through ``ServerContext.sample``.
        Mirrors :meth:`send_elicitation` for ergonomics — same pending-
        request registry, same timeout+cancel-notice path.
        """

        rid = next(self._id_counter)
        pending = _PendingServerRequest()
        self._pending_server_requests[rid] = pending

        params: dict[str, Any] = {
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "maxTokens": int(max_tokens),
        }
        if system_prompt is not None:
            params["systemPrompt"] = system_prompt
        if temperature is not None:
            params["temperature"] = float(temperature)
        if stop_sequences:
            params["stopSequences"] = list(stop_sequences)
        if metadata:
            params["metadata"] = dict(metadata)
        if model_preferences:
            params["modelPreferences"] = dict(model_preferences)
        if include_context is not None:
            params["includeContext"] = include_context

        request = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "sampling/createMessage",
            "params": params,
        }
        try:
            await outbox.send(request)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
            self._pending_server_requests.pop(rid, None)
            raise ConnectionError("MCP transport closed before sampling send") from exc

        try:
            if timeout_s is None:
                await pending.event.wait()
            else:
                with anyio.fail_after(timeout_s):
                    await pending.event.wait()
        except TimeoutError:
            self._pending_server_requests.pop(rid, None)
            try:
                await outbox.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": rid, "reason": "server timeout"},
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            raise

        self._pending_server_requests.pop(rid, None)
        if pending.error is not None:
            raise pending.error
        response = pending.response or {}
        if "error" in response:
            err = response["error"]
            raise AgentError(
                f"sampling/createMessage error {err.get('code')}: "
                f"{err.get('message', '')}"
            )
        result = response.get("result") or {}
        # Be lenient on slightly off shapes — clients in the wild are
        # imperfect; we'd rather surface what we got than crash.
        content = result.get("content") if isinstance(result, dict) else None
        if not isinstance(content, dict):
            content = {}
        return SamplingResult(
            role=str(result.get("role") or "assistant"),
            content=content,
            model=str(result.get("model") or ""),
            stop_reason=result.get("stopReason"),
        )

    def _route_server_response(self, message: dict[str, Any]) -> None:
        """Match an inbound response to one of our server-initiated requests."""

        mid = message.get("id")
        try:
            key = int(mid)
        except (TypeError, ValueError):
            return
        pending = self._pending_server_requests.get(key)
        if pending is None:
            return
        pending.response = message
        pending.event.set()

    # -- wire helpers -------------------------------------------------------

    async def _send_result(
        self,
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
        mid: Any,
        result: dict[str, Any],
    ) -> None:
        try:
            await outbox.send({"jsonrpc": "2.0", "id": mid, "result": result})
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass

    async def _send_error(
        self,
        outbox: anyio.streams.memory.MemoryObjectSendStream[dict[str, Any]],
        mid: Any,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": code, "message": message},
        }
        if data is not None:
            payload["error"]["data"] = data
        try:
            await outbox.send(payload)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _PendingServerRequest:
    """Bookkeeping for one in-flight server→client request.

    The dispatcher sets ``response`` (or ``error``) and fires ``event`` to
    unblock whichever task is awaiting the answer.
    """

    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = anyio.Event()
        self.response: dict[str, Any] | None = None
        self.error: BaseException | None = None


def _detect_ctx_param(fn: Any) -> str | None:
    """Return the name of the ``ServerContext`` parameter on ``fn``, if any.

    Walks the signature once per tool call — cheap. Wrapped functions
    (Claude-style ``args: dict`` wrappers) typically don't have ``ctx`` in
    their wrapper signature because we re-pack kwargs before calling the
    real body; that's fine, those tools simply don't get elicitation.
    """

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    for pname in _CTX_PARAM_NAMES:
        param = sig.parameters.get(pname)
        if param is None:
            continue
        # Accept positional-or-keyword, keyword-only, or VAR_KEYWORD
        # (the **kw bucket would swallow ``ctx`` too, but explicit param
        # is what we want).
        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return pname
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def create_sdk_server(name: str, tools: list[Tool]) -> SdkServerConfig:
    """Build an in-process MCP server exposing ``tools`` under ``name``.

    Drop the returned ``SdkServerConfig`` into ``MCPClient(...)`` (or into
    an Agent's ``mcp_servers=[...]`` list, once that exists) to use it.
    """

    server = SdkServer(name, tools)
    return SdkServerConfig(name=name, server=server)
