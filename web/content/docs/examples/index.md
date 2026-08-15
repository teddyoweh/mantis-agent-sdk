# Examples

Every example ships inside the package, so you can run any of them the
moment you've installed — no cloning, no setup:

```bash
python -m mantis_agent.examples.quickstart
```

New here? Run `quickstart` first, then `tools_option`, then
`mcp_calculator` — that's the core of the SDK in three commands. No API
key? Prefix any of them with `MANTIS_AGENT_MOCK=1` (details at the bottom).

## quickstart — one tool, the whole loop

The canonical starting point: `query()` with a single tool, byte-for-byte
the Claude Agent SDK pattern.

```python
from mantis_agent import MantisAgentOptions, query, tool

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city. Returns a one-line summary."""
    return f"{city}: 67°F, partly cloudy, wind 8 mph NW"

async def main() -> None:
    async for msg in query(
        prompt="What's the weather in San Francisco?",
        options=MantisAgentOptions(
            model="qwen2.5-coder:7b",
            system_prompt="You are a concise weather assistant.",
            tools=[get_weather],
            max_turns=5,
        ),
    ):
        if msg.type == "assistant":
            for block in msg.content:
                if getattr(block, "text", None):
                    print(block.text)
        elif msg.type == "result":
            print(f"[{msg.subtype}] {msg.num_turns} turns, ${msg.total_cost_usd:.4f}")
```

```bash
python -m mantis_agent.examples.quickstart
```

[Full source →](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/quickstart.py)

## tools_option — controlling which tools the model sees

Pass specific tool names, or an empty list to disable everything:

```python
from mantis_agent import MantisAgentOptions, SystemMessage, query

options = MantisAgentOptions(
    tools=["Read", "Glob", "Grep"],   # or tools=[] to disable all built-ins
    max_turns=1,
)

async for message in query(
    prompt="What tools do you have available?",
    options=options,
):
    if isinstance(message, SystemMessage) and message.subtype == "init":
        print("Tools:", message.data.get("tools", []))
```

[Full source →](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/tools_option.py)

## mcp_calculator — an in-process MCP server

Author MCP tools with the same `@tool` decorator and serve them without a
subprocess:

```python
from mantis_agent import MantisAgentOptions, create_sdk_mcp_server, tool

@tool("add", "Add two numbers", {"a": float, "b": float})
async def add_numbers(args):
    result = args["a"] + args["b"]
    return {"content": [{"type": "text", "text": f"{args['a']} + {args['b']} = {result}"}]}

calculator = create_sdk_mcp_server(
    name="calculator",
    version="1.0.0",
    tools=[add_numbers],   # plus subtract / multiply / divide in the full file
)

options = MantisAgentOptions(mcp_servers={"calc": calculator})
```

[Full source →](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/mcp_calculator.py)
· External stdio server variant:
[mcp_filesystem.py](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/mcp_filesystem.py)

## streaming_render — token-by-token output

The one example that uses the lower-level `Agent` API: raw events for
TUIs and WebSocket renderers.

```python
from mantis_agent import Agent, UserMessage
from mantis_agent.events import ContentBlockDelta, TextDelta

agent = Agent(model="qwen2.5-7b-instruct", max_tokens=200)

messages = [UserMessage(content="Tell me one interesting fact about the moon.")]
async for ev in agent.stream(messages):
    if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, TextDelta):
        sys.stdout.write(ev.delta.text)   # print each token as it arrives
        sys.stdout.flush()
```

[Full source →](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/streaming_render.py)
· Notebook variant:
[streaming_mode_ipython.py](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/streaming_mode_ipython.py)

## max_budget_usd — a hard ceiling on spend

Set a dollar cap; the run stops cleanly when it would overspend:

```python
from mantis_agent import MantisAgentOptions, ResultMessage, query

options = MantisAgentOptions(max_budget_usd=0.10)  # ten cents, max

async for message in query(prompt="What is 2 + 2?", options=options):
    if isinstance(message, ResultMessage):
        print(f"Total cost: ${message.total_cost_usd:.4f}")
        print(f"Status: {message.subtype}")   # 'error_budget_exceeded' if capped
```

[Full source →](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/max_budget_usd.py)

## multi_agent_research — a team of sub-agents

A parent agent fans out to researcher, drafter, and reviewer sub-agents,
then assembles the result. The biggest example in the package, and the
best tour of `SubAgentSpec`.

```bash
python -m mantis_agent.examples.multi_agent_research
# or, no API key:
MANTIS_AGENT_MOCK=1 python -m mantis_agent.examples.multi_agent_research
```

[Full source →](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/multi_agent_research.py)
· Single-agent variant:
[research_agent.py](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/research_agent.py)

## Everything else

| Example | What it shows |
|---|---|
| [`ollama_local.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/ollama_local.py) | Pointing at a local Ollama daemon. |
| [`with_thinking.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/with_thinking.py) | Rendering thinking blocks separately from the final answer. |
| [`system_prompt.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/system_prompt.py) | Setting `system_prompt`. |
| [`with_tracing.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/with_tracing.py) | Full span tree of a run with `InMemoryTracer`. |
| [`stderr_callback_example.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/stderr_callback_example.py) | The `stderr` callback for debug logging. |
| [`fireworks_hosted.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/fireworks_hosted.py) | Running against live Fireworks. |
| [`vllm_self_hosted.py`](https://github.com/teddyoweh/mantis-agent-sdk/blob/main/mantis_agent/examples/vllm_self_hosted.py) | Running against a self-hosted vLLM. |

## Running without an API key

Most examples detect `MANTIS_AGENT_MOCK=1` and run against the mock
provider, so CI works with no keys:

```bash
MANTIS_AGENT_MOCK=1 python -m mantis_agent.examples.quickstart
```

In mock mode the assistant emits canned but correctly-shaped responses —
useful for verifying your integration plumbing end to end.
