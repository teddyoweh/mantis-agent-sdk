# Hooks

Hooks observe the agent lifecycle. They don't gate execution — for that
use [permissions](permissions.md) — but they can record state, write logs,
or trigger side effects in response to specific events.

## The shape

```python
from mantis_agent import HookMatcher, ClaudeAgentOptions

async def log_tool(hook_input, hook_ctx):
    print(f"[{hook_input.event}] {hook_input.tool_name}")
    return None  # Return None to do nothing; return HookJSONOutput to
                 # mutate the in-flight payload.

options = ClaudeAgentOptions(
    model="qwen2.5:7b",
    tools=[get_weather],
    hooks=[
        HookMatcher(event="PreToolUse", handler=log_tool),
        HookMatcher(event="PostToolUse", handler=log_tool),
    ],
)
```

A `HookMatcher` pairs an event name with an async handler. You can attach
multiple matchers for the same event — they all run, in declaration order.

## Events

The 28 hook events the runtime emits:

**Session lifecycle**

- `SessionStart` — agent is starting a new session.
- `SessionEnd` — final result message has been yielded.
- `SessionResume` — restored from a checkpoint.

**Per-turn**

- `Stop` — agent loop is about to exit (e.g. `stop_reason='end_turn'`).
- `PreCompact` / `PostCompact` — about to / just finished compacting.

**Per-message**

- `PreModelCall` / `PostModelCall` — wrapping an outbound HTTP call.

**Per-tool**

- `PreToolUse` — model emitted a `tool_use`, before the executor runs it.
- `PostToolUse` — tool returned, before the result is threaded back.
- `ToolUseDenied` — `can_use_tool` returned a Deny.

**Per-block**

- `PreContentBlockStart` / `PostContentBlockStop`
- `TextBlockComplete`, `ThinkingBlockComplete`, `ToolUseBlockComplete`

**Per-MCP server**

- `McpServerStart`, `McpServerStop`, `McpServerError`

**Memory / settings**

- `PreSettingsLoad` / `PostSettingsLoad`
- `PreMemoryLoad` / `PostMemoryLoad`
- `MemoryEntrySaved`

## `HookInput`

The first argument to every handler:

```python
@dataclass
class HookInput:
    event: str                 # event name
    session_id: str
    turn: int
    tool_name: str | None
    tool_input: dict | None
    tool_result: dict | None
    payload: dict              # event-specific extras
```

## `HookJSONOutput`

Return `None` to do nothing. Return a `HookJSONOutput` to mutate the
in-flight value:

```python
from mantis_agent import HookJSONOutput

async def normalise(hook_input, hook_ctx):
    if hook_input.event == "PreToolUse":
        return HookJSONOutput(
            updated_tool_input={"city": hook_input.tool_input["city"].title()},
        )
    return None
```

Available mutations per event:

| Event | Mutable fields |
|---|---|
| `PreToolUse` | `updated_tool_input`, `block` |
| `PostToolUse` | `updated_tool_result`, `block` |
| `PreModelCall` | `extra_headers`, `block` |
| `PostModelCall` | nothing — observation only |
| `Stop` | `continue_with` (force one more turn) |

`block=True` cancels the in-flight action (denying the tool, dropping the
model call) without an exception.

## Plugins ship hooks too

A `Plugin` bundles hooks alongside tools and a system-prompt addition:

```python
from mantis_agent import Plugin

logging_plugin = Plugin(
    tools=[],
    system_prompt_addition="",
    hooks=[
        HookMatcher(event="PreToolUse", handler=log_tool),
        HookMatcher(event="PostToolUse", handler=log_tool),
    ],
)

options = ClaudeAgentOptions(plugins=[logging_plugin])
```

See [Plugins](plugins.md).
