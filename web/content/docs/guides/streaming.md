# Streaming

Every model call streams. The high-level `query()` API gives you flat
`SDKMessage` objects per assistant turn; the lower-level `Agent.run_iter()`
gives you per-token events the moment they arrive.

## `query()` — flat-shape messages

```python
async for msg in query(prompt="...", options={...}):
    if msg.type == "assistant":
        for block in msg.message["content"]:
            if block["type"] == "text":
                print(block["text"], end="", flush=True)
    elif msg.type == "result":
        print(f"\n[done — ${msg.total_cost_usd:.4f}]")
```

`query()` yields one `SDKMessage` per *logical message*: a complete
assistant turn, a tool-result user turn, a system event, or a final result
message. It does not stream individual tokens — for that, use
`Agent.run_iter()`.

## `Agent.run_iter()` — stream events

```python
from mantis_agent import Agent

agent = Agent(model="qwen2.5:7b", tools=[get_weather])
async for event in agent.run_iter("What's the weather in Lagos?"):
    match event.type:
        case "content_block_start":
            ...
        case "text_delta":
            print(event.text, end="", flush=True)
        case "thinking_delta":
            print(f"\033[2m{event.text}\033[0m", end="", flush=True)
        case "input_json_delta":
            ...  # tool-call args streaming in
        case "content_block_stop":
            # tool calls fire here, mid-stream
            ...
        case "message_stop":
            ...
```

Event types:

| Type | Meaning |
|---|---|
| `message_start` | New assistant message. |
| `content_block_start` | New content block (text / thinking / tool_use). |
| `text_delta` | Token of text. |
| `thinking_delta` | Token of out-of-band thinking. |
| `input_json_delta` | Token of a tool-call's JSON arguments. |
| `content_block_stop` | Block finished. **Tool dispatch fires here.** |
| `message_delta` | Mid-message metadata (e.g. stop_reason updates). |
| `message_stop` | Message finished. |

## Mid-stream tool dispatch

When a `tool_use` block reaches `content_block_stop`, the runtime starts
the tool **immediately**. It does *not* wait for the rest of the message.
So if the model emits three tool calls in a row, the first runs while the
second's JSON is still streaming in.

This matters in two cases:

- **Latency.** A slow tool can overlap with model generation.
- **Cancellation.** You can interrupt the agent mid-stream (see below) and
  in-flight tools are cancelled cleanly.

## Mid-stream cancellation

```python
import asyncio
from mantis_agent import Agent

agent = Agent(model="qwen2.5:7b", tools=[slow_tool])
task = asyncio.create_task(agent.run("..."))
await asyncio.sleep(2)
agent.cancel()  # fires ToolPermissionContext.signal
await task     # returns cleanly with a "cancelled by signal" message
```

`Agent.cancel()` fires the `anyio.Event` carried on
`ToolPermissionContext.signal`. The streaming tool executor watches that
event and cancels every in-flight `CancelScope`. The agent loop checks the
signal at the top of each iteration and exits via the `Stop` hook without
making another model call.

The cancelled tool call surfaces in the transcript as a tool result with
`is_error=True` and message `"cancelled by signal"`.

## `ClaudeSDKClient` — multi-turn streaming

```python
from mantis_agent import ClaudeSDKClient, MantisAgentOptions

options = MantisAgentOptions(model="qwen2.5:7b", tools=[get_weather])
async with ClaudeSDKClient(options) as client:
    async for msg in client.query("What's the weather in Lagos?"):
        ...
    async for msg in client.query("Now compare it to Lisbon."):
        ...
```

The session persists across `query()` calls — the second turn sees the
full transcript from the first. State is written to
`~/.mantis-agent/sessions/{session_id}.jsonl` between calls.

## Stderr callback

If you want token-level visibility without writing your own event loop:

```python
options = {
    "model": "qwen2.5:7b",
    "stderr": lambda line: print(line, file=sys.stderr),
}
```

`stderr` is invoked with each delta as it streams.
