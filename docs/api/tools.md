# Tools API

## `@tool`

```python
def tool(
    fn: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parallel_safe: bool = True,
) -> Tool | Callable: ...
```

Decorate an async function to turn it into a `Tool`.

**Arguments**

- `fn` — the async function being decorated. Filled in automatically
  when used as `@tool` with no arguments.
- `name` — override the tool name (defaults to the function name).
- `description` — override the tool description (defaults to the
  docstring's first paragraph).
- `parallel_safe` — `False` if this tool shouldn't run concurrently
  with other tools. Default `True`.

**Usage**

```python
@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F"

@tool(name="my_renamed", parallel_safe=False)
async def get_weather_v2(city: str) -> str:
    """Get the current weather for a city."""
    return ...
```

## `Tool` (dataclass)

The object the decorator returns:

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict       # JSON schema derived from the signature
    handler: Callable        # the async function
    parallel_safe: bool = True
```

You can construct one manually if you don't want the decorator:

```python
from mantis_agent import Tool

my_tool = Tool(
    name="lookup_user",
    description="Look up a user by id",
    input_schema={
        "type": "object",
        "properties": {"user_id": {"type": "string"}},
        "required": ["user_id"],
    },
    fn=lookup_user_impl,
)
```

## `ToolRegistry`

The runtime stores tools in a `ToolRegistry`. You rarely touch it
directly — pass tools via `options.tools` instead — but it's useful if
you're composing dynamic tool sets:

```python
from mantis_agent import ToolRegistry

reg = ToolRegistry()
reg.add(get_weather)
reg.add_many([get_forecast, list_cities])

options = {"tools": reg.list()}
```

## Built-in tools

### `WebSearch`

```python
class WebSearch:
    def __init__(self, num_results: int = 5, api_key: str | None = None): ...
```

Backed by Exa. Reads `EXA_API_KEY` from env if `api_key` isn't passed.
Returns top-N URLs with titles and snippets.

```python
from mantis_agent import WebSearch

options = {"tools": [WebSearch(num_results=10)]}
```

### `WebFetch`

```python
class WebFetch:
    def __init__(self, max_chars: int = 50_000): ...
```

Fetches a URL and converts to clean markdown. Truncates at
`max_chars` to fit in context.

```python
from mantis_agent import WebFetch

options = {"tools": [WebFetch()]}
```

### Functional forms

If you prefer functions over classes:

```python
from mantis_agent import web_fetch, web_search

options = {"tools": [web_search, web_fetch]}
```

These are pre-instantiated default versions of the classes above.

## Tool errors

A tool signals failure by raising anything at all — you don't construct
`ToolExecutionError` yourself:

```python
from mantis_agent import tool


@tool
async def query_db(sql: str) -> str:
    """Run a read-only query."""
    if not _has_auth():
        raise RuntimeError("Database auth missing")
    return _run(sql)
```

The runtime catches it, wraps it in `ToolExecutionError(tool_name, tool_use_id,
cause)`, and threads a `tool_result` block back to the model with
`is_error=True` — so the model can react instead of the loop dying. Catch it
around your own dispatch if you need the structured form:

```python
from mantis_agent import ToolExecutionError

try:
    ...
except ToolExecutionError as e:
    print(e.tool_name, e.tool_use_id, repr(e.cause))
```

There is no `fatal=` flag; to stop the loop, raise a `BudgetExceededError` or
cancel the agent.
