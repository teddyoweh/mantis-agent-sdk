---
name: mantis-agent-sdk
description: >
  Build tool-calling AI agents in Python on any model — five first-class
  provider families: Claude (Anthropic, API key or subscription login),
  OpenAI, Gemini, Grok (xAI), and open-source models on local Ollama, vLLM,
  llama.cpp, or hosted providers (Together, Fireworks, Groq, OpenRouter,
  Cerebras) — using Anthropic's claude-agent-sdk surface. Also deploys any
  open-weight model on your own GPU cloud (RunPod, HF Endpoints, Modal,
  DeepInfra, Baseten, Vast.ai). Use this skill when the user wants an
  agent, tool use, MCP, sessions, or sub-agents on a model they choose, or
  wants to run Claude Agent SDK code on a different model.
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
- **Vendor APIs:** set `ANTHROPIC_API_KEY` (or a Claude subscription token in
  `ANTHROPIC_AUTH_TOKEN`), `OPENAI_API_KEY`, `GEMINI_API_KEY`, or
  `XAI_API_KEY` — a bare model name then routes to the vendor.
- **Your own GPU cloud:** `mantis-agent deploy creds runpod --set RUNPOD_API_KEY=...`
  then `mantis-agent deploy up runpod Qwen/Qwen3-32B --gpu <id>` (see below).

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
| `claude-opus-5`, `claude-sonnet-5` (`claude-*`) | Anthropic Messages API, native — `ANTHROPIC_API_KEY` or a subscription OAuth token in `ANTHROPIC_AUTH_TOKEN`; thinking mapped per generation |
| `gpt-5.4`, `o3`, `o4-mini` | OpenAI (`OPENAI_API_KEY`; reasoning models get `reasoning_effort` / `max_completion_tokens`) |
| `gemini-2.5-pro` (`gemini-*`) | Google Gemini (`GEMINI_API_KEY` / `GOOGLE_API_KEY`) |
| `grok-4`, `grok-3-mini` (`grok-*`) | xAI at `api.x.ai` (`XAI_API_KEY` / `GROK_API_KEY`; `reasoning_effort` only where the model takes it) |
| `Qwen/Qwen2.5-72B-Instruct` (org/model) | Together, or any OpenAI-compat URL via `MANTIS_AGENT_BASE_URL` |
| `accounts/fireworks/models/…` | Fireworks |
| `gpt-oss:20b` | Local Ollama — open weights, not served by OpenAI |

Overrides: `backend="https://..."` in options (or `MANTIS_AGENT_BASE_URL`)
always wins; `backend="anthropic"` is the explicit Claude form, and any
`/anthropic/v1` gateway URL selects the same native adapter. Each vendor's
own key wins by host, so several keys in one shell don't collide.
`extra_headers={...}` (or `MANTIS_AGENT_EXTRA_HEADERS` as JSON) adds
per-request headers for endpoints that authenticate that way (Modal
proxy tokens, gateway tenant headers). `MANTIS_AGENT_MOCK=1` forces the
mock provider (CI, no keys).

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

## Headless / CI (no interaction)

One-shot coding agent from the shell — great inside scripts and CI:

```bash
mantis-agent run "Fix the failing test" --model qwen2.5:7b --tools --json
cat spec.md | mantis-agent run - --model qwen2.5:7b --tools   # prompt from stdin
```

`--tools` grants read/write/edit/bash/grep/glob/lsp/web (dangerous shell
commands are refused unless you add `--dangerously-skip-permissions`/
`--yes`). `--json` prints one object: `result`, `is_error`, `num_turns`,
`total_cost_usd`, `usage`, `session_id` — gate CI on `is_error`.

