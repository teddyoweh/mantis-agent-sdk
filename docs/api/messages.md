# Message types

`mantis-agent-sdk` exposes two parallel shapes:

- **Flat shape** (`SDKMessage` family) — matches the Claude Agent SDK
  exactly. Yielded by `query()` and `client.query()`.
- **Internal shape** (`Message` family) — what the runtime uses
  internally. Imported as `AssistantMessage`, `UserMessage`, etc.

You can mostly work with the flat shape and never touch the internal one.

## Flat shape (`SDKMessage`)

```python
from mantis_agent import (
    SDKAssistantMessage,
    SDKCompactBoundaryMessage,
    SDKMessage,
    SDKPermissionDenial,
    SDKResultMessage,
    SDKStatusMessage,
    SDKSystemMessage,
    SDKUserMessage,
)
```

Every yielded value from `query()` has a `.type` attribute:

| `.type` | Class | What it represents |
|---|---|---|
| `"assistant"` | `SDKAssistantMessage` | A completed assistant turn. |
| `"user"` | `SDKUserMessage` | Tool results threaded back as a user turn. |
| `"system"` | `SDKSystemMessage` | Internal event (compact, MCP elicit/sample request, etc). |
| `"result"` | `SDKResultMessage` | Final result with cost / usage / stop reason. |
| `"status"` | `SDKStatusMessage` | Progress / status update. |
| `"compact_boundary"` | `SDKCompactBoundaryMessage` | Compaction occurred at this point. |
| `"permission_denial"` | `SDKPermissionDenial` | Surfaced into `result.permission_denials`. |

### `SDKAssistantMessage`

```python
@dataclass
class SDKAssistantMessage:
    type: Literal["assistant"]
    message: dict   # {"role":"assistant","content":[block_dict, ...]}
    session_id: str
    parent_tool_use_id: str | None
```

`message["content"]` is a list of block dicts:

- `{"type":"text","text":...}`
- `{"type":"thinking","text":...}`
- `{"type":"tool_use","id":...,"name":...,"input":{...}}`

### `SDKResultMessage`

```python
@dataclass
class SDKResultMessage:
    type: Literal["result"]
    session_id: str
    stop_reason: str           # end_turn / max_turns / max_tokens / max_usd / cancelled
    total_cost_usd: float
    modelUsage: dict[str, ModelUsage]
    duration_ms: int
    permission_denials: list[SDKPermissionDenial]
    error: str | None
```

## Internal shape (`Message`)

```python
from mantis_agent import (
    AssistantMessage,
    Message,
    SystemMessage as InternalSystemMessage,
    UserMessage,
)
```

These are `msgspec.Struct` types matching Anthropic's message shape but
implementation-internal. Use them when you need to feed a pre-built
transcript into `query(prompt=[...])`:

```python
from mantis_agent import AssistantMessage, UserMessage, TextBlock

seed = [
    UserMessage(role="user", content=[TextBlock(text="hi")]),
    AssistantMessage(role="assistant", content=[TextBlock(text="hello!")]),
]
async for msg in query(prompt=seed, options={...}):
    ...
```

## Content blocks

Both shapes share the same block types:

```python
from mantis_agent import (
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    ContentBlock,
)
```

| Block | `type` |
|---|---|
| `TextBlock(text)` | `"text"` |
| `ThinkingBlock(text)` | `"thinking"` |
| `ToolUseBlock(id, name, input)` | `"tool_use"` |
| `ToolResultBlock(tool_use_id, content, is_error=False)` | `"tool_result"` |

All blocks implement `.to_dict()` for serialisation.

## Usage and cost

```python
from mantis_agent import Usage, ModelUsage

@dataclass
class Usage:
    input_tokens: int
    output_tokens: int

@dataclass
class ModelUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
```

`ModelUsage` is what `SDKResultMessage.modelUsage` is keyed by model. See
[Budget](../guides/budget.md) for how cost is computed.
