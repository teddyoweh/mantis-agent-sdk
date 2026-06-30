# mantis-agent-sdk

**Claude Agent SDK for open-source models.** Drop-in compatible with `claude-agent-sdk` — swap the import, keep your code — but the agent loop runs against Llama, Qwen, DeepSeek, Mixtral, Phi, Gemma, or anything you serve through Ollama, vLLM, llama.cpp, TGI, Together, Fireworks, Groq, or OpenRouter.

```python
# Before
from claude_agent_sdk import query, ClaudeAgentOptions, tool

# After
from mantis_agent import query, ClaudeAgentOptions, tool
```

That's it. Every canonical Claude SDK example runs verbatim. The wire format underneath is OpenAI-compat or Ollama; the surface above is Anthropic-shaped.

---

## Quick start

```bash
pip install mantis-agent-sdk
mantis-agent setup-local         # installs Ollama if missing, pulls qwen2.5:1.5b, verifies
```

```python
import asyncio
from mantis_agent import query, ClaudeAgentOptions, tool, AssistantMessage

@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: 67°F"

async def main():
    async for msg in query(
        prompt="What's the weather in SF?",
        options=ClaudeAgentOptions(
            model="qwen2.5:1.5b",   # routes to local Ollama automatically
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

Same script against Together AI — change one line:

```python
options = ClaudeAgentOptions(
    model="Qwen/Qwen2.5-72B-Instruct-Turbo",  # routes to Together automatically (uses $TOGETHER_API_KEY)
    tools=[get_weather],
    max_turns=5,
)
```

Same script against Fireworks, vLLM, llama.cpp, Groq — just change `model`. The backend URL is inferred from the model name shape; pass `backend=` explicitly to override.

---

## Custom backend — point at any OpenAI-compatible server

Auto-routing covers the well-known providers from the model name. For everything else — your own vLLM on a private GPU box, LM Studio on a custom port, a corporate proxy, OpenRouter, Groq, an internal inference cluster — pass `backend=` explicitly. The URL wins over inference.

```python
# Self-hosted vLLM on a private GPU box
options = ClaudeAgentOptions(
    model="Qwen/Qwen2.5-72B-Instruct",
    backend="https://gpu-box.internal:8000/v1",
    api_key=os.environ["INTERNAL_KEY"],
    tools=[get_weather],
)

# LM Studio on a non-standard port
options = ClaudeAgentOptions(
    model="qwen2.5:7b",
    backend="http://localhost:1234/v1",
    tools=[get_weather],
)

# Groq (blazing fast llama / mixtral)
options = ClaudeAgentOptions(
    model="llama-3.3-70b-versatile",
    backend="https://api.groq.com/openai/v1",
    api_key=os.environ["GROQ_API_KEY"],
)

