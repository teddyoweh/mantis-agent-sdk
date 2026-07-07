"""MCP server-config tagged union, tool wrapper, and call result.

Design
------
* ``ServerConfig`` is a tagged union of four transport flavors. msgspec
  dispatches on the ``type`` field at decode time — same trick we use for
  ``ContentBlock`` in ``mantis_agent.types``.
* ``MCPTool`` is the *MCP-side* representation of a remote tool. It knows
  which server it came from (``server_id``) and how to invoke itself back
  through an ``MCPClient``. ``.to_mantis_agent_tool()`` wraps it in a regular
  ``mantis_agent.Tool`` so the agent loop can dispatch MCP tools and local
  ``@tool``-decorated tools through the same registry.
* ``CallToolResult`` mirrors the MCP wire shape (``content: list[block]``,
  ``is_error: bool``). Helper ``.to_string()`` flattens text/image blocks to
  a single string suitable for ``ToolResultBlock.content`` — the agent loop
  is unaware of MCP block structure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

import msgspec

from ..tools import Tool

if TYPE_CHECKING:
    from .client import MCPClient
    from .server import SdkServer


# ---------------------------------------------------------------------------
# Server configs (tagged union)
# ---------------------------------------------------------------------------


class StdioServerConfig(
    msgspec.Struct,
    tag="stdio",
    tag_field="type",
    omit_defaults=True,
):
    """Spawn a local subprocess speaking line-delimited JSON-RPC over stdio."""

    command: str
    args: list[str] = []
    env: dict[str, str] = {}


class SseServerConfig(
    msgspec.Struct,
    tag="sse",
    tag_field="type",
    omit_defaults=True,
):
    """Connect to a remote MCP server using the legacy SSE+POST transport."""

    url: str
    headers: dict[str, str] = {}


class HttpServerConfig(
    msgspec.Struct,
    tag="http",
    tag_field="type",
    omit_defaults=True,
):
    """Streamable-HTTP transport (newer MCP spec). Single bidirectional URL."""

    url: str
    headers: dict[str, str] = {}


class SdkServerConfig(
    msgspec.Struct,
    tag="sdk",
    tag_field="type",
    omit_defaults=True,
):
    """In-process MCP server. ``server`` is held by reference, not serialized.

    Constructed via ``mcp.server.create_sdk_server(name, tools)``. Wiring runs
    entirely through ``InProcessTransport`` — no sockets, no subprocess.
    """

    name: str
    # SdkServer instance held opaquely. ``Any`` so msgspec doesn't try to
    # serialize it; we never round-trip an SdkServerConfig over the wire.
    server: Any = None


ServerConfig = Annotated[
    Union[StdioServerConfig, SseServerConfig, HttpServerConfig, SdkServerConfig],
    msgspec.Meta(description="MCP server connection config."),
]


# ---------------------------------------------------------------------------
# MCPTool — remote tool exposed by an MCP server
# ---------------------------------------------------------------------------


class MCPResource(msgspec.Struct, frozen=True, omit_defaults=True):
    """A resource discovered via ``resources/list`` — a readable blob the server
    exposes (a file, a DB row, an API response) addressed by ``uri``. Read its
    contents with ``MCPClient.read_resource(uri)``."""

    uri: str
    name: str = ""
    description: str = ""
    mime_type: str | None = None
    server_id: str = ""


class MCPPrompt(msgspec.Struct, frozen=True, omit_defaults=True):
    """A prompt template discovered via ``prompts/list`` — a reusable, named
    message the server can render (with arguments) via ``MCPClient.get_prompt``.
    ``arguments`` is the list of ``{name, description, required}`` the template
    accepts."""

    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = []
    server_id: str = ""


class MCPTool(msgspec.Struct, frozen=True, omit_defaults=True):
    """A tool discovered via ``tools/list`` on an MCP server.

    Wrap with ``.to_mantis_agent_tool(client)`` to drop it into a normal
    ``ToolRegistry`` alongside local tools. The wrapped tool's ``fn``
    calls back into the originating ``MCPClient.call_tool``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    server_id: str

    def to_mantis_agent_tool(self, client: "MCPClient") -> Tool:
        """Wrap this MCP tool as a local ``Tool``.

        The returned tool's ``fn`` is an async closure that round-trips the
        call through ``client.call_tool`` and stringifies the result. Errors
        from the server surface as the string representation of
        ``CallToolResult`` with ``is_error=True``; ``Tool`` dispatch in
        ``mantis_agent.tools`` handles wrapping it into a ``ToolResultBlock``.
        """

        tool_name = self.name

        async def _invoke(**kwargs: Any) -> str:
            result = await client.call_tool(tool_name, kwargs)
            text = result.to_string()
            if result.is_error:
                # Surface as exception so dispatch_tool_calls flags it
                # as a tool error result.
                raise RuntimeError(text or f"MCP tool {tool_name!r} returned an error")
            return text

        # MCP tool names can include separators MCP servers consider valid
        # but the agent loop's registry should still see a stable key.
        return Tool(
            name=tool_name,
            description=self.description,
            input_schema=self.input_schema,
            fn=_invoke,
            # ``parallel_safe`` is the deprecated alias — a property, not an
            # init field, so it must be spelled the real way here.
            is_concurrency_safe=True,
        )


