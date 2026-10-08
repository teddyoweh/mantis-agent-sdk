# `query` and `ClaudeSDKClient`

The two entry points. Use `query()` for one-shot calls; use
`ClaudeSDKClient` for multi-turn sessions.

## `query()`

```python
def query(
    *,
    prompt: str | list[Message],
    options: MantisAgentOptions | dict | None = None,
) -> QueryRun: ...
```

Runs a single agent loop and yields every message it produces. The
returned `QueryRun` is an async generator (`async for`, `aclose()`) with
one extra method, `await run.steer(text) -> bool` — see
[Steering](#steering-a-running-query) below.

**Arguments**

- `prompt` — either a string (sent as the first user turn) or an
  explicit message list (full conversation seed).
- `options` — either a `MantisAgentOptions` instance or a `dict` with
  the same keys. See [MantisAgentOptions](options.md).

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

### Steering a running query

Hold on to the run and you can add a message to its **next** turn while it
works. It is not an interrupt: the model call and tool calls in flight
finish, then the next request carries your text as a user message. If the
model had already given its final answer, the run keeps going and answers
you; if a `task` subagent was blocking the turn, it moves to the background
(a job) and its result arrives later.

```python
from mantis_agent import query

run = query(prompt="Add dark mode", options={"model": "qwen2.5:7b"})
async for msg in run:
    if msg.type == "assistant" and "styled-components" in str(msg.message.content):
        await run.steer("No new dependencies, please.")
```

`steer()` returns `True` when the live run accepted the message and `False`
when there is no live run (iteration hasn't reached the model yet, or the
run has ended) — a refused message is not kept. It returns at once; delivery
happens as you keep iterating.

## `ClaudeSDKClient`

```python
class ClaudeSDKClient:
    def __init__(self, options: MantisAgentOptions): ...
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

`await client.steer(text) -> bool` adds a message to the in-progress
response's next turn (call it while iterating `receive_response()`); same
semantics as `QueryRun.steer`, `False` when no response is in progress.

**Lifetime**

- `__aenter__` opens any MCP servers, attaches plugins, loads memory,
  rehydrates from the session store if `session_id` is set.
- `__aexit__` tears down servers, flushes the transcript, fires the
  `SessionEnd` hook.

**Per-call overrides**

`client.query()` accepts a subset of `MantisAgentOptions` keys as
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
from mantis_agent import Agent, UserMessage

agent = Agent(
    model="qwen2.5:7b",
    backend="http://localhost:11434",
    tools=[get_weather],
    system="...",          # `system`, not `system_prompt` — that name is the
)                          # typed-options spelling, and Agent rejects it

# Agent works in messages, not prompt strings: pass a list, get a list back.
messages = await agent.run([UserMessage(content="What's the weather in Lagos?")])

# Stream raw events instead
async for event in agent.stream([UserMessage(content="...")]):
    ...

# Cancel mid-stream
agent.cancel()
```

`Agent` is what `ClaudeSDKClient` wraps. The public methods:

- `agent.run(messages) -> list[Message]` — run to completion
- `agent.run_iter(messages) -> AsyncIterator[Message]` — messages as they finish
- `agent.stream(messages) -> AsyncIterator[StreamEvent]` — every low-level event
- `agent.cancel()` — fires the cancellation signal
- `agent.steer(text) -> bool` — add a message to the live run's next turn
  (not an interrupt; `False` when no run is live). Thread-safe.
- `agent.take_undelivered_steers() -> list[str]` — steers the last run
  accepted but ended before sending (cancelled, step cap, an error)
- `await agent.aclose()` — releases the HTTP client

See [Streaming](../guides/streaming.md) for the full event taxonomy.
