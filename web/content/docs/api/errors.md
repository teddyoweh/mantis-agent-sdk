# Errors

The full error hierarchy.

```
Exception
├── ClaudeSDKError                 (Claude SDK compatibility base)
│   ├── AgentError                 (mantis-agent base)
│   │   ├── AuthError              (401, missing API key)
│   │   ├── ProviderError          (provider-side 4xx/5xx)
│   │   │   └── RateLimitError     (429)
│   │   ├── BudgetExceededError    (max_usd / max_tokens hit)
│   │   ├── PermissionDeniedError  (can_use_tool returned Deny)
│   │   ├── ToolExecutionError     (raised inside a tool)
│   │   └── StreamProtocolError    (malformed stream chunk)
│   └── CLIConnectionError         (subprocess transport errors)
```

## Public errors

```python
from mantis_agent import (
    AgentError,
    AuthError,
    BudgetExceededError,
    CLIConnectionError,
    ClaudeSDKError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    StreamProtocolError,
    ToolExecutionError,
)
```

### `BudgetExceededError`

```python
class BudgetExceededError(AgentError):
    spent: float       # total dollars spent so far
    cap: float         # the cap that was exceeded
    last_message: ResultMessage | None
```

Raised before a model call that would push spend over `max_usd`. The
partial transcript is preserved on disk; fork from the last checkpoint
to continue with a fresh cap.

### `RateLimitError`

```python
class RateLimitError(ProviderError):
    retry_after: float | None       # seconds, from the Retry-After header
```

The HTTP client retries 429s with exponential backoff up to a configurable
limit (default 3 retries). If the limit is exhausted, this error reaches
your code.

### `ProviderError`

Covers all other provider-side failures (4xx other than 429, 5xx, network
errors). Carries the response body in `.body` for debugging.

### `ToolExecutionError`

```python
class ToolExecutionError(AgentError):
    tool_name: str
    fatal: bool = False
```

Raised by tool handlers. `fatal=False` is caught and threaded back as a
`ToolResultBlock(is_error=True)` so the model can recover. `fatal=True`
propagates out of the agent loop.

### `PermissionDeniedError`

Raised when a tool tries to run despite a `PermissionResultDeny`. In
normal operation this never escapes — denials become tool results.
You'll only see it if you bypass the permission layer manually.

### `StreamProtocolError`

The provider sent a malformed SSE chunk or a frame the SDK couldn't
parse. Almost always indicates a provider bug; report it with the chunk
attached.

### `AuthError`

401 from the provider. Usually means a missing or expired API key.

### `CLIConnectionError`

stdio MCP transport errors — process spawn failure, broken pipe, etc.

## Handling

The general pattern:

```python
from mantis_agent import (
    query, BudgetExceededError, RateLimitError, ToolExecutionError,
)

try:
    async for msg in query(prompt="...", options={...}):
        ...
except BudgetExceededError as e:
    print(f"Stopped at ${e.spent:.4f} of ${e.cap:.2f}")
except RateLimitError as e:
    print(f"Rate limited; retry after {e.retry_after}s")
except ToolExecutionError as e:
    print(f"Fatal in tool {e.tool_name}: {e}")
```

For non-fatal tool errors, you don't need a handler — they show up as
`is_error=True` tool result blocks and the model decides what to do.