# ---------------------------------------------------------------------------
# CallToolResult — wire shape of tools/call response
# ---------------------------------------------------------------------------


class CallToolResult(msgspec.Struct, omit_defaults=True):
    """Result of ``tools/call``.

    ``content`` is a list of MCP content blocks. Each block is a dict with
    at least a ``type`` field — usually ``"text"`` (with ``text``), but the
    spec also allows ``"image"`` (``data``, ``mimeType``) and
    ``"resource"`` (``resource`` link). We keep the raw dicts here and
    flatten in ``.to_string()``.
    """

    content: list[dict[str, Any]] = []
    is_error: bool = False

    def to_string(self) -> str:
        """Flatten content blocks into a single string for the agent loop.

        * ``text`` blocks contribute their text.
        * ``image`` blocks contribute a placeholder ``[image:<mime>]``.
        * ``resource`` blocks contribute the resource ``uri`` (if present).
        * Unknown block types fall back to the JSON-encoded block.
        """

        parts: list[str] = []
        for block in self.content:
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "image":
                mime = block.get("mimeType") or block.get("mime_type") or "image"
                parts.append(f"[image:{mime}]")
            elif btype == "resource":
                resource = block.get("resource") or {}
                uri = resource.get("uri") if isinstance(resource, dict) else None
                parts.append(f"[resource:{uri or '?'}]")
            else:
                # Best-effort: stringify the whole block.
                parts.append(str(block))
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Elicitation — server-initiated mid-tool prompts to the user
# ---------------------------------------------------------------------------


ElicitAction = Literal["accept", "decline", "cancel"]


class ElicitationRequest(msgspec.Struct, omit_defaults=True):
    """Server is asking the user (via the client) for input mid-tool-call.

    Mirrors the MCP ``elicitation/create`` request shape. ``message`` is the
    human-readable prompt. ``requested_schema`` is an optional JSON Schema
    describing the shape of the answer the server expects — clients with a
    UI render a form from it; headless clients can ignore it and return
    free-form content.

    ``raw`` is the full params payload from the server, kept so handlers
    can inspect MCP fields we haven't surfaced as first-class attributes
    yet (e.g. ``progressToken``, future spec additions).
    """

    message: str
    requested_schema: dict[str, Any] = {}
    raw: dict[str, Any] = {}


class ElicitationResult(msgspec.Struct, omit_defaults=True):
    """Client's response to an ``ElicitationRequest``.

    ``action``:
      * ``"accept"`` — user filled in ``content`` and submitted.
      * ``"decline"`` — user actively declined (e.g. "no, don't ask me that").
      * ``"cancel"``  — user dismissed without answering (e.g. closed dialog).

    ``content`` is the structured answer when ``action == "accept"``; for
    decline/cancel it's left empty. The server interprets ``content`` against
    its own ``requested_schema``.
    """

    action: ElicitAction = "accept"
    content: dict[str, Any] = {}

    @classmethod
    def accept(cls, content: dict[str, Any] | None = None) -> "ElicitationResult":
        """Convenience: ``ElicitationResult.accept({"name": "Ada"})``."""
        return cls(action="accept", content=content or {})

    @classmethod
    def decline(cls) -> "ElicitationResult":
        """User actively declined to answer."""
        return cls(action="decline", content={})

    @classmethod
    def cancel(cls) -> "ElicitationResult":
        """User dismissed the prompt without answering."""
        return cls(action="cancel", content={})


