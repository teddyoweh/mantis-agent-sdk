# Quickstart

A working agent with tool use, on your own machine, for free. Two minutes.

## 1. Install

```bash
pip install mantis-agent-sdk
```

## 2. Get a model

Free and local, no account:

```bash
ollama pull qwen2.5-coder:7b
```

Already have a hosted key? Skip this and see [step 5](#5-point-it-somewhere-else).

## 3. Your first agent

`quickstart.py`:

```python
import asyncio

from mantis_agent import MantisAgentOptions, query, tool


@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city. Returns a one-line summary."""
    return f"{city}: 67°F, partly cloudy, wind 8 mph NW"


async def main() -> None:
    async for msg in query(
        prompt="What's the weather in Lagos?",
        options=MantisAgentOptions(
            model="qwen2.5-coder:7b",
            tools=[get_weather],
            max_turns=4,
        ),
    ):
        if msg.type == "assistant":
            for block in msg.content:
                if getattr(block, "text", None):
                    print(block.text)
        elif msg.type == "result":
            print(f"[{msg.subtype}] {msg.num_turns} turns, ${msg.total_cost_usd:.4f}")


asyncio.run(main())
```

```bash
python quickstart.py
```

```text
The current weather in Lagos is 67°F with partly cloudy skies.
[success] 2 turns, $0.0000
```

Two turns: the model called `get_weather`, then answered from the result. No
backend argument — `qwen2.5-coder:7b` is Ollama tag form, so it routed to
`http://localhost:11434` on its own.

## 4. What each piece does

- **`@tool`** turns an async function into something the model can call. The
  signature becomes the JSON schema; the docstring is what the model reads to
  decide *when* to call it, so write it for that audience.
- **`query()`** runs the whole loop — model call, tool dispatch, feeding the
  result back — and yields messages as they happen.
- **`MantisAgentOptions`** is the typed form. It infers a backend from the model
  name, and yields flat messages (`msg.content`).
- **`max_turns=4`** is a hard ceiling on model calls. Pair with
  `max_budget_usd=` once you're on a paid provider.

!!! warning "If you get silence, check `msg.subtype`"

    `query()` does not raise on a provider failure — it reports it on the final
    message. A loop that only prints assistant text will print *nothing* and
    look like a hang. Always read the result:

    ```python
    async def report(messages):
        async for msg in messages:
            if msg.type == "result" and msg.is_error:
                print("failed:", msg.errors)   # e.g. ['ProviderError: Not Found']
    ```

    `ProviderError: Not Found` almost always means the model isn't pulled, or the
    backend URL is wrong. `ollama list` tells you the first.

## 5. Point it somewhere else

One line changes, nothing else does:

```python
import os

from mantis_agent import MantisAgentOptions

local = MantisAgentOptions(model="qwen2.5-coder:7b")

hosted = MantisAgentOptions(
    model="accounts/fireworks/models/deepseek-v3",
    base_url="https://api.fireworks.ai/inference/v1",
    api_key=os.environ["FIREWORKS_API_KEY"],
)

claude = MantisAgentOptions(
    model="claude-opus-5",
    backend="anthropic",
    api_key=os.environ["ANTHROPIC_API_KEY"],
)
```

See [Models and backends](../guides/models-and-backends.md) for the full routing
and auth rules.

## 6. Next

- [Recipes](../guides/recipes.md) — a coding agent, JSON extraction, guardrails,
  sub-agents, each a complete script.
- [How configuration works](../guides/how-it-works.md) — the option shapes, and
  why an unknown key never errors.
- [Tools](../guides/tools.md) — schemas, errors, built-ins.

### The other option shape

A plain `dict` also works, and it's what the wire-shape examples use. Two
differences that bite: it does **not** infer a backend, and its messages nest
under `.message`.

```python
import asyncio

from mantis_agent import query


async def main() -> None:
    async for msg in query(
        prompt="What's the weather in Lagos?",
        options={
            "model": "qwen2.5-coder:7b",
            "backend": "http://localhost:11434",   # required here
            "max_turns": 4,
        },
    ):
        if msg.type == "assistant":
            for block in msg.message.content:      # note: .message.content
                if getattr(block, "text", None):
                    print(block.text)


asyncio.run(main())
```
