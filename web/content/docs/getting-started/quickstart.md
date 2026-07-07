# Quickstart

Five minutes from an empty folder to an agent that calls your Python
functions. You need Python 3.11+ and one place to run a model — your own
laptop counts.

## 1. Install

```bash
pip install mantis-agent-sdk
```

## 2. Give it a model

Pick **one** of these. If you're not sure, pick the first — it's free and
runs on any laptop.

**On your laptop (Ollama)**

```bash
mantis-agent setup-local        # installs Ollama + pulls a small model
# or, if you already have Ollama:
ollama pull qwen2.5:7b
```

No keys, no env vars. mantis finds Ollama on `localhost:11434` by itself.

**On a hosted provider (Together, Fireworks, Groq, …)**

```bash
export MANTIS_AGENT_BASE_URL=https://api.together.xyz/v1
export MANTIS_AGENT_API_KEY=$TOGETHER_API_KEY
```

Any provider with an OpenAI-compatible endpoint works the same way — set
its URL and key. Full recipes per provider are in
[Models and backends](../guides/models-and-backends.md).

**OpenAI**

```bash
export OPENAI_API_KEY=sk-...
```

Use a model name like `gpt-4o-mini` and mantis routes to OpenAI directly.

## 3. Write your first agent

Save this as `quickstart.py`:

```python
import asyncio
from mantis_agent import query, MantisAgentOptions, tool, AssistantMessage

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F, partly cloudy"

async def main():
    async for msg in query(
        prompt="What's the weather in Lagos?",
        options=MantisAgentOptions(
            model="qwen2.5:7b",   # swap for "gpt-4o-mini" or any model you set up
            tools=[get_weather],
            max_turns=4,
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if hasattr(block, "text"):
                    print(block.text)

asyncio.run(main())
```

```bash
python quickstart.py
```

The model reads your question, decides to call `get_weather("Lagos")`,
gets the result back, and answers in plain English. That round-trip —
model → your function → model — is the agent loop, and it's the whole
foundation of the SDK.

## What each piece does

- **`@tool`** turns an async Python function into something the model can
  call. The function signature and docstring become the schema the model
  sees — no separate JSON to write.
- **`query()`** runs the agent loop and streams back messages as they
  happen: what the assistant said, which tools it called, and a final
  result with the cost.
- **`model="qwen2.5:7b"`** is the only routing you do. mantis reads the
  name and works out where the model lives — this one goes to your local
  Ollama. `gpt-4o-mini` would go to OpenAI. Nothing else changes.
- **`max_turns=4`** caps the loop so it can't run away. Add
  `max_usd=0.10` to cap spend too — see [Budget](../guides/budget.md).

## Keep a conversation going

`query()` is one-shot. For a conversation the model remembers, use
`ClaudeSDKClient`:

```python
from mantis_agent import ClaudeSDKClient, MantisAgentOptions

async def main():
    options = MantisAgentOptions(model="qwen2.5:7b", tools=[get_weather])
    async with ClaudeSDKClient(options) as client:
        async for msg in client.query("What's the weather in Lagos?"):
            ...
        async for msg in client.query("Now compare it to Lisbon."):
            ...   # the model remembers Lagos from the previous turn
```

Every conversation is saved to `~/.mantis-agent/sessions/` as it happens,
so you can [resume or fork it](../guides/sessions.md) later — even after a
restart.

## Where to go next

- [Models and backends](../guides/models-and-backends.md) — every provider, with copy-paste setup
- [Tools](../guides/tools.md) — more tools, parallel calls, error handling
- [MCP servers](../guides/mcp.md) — plug in the same servers Claude Code uses
- [Streaming](../guides/streaming.md) — render tokens as they arrive
- [API reference](../api/index.md) — every symbol, typed