# An async callback the MCPClient invokes when the server sends an
# elicitation/create request. The handler decides how to surface the
# request to the user (CLI prompt, GUI dialog, web form, …) and returns
# the answer.
ElicitationHandler = Callable[[ElicitationRequest], Awaitable[ElicitationResult]]


# ---------------------------------------------------------------------------
# Sampling — server asks the client to run an LLM call on its behalf
# ---------------------------------------------------------------------------
#
# This is the second server→client request shape in MCP, alongside
# elicitation. A server that needs a model in its tool path (summarise
# a doc, classify an email, decide what to do next) sends
# ``sampling/createMessage`` instead of carrying its own API keys. The
# client routes the request to whichever model the agent is currently
# using and ships the assistant reply back. Handlers are free to pick a
# different model — ``model_preferences`` is a hint, not a constraint.
#
# Shape mirrors the MCP ``sampling/createMessage`` params with camelCase
# translated to snake_case. ``raw`` preserves the verbatim params dict so
# handlers can inspect fields we haven't yet surfaced as attributes
# (sampling is the part of the MCP spec most likely to add new options).


class SamplingMessage(msgspec.Struct, omit_defaults=True):
    """One message in a :class:`SamplingRequest` conversation.

    ``role`` is either ``"user"`` or ``"assistant"``. ``content`` is the
    MCP content-block dict — typically ``{"type": "text", "text": "..."}``
    but the spec also allows ``image`` and ``audio`` blocks; the raw
    dict is preserved so handlers can branch on ``content["type"]``.
    """

    role: str
    content: dict[str, Any]


class SamplingRequest(msgspec.Struct, omit_defaults=True):
    """Server is asking the client to run an LLM call on its behalf.

    Required:
      * ``messages`` — the chat transcript to send to the model. At
        least one entry; the *last* one is the prompt to respond to.
      * ``max_tokens`` — required by the spec. Default 1024 if the
        server omits it (some legacy servers do).

    Optional hints from the server:
      * ``system_prompt`` — system message to prepend.
      * ``temperature`` — sampling temperature; ``None`` = handler picks.
      * ``stop_sequences`` — additional stop strings.
      * ``metadata`` — opaque server metadata (trace ids, etc.).
      * ``model_preferences`` — speed/intelligence/cost hints + a list
        of preferred model names. The handler is free to ignore.
      * ``include_context`` — ``"none"`` | ``"thisServer"`` |
        ``"allServers"``. When the client builds the message list it
        can include context from MCP servers it's connected to.

    ``raw`` is the verbatim params dict from the server — reach into
    this for any field not yet typed.
    """

    messages: list[SamplingMessage]
    max_tokens: int = 1024
    system_prompt: str | None = None
    temperature: float | None = None
    stop_sequences: list[str] = []
    metadata: dict[str, Any] = {}
    model_preferences: dict[str, Any] = {}
    include_context: str | None = None
    raw: dict[str, Any] = {}


class SamplingResult(msgspec.Struct, omit_defaults=True):
    """Client's reply to a :class:`SamplingRequest`.

    ``role`` must be ``"assistant"`` per the MCP spec; we normalize a
    misconfigured ``"user"`` to ``"assistant"`` rather than crashing the
    handler (wire correctness over Pythonic strictness).

    ``content`` is the MCP content-block dict — typically
    ``{"type": "text", "text": "<assistant reply>"}``. Pass through any
    additional fields (``data`` for images, etc.) verbatim.

    ``model`` MUST identify the model that produced the output; the
    server uses it for audit logs and downstream prompt construction.
    """

    role: str = "assistant"
    content: dict[str, Any] = {}
    model: str = ""
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.role != "assistant":
            self.role = "assistant"


# An async callback the MCPClient invokes when the server sends a
# sampling/createMessage request. The handler runs the model call —
# typically against the agent's own provider — and returns the
# assistant reply.
SamplingHandler = Callable[[SamplingRequest], Awaitable[SamplingResult]]
