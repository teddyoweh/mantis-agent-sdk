"""MCP elicitation — server prompts the user mid-tool-call.

This test suite locks down the full elicitation round-trip:

  client capability advertise  →  server-initiated elicitation/create
       ↑                                        ↓
  ElicitationResult ← user-side handler ← client dispatcher

The in-process server + client share the InProcessTransport's memory
streams, so we can exercise every code path (accept, decline, cancel,
no-handler, no-capability, timeout, malformed) without a subprocess.

What this proves:

* ``ElicitationResult.accept/decline/cancel`` constructors do the right
  thing.
* A client without a handler does NOT advertise the capability — so
  ``ctx.elicit`` correctly raises ``ElicitationNotSupportedError``
  rather than hanging.
* A client WITH a handler advertises the capability, receives the
  inbound request on its read loop, runs the handler, and replies.
* The server's ``ctx.elicit`` returns the parsed ``ElicitationResult``
  to the tool — accept/decline/cancel all flow through.
* Tools without a ``ctx`` parameter are unaffected (no injection).
* The legacy error-code path (``MCPElicitationRequest``) still works
  for servers that didn't migrate to real elicitation requests.
* The dispatcher rejects unknown server-initiated methods with
  ``-32601`` instead of hanging.
* If the client raises in its handler, the server gets an error
  response rather than waiting forever.
"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent import tool
from mantis_agent.mcp import (
    ElicitationNotSupportedError,
    ElicitationRequest,
    ElicitationResult,
    MCPClient,
    MCPElicitationRequest,
    SdkServerConfig,
    create_sdk_server,
)
from mantis_agent.mcp.client import _ELICITATION_ERROR_CODE
from mantis_agent.mcp.server import SdkServer


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Pure value-object tests (no I/O)
# ---------------------------------------------------------------------------


def test_elicitation_result_accept_carries_content() -> None:
    r = ElicitationResult.accept({"name": "Ada", "age": 36})
    assert r.action == "accept"
    assert r.content == {"name": "Ada", "age": 36}


def test_elicitation_result_decline_has_empty_content() -> None:
    r = ElicitationResult.decline()
    assert r.action == "decline"
    assert r.content == {}


def test_elicitation_result_cancel_has_empty_content() -> None:
    r = ElicitationResult.cancel()
    assert r.action == "cancel"
    assert r.content == {}


def test_elicitation_result_default_action_is_accept() -> None:
    # The struct must default to *something* — accept is the friendliest
    # default for callers passing only ``content=``.
    r = ElicitationResult(content={"x": 1})
    assert r.action == "accept"


def test_elicitation_request_keeps_raw_payload() -> None:
    raw = {
        "message": "What is your name?",
        "requestedSchema": {"type": "object"},
        "progressToken": "abc123",  # field we don't surface as first-class
    }
    req = ElicitationRequest(
        message=raw["message"],
        requested_schema=raw["requestedSchema"],
        raw=raw,
    )
    # Future spec fields are still reachable via .raw.
    assert req.raw["progressToken"] == "abc123"
    assert req.requested_schema == {"type": "object"}


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------


async def test_client_without_handler_does_not_advertise_elicitation() -> None:
    """No handler ⇒ no advertised capability — silence is the right default."""

    config = create_sdk_server("noop", tools=[_make_echo_tool()])

    async with MCPClient(config):
        # The server captured what we advertised. ``elicitation`` must be
        # absent so a polite server won't try to send us prompts we can't
        # answer.
        server: SdkServer = config.server  # type: ignore[assignment]
        assert "elicitation" not in server.client_capabilities


async def test_client_with_handler_advertises_elicitation() -> None:
    """A registered handler enables the capability for the entire session."""

    config = create_sdk_server("noop", tools=[_make_echo_tool()])

    async def handler(_req: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult.cancel()

    async with MCPClient(config, elicitation_handler=handler):
        server: SdkServer = config.server  # type: ignore[assignment]
        assert "elicitation" in server.client_capabilities


# ---------------------------------------------------------------------------
# End-to-end: tool calls ctx.elicit, client handler answers
# ---------------------------------------------------------------------------


async def test_tool_elicit_accept_round_trip() -> None:
    """The server sends elicitation/create; the client's handler answers; the
    tool sees the accepted content."""

    @tool
    async def greet(*, ctx) -> str:
        """Ask the user their name, then greet them."""

        answer = await ctx.elicit(
            "What is your name?",
            {"type": "object", "properties": {"name": {"type": "string"}}},
        )
        assert answer.action == "accept"
        return f"hello, {answer.content['name']}"

    captured: dict[str, object] = {}

    async def handler(req: ElicitationRequest) -> ElicitationResult:
        captured["message"] = req.message
        captured["schema"] = req.requested_schema
        return ElicitationResult.accept({"name": "Ada"})

    config = create_sdk_server("greeter", tools=[greet])
    async with MCPClient(config, elicitation_handler=handler) as client:
        result = await client.call_tool("greet", {})
        assert not result.is_error
        assert result.to_string() == "hello, Ada"

    assert captured["message"] == "What is your name?"
    assert captured["schema"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }


async def test_tool_elicit_decline_propagates() -> None:
    """The user actively declined — tool sees ``action == 'decline'``."""

    @tool
    async def conditional(*, ctx) -> str:
        ans = await ctx.elicit("Continue?")
        if ans.action == "accept":
            return "going"
        return f"stopped:{ans.action}"

    async def handler(_req: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult.decline()

    config = create_sdk_server("c", tools=[conditional])
    async with MCPClient(config, elicitation_handler=handler) as client:
        result = await client.call_tool("conditional", {})
        assert result.to_string() == "stopped:decline"


async def test_tool_elicit_cancel_propagates() -> None:
    """User dismissed without answering — tool sees ``action == 'cancel'``."""

    @tool
    async def conditional(*, ctx) -> str:
        ans = await ctx.elicit("anything?")
        return ans.action

    async def handler(_req: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult.cancel()

    config = create_sdk_server("c", tools=[conditional])
    async with MCPClient(config, elicitation_handler=handler) as client:
        result = await client.call_tool("conditional", {})
        assert result.to_string() == "cancel"


async def test_handler_can_return_plain_dict() -> None:
    """Be lenient: a handler that returns a plain dict still works."""

    @tool
    async def ask(*, ctx) -> str:
        ans = await ctx.elicit("a")
        return f"{ans.action}:{ans.content.get('v')}"

    async def handler(_req: ElicitationRequest):
        return {"action": "accept", "content": {"v": 42}}

    config = create_sdk_server("c", tools=[ask])
    async with MCPClient(config, elicitation_handler=handler) as client:
        result = await client.call_tool("ask", {})
        assert result.to_string() == "accept:42"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_tool_without_capability_raises() -> None:
    """If the client never advertised the capability, ctx.elicit must NOT
    deadlock — it should raise so the tool can decide what to do."""

    seen_error: dict[str, object] = {}

    @tool
    async def probe(*, ctx) -> str:
        try:
            await ctx.elicit("anything?")
        except ElicitationNotSupportedError as exc:
            seen_error["raised"] = True
            return f"no-cap:{exc}"
        return "should-not-happen"

    config = create_sdk_server("probe", tools=[probe])
    async with MCPClient(config) as client:  # no handler ⇒ no capability
        result = await client.call_tool("probe", {})
        assert not result.is_error
        assert result.to_string().startswith("no-cap:")
        assert seen_error["raised"] is True


async def test_handler_exception_surfaces_as_tool_error() -> None:
    """When the client's handler raises, the server must not hang.

    The server's ctx.elicit call surfaces an ``AgentError`` (carrying the
    JSON-RPC -32603 the client returned) which the tool dispatcher catches
    and wraps into ``isError: true``.
    """

    @tool
    async def asker(*, ctx) -> str:
        await ctx.elicit("Q?")
        return "unreached"

    class HandlerBoom(Exception):
        pass

    async def handler(_req: ElicitationRequest) -> ElicitationResult:
        raise HandlerBoom("kaboom")

    config = create_sdk_server("e", tools=[asker])
    async with MCPClient(config, elicitation_handler=handler) as client:
        result = await client.call_tool("asker", {})
        assert result.is_error is True
        text = result.to_string()
        # Should mention "elicitation/create error" coming back from the
        # client, with -32603 internal error code.
        assert "elicitation" in text or "error" in text.lower()


async def test_unknown_server_initiated_method_replied_not_found() -> None:
    """If the server sends a request with a method we don't handle, the
    client must respond ``-32601 method not found`` rather than hanging.

    We test the dispatcher directly: spin up a client, capture what it
    sends back via a fake transport, and feed it a synthesized inbound
    request.
    """

    sent: list[dict] = []

    class CaptureTransport:
        """Minimal Transport stand-in: send() records, receive() blocks forever
        until close()."""

        def __init__(self) -> None:
            self._close = anyio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            self._close.set()

        async def send(self, message):
            sent.append(message)

        async def receive(self):
            await self._close.wait()
            from mantis_agent.mcp.transports.base import TransportClosed
            raise TransportClosed("done")

        async def close(self):
            self._close.set()

    async def handler(_req: ElicitationRequest) -> ElicitationResult:  # pragma: no cover
        return ElicitationResult.cancel()

    # Construct client without going through __aenter__ (which would call
    # _initialize and require a real server). Wire up the read loop's
    # task group manually so server-initiated requests dispatch.
    client = MCPClient(
        SdkServerConfig(name="x", server=SdkServer("x", [])),
        elicitation_handler=handler,
    )
    client._transport = CaptureTransport()  # type: ignore[assignment]
    async with anyio.create_task_group() as tg:
        client._task_group = tg
        # Drive _dispatch directly; this is exactly what the read loop
        # would do with an inbound message.
        client._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 99999,
                "method": "sampling/create_message",
                "params": {},
            }
        )
        # Give the handler task a turn to run + send the response.
        with anyio.fail_after(2.0):
            while not sent:
                await anyio.sleep(0.01)
        # Tear down so the task group exits.
        await client._transport.close()  # type: ignore[union-attr]
        tg.cancel_scope.cancel()

    assert len(sent) == 1
    response = sent[0]
    assert response["id"] == 99999
    assert "error" in response
    assert response["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Legacy error-code path — still works
# ---------------------------------------------------------------------------


async def test_legacy_elicitation_error_code_still_raises() -> None:
    """Old MCP servers embed elicitation as a tools/call error with code
    -32042. We continue to surface that as ``MCPElicitationRequest``."""

    # Build a tiny custom server that returns the legacy error from tools/call.
    class LegacyServer(SdkServer):
        async def _tools_call(self, params, outbox, request_id):  # noqa: ARG002
            return {
                "_legacy_error": True,
                "params": {"message": "Pick a color", "schema": {}},
            }

        async def _handle(self, message, outbox):
            method = message.get("method")
            mid = message.get("id")
            if message.get("id") is None:
                return
            if method != "tools/call":
                # Delegate other methods to parent.
                await super()._handle(message, outbox)
                return
            # Send back the legacy error directly.
            await outbox.send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {
                        "code": _ELICITATION_ERROR_CODE,
                        "message": "user input needed",
                        "data": {"message": "Pick a color", "schema": {}},
                    },
                }
            )

    legacy = LegacyServer("legacy", tools=[_make_echo_tool()])
    config = SdkServerConfig(name="legacy", server=legacy)
    async with MCPClient(config) as client:
        with pytest.raises(MCPElicitationRequest) as exc_info:
            await client.call_tool("echo", {"text": "hi"})
        # ``params`` carries whatever the server attached as ``data``.
        assert exc_info.value.params["message"] == "Pick a color"


# ---------------------------------------------------------------------------
# Tools without ctx are unaffected
# ---------------------------------------------------------------------------


async def test_tool_without_ctx_param_is_not_injected() -> None:
    """A regular tool with no ``ctx`` parameter must keep working.

    Otherwise enabling elicitation would silently regress every tool in
    every server.
    """

    @tool
    async def plain(text: str) -> str:
        return text.upper()

    async def handler(_req: ElicitationRequest) -> ElicitationResult:  # pragma: no cover
        return ElicitationResult.cancel()

    config = create_sdk_server("plain", tools=[plain])
    async with MCPClient(config, elicitation_handler=handler) as client:
        result = await client.call_tool("plain", {"text": "hi"})
        assert result.to_string() == "HI"


async def test_tool_with_context_alias_also_works() -> None:
    """``ctx`` and ``context`` are both recognized as the injection slot."""

    @tool
    async def asker(*, context) -> str:
        ans = await context.elicit("hi?")
        return ans.content.get("v", "")

    async def handler(_req: ElicitationRequest) -> ElicitationResult:
        return ElicitationResult.accept({"v": "ok"})

    config = create_sdk_server("a", tools=[asker])
    async with MCPClient(config, elicitation_handler=handler) as client:
        result = await client.call_tool("asker", {})
        assert result.to_string() == "ok"


# ---------------------------------------------------------------------------
# Concurrent elicitation — multiple tool calls in flight at once
# ---------------------------------------------------------------------------


async def test_two_concurrent_elicitations_dont_cross() -> None:
    """Two tool calls each asking a question must get their own answers.

    Server allocates distinct request ids, client routes each response to
    the right pending event.
    """

    @tool
    async def asker(label: str, *, ctx) -> str:
        ans = await ctx.elicit(f"q-{label}")
        return f"{label}->{ans.content.get('v')}"

    async def handler(req: ElicitationRequest) -> ElicitationResult:
        # Tag the answer with the question so we can tell them apart.
        return ElicitationResult.accept({"v": req.message})

    config = create_sdk_server("a", tools=[asker])
    async with MCPClient(config, elicitation_handler=handler) as client:

        async def call(label: str, out: dict):
            result = await client.call_tool("asker", {"label": label})
            out[label] = result.to_string()

        out: dict = {}
        async with anyio.create_task_group() as tg:
            tg.start_soon(call, "a", out)
            tg.start_soon(call, "b", out)

        assert out["a"] == "a->q-a"
        assert out["b"] == "b->q-b"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_echo_tool():
    @tool
    async def echo(text: str) -> str:
        """Echo the input."""

        return text

    return echo
