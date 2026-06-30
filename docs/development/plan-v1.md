# mantis-agent-sdk — v0 plan

**One line.** A drop-in, open-source analog to the Claude Agent SDK that runs against Anthropic, OpenAI, Gemini, Bedrock, and local models, with full MCP + sub-agent support — and is faster and lighter than the upstream SDK because we're not bound to backward compat.

## Locked decisions (from prior session)

| Fork | Decision | Why |
|---|---|---|
| Deployment | Hosted **and** local from day one | Can't ship a "toy" — regulated users need local, casual users want hosted. The local path forces clean provider abstraction anyway. |
| Model families | Anthropic, OpenAI, Gemini, Bedrock/Vertex, local (Ollama/vLLM/llama.cpp) | These five cover ~98% of real usage. Cohere/Mistral/Groq drop in via OpenAI-compatible API for free. |
| Feature set | Full parity: MCP, sub-agents, streaming, tool use, thinking, prompt caching, batch, files | A subset SDK is dead on arrival. Users who can't drop-in won't migrate. |
| API surface | Mirror `anthropic.AsyncAnthropic` + `claude_agent_sdk` entry points | Drop-in means literally `from mantis_agent import AsyncAgent as AsyncAnthropic`. |

## Forks still open (need a decision before M1)

1. **Sub-agent isolation model.** Subprocess vs. asyncio task vs. external worker. Recommendation: **asyncio task** by default, subprocess flag for hard isolation. Reason: speed. Subprocess per sub-agent is a 50–200 ms tax we don't need on every spawn.
2. **State store for sessions.** SQLite by default for local, Postgres/Redis pluggable for hosted. Recommendation: **single `SessionStore` protocol**, ship a SQLite + an in-memory impl, let users implement Redis themselves. No ORM, raw SQL.
3. **Tool dispatcher concurrency.** Serial vs. parallel tool execution within one assistant turn. Recommendation: **parallel by default** with `gather`, single-flight per tool name for tools that aren't safe to parallelize (declared via `@tool(parallel_safe=False)`).

## Architecture

```
                      ┌──────────────────────────────┐
                      │            Agent             │
                      │  run() / stream() / chat()   │
                      └────────────┬─────────────────┘
                                   │
              ┌────────────────────┼───────────────────┐
              │                    │                   │
       ┌──────▼──────┐      ┌──────▼──────┐    ┌───────▼───────┐
       │  Provider   │      │   Tools     │    │    MCP        │
       │ (adapter)   │      │ (registry + │    │ (client +     │
       │             │      │  dispatch)  │    │  spawned svr) │
       └──────┬──────┘      └─────────────┘    └───────────────┘
              │
   ┌──────────┼──────────┬──────────┬──────────┐
   ▼          ▼          ▼          ▼          ▼
Anthropic  OpenAI    Gemini     Bedrock     Local
  HTTP    HTTP+SSE  HTTP+SSE     boto3    Ollama/vLLM
```

Everything flows through one **normalized event stream**. Every provider adapter emits the same `StreamEvent` variants (`MessageStart`, `ContentBlockStart`, `ContentBlockDelta`, `ContentBlockStop`, `MessageDelta`, `MessageStop`, `Error`). The agent loop never knows which provider it's talking to.

## Performance commitments (the "no slop" part)

These are non-negotiable. If a contributor PRs code that breaks one, we revert:

1. **`msgspec.Struct` everywhere, no Pydantic.** msgspec is 5–10× faster than Pydantic v2 for our shapes and uses ~3× less memory. We're typed, immutable-by-default, and zero-copy where it matters.
2. **Single shared `httpx.AsyncClient` with HTTP/2 and connection pooling.** No `async with` per request — that costs a TLS handshake every time.
3. **Streaming all the way down.** We never materialize a full SSE response into memory before yielding. `aiter_lines()` → parse → yield event. The agent loop consumes the iterator; no list-builds in the hot path.
4. **Zero-copy text deltas.** Text deltas pass through as `str` slices, not concatenated into a growing buffer until the user asks for the full message.
5. **No reflection in the hot path.** No `getattr` games, no dynamic dispatch via dicts of strings. Use match statements on tagged unions.
6. **anyio for async primitives.** Works on asyncio and trio. No `asyncio.*` imports in core; only in the adapter that calls into asyncio-only deps (boto3 in a threadpool).
7. **Lazy imports for providers.** Importing the Anthropic adapter doesn't load `boto3`. Importing `mantis_agent` doesn't load any provider — they register lazily via entry points.
8. **No global state.** Every long-lived object (HTTP client, MCP connection pool, session store) hangs off the `Agent` instance or is passed explicitly.

## Module map

```
mantis_agent/
  __init__.py            # public API surface
  types.py               # universal Message, ContentBlock, Usage — msgspec
  events.py              # StreamEvent variants (tagged union)
  errors.py              # typed exceptions
  http.py                # shared AsyncClient factory + SSE parser
  agent.py               # Agent class — the loop
  session.py             # SessionStore protocol + SQLite + InMemory
  tools.py               # @tool decorator, Tool registry, dispatcher
  providers/
    base.py              # Provider protocol
    anthropic.py         # reference impl
    openai.py
    gemini.py
    bedrock.py
    local.py             # OpenAI-compatible local servers (Ollama, vLLM)
  mcp/
    client.py            # MCP client (stdio + HTTP transports)
    server.py            # spawn + manage MCP servers as sub-agents' tool sources
  subagent.py            # sub-agent orchestration
  cli.py                 # `mantis-agent` CLI
```

## Milestones

**M0 — Skeleton + Anthropic adapter** (Week 1)
- Universal types, event stream, shared HTTP client
- Anthropic adapter with streaming + tool use + prompt caching
- `Agent.run()` and `Agent.stream()` with a single-provider tool loop
- 90%+ test coverage on the core loop, real recorded fixtures for the adapter

**M1 — Multi-provider parity** (Week 2)
- OpenAI, Gemini, Bedrock adapters — all behind the same Provider protocol
- Provider auto-detection from model name (`claude-*` → Anthropic, `gpt-*` → OpenAI, etc.)
- Cross-provider test matrix on the same 20 scripted scenarios

**M2 — MCP + sub-agents** (Week 3)
- MCP client (stdio + HTTP)
- Sub-agent spawning via asyncio tasks
- Tool dispatch supports both local Python tools and MCP-provided tools

**M3 — Production polish** (Week 4)
- SessionStore implementations (SQLite + InMemory)
- CLI for quick prototyping
- Batch API support (Anthropic + OpenAI)
- Docs, examples, a one-line install

## Non-goals (cut ruthlessly)

- No web UI. Not our job — that's downstream.
- No vector DB integration. Use MCP for that.
- No prompt template DSL. Strings work fine; users who want Jinja can pipe.
- No "evals framework." Promptfoo and Inspect exist; we integrate, we don't reinvent.
- No fine-tuning. That's a different product.

## Open questions for the user

1. **License.** MIT vs. Apache-2.0. Apache gives explicit patent grants — recommend that for an SDK.
2. **Repo home.** Personal GitHub vs. an org. Affects branding and trust signals.
3. **Telemetry.** Opt-in usage pings (model + count, no payloads) help us prioritize, or zero-telemetry purist? Recommend opt-in with very-clear-disclosure.
