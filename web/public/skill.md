---
name: mantis-agent-sdk
description: >
  Build tool-calling AI agents in Python on any model — local Ollama, vLLM,
  llama.cpp, hosted providers (Together, Fireworks, Groq, OpenRouter,
  Cerebras), or closed APIs (OpenAI, Gemini) — using Anthropic's
  claude-agent-sdk surface. Use this skill when the user wants an agent,
  tool use, MCP, sessions, or sub-agents on a model they choose, or wants
  to migrate Claude Agent SDK code off the Anthropic API.
license: Apache-2.0
---

# mantis-agent-sdk

The Claude Agent SDK surface, reimplemented for any model. If you know
`claude_agent_sdk`, you know this — the migration is one import:

```python
# from claude_agent_sdk import query, ClaudeAgentOptions, tool
from mantis_agent import query, MantisAgentOptions, tool
```

## Install

```bash
pip install mantis-agent-sdk
```

Needs Python ≥ 3.11 and one place to run a model:

- **Local, free:** `mantis-agent setup-local` (installs Ollama, pulls a
  CPU-friendly model, smoke-tests it). Or `ollama pull qwen2.5:7b`.
- **Hosted:** set `MANTIS_AGENT_BASE_URL` + `MANTIS_AGENT_API_KEY`
  (any OpenAI-compatible endpoint).
- **Closed models:** set `OPENAI_API_KEY` or `GEMINI_API_KEY`.

## The core pattern

```python
import asyncio
from mantis_agent import query, MantisAgentOptions, tool, AssistantMessage

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F"

async def main():
    async for msg in query(
        prompt="What's the weather in SF?",
        options=MantisAgentOptions(
            model="qwen2.5:7b",      # routing happens from this name
            tools=[get_weather],
            max_turns=5,
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if hasattr(block, "text"):
                    print(block.text)

asyncio.run(main())
```

`@tool` turns the function signature + docstring into the schema the model
sees. `query()` streams `SDKMessage` objects (assistant / user / system /
result). The final `ResultMessage` carries `total_cost_usd`, `num_turns`,
and `subtype` (e.g. `error_budget_exceeded`).

## Routing — how model names resolve

| Model name shape | Backend |
|---|---|
| `qwen2.5:7b`, `llama3.2:3b` (name:tag) | Local Ollama (`localhost:11434`) |
| `gpt-4o-mini`, `o3-mini` | OpenAI |
| `gemini-2.0-flash` | Google Gemini |
| `Qwen/Qwen2.5-72B-Instruct` (org/model) | OpenAI-compat via `MANTIS_AGENT_BASE_URL` |
| `claude-*` | Refused — parity testing only; use Anthropic's own SDK for Claude |

Overrides: `backend="https://..."` in options (or `MANTIS_AGENT_BACKEND`)
always wins. `MANTIS_AGENT_MOCK=1` forces the mock provider (CI, no keys).

Hosted provider recipes (all the same two env vars):

```bash
# Together
export MANTIS_AGENT_BASE_URL=https://api.together.xyz/v1
export MANTIS_AGENT_API_KEY=$TOGETHER_API_KEY
# Groq:      https://api.groq.com/openai/v1
# Fireworks: https://api.fireworks.ai/inference/v1
# OpenRouter:https://openrouter.ai/api/v1
# Cerebras:  https://api.cerebras.ai/v1
# vLLM:      http://localhost:8000/v1
# llama.cpp: http://localhost:8080/v1   (run llama-server with --jinja)
```

## Multi-turn conversations

```python
from mantis_agent import ClaudeSDKClient, MantisAgentOptions

async with ClaudeSDKClient(MantisAgentOptions(model="qwen2.5:7b")) as client:
    async for msg in client.query("What's the weather in Lagos?"):
        ...
    async for msg in client.query("Now compare it to Lisbon."):
        ...  # remembers the previous turn
```

Transcripts persist to `~/.mantis-agent/sessions/*.jsonl`; sessions can be
forked and resumed (`fork_session`, `resume_session`).

## Frequently needed options

```python
MantisAgentOptions(
    model="qwen2.5:7b",
    tools=[...],                 # @tool functions, or names of built-ins
    system_prompt="...",
    max_turns=5,                 # loop ceiling
    max_budget_usd=0.10,         # spend ceiling → error_budget_exceeded
    mcp_servers={...},           # MCP: in-process or {"transport": "stdio"|"sse"|"http", ...}
    permissions=...,             # can_use_tool / PermissionResultAllow(updated_input=...)
    hooks=[HookMatcher(...)],    # 28 lifecycle events
    setting_sources=[...],       # JSON settings files, later overrides earlier
)
```

## MCP in one snippet

```python
from mantis_agent import MantisAgentOptions, create_sdk_mcp_server, tool

@tool("add", "Add two numbers", {"a": float, "b": float})
async def add_numbers(args):
    return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}

calc = create_sdk_mcp_server(name="calculator", version="1.0.0", tools=[add_numbers])
options = MantisAgentOptions(mcp_servers={"calc": calc})
```

External servers: `{"mcp_servers": [{"transport": "stdio", "command": "uvx",
"args": ["mcp-server-fetch"]}]}` — also `sse` and `http`.

## Verify and debug

- Run any bundled example: `python -m mantis_agent.examples.quickstart`
  (add `MANTIS_AGENT_MOCK=1` to run with no model/keys).
- Check routing: `from mantis_agent.routing import resolve_backend;
  resolve_backend("qwen2.5:7b")  # 'ollama'`.
- Tool-use strategy per model: `from mantis_agent import lookup_model;
  lookup_model("deepseek-r1:1.5b").tool_use_path` — native, prompted XML,
  or grammar-constrained JSON is chosen automatically; override with
  `tool_use_path="xml_prompt_engineered"` if a model misbehaves.
- Tracing: `Agent(model=..., tracer=InMemoryTracer())` → `tracer.summary()`
  gives turns/tokens/cost; `OTelTracer()` ships the same spans to any
  OpenTelemetry pipeline.

## Gotchas

- Old models without function calling still get tools (prompted XML path) —
  don't filter them out, just try.
- Groq/Cerebras context windows are tighter than the model cards claim;
  the capability table accounts for it.
- The `mantis` terminal (Claude-Code-style coding agent) ships in the same
  pip package: `mantis setup && mantis`.

Docs: https://mantis-agent-sdk.vercel.app/docs ·
Source: https://github.com/teddyoweh/mantis-agent-sdk
