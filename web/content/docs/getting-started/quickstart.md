# Quickstart

Five minutes from a clean Python env to a streaming agent with tool use.

## 1. Install

```bash
pip install mantis-agent-sdk
```

## 2. Choose a backend

Any of these works. Pick whichever you have credentials for.

=== "Ollama (local)"

    ```bash
    ollama pull qwen2.5:7b
    ```

    No env vars. `mantis-agent-sdk` will auto-discover Ollama on
    `http://localhost:11434`.

=== "Together / Fireworks / Groq / vLLM (hosted OpenAI-compat)"

    ```bash
    export MANTIS_AGENT_BASE_URL=https://api.together.xyz/v1
    export MANTIS_AGENT_API_KEY=$TOGETHER_API_KEY
    export MANTIS_AGENT_MODEL=Qwen/Qwen2.5-72B-Instruct
    ```

=== "OpenAI"

    ```bash
    export OPENAI_API_KEY=sk-...
    ```

=== "Anthropic (parity testing)"

    ```bash
    export ANTHROPIC_API_KEY=sk-ant-...
    ```

## 3. Your first agent

`quickstart.py`:

```python
import asyncio
from mantis_agent import query, tool


@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city. Returns a one-line summary."""
    return f"{city}: 67°F, partly cloudy, wind 8 mph NW"


async def main():
    async for msg in query(
        prompt="What's the weather in Lagos?",
        options={
            "model": "qwen2.5:7b",       # or "gpt-4o-mini", or
                                          # "Qwen/Qwen2.5-72B-Instruct"
            "tools": [get_weather],
            "max_turns": 4,
        },
    ):
        if msg.type == "assistant":
            for block in msg.message["content"]:
                if block["type"] == "text":
                    print(block["text"])
        elif msg.type == "result":
            print(f"\n[done — cost ${msg.total_cost_usd:.4f}]")


asyncio.run(main())
```

```bash
python quickstart.py
```

You should see the assistant pick `get_weather`, run it, then narrate the
result in plain English.

## What just happened

- `query()` is the same function the Claude Agent SDK ships. It returns an
  async iterator of `SDKMessage` objects (assistant / user / system /
  result).
- `@tool` decorates an async Python function. The signature becomes the
  JSON schema sent to the model.
- `options={"model": "qwen2.5:7b"}` — `mantis-agent-sdk` auto-routes from
  the model name. `qwen2.5:7b` → Ollama. No `backend=` argument needed.
- `max_turns=4` puts a hard ceiling on the agent loop. Pair it with
  `max_usd=0.10` for cost limits — see [Budget](../guides/budget.md).

## Streaming with `ClaudeSDKClient`

For a session that survives multiple `query()` calls:

```python
from mantis_agent import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    options = ClaudeAgentOptions(
        model="qwen2.5:7b",
        tools=[get_weather],
    )
    async with ClaudeSDKClient(options) as client:
        async for msg in client.query("What's the weather in Lagos?"):
            ...
        async for msg in client.query("Now compare it to Lisbon."):
            ...
```

The transcript is persisted to `~/.mantis-agent/sessions/{session_id}.jsonl`
between calls and can be [forked or resumed](../guides/sessions.md) later.

## Next steps

- [Pick the right backend](../guides/models-and-backends.md) for your model
- [Write more tools](../guides/tools.md), including parallel-safe ones
- [Plug in MCP servers](../guides/mcp.md)
- [Stream and dispatch tools mid-response](../guides/streaming.md)
- [Look up the full API](../api/index.md)
