# `query` and `ClaudeSDKClient`

The two entry points. Use `query()` for one-shot calls; use
`ClaudeSDKClient` for multi-turn sessions.

## `query()`

```python
def query(
    *,
    prompt: str | list[Message],
    options: ClaudeAgentOptions | dict | None = None,
) -> AsyncIterator[SDKMessage]: ...
```

Runs a single agent loop and yields every message it produces.

**Arguments**

- `prompt` — either a string (sent as the first user turn) or an
  explicit message list (full conversation seed).
- `options` — either a `ClaudeAgentOptions` instance or a `dict` with
  the same keys. See [ClaudeAgentOptions](options.md).

**Yields**

`SDKMessage` instances with `.type ∈ {assistant, user, system, result}`:

- `SDKAssistantMessage(type="assistant", message={"role":"assistant","content":[...]})`
- `SDKUserMessage(type="user", message={"role":"user","content":[...]})` —
  yielded when tool results thread back into the conversation.
- `SDKSystemMessage(type="system", subtype="elicit_request"|"sampling_request"|...)`
- `SDKResultMessage(type="result", total_cost_usd=..., modelUsage=..., stop_reason=...)`
- `SDKPermissionDenial(type="permission_denial", tool_name=..., reason=...)` —
  surfaced as part of the result's `permission_denials` list.

**Example**

```python
import asyncio
from mantis_agent import query

async def main():
    async for msg in query(
        prompt="hi",
        options={"model": "qwen2.5:7b"},
    ):
        print(msg.type, getattr(msg, "message", None))

asyncio.run(main())
```

## `ClaudeSDKClient`

```python
class ClaudeSDKClient:
    def __init__(self, options: ClaudeAgentOptions): ...
    async def __aenter__(self) -> "ClaudeSDKClient": ...
    async def __aexit__(self, *args) -> None: ...

    def query(
        self,
        prompt: str | list[Message],
        **per_call_overrides,
    ) -> AsyncIterator[SDKMessage]: ...
```

Streaming context manager. The session persists across multiple
`query()` calls within the `async with`.

**Lifetime**

- `__aenter__` opens any MCP servers, attaches plugins, loads memory,
  rehydrates from the session store if `session_id` is set.
- `__aexit__` tears down servers, flushes the transcript, fires the
  `SessionEnd` hook.

**Per-call overrides**

`client.query()` accepts a subset of `ClaudeAgentOptions` keys as
keyword arguments, which apply *only* to that call:

- `max_turns`
- `max_usd`
- `max_tokens`
- `temperature`
- `allowed_tools`
- `disallowed_tools`

```python
async with ClaudeSDKClient(options) as client:
    async for msg in client.query("research X", max_usd=0.10):
        ...
    async for msg in client.query("now draft a report", max_usd=0.20,
                                   allowed_tools=["write_file"]):
        ...
```

## `Agent` — the low-level driver

If you need finer control than `ClaudeSDKClient`, use `Agent`:

```python
from mantis_agent import Agent

agent = Agent(
    model="qwen2.5:7b",
    tools=[get_weather],
    system_prompt="...",
)

# Run to completion, return final ResultMessage
result = await agent.run("What's the weather in Lagos?")

# Stream every event
async for event in agent.run_iter("..."):
    ...

# Cancel mid-stream
agent.cancel()
```

`Agent` is what `ClaudeSDKClient` wraps. The public methods:

- `agent.run(prompt) -> ResultMessage`
- `agent.run_iter(prompt) -> AsyncIterator[StreamEvent]`
- `agent.cancel()` — fires the cancellation signal
- `agent.session` — current `Session` instance

See [Streaming](../guides/streaming.md) for the full event taxonomy.
