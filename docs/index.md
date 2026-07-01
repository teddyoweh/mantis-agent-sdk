# mantis-agent-sdk

**Drop-in open-source agent SDK. Multi-model, streaming, MCP, sub-agents.**

`mantis-agent-sdk` is the [Claude Agent SDK](https://docs.anthropic.com/) API
surface, reimplemented on top of any model you can call from Python: Ollama,
vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras, llama.cpp, TGI,
OpenAI, Gemini — and the Anthropic API too (via the `anthropic_passthrough`
provider, for parity testing).

If you have working Claude SDK code, you almost always change two lines:

```python
# from claude_agent_sdk import query, MantisAgentOptions
from mantis_agent import query, MantisAgentOptions
```

The yielded message shapes match. `MantisAgentOptions`, `Plugin`,
`HookMatcher`, `ToolPermissionContext`, `create_sdk_mcp_server`,
`PermissionResultAllow(updated_input=...)` — all of it works.

---

## What you get

<div class="grid cards" markdown>

-   :material-cube-outline: **One API, many backends**

    Auto-routes from the model name. `llama3.2:3b` → Ollama.
    `qwen2.5-72b-instruct` over OpenAI-compat → vLLM/Together/Fireworks.
    `gpt-4o-mini` → OpenAI. No `backend=` parameter needed.

-   :material-wrench-outline: **Real tool use**

    Native `tools[]` where supported. Falls back to prompt-engineered
    `<tool_call>` XML or grammar-constrained JSON for older models. Parallel
    dispatch. Mid-stream execution (tools fire on `ContentBlockStop`, not
    after `MessageStop`).

-   :material-connection: **Full MCP support**

    In-process via `create_sdk_mcp_server`. stdio / sse / http transports.
    Elicitation (servers prompt users mid-tool-call). Sampling (servers call
    back into the agent's model).

-   :material-floppy: **Sessions that survive restarts**

    JSONL transcript persistence, fork from any checkpoint, resume from
    arbitrary checkpoint, auto-compaction at token threshold. SQLite or
    in-memory store.

-   :material-account-group-outline: **Sub-agents and plugins**

    Compose agents as tools. `Plugin(tools=, system_prompt_addition=,
    hooks=)` merges at session start. Permission rewriting via
    `PermissionResultAllow(updated_input=...)`.

-   :material-cash-multiple: **Budget controls**

    Per-model pricing table. `max_usd` ceiling, `max_turns` ceiling,
    `BudgetExceededError`. Cost surfaces on every `ResultMessage`.

</div>

---

## Install

```bash
pip install mantis-agent-sdk
```

That's all you need for hosted backends (Ollama, OpenAI-compat, OpenAI,
Anthropic). For local CPU-friendly models:

```bash
pip install mantis-agent-sdk
mantis-agent setup-local
```

This installs Ollama, pulls a small CPU-friendly model
(`qwen2.5:0.5b` by default), and smoke-tests the install. Linux, macOS, and
Windows are supported.

---

## Hello world

```python
import asyncio
from mantis_agent import query, tool


@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F, partly cloudy"


async def main():
    async for msg in query(
        prompt="What's the weather in Lagos?",
        options={
            "model": "qwen2.5:7b",
            "tools": [get_weather],
        },
    ):
        if msg.type == "assistant":
            for block in msg.message["content"]:
                if block["type"] == "text":
                    print(block["text"])


asyncio.run(main())
```

---

## Where to go next

| You want to… | Read |
|---|---|
| Get up and running in 5 minutes | [Quickstart](getting-started/quickstart.md) |
| Pick a model + backend | [Models and backends](guides/models-and-backends.md) |
| Stream and dispatch tools mid-response | [Streaming](guides/streaming.md) |
| Plug in an MCP server | [MCP servers](guides/mcp.md) |
| Fork or resume a session | [Sessions and resume](guides/sessions.md) |
| Cap spend | [Budget and limits](guides/budget.md) |
| Build sub-agents | [Sub-agents](guides/sub-agents.md) |
| Look up a symbol | [API reference](api/index.md) |
| Understand the design | [Upstream comparison](development/upstream-comparison.md) |

---

## Status

- Python 3.11+
- Apache-2.0 licensed
- See the [Roadmap](https://github.com/teddyoweh/mantis-agent-sdk#roadmap) on
  GitHub for current 1.0 prerequisites
