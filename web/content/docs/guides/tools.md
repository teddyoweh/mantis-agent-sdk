# Tools

A tool is an async Python function the model can call. The `@tool`
decorator inspects its signature and converts it to JSON schema; the SDK
handles routing, dispatch, result threading, and parallelism.

## The basic shape

```python
from mantis_agent import tool

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city. Returns a one-line summary."""
    return f"{city}: 67°F, partly cloudy, wind 8 mph NW"
```

Three rules:

1. **Async.** The runtime expects coroutines.
2. **Docstring.** It becomes the tool description the model sees. Be
   specific.
3. **Typed parameters.** Each parameter's annotation becomes its JSON
   schema. Supported: `str`, `int`, `float`, `bool`, `list[...]`,
   `dict[...]`, `Optional[...]`, `Union[...]`, `Literal[...]`, `Enum`
   subclasses, `msgspec.Struct` / `pydantic.BaseModel`.

## Registering tools

Pass them in `options.tools`:

```python
async for msg in query(
    prompt="What's the weather in Lagos?",
    options={
        "model": "qwen2.5:7b",
        "tools": [get_weather, get_forecast, list_cities],
    },
):
    ...
```

Or with `MantisAgentOptions`:

```python
from mantis_agent import MantisAgentOptions, ClaudeSDKClient

opts = MantisAgentOptions(
    model="qwen2.5:7b",
    tools=[get_weather],
)
async with ClaudeSDKClient(opts) as client:
    async for msg in client.query("..."):
        ...
```

## Tool return types

Anything JSON-serialisable. `str` is most common; the SDK wraps it in
a single text content block. You can return a list of content blocks
directly if you want images or structured data:

```python
from mantis_agent import TextBlock

@tool
async def render(spec: str) -> list[dict]:
    """Render a chart and return both an explanation and an image."""
    return [
        TextBlock(text="Here's the chart you asked for:").to_dict(),
        {"type": "image", "source": {"type": "base64", "data": "..."}},
    ]
```

## Parallel dispatch

By default, tools run **concurrently** when the model emits multiple
`tool_use` blocks in the same turn. The SDK uses `anyio.create_task_group`
and threads results back in the order the tool calls were emitted.

Some tools shouldn't run in parallel (anything that writes to a shared
file, or mutates global state). Mark them:

```python
@tool(parallel_safe=False)
async def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    ...
```

When `parallel_safe=False` is set on any tool in a batch, the entire batch
is serialised.

## Mid-stream dispatch

The runtime starts a tool **the moment the model finishes emitting its
`tool_use` block**, not after the message ends. So if the model emits
three tool calls in sequence, the first one is already running while the
second is still streaming in. See [Streaming](streaming.md).

## Built-in tools

```python
from mantis_agent import WebFetch, WebSearch

options = {
    "tools": [WebFetch(), WebSearch(num_results=5)],
}
```

- `WebSearch` — backed by Exa (`EXA_API_KEY`). Returns top-N URLs with
  titles + snippets.
- `WebFetch` — fetch a URL and convert to clean markdown.

## Tools as MCP servers

If you have a set of tools that belongs to a logical "service" — a
filesystem, a database, a remote API — wrap them in an in-process MCP
server:

```python
from mantis_agent import create_sdk_mcp_server

calc = create_sdk_mcp_server(
    name="calculator",
    version="0.1.0",
    tools=[add, subtract, multiply, divide],
)
options = {"mcp_servers": [calc]}
```

See [MCP servers](mcp.md) for transport options (stdio / sse / http).

## Tool errors

If your tool raises, the SDK catches the exception and returns a
`ToolResultBlock(is_error=True)` to the model. The model sees a structured
error and can recover (retry, ask the user, give up gracefully).

To raise an error the SDK should *not* catch — e.g. an auth failure that
should abort the whole agent loop — raise a `ToolExecutionError` with
`fatal=True`:

```python
from mantis_agent import ToolExecutionError

@tool
async def query_db(sql: str) -> str:
    if not _has_auth():
        raise ToolExecutionError("Database auth missing", fatal=True)
    ...
```
