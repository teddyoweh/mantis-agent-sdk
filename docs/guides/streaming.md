# Streaming

Every model call streams. The high-level `query()` API gives you flat
`SDKMessage` objects per assistant turn; the lower-level `Agent.run_iter()`
gives you per-token events the moment they arrive.

## `query()` — flat-shape messages

```python
from mantis_agent import MantisAgentOptions, query

async for msg in query(
    prompt="...", options=MantisAgentOptions(model="qwen2.5:7b")
):
    if msg.type == "assistant":
        for block in msg.content:
            if getattr(block, "text", None):
                print(block.text, end="", flush=True)
    elif msg.type == "result":
        print(f"\n[done — ${msg.total_cost_usd:.4f}]")
```

`query()` yields one `SDKMessage` per *logical message*: a complete
assistant turn, a tool-result user turn, a system event, or a final result
message. It does not stream individual tokens — for that, use
`Agent.run_iter()`.

## `Agent.run_iter()` — stream events

Events are `msgspec` structs, so you dispatch on **type**, not on a string
tag. Deltas are nested: a text token arrives as
`ContentBlockDelta(index, delta=TextDelta(text=...))`.

```python
import sys

from mantis_agent import Agent, UserMessage
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStop,
    InputJsonDelta,
    TextDelta,
    ThinkingDelta,
)

agent = Agent(
    model="qwen2.5:7b",
    backend="http://localhost:11434",
    tools=[get_weather],
)

async for event in agent.stream([UserMessage(content="Weather in Lagos?")]):
    if isinstance(event, ContentBlockDelta):
        if isinstance(event.delta, TextDelta):
            sys.stdout.write(event.delta.text)
        elif isinstance(event.delta, ThinkingDelta):
            # note: ThinkingDelta's field is `thinking`, not `text`
            sys.stdout.write(f"\033[2m{event.delta.thinking}\033[0m")
        elif isinstance(event.delta, InputJsonDelta):
            pass  # tool-call arguments streaming in
    elif isinstance(event, ContentBlockStop):
        pass  # a block finished; tool dispatch fires around here
```

`agent.stream()` covers **one** assistant turn — it does not run the multi-turn
loop. Append the assistant message and any tool results, then call it again.
Use `agent.run_iter(messages)` for whole messages as they complete, or
`query()` for the fully-managed loop.

Event structs:

| Struct | Fields | Meaning |
|---|---|---|
| `MessageStart` | `message_id`, `model`, `role` | New assistant message. |
| `ContentBlockStart` | `index`, `block` | New block (text / thinking / tool_use). |
| `ContentBlockDelta` | `index`, `delta` | A token; `delta` is one of the three below. |
| `TextDelta` | `text` | Token of visible text. |
| `ThinkingDelta` | `thinking` | Token of reasoning. |
| `InputJsonDelta` | `partial_json` | Token of a tool call's JSON arguments. |
| `ContentBlockStop` | `index` | Block finished. |
| `MessageDelta` | `stop_reason`, `stop_sequence`, `usage` | Mid-message metadata. |
| `MessageStop` | — | Message finished. |
| `ErrorEvent` | `error_type`, `message`, `raw` | Provider-level error mid-stream. |

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

## Steering a running agent

`Agent.steer(text)` adds a message to the run's **next** turn while it runs.
Nothing is cancelled: the model call in flight finishes, the tools it asked
for finish, and the next request includes your text as a user message.

```python
from mantis_agent import Agent, UserMessage

agent = Agent(model="qwen2.5:7b", tools=[install_package])
messages = [UserMessage(content="Add dark mode")]
async for msg in agent.run_iter(messages):
    if msg.role == "assistant" and "styled-components" in str(msg.content):
        agent.steer("No new dependencies, please.")
```

Where the text lands depends on when it arrives:

- **While tools run** — it rides the same user message as that turn's tool
  results, as text blocks *after* every `tool_result`. Each `tool_use` is
  still answered by the message right after it, and every wire format keeps
  results first (OpenAI/Ollama: the `tool` messages, then a user message;
  Anthropic: text blocks after the `tool_result` blocks).
- **After the final answer** (or between turns) — its own user message, and
  the run continues to answer it instead of stopping.

Several steers keep their order. Each one also passes the `UserPromptSubmit`
hook, and when it joins the history the agent fires a `SteerEvent` on
`agent.on_event` (`status="delivered"` or `"blocked"`, `placement=
"tool_results"` or `"user_turn"`) — what a UI needs to show it.

`steer()` returns `False` when no run is live (not started, or already
ended); the message is not kept. A run that stops before it can send an
accepted steer — cancelled, the step cap — hands it back through
`agent.take_undelivered_steers()`.

If a `task` subagent is blocking the turn when you steer, it moves to the
background: the `task` call returns "moved to background job #N", the turn
continues with your message, and the subagent keeps working as a job (it
needs `make_task_tool(jobs=JobManager(...))`). Its result arrives through
the job's `on_event` callback or `job_output(job_id=N)`. The subagent keeps
its concurrency slot until it finishes.

`query()` returns a `QueryRun` with `await run.steer(text)`, and
`ClaudeSDKClient` has `await client.steer(text)` — both call `Agent.steer` on
the live run.

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

## About the `stderr` option

`MantisAgentOptions` accepts a `stderr` callable for signature parity with the
Claude SDK, where it receives lines from the CLI subprocess. This SDK doesn't
shell out to a CLI, and **nothing currently invokes the callback** — passing it
is harmless but does nothing. (In a plain options dict it isn't even a
recognized key; it lands in `Agent.extra`.)

For token-level visibility, stream the events yourself:

```python
from mantis_agent import Agent, UserMessage

agent = Agent(model="qwen2.5:7b", backend="http://localhost:11434")

async for event in agent.stream([UserMessage(content="hi")]):
    print(event.type)
```

`agent.stream` yields one assistant turn's events; append the resulting message
(plus any tool results) and call it again to continue the conversation.
