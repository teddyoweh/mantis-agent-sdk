# MCP servers

The [Model Context Protocol](https://modelcontextprotocol.io/) standardises
how tools, prompts, and resources expose themselves to LLMs.
`mantis-agent-sdk` is both an MCP **client** (any agent can talk to MCP
servers) and an MCP **server-runtime** (you can author servers in-process
using the same `@tool` decorator).

## In-process server

The fastest way to expose a set of tools as an MCP server:

```python
from mantis_agent import create_sdk_mcp_server, tool

@tool
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

calc = create_sdk_mcp_server(
    name="calculator",
    version="0.1.0",
    tools=[add],
)

options = {"mcp_servers": [calc]}
```

The server runs in the same process — no subprocess, no socket — but
exposes the full MCP protocol surface to the agent loop. From the
model's point of view it's identical to an out-of-process server.

## External servers (stdio / sse / http)

```python
options = {
    "mcp_servers": [
        # stdio: spawn a subprocess
        {"transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]},

        # sse: connect to a Server-Sent Events endpoint
        {"transport": "sse", "url": "https://mcp.example.com/sse"},

        # http: connect via streamable-http transport
        {"transport": "http", "url": "https://mcp.example.com/mcp"},
    ],
}
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

```python
options = {
    "sampling_handler": "auto",          # use the agent's own model
    # or a custom handler that may route to a different model entirely:
    "sampling_handler": my_sampler_fn,
}
```

If a server requests sampling and no handler is set, the server sees a
`SamplingNotSupportedError` and decides how to handle it.

## Authoring an out-of-process server

The same `@tool` decorator works for stdio servers — `mantis-agent-sdk`
ships a minimal runtime you can install as a script:

```python
# my_server.py
from mantis_agent import tool
from mantis_agent.mcp import SdkServer

@tool
async def echo(text: str) -> str:
    return text

if __name__ == "__main__":
    SdkServer(name="echo", version="0.1.0", tools=[echo]).serve_stdio()
```

```bash
uv run my_server.py        # or: python my_server.py
```

From the agent side:

```python
options = {
    "mcp_servers": [
        {"transport": "stdio", "command": "uv", "args": ["run", "my_server.py"]},
    ],
}
```
