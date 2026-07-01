"""MCP (Model Context Protocol) client + in-process server.

Public surface
--------------
* ``ServerConfig`` — tagged union of stdio/sse/http/sdk configs.
* ``MCPClient`` — async-context-managed client for one MCP server.
  Pass ``elicitation_handler=...`` to handle server-initiated
  ``elicitation/create`` requests (the server asks the user a question
  mid-tool-call).
* ``MCPTool`` — remote tool description; ``.to_mantis_agent_tool(client)``
  drops it into a regular ``mantis_agent.Tool`` registry.
* ``create_sdk_server(name, tools)`` — build an in-process MCP server
  exposing local ``@tool`` functions through MCP wire format. Tools
  whose signature includes ``ctx`` receive a ``ServerContext`` they
  can use to call ``await ctx.elicit(message, schema)``.
* ``ElicitationRequest`` / ``ElicitationResult`` — the request/response
  pair that flows through the elicitation handler.

Everything else (transports, JSON-RPC plumbing, low-level error types)
is implementation detail; reach into ``.client`` / ``.server`` /
``.transports`` if you need it but expect those paths to shift.
"""

from .client import (
    MCPClient,
    MCPElicitationRequest,
    MCPError,
    MCPProtocolError,
)
from .server import (
    ElicitationNotSupportedError,
    SamplingNotSupportedError,
    SdkServer,
    ServerContext,
    create_sdk_server,
)
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

__all__ = [
    "CallToolResult",
    "ElicitationHandler",
    "ElicitationNotSupportedError",
    "ElicitationRequest",
    "ElicitationResult",
    "HttpServerConfig",
    "MCPClient",
    "MCPElicitationRequest",
    "MCPError",
    "MCPProtocolError",
    "MCPPrompt",
    "MCPResource",
    "MCPTool",
    "SamplingHandler",
    "SamplingMessage",
    "SamplingNotSupportedError",
    "SamplingRequest",
    "SamplingResult",
    "SdkServer",
    "SdkServerConfig",
    "ServerConfig",
    "ServerContext",
    "SseServerConfig",
    "StdioServerConfig",
    "create_sdk_server",
]