The interactive terminal: `mantis` (resume last conversation with
`mantis --continue`; autonomy via `/goal`, `/watch`, `/loop`; `/init`
writes a MANTIS.md project brief). `/model claude-opus-5` / `/model grok-4`
/ `/model gpt-5` / `/model qwen3:8b` switch across all five families with a
one-line routing confirmation; `/enable <provider>` adds a key inline;
`/dash` (or `/dash live`) is an in-terminal dashboard — model, family and
auth source, context bar, session cost, jobs, MCP, edits; `/thinking
show|hide|collapse` controls reasoning rendering; `/deploy …` deploys a
model on your GPU cloud from inside the session.

## Dashboard and deploy

`mantis serve` opens a local instrument panel (`http://127.0.0.1:8787`,
`--lan` to share on the wifi with a token): five provider-family cards with
auth state and a connection test, sessions with a per-turn context-fill
chart, models grouped by family with price and context window, spend, and
a **Deploy** page. Keys `1…7` and `g o / g s / g m / g d` navigate.

Bring your own GPU provider — RunPod, HF Inference Endpoints, Modal,
DeepInfra, Baseten, Vast.ai — and deploy any open-weight model as an
OpenAI-compatible endpoint:

```bash
mantis-agent deploy creds runpod --set RUNPOD_API_KEY=...   # once, validated
mantis-agent deploy models qwen3                           # HF Hub search: params · VRAM · vLLM-ok
mantis-agent deploy gpus runpod --min-vram 48              # catalogue, cheapest first
mantis-agent deploy up runpod Qwen/Qwen3-32B --gpu <id>    # pre-flight, deploy, wait, connect
mantis-agent deploy ls | logs <id> | down <id>             # every action takes --json
```

Pre-flight estimates VRAM (weights + KV cache) and grades GPUs fits / tight
/ no. A deployment records `endpoint_url`, `served_model_name` (what goes in
`model=`) and the auth env var *name* — never a secret — so the SDK side is
`MantisAgentOptions(model=dep.served_model_name, backend=dep.endpoint_url)`.
Python: `from mantis_agent.deploy import deploy, connect, teardown`.
Vast.ai endpoints are plain HTTP on a public IP; everything bills by the
hour until `down`.

## Verify and debug

- Run any bundled example: `python -m mantis_agent.examples.quickstart`
  (add `MANTIS_AGENT_MOCK=1` to run with no model/keys).
- Check routing: `from mantis_agent.routing import infer_backend;
  infer_backend("qwen2.5:7b")  # 'http://localhost:11434'`,
  `infer_backend("grok-4")  # 'https://api.x.ai/v1'`,
  `infer_backend("claude-opus-5")  # 'anthropic'` (the sentinel, not a URL).
- Tool-use strategy per model: `from mantis_agent import lookup_model;
  lookup_model("deepseek-r1:1.5b")` — native, prompted XML, or
  grammar-constrained JSON is chosen automatically from the model and the
  backend together. There is no `tool_use_path` option; to force a path,
  pass `model_capability=replace(cap, supports_native_tools=False)`.
- Tracing: `Agent(model=..., tracer=InMemoryTracer())` → `tracer.summary()`
  gives turns/tokens/cost; `OTelTracer()` ships the same spans to any
  OpenTelemetry pipeline.

## Gotchas

- Old models without function calling still get tools (prompted XML path) —
  don't filter them out, just try. Small-model slop (string-typed args,
  extra kwargs, near-miss tool names, `<function=…>` formats) is coerced
  and salvaged automatically.
- Reliability is built in: transient errors retry with backoff (honoring
  `Retry-After`), context overflow auto-compacts and retries, and
  `fallback_model="..."` retries a pre-output failure on a second model.
- `max_tokens` defaults to the model's output budget — don't set 1024
  manually out of habit.
- Hooks include `UserPromptSubmit` (inject context / block a prompt) and
  `PreCompact`, with multiple hooks per event and tool-name matchers.
- Groq/Cerebras context windows are tighter than the model cards claim;
  the capability table accounts for it.
- The `mantis` terminal (Claude-Code-style coding agent) ships in the same
  pip package: `mantis setup && mantis`.

Docs: https://mantisagent.cc/docs ·
Source: https://github.com/teddyoweh/mantis-agent-sdk
