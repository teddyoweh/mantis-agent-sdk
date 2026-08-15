# MCP servers

The [Model Context Protocol](https://modelcontextprotocol.io/) standardises
how tools, prompts, and resources expose themselves to LLMs.
`mantis-agent-sdk` is both an MCP **client** (any agent can talk to MCP
servers) and an MCP **server-runtime** (you can author servers in-process
using the same `@tool` decorator).

## In-process server

The fastest way to expose a set of tools as an MCP server:

```python
from mantis_agent import MantisAgentOptions, create_sdk_mcp_server, tool

@tool
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

calc = create_sdk_mcp_server(
    name="calculator",
    version="0.1.0",
    tools=[add],
)

options = MantisAgentOptions(
    mcp_servers=[calc],
)
```

The server runs in the same process — no subprocess, no socket — but
exposes the full MCP protocol surface to the agent loop. From the
model's point of view it's identical to an out-of-process server.

## External servers (stdio / sse / http)

```python
options = MantisAgentOptions(
    mcp_servers=[
        # stdio: spawn a subprocess
        {"transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]},

        # sse: connect to a Server-Sent Events endpoint
        {"transport": "sse", "url": "https://mcp.example.com/sse"},

        # http: connect via streamable-http transport
        {"transport": "http", "url": "https://mcp.example.com/mcp"},
    ],
)
```

Each transport starts its handshake at session start and tears down at
session end. Failures during handshake surface as `McpServerError` hooks;
failures mid-call surface as tool errors.

## Elicitation

Servers can prompt the user mid-tool-call. The `ctx.elicit()` API
inside a server-side tool blocks the call until the agent gathers a
response:

```python
# server side
@tool
async def book_flight(destination: str) -> str:
    """Book a flight."""
    seat = await ctx.elicit(
        prompt="Window or aisle?",
        options=["window", "aisle"],
    )
    return f"Booked {destination}, {seat} seat."
```

The agent loop pauses, surfaces a `system` message of subtype
`elicit_request`, gathers a response (from your UI / human-in-the-loop /
config-driven default), and returns it to the server.

## Sampling

Servers can call **back into the agent's model** to do their own
generation. Useful when a server tool needs an LLM but shouldn't ship
its own model dependency:

```python
# server side
@tool
async def summarise(text: str) -> str:
    """Summarise text using the calling agent's model."""
    result = await ctx.sample(
        messages=[{"role": "user", "content": f"Summarise: {text}"}],
        system_prompt="You are a concise summariser.",
        max_tokens=200,
    )
    return result.content[0]["text"]
```

The agent receives a `sampling_request` system message, runs it through
its current model + options, and returns the result to the server.

To allow sampling, register a handler:

`sampling_handler` is a constructor argument on `MCPClient`, not an options
key — a `"sampling_handler"` entry in an options dict is silently ignored:

```python
from mantis_agent.mcp.client import MCPClient, SamplingResult


async def my_sampler(request):
    # request.messages carries what the server wants sampled.
    return SamplingResult(content="…", model="qwen2.5:7b")


client = MCPClient(config, sampling_handler=my_sampler)
```

`MCPClient` also takes `elicitation_handler`, `notification_handler`, and
`request_timeout_s`. If a server requests sampling and no handler is set, the
server is told sampling is unsupported and decides how to proceed.

## Serving your own tools over MCP

`SdkServer` is **in-process**: it exposes `@tool` functions to the MCP
machinery without a subprocess or a socket. Build one with
`create_sdk_server`:

```python
from mantis_agent import tool
from mantis_agent.mcp import create_sdk_server


@tool
async def echo(text: str) -> str:
    """Echo the input back."""
    return text


config = create_sdk_server(name="echo", tools=[echo])
```

`create_sdk_server` returns an `SdkServerConfig` you hand to `MCPClient` (or
list in `mcp_servers`) exactly like a remote server's config. Note that
`SdkServer.run(inbox, outbox)` speaks anyio memory streams, not stdin/stdout —
there is no stdio server runtime in this package, so a `serve_stdio()` call (as
earlier versions of this page showed) does not exist.

For an out-of-process server, write it with any MCP implementation and *connect*
to it — the client side handles stdio, SSE, and HTTP:

```python
from mantis_agent.mcp import HttpServerConfig, StdioServerConfig

local = StdioServerConfig(command="uv", args=["run", "my_server.py"])
remote = HttpServerConfig(url="https://mcp.example.com", headers={"authorization": "Bearer …"})
```

From the agent side, the same servers as a plain options entry:

```python
options = MantisAgentOptions(
    model="qwen2.5:7b",
    backend="http://localhost:11434",
    mcp_servers=[
        {"transport": "stdio", "command": "uv", "args": ["run", "my_server.py"]},
    ],
)
```

## Resources and prompts

Beyond tools, the MCP client speaks the other two halves of the protocol:
servers can expose **resources** (documents, tables, file trees the agent
can list and read) and **prompts** (parameterized prompt templates). Both
are available through the client methods on the session — list what a
server offers, read a resource by URI, or expand a named prompt with
arguments — so any server that publishes them works out of the box.

## Terminal integration (`.mcp.json`)

The `mantis` terminal discovers MCP servers automatically — Claude Code's
config format, verbatim:

```json
// ./.mcp.json (project) or ~/.mantis-agent/mcp.json (user)
{
  "mcpServers": {
    "github":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                 "env": {"GITHUB_TOKEN": "..."}},
    "internal": {"type": "http", "url": "https://mcp.example.com/api"},
    "legacy":   {"type": "sse",  "url": "https://old.example.com/sse"}
  }
}
```

`settings.json` may also carry a top-level `mcpServers` object (lowest
priority; project `.mcp.json` wins by name). Servers connect in the
background at launch — a slow or broken server never delays your prompt, and
each failure is isolated. Tools land namespaced as `mcp__{server}__{tool}`
and survive model switches. `/mcp` shows per-server status + tools.

Handshakes time out at 10s and tool calls at 120s, so a mute server fails
fast instead of hanging the terminal.

## SDK: `options.mcp_servers`

External transports work through the Claude-SDK-shaped API too:

```python
from mantis_agent import MantisAgentOptions
from mantis_agent.compat_query import query

options = MantisAgentOptions(
    model="qwen2.5:7b",
    mcp_servers={
        "tiny":  {"command": "python3", "args": ["./my_mcp_server.py"]},  # stdio
        "calc":  create_sdk_mcp_server("calc", tools=[echo]),             # in-process
    },
)
async for msg in query(prompt="call mcp__tiny__ping", options=options):
    ...
```

Lifecycle is handled for you (`MCPManager.start()/stop()` — connect at query
start, clean close at the end, safe from async generators).