# OpenRouter aggregator (200+ models behind one API)
options = ClaudeAgentOptions(
    model="anthropic/claude-3.5-sonnet",  # OpenRouter proxies even Anthropic
    backend="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)
```

Or set it once for the whole process via env:

```bash
export MANTIS_AGENT_BASE_URL=https://gpu-box.internal:8000/v1
export MANTIS_AGENT_API_KEY=...
python my_agent.py
```

Precedence: explicit `backend=` > `$MANTIS_AGENT_BASE_URL` > model-name inference > Ollama default.

---

## Models — ranked, picked by where they run

Ranked by current OSS leaderboards (Arena Elo · GPQA · SWE-bench, May 2026). Pick the highest-ranked model that fits your hardware.

| # | Model                  | Runs                       | `model=`                                  | Notable                                             |
|--:|------------------------|----------------------------|-------------------------------------------|-----------------------------------------------------|
| 1 | **Kimi K2.6**          | cloud                      | `moonshotai/Kimi-K2.6-Instruct`           | #1 open-weights GPQA (90.5%)                        |
| 2 | **Qwen3 235B-A22B**    | cloud · 64 GB+ local       | `Qwen/Qwen3-235B-A22B-Instruct-Turbo`     | Broadest benchmark leader · Apache 2.0              |
| 3 | **GLM-5**              | cloud                      | `zai-org/GLM-5`                           | Best Arena Elo among open (1451)                    |
| 4 | **MiniMax M2.5**       | cloud                      | `minimaxai/MiniMax-M2.5`                  | 80.2% SWE-bench · ties Claude Opus 4.6 on code      |
| 5 | **DeepSeek-V3.2**      | cloud · 80 GB+ local       | `deepseek-ai/DeepSeek-V3.2`               | Top general-purpose OSS                             |
| 6 | **Llama 4 Maverick**   | cloud · 72 GB local        | `meta-llama/Llama-4-Maverick-17B-128E`    | Meta's flagship 2025 MoE                            |
| 7 | **gpt-oss-120b**       | cloud · 80 GB local        | `gpt-oss:120b`                            | OpenAI's open release · ~o4-mini class              |
| 8 | **DeepSeek-R1**        | cloud · 48 GB+ local       | `deepseek-r1:70b` / `deepseek-ai/...`     | Reasoning · emits `<think>` blocks                  |
| 9 | **Llama 4 Scout**      | 24 GB local · cloud        | `llama4:scout`                            | 10M context window · fits a 24 GB GPU               |
| 10 | **Hermes 4 70B**      | 48 GB local · cloud        | `hermes4:70b`                             | Nous — tool-use + reasoning tuned                   |
| 11 | **DeepSeek-R1 32B**   | 24 GB local                | `deepseek-r1:32b`                         | Reasoning, fits a big-laptop GPU                    |
| 12 | **Qwen3 32B**         | 24 GB local                | `qwen3:32b`                               | Strong general-purpose                              |
| 13 | **Llama 3.3 70B**     | 48 GB local · cloud        | `llama3.3:70b`                            | Stable, well-supported                              |
| 14 | **gpt-oss-20b**       | 16 GB local                | `gpt-oss:20b`                             | OpenAI open · runs on a laptop                      |
| 15 | **Phi 4 medium**      | 16 GB local                | `phi4:medium`                             | MS — strong reasoning for size                      |
| 16 | **Gemma 3 27B**       | 16 GB local                | `gemma3:27b`                              | Google's latest                                     |
| 17 | **Qwen3 14B / 8B**    | 8–12 GB local              | `qwen3:14b` / `qwen3:8b`                  | Mid-tier all-rounder                                |
| 18 | **Llama 3.1 8B**      | 8 GB local                 | `llama3.1:8b`                             | Mainstream baseline                                 |
| 19 | **Phi 4 small**       | 8 GB local                 | `phi4:small`                              | Compact reasoning                                   |
| 20 | **DeepSeek-R1 8B/14B**| 8–12 GB local              | `deepseek-r1:8b` / `:14b`                 | Reasoning on a mainstream laptop                    |

**CPU-laptop tier** (no GPU, ≤ 8 GB RAM) — `mantis-agent setup-local` picks from this list:

| # | Tag                  | Params | RAM   | Tools | Reasoning | Notes                              |
|--:|----------------------|-------:|------:|:-----:|:---------:|------------------------------------|
| C1 | `qwen2.5:1.5b`       | 1.5B   | 4 GB  | yes   | no        | **Default** — best 1.5B for agents |
| C2 | `deepseek-r1:1.5b`   | 1.5B   | 4 GB  | yes   | yes       | Reasoning, emits `<think>`         |
| C3 | `llama3.2:3b`        | 3.2B   | 6 GB  | yes   | no        | Best 3B for 8 GB laptops           |
| C4 | `qwen2.5:3b`         | 3B     | 6 GB  | yes   | no        | Same class as Llama 3.2 3B         |
| C5 | `phi3.5:3.8b`        | 3.8B   | 6 GB  | yes   | no        | Punches above its weight           |
| C6 | `llama3.2:1b`        | 1.2B   | 4 GB  | yes   | no        | Sharper than 0.5B Qwen             |
| C7 | `qwen2.5:0.5b`       | 0.5B   | 2 GB  | yes   | no        | Smallest with tool calls           |
| C8 | `gemma2:2b`          | 2B     | 4 GB  | no    | no        | Chat only, polished prose          |
| C9 | `tinyllama:1.1b`     | 1.1B   | 2 GB  | no    | no        | RAM-constrained pick               |
| C10 | `smollm2:135m`      | 135M   | 2 GB  | no    | no        | Tiny — sanity-check install        |

```bash
mantis-agent setup-local           # one command — installs Ollama if missing, pulls C1, smoke tests
mantis-agent setup-local --list    # see the catalog
mantis-agent setup-local --model qwen2.5:3b
```

### How to actually call them

Auto-routing reads the model name shape (see `mantis_agent/routing.py`):

| Shape                                      | Backend it routes to                          | Env to set                                  |
|--------------------------------------------|-----------------------------------------------|---------------------------------------------|
| `name:tag` (e.g. `qwen3:8b`)               | Ollama (`http://localhost:11434`)             | —                                           |
| `org/repo` (e.g. `Qwen/Qwen3-235B-...`)    | Together AI                                   | `TOGETHER_API_KEY`                          |
| `accounts/fireworks/models/...`            | Fireworks AI                                  | `FIREWORKS_API_KEY`                         |
| `gpt-*`, `o1-*`, `o3-*`, `o4-*`            | OpenAI native                                 | `OPENAI_API_KEY`                            |
| `gemini-*`                                 | Google Gen-Lang (OpenAI-compat)               | `GEMINI_API_KEY`                            |
| `claude-*`                                 | refused — use the real `claude-agent-sdk`     | —                                           |
| anything else                              | Ollama default                                | —                                           |

For Groq, Moonshot (Kimi native), DeepSeek native, OpenRouter, Cerebras, DeepInfra, Anyscale, LM Studio, self-hosted vLLM / llama.cpp / TGI — pass `backend=` explicitly or set `MANTIS_AGENT_BASE_URL` (see **Custom backend** above). The pattern is the same: it's an OpenAI-compatible URL plus an API key.

---

## Why this exists

The Claude Agent SDK is the best-designed agent runtime in the open. Streaming tool dispatch, 28-event hook system, permission rules per source, MCP across four transports, sub-agents, sessions with fork/resume, auto-compaction — none of the OSS alternatives ship the whole set. LangGraph is too heavy and skips MCP. smolagents is too small. llama-stack is tightly scoped. The Anthropic and OpenAI agent SDKs are bound to their hosted APIs.

mantis-agent-sdk is the same surface, model-agnostic underneath. You write to Anthropic's design; you run it on whatever you can serve.

Plus the OSS-specific bits the hosted SDKs don't need to think about:

- **Universal tool use** — Path A (native via OpenAI-compat `tools[]`) when supported; Path B (prompt-engineered `<tool_call>` XML) when not; Path C (grammar-constrained JSON) when the server can enforce it. Capability-table-driven, automatic per model.
- **Universal thinking** — handles inline `<think>` tags (R1, QwQ, Marco-o1, R1-Distill) and out-of-band thinking blocks. Zero cost when the model doesn't emit thinking.
- **Backend agnosticism** — same agent code, one env var or one kwarg between Ollama at `localhost:11434` and Fireworks at `api.fireworks.ai`.
- **Tracing built in** — `Agent(tracer=InMemoryTracer())` gives you a full span tree of every run (`agent.run` → `agent.turn` → `llm.call` + `tool.call`), with token / cost totals on the root span and `tool.call` spans that record input KEYS but never values. Swap in `OTelTracer()` to ship the same spans to Datadog / Honeycomb / Tempo / Jaeger with zero extra code. Anthropic's official SDK requires you to wire OpenTelemetry yourself; we ship it.

---

## Observability

```python
from mantis_agent import Agent, InMemoryTracer, UserMessage, TextBlock

tracer = InMemoryTracer()
agent = Agent(model="claude-sonnet-4.5", tools=[...], tracer=tracer)
await agent.run([UserMessage(content=[TextBlock(text="...")])])

# Flat list of every finished span, in end-time order.
for sp in tracer.spans:
    print(sp.name, sp.duration_ms, sp.attributes)

# Or the forest, with parent/child links restored.
import json; print(json.dumps(tracer.tree(), indent=2, default=str))

# Or per-span-name aggregates + run totals (turns / tokens / cost_usd).
print(tracer.summary())

# Or ship the trace to disk for offline analysis.
tracer.write_jsonl("trace.jsonl")
```

To push the same spans into an existing OpenTelemetry pipeline:

```python
from mantis_agent import OTelTracer
tracer = OTelTracer(service_name="my-agent")          # requires opentelemetry-api
agent  = Agent(model="claude-sonnet-4.5", tracer=tracer)
```

`OTelTracer` uses your already-configured `TracerProvider` — point it at Datadog, Honeycomb, Tempo, Jaeger, or anything else that speaks OTLP. We don't ship an exporter; we ship spans that fit your existing one. Spans carry the same attributes whether you use `InMemoryTracer` or `OTelTracer`, so dashboards built against one work against both.

**Privacy by default.** Tool spans carry the sorted list of input *keys* but never input *values* — agent traces routinely get shipped to third-party SaaS and showed up in screenshots and tickets, so we made the safe choice the only choice. If you need values too, build your own `Tracer` impl in ~30 lines.

Live example you can run with no API key:

```bash
python -m mantis_agent.examples.with_tracing
```

---

## The acceptance test

v1.0 ships when this is true on a fresh machine:

```bash
pip install mantis-agent-sdk
mantis-agent setup-local
# ...10-line script with 2 tools + 5-turn agent task...
python my_agent.py   # Just Works on the first try
```

Then the same script works against Together, Fireworks, vLLM, llama.cpp, Groq just by changing `model`. Today: DeepSeek-R1 1.5B on local Ollama runs six of Anthropic's own canonical examples verbatim. Suite at 202 tests. The acceptance test passes on Ollama; provider matrix expansion is the remaining work.

---

## Roadmap

What's shipped — and what's still ahead. Check our progress.

**Drop-in surface (Claude SDK parity)**
- [x] `query()` yielding flat-shape `AssistantMessage` / `UserMessage` / `SystemMessage` / `ResultMessage`
- [x] `ClaudeAgentOptions` with model, backend, tools, system_prompt, max_turns, max_tokens, temperature, hooks, can_use_tool, permissions, mcp_servers, plugins, agents, max_budget_usd, setting_sources, allowed_tools, disallowed_tools, cwd, session_id, persist, stderr
- [x] `ClaudeSDKClient` — streaming async context manager
- [x] `@tool` decorator (Claude-shaped positional signature)
- [x] `AgentDefinition` for sub-agents
- [x] `Plugin(tools=, system_prompt_addition=, hooks=)` — merges at session start
- [x] `PermissionResultAllow(updated_input=...)` rewriting tool args before dispatch
- [x] `PermissionResultDeny` surfacing through `ResultMessage.permission_denials`
- [x] `HookMatcher` for 28 hook events (PreToolUse, PostToolUse, SessionStart, SessionEnd, Stop, ...)
- [x] `ToolPermissionContext` passed to `can_use_tool`
- [x] `create_sdk_mcp_server(name, version, tools=)`
- [x] `WebFetch` / `WebSearch` built-in tools (Exa-backed)
- [x] `CLIConnectionError`, `ClaudeSDKError`
- [x] `ToolPermissionContext.signal` for cancellation (`anyio.Event`, fired by `Agent.cancel()`)
- [x] `setting_sources` actually loading and persisting per source
- [x] Streaming-mode `client.query()` with mid-stream tool dispatch

**Backends**
- [x] Ollama (native API + auto-routing from tag form)
- [x] OpenAI-compat (vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras)
- [x] llama.cpp (via `--jinja`)
- [x] TGI (HuggingFace text-generation-inference)
- [x] OpenAI native (`gpt-*`, `o1`/`o3`/`o4`)
- [x] Gemini OpenAI-compat endpoint
- [x] Mock provider for tests
- [x] Auto-route from model name shape — no `backend=` needed
- [x] Modal serverless adapter
- [x] Anthropic via separate `anthropic_passthrough` (for parity testing only)

**Tool use**
- [x] Path A: native via OpenAI-compat `tools[]`
- [x] Path B: prompt-engineered `<tool_call>` XML (for Llama 2, Mistral 7B, older Qwens)
- [x] Path C: grammar-constrained JSON
- [x] Capability-table-driven path selection (30+ models)
- [x] Parallel tool dispatch
- [x] Tool result threading
- [x] Streaming tool dispatch (start tool execution mid-stream, not after `MessageStop`)

**Thinking / reasoning**
- [x] Inline `<think>` blocks (DeepSeek-R1, QwQ, Marco-o1, R1-distill family)
- [x] Out-of-band thinking blocks (DeepSeek API)
- [x] `ThinkingBlock` in `AssistantMessage.content`

**MCP**
- [x] In-process MCP server via `create_sdk_mcp_server`
- [x] stdio transport
- [x] sse transport
- [x] http transport
- [x] Elicitation (server prompts user mid-session)
- [x] Sampling (server calls back into the agent's model)

**Sessions + state**
- [x] JSONL transcript persistence
- [x] `~/.mantis-agent/` directory + per-session paths
- [x] Memory entries + index
- [x] `<system-reminder>` + `isMeta` injection
- [x] Auto-compaction at token threshold
- [x] Session fork
- [x] Session resume from arbitrary checkpoint

**Structured output**
- [x] `response_format={"type": "json_object"}` — free-form JSON mode
- [x] `response_format={"type": "json_schema", "json_schema": {...}}` — schema-constrained
- [x] Per-backend translation (OpenAI envelope / Ollama `format` / TGI grammar)
- [x] Loud rejection on backends without support (`anthropic_passthrough`)

**Budget**
- [x] Per-model pricing table
- [x] `max_usd` ceiling → `BudgetExceededError`
- [x] `total_cost_usd` on `ResultMessage`
- [x] `modelUsage` per-model breakdown
- [x] `max_turns` ceiling

**Local install**
- [x] `mantis-agent setup-local` — installs Ollama if missing, pulls a CPU-friendly model, smoke tests
- [x] 12-entry CPU-friendly catalog (135M → 8B params)
- [x] Auto-install of Ollama on Linux/macOS via official script
- [x] Windows installer wrapper
- [x] llama.cpp `setup-local` alternative for users who prefer it (`mantis-agent setup-local-llamacpp`)

**Examples (run verbatim against DeepSeek-R1 1.5B on local Ollama)**
- [x] `quickstart.py`
- [x] `ollama_local.py`
- [x] `with_thinking.py`
- [x] `tools_option.py`
- [x] `mcp_calculator.py`
- [x] `system_prompt.py`
- [x] `fireworks_hosted.py` runs against live Fireworks
- [x] `vllm_self_hosted.py` runs against live vLLM (+ `MANTIS_AGENT_MOCK=1` offline mode)
- [x] `multi_agent_research.py` end-to-end with sub-agents

**1.0 prerequisites**
- [x] Streaming tool dispatch rewrite (`iter_completions` / `wait_one` — observe results in completion order, not batched on `wait_all`)
- [x] Mid-stream cancellation via `ToolPermissionContext.signal`
- [x] All 16 examples verified against ≥ 3 backends
- [x] Docs site (mkdocs-material)
- [x] PyPI 1.0 release with semver guarantee

---

## Drop-in compatibility — what works today

```python
from mantis_agent import (
    # Core
    query, ClaudeAgentOptions, ClaudeSDKClient,

    # Messages (flat shape, matches claude_agent_sdk)
    AssistantMessage, UserMessage, SystemMessage, ResultMessage,
    TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock,

    # Tools
    tool, Tool, ToolRegistry, create_sdk_mcp_server,

    # Permissions
    PermissionResultAllow, PermissionResultDeny, ToolPermissionContext,

    # Hooks
    HookMatcher, HookInput, HookJSONOutput, HookContext,

    # Sub-agents
    AgentDefinition,

    # Plugins
    Plugin,

    # Built-in tools
    WebFetch, WebSearch,

    # Errors
    ClaudeSDKError, CLIConnectionError,
)
```

Every name in that import block has a working implementation backed by tests. `ClaudeSDKClient` is a streaming async context manager. `Plugin(tools=..., system_prompt_addition=..., hooks=...)` merges into the agent at session start. `PermissionResultAllow(updated_input={...})` rewrites tool args before dispatch. `ResultMessage.permission_denials` carries every rejected call.

---

## License

Apache-2.0. See `LICENSE`.
