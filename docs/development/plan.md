# mantis-agent-sdk — full plan (v2, OSS-first)

> **One line.** Claude Code for open-source models. A production-grade agent
> runtime — streaming, tools, MCP, sub-agents, hooks, permissions, compaction —
> that runs on Llama, Qwen, DeepSeek, Mixtral, Phi, Gemma, and anything else you
> can serve through Ollama, vLLM, llama.cpp, TGI, Together, Fireworks, Groq,
> or OpenRouter. Drop-in API-compatible with the Claude Agent SDK where it
> makes sense; OSS-first where the OSS reality differs.

This is the authoritative plan. Read this before opening a PR. v1 is preserved at [`plan-v1.md`](plan-v1.md) for history.

---

## 1. Why this exists

### 1.1 The state of the world (May 2026)

Three things are simultaneously true:

1. **Open-source models are good enough for real agent work.** Llama 3.3 70B, Qwen 2.5 72B, DeepSeek-V3, Mixtral 8x22B, and the R1/QwQ reasoning class hit Claude Sonnet-grade performance on most tool-use benchmarks. DeepSeek-V3 is *cheaper than Haiku per token* via the official API and runs on a single H100 self-hosted.
2. **The serving layer is solved.** Ollama, vLLM, llama.cpp, and TGI cover every realistic deployment. OpenRouter, Together, Fireworks, Groq, DeepInfra, Anyscale, and Cerebras provide OpenAI-compatible HTTP for hosted access. Cold-start, prefix-cache, paged-attention — production tooling is mature.
3. **The agent layer has *not* caught up.** The two production agent SDKs (Anthropic's Claude Agent SDK; OpenAI's Agents SDK) are tightly bound to their respective hosted APIs. Every OSS-friendly agent library — LangGraph, llama-stack, smolagents, openai-agents-python with provider shims — either (a) supports only OpenAI-compatible servers and thus only models with native tool calling, or (b) hardcodes prompt engineering for one model family and breaks the moment you switch.

There is no production-grade agent runtime that *just works* across the OSS model + serving matrix. That's the gap.

### 1.2 What "Claude Code-grade" means

We benchmarked against the actual Claude Code source (see `upstream-comparison.md`). The bar is:

- **Streaming tool execution** — start tool calls as they arrive in the stream, not after the message finalizes.
- **Concurrent + serial dispatch** with input-dependent concurrency safety, configurable cap, sibling-abort on first error.
- **28-event hook system** covering pre/post-tool, session, compact, permission, and sub-agent lifecycle.
- **Permission system** with default/auto/bypass modes + allow/deny/ask rules per source.
- **MCP support** across four transports (stdio, sse, http, in-process).
- **Auto-compaction** when the context window saturates.
- **Sub-agent orchestration** with shared state.
- **Skills** as first-class entities separate from tools.
- **Sessions** with fork, resume, replay.
- **Cost + budget tracking** with per-model pricing.

Plus, because we're OSS:

- **Tool-use universalization** — works whether the model has native tool calling or needs prompt-engineered tool calling.
- **Thinking universalization** — handles `<think>` tags inline (R1/QwQ) and out-of-band thinking blocks (DeepSeek API thinking tokens, future model variants).
- **Chat-template fallback** — when the server doesn't apply templates server-side (rare but real with raw llama.cpp completions endpoint).
- **Backend agnosticism** — same agent code switches between Ollama at `localhost:11434` and Fireworks at `api.fireworks.ai` with one env var.

### 1.3 Non-goals (cut ruthlessly)

* **Hosted Anthropic / OpenAI / Gemini support.** Out of scope. Those have first-party SDKs. We are explicitly OSS-first. We will reluctantly add OpenAI-compatible hosted-API support (Together, Fireworks, etc.) because they speak the same protocol as vLLM, but we will *not* implement an Anthropic adapter or a Gemini adapter. (This is a reversal from v1 of the plan.)
* **Web UI.** Downstream's job.
* **Vector DB.** Use MCP.
* **Prompt template DSL.** Strings work.
* **Eval framework.** Promptfoo + Inspect exist.
* **Fine-tuning.** Different product.
* **Mobile / edge.** Different surface.

---

## 2. The hard problem: tool use across the OSS stack

This is what makes or breaks an OSS agent SDK. Every other concern is downstream of getting this right.

### 2.1 The matrix

Models and serving stacks have three orthogonal axes:

| Axis | Variants | Implication |
|---|---|---|
| **Model native tool support** | Yes (Qwen 2.5, DeepSeek-V3, Llama 3.1/3.3, Mistral Large, Mixtral, Hermes-Pro, Functionary) / No (vanilla Llama 3, base Mistral 7B, Phi, Gemma, R1) | Determines whether we use the server's native `tools=[…]` API or our own prompt-engineered tool protocol |
| **Server tool-use API** | OpenAI-compatible (vLLM, Ollama 0.3+, llama.cpp server with `--jinja`, Together, Fireworks, Groq) / Native HF Inference (TGI with grammar) / Raw completion (llama.cpp default, custom) | Determines call shape; OpenAI-compatible is dominant — we default to that |
| **Thinking support** | Native blocks (DeepSeek API, future) / Inline `<think>` tags (R1, QwQ, Marco-o1) / None (most) | Determines parsing strategy |

Cross product is large but reduces to ~5 real adapter behaviors.

### 2.2 The three tool-use paths

**Path A — Native via OpenAI-compatible server.**
Server speaks `POST /v1/chat/completions` with `tools=[…]` and emits `tool_calls` in deltas. Works for: Qwen 2.5 on vLLM, Llama 3.1 on Together, DeepSeek-V3 on Fireworks, Mistral on Groq, Hermes-Pro on Ollama, anything via OpenRouter. **This is the default path.** 80% of users land here.

**Path B — Prompt-engineered tool use for models without native support.**
We inject tool definitions into the system prompt with a strict output protocol. Model emits `<tool_call name="…">{...JSON...}</tool_call>` or similar; we parse on the fly. Works for: vanilla Llama 3, Phi, Gemma, R1-Distill-Llama, any base model. **This is the fallback path.** It's slower (the model has to learn the protocol in-context) and slightly less reliable (~3-5% protocol violations on Llama 3 8B in our pilot tests; ~1% on 70B). We mitigate with grammar-constrained sampling on servers that support it (TGI grammar, vLLM guided_json, llama.cpp grammar).

**Path C — Grammar-constrained native.**
Some servers (vLLM, llama.cpp, TGI) support GBNF/JSON-schema grammar at the sampling layer. When available and the model isn't natively trained for tools, we *combine* Path B's prompt engineering with grammar to force-valid JSON. Higher reliability than Path B alone (~99.5% protocol adherence in pilots), modest latency overhead. Use when: model lacks native tool calling AND server supports grammar.

**Decision logic** (`ToolUseStrategy.resolve`):

```
if model.supports_native_tools and backend.supports_native_tools:
    return Path.A  # native
elif backend.supports_grammar:
    return Path.C  # prompt + grammar
else:
    return Path.B  # prompt only
```

A `ModelCapability` registry holds the per-model knowledge. Backend capabilities are detected at adapter init (heuristic: probe `/v1/models` and known endpoint signatures; cache for the connection lifetime).

### 2.3 The prompt-engineered tool protocol (Path B / C)

This is the single most important design decision because it determines reliability for the long tail of OSS models. We adopt a hybrid of the Anthropic format and Hermes-Pro's syntax (the OSS community's de facto standard):

**System prompt injection** (templated, ~400 tokens for 8 tools):

```
You have access to the following tools. To call a tool, emit a single
<tool_call> block in your response. You can call multiple tools in one
response by emitting multiple <tool_call> blocks back-to-back.

<tool_call>
{"name": "<tool_name>", "arguments": {<JSON object>}}
</tool_call>

Available tools:
{tool_definitions_as_json_array}

When you receive <tool_result> messages, continue your response using
the new information. If you have completed the user's request, respond
without any <tool_call> blocks.
```

**Parser** (streaming, two-state machine):
- State 0: `IN_TEXT` — accumulate text deltas as content
- State 1: `IN_TOOL_CALL` — accumulate JSON between `<tool_call>` and `</tool_call>`

The parser runs on the token stream from the model. When it detects `<tool_call>` opening tag it flips state, accumulates JSON, and emits a `ToolUseBlock` on `</tool_call>`. The text before/after stays as `TextBlock`s. **This is the same normalized event stream the rest of the agent loop expects** — Path A and Path B emit identical normalized events.

**Why this format over alternatives:**
- ReAct (`Thought: … Action: … Action Input: …`) — fragile to multi-tool; bad for streaming
- JSON-only output mode — breaks streaming text + tool interleaving
- Function-call syntax (Python-like) — harder to parse robustly
- Hermes-Pro XML — proven on hundreds of millions of OSS agent calls

### 2.4 Tool result encoding back to the model

For Path A, tool results go in the `tool` role message with `tool_call_id` (OpenAI spec). For Path B/C, tool results go as a user message with `<tool_result tool_call_id="…">…</tool_result>` blocks. The chat template engine handles either case; the agent loop is unaware.

---

## 3. Architecture

### 3.1 The picture

```
                              ┌──────────────────────────────────────┐
                              │             Agent.run / .stream      │
                              │ multi-turn loop, sub-agent dispatch  │
                              └─────────────────┬────────────────────┘
                                                │
        ┌───────────────────┬───────────────────┼────────────────────┬─────────────────────┐
        ▼                   ▼                   ▼                    ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐    ┌───────────────┐    ┌────────────────┐
│ StreamingTool │   │ ToolUseDeriver│   │ Permissions + │    │   MCP Client  │    │  Compactor     │
│   Executor    │   │ (Path A/B/C)  │   │     Hooks     │    │ (stdio/sse/   │    │ (auto+reactive)│
│               │   │               │   │               │    │  http/in-proc)│    │                │
└───────┬───────┘   └───────┬───────┘   └───────────────┘    └───────┬───────┘    └────────────────┘
        │                   │                                        │
        └─────────┬─────────┘                                        │
                  ▼                                                  │
        ┌──────────────────────────────────────────────────────────┐ │
        │                       Provider                           │ │
        │   (one adapter per backend protocol family)              │◀┘
        ├────────────────┬────────────────┬─────────────────────────┤
        │ OpenAI-Compat  │ Ollama Native  │ Raw Completion          │
        │  vLLM, llama   │ /api/chat,     │ llama.cpp /completion,  │
        │  .cpp server   │ /api/generate  │ TGI /generate, custom   │
        │  (Jinja),      │                │                         │
        │  Together,     │                │                         │
        │  Fireworks,    │                │                         │
        │  Groq,         │                │                         │
        │  OpenRouter,   │                │                         │
        │  Cerebras,     │                │                         │
        │  DeepInfra     │                │                         │
        └────────────────┴────────────────┴─────────────────────────┘
                  │                │                  │
                  ▼                ▼                  ▼
          ┌──────────────────────────────────────────────┐
          │      ChatTemplate engine (server-side or    │
          │      client-side, per backend)               │
          ├──────────────────────────────────────────────┤
          │  Jinja templates from HF tokenizer config    │
          │  for: Llama 3.x, Qwen 2.5, DeepSeek-V3,      │
          │  Mixtral, Mistral, Gemma, Phi, ChatML, ...   │
          └──────────────────────────────────────────────┘
```

### 3.2 Module layout

```
mantis_agent/
  __init__.py                public API surface
  types.py                   universal Message, ContentBlock, Usage (msgspec)
  events.py                  normalized StreamEvent variants
  errors.py                  typed exceptions
  http.py                    shared httpx client + SSE parser
  capabilities.py            ModelCapability registry, BackendCapability probing
  budget.py                  token + USD budget tracking, per-model pricing
  agent.py                   Agent — the loop, multi-turn
  session.py                 SessionStore protocol + SQLite + InMemory + fork/resume
  hooks.py                   Hook events (28 types), HookContext, dispatch
  permissions.py             PermissionMode, rules, canUseTool callback
  tools.py                   @tool, ToolRegistry, ToolUseDeriver
  compact.py                 Compactor protocol + SimpleCompactor + boundary marker
  templates/
    base.py                  ChatTemplate protocol
    jinja.py                 Jinja-based loader (reads HF tokenizer_config.json)
    bundled/                 bundled tokenizer_config.json for top 20 OSS models
  streaming/
    executor.py              StreamingToolExecutor — start tools mid-stream
    sse.py                   SSE line parser (already in http.py, refactor here)
    text_tool_parser.py      Path B/C streaming parser (state machine for <tool_call>)
    thinking_parser.py       inline <think> tag splitter
  providers/
    base.py                  Provider protocol + lazy registry + capability probe
    openai_compat.py         vLLM, Together, Fireworks, Groq, OpenRouter, etc.
    ollama.py                native /api/chat + /api/generate (slightly different shape)
    llamacpp.py              raw /completion endpoint (Jinja applied client-side)
    tgi.py                   HF Text Generation Inference + grammar
    modal.py                 Modal-hosted inference (convenience)
  mcp/
    types.py                 ServerConfig union (stdio/sse/http/sdk/proxy)
    client.py                MCP protocol client
    transports/
      stdio.py
      sse.py
      http.py
      in_process.py
    elicitation.py           handle JSON-RPC -32042 mid-tool URL prompts
  subagent.py                spawn + manage sub-agents (asyncio task or subprocess)
  skills.py                  Skill registry, prefetch, search
  cli.py                     `mantis-agent` CLI (chat, run, eval-tools, list-models)
  examples/
    quickstart.py
    ollama_local.py
    vllm_self_hosted.py
    fireworks_hosted.py
    mcp_filesystem.py
    multi_agent_research.py
  tests/
    recorded/                vcr-style JSON fixtures per (model, backend, scenario)
    test_*.py
```

### 3.3 Why this layout

- `streaming/` is its own folder because three different streaming surfaces converge there (SSE bytes, tool-call text parsing, thinking-tag parsing). Keep them together.
- `providers/` are backend protocols, not models. One adapter handles N models that speak the same wire format.
- `templates/bundled/` ships HF `tokenizer_config.json` for the top 20 OSS models so we don't have to hit huggingface.co at startup. ~50 KB total.
- `mcp/` mirrors upstream's module layout to ease porting.
- `capabilities.py` is the brain — it knows that "Qwen2.5-72B-Instruct on vLLM supports native tools and grammar but not thinking" so the agent picks Path A and skips thinking parsing.

---

## 4. Core abstractions

### 4.1 ModelCapability

The single source of truth about what a (model, backend) pair can do. Hand-maintained as a JSON in `capabilities.py` for the top ~40 OSS models we explicitly support, plus heuristic fallback (string-match the model id).

```python
@dataclass(frozen=True, slots=True)
class ModelCapability:
    name: str                          # "qwen2.5-72b-instruct"
    family: str                        # "qwen2.5"
    supports_native_tools: bool        # via OpenAI-compat tools[]
    supports_grammar: bool             # GBNF / guided_json / structured outputs
    emits_thinking_blocks: bool        # out-of-band thinking field
    emits_inline_thinking: bool        # <think>...</think> in content
    context_window: int                # tokens
    max_output_tokens: int
    chat_template_id: str              # key into templates/bundled/
    recommended_temperature: float
    family_specific_stops: tuple[str, ...]  # e.g. ("<|im_end|>",)
```

Capability lookup is O(1) at agent init and the result is frozen onto the Agent instance.

### 4.2 BackendCapability

Probed at adapter init by hitting `/v1/models`, `/api/version` (Ollama), etc. and inspecting response shape. Cached for the connection lifetime.

```python
@dataclass(frozen=True, slots=True)
class BackendCapability:
    kind: Literal["openai_compat", "ollama", "llamacpp", "tgi", "modal"]
    supports_native_tools: bool
    supports_grammar: bool             # guided_json / GBNF
    supports_logprobs: bool
    supports_prefix_caching: bool      # vLLM/llama.cpp
    supports_streaming: bool           # always True in practice, but explicit
    max_concurrent_requests: int       # heuristic; configurable
```

### 4.3 Universal message + event types

Same shape as v1 plan — `msgspec.Struct` tagged unions. **Unchanged.** This was the right call and the upstream comparison confirmed it. Tagged unions for `ContentBlock`, `Message`, and `StreamEvent`.

### 4.4 Tool model — revised

Upstream has `tool.isConcurrencySafe(input) -> bool`. v2 plan adopts this:

```python
@dataclass(slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn
    is_concurrency_safe: Callable[[dict], bool] | bool = True
    abort_siblings_on_error: bool = False
    is_read_only: bool = False         # informs auto-permission rules
    timeout_s: float | None = None
```

### 4.5 Hooks — adopt upstream's 28-event vocabulary

```python
HOOK_EVENTS = (
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "Notification", "UserPromptSubmit",
    "SessionStart", "SessionEnd",
    "Stop", "StopFailure",
    "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact",
    "PermissionRequest", "PermissionDenied",
    "Setup",
    "TaskCreated", "TaskCompleted",
    "Elicitation", "ElicitationResult",
    "ConfigChange",
    "FileChanged",  "CwdChanged",
    "InstructionsLoaded",
    "WorktreeCreate", "WorktreeRemove",
    "TeammateIdle",
)
```

Most users will only ever register `PreToolUse` + `PostToolUse` + `Stop`. The rest are there so integration projects can hook in without us shipping breaking changes.

```python
class Hooks(msgspec.Struct, omit_defaults=True):
    pre_tool_use: HookFn | None = None
    post_tool_use: HookFn | None = None
    stop: HookFn | None = None
    # ... others optional
```

### 4.6 Permissions — adopt upstream's model

`canUseTool(tool, input, ctx) -> Allow | Deny(reason) | Ask`. Modes: `default | auto | bypass`. Rules: `{allow, deny, ask}` by source `(user, project, local)`. Identical to upstream because integrators already understand it.

### 4.7 Compactor

```python
class Compactor(Protocol):
    async def should_compact(self, messages: list[Message], usage: Usage,
                              ctx_window: int) -> bool: ...
    async def compact(self, messages: list[Message]) -> list[Message]: ...
```

`SimpleCompactor` ships in v0: when total tokens > 85% of context window, summarize the oldest N messages with the same model (or a cheap fallback model) and insert a `SDKCompactBoundaryMessage`. Plugin point so users can ship `MapReduceCompactor` etc. later.

### 4.8 Budget

Token + USD budget tracker. Per-model pricing JSON in `pricing/`. Open-source models priced for: Together, Fireworks, Groq, DeepInfra, Cerebras, OpenRouter, plus zero-cost when serving locally (Ollama, vLLM, llama.cpp).

```python
class Budget:
    max_turns: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_usd: float | None
    fallback_model: str | None
```

When a budget is exceeded the agent raises `BudgetExceededError` *after* finalizing the in-flight tool call (so we never strand a subprocess).

### 4.9 Sessions

Same as upstream: persisted to a backend by a `SessionStore` protocol. Ships with:
- `InMemorySessionStore` — dev/test.
- `SqliteSessionStore` — single-file, durable, ~5 KLOC of SQL incl. fork/resume.

`Redis`, `Postgres`, custom stores left as user impls.

### 4.10 Sub-agents

Three isolation modes:
1. `asyncio_task` (default) — cheap, shared event loop, shared HTTP client pool.
2. `subprocess` — hard isolation, separate Python process. ~80 ms spawn tax.
3. `remote` — submit to another machine via the SDK's own protocol (used in distributed deployments).

Sub-agents inherit `Hooks`, `Permissions`, `SessionStore`, and `Compactor` from the parent unless overridden. They get a child `Budget` (default 50% of parent's remaining).

---

## 5. Streaming tool execution — the speed unlock

This is the single most important runtime optimization, from the upstream comparison.

**The naive loop** (v1 plan, what we shipped):

```
1. Send messages → stream events from model
2. Consume the full event stream, assemble AssistantMessage
3. Extract tool_use blocks
4. Dispatch in parallel
5. Wait for all
6. Append results, loop
```

**The streaming loop** (v2 plan):

```
1. Send messages → stream events from model
2. For each event:
     - text/thinking delta → emit upward
     - content_block_stop on a tool_use → kick off dispatch task NOW
3. On message_stop:
     - Wait for any in-flight dispatch tasks
     - Assemble tool_result list in original order
4. Append assistant + tool_result messages, loop
```

Why this matters: on a turn where the assistant streams 3 tool calls and each tool takes 4 s, the naive loop waits 12 s after the message finalizes. The streaming loop starts tool #1 the moment its JSON closes — if all three tools start within 1 s of each other in the stream, total tool time is ~4 s. **3× speedup on a typical multi-tool turn.**

Concurrency safety is checked per-input via `Tool.is_concurrency_safe(input)`. Default cap from `MANTIS_AGENT_MAX_TOOL_CONCURRENCY=10`. Sibling-abort: when one tool errors and has `abort_siblings_on_error=True` (default for `bash`-like tools), a per-batch `anyio.CancelScope` cancels in-flight siblings so subprocesses die fast.

---

## 6. Thinking — universalization

Three observed patterns in OSS models:

1. **No thinking.** Llama 3, Mistral, Mixtral, Qwen 2.5 non-reasoning, Gemma. Pass through; agent loop sees no thinking blocks.
2. **Inline `<think>…</think>` tags.** DeepSeek-R1, R1-Distill-*, QwQ, Marco-o1, OpenThinker. The model emits thinking as part of the content stream. We run a streaming parser that splits this into `ThinkingBlock`s and `TextBlock`s.
3. **Out-of-band thinking field.** DeepSeek API in some modes; future R2-class models. Server emits thinking in a separate stream field. Pass directly to `ThinkingBlock`.

The `ThinkingParser` runs after the SSE/text-delta layer but before the tool-call parser. State machine: `OUTSIDE` ↔ `INSIDE`. Tokens inside `<think>` flow to `ThinkingBlock`; tokens outside flow to text/tool parsing.

The parser is *capability-gated* — if `ModelCapability.emits_inline_thinking == False`, we don't run the parser at all (zero cost). False positives on `<think>` tokens in regular text are vanishingly rare for the affected models because they're trained to use it.

---

## 7. Backends — the seven adapters

### 7.1 OpenAIcompat (the default)

Single adapter covers vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras, DeepInfra, Anyscale, Lepton, Lambda, Mistral's own API, DeepSeek's own API, any future provider that speaks `POST /v1/chat/completions`.

- Streaming: `stream: true` → SSE chunks with `delta.content`, `delta.tool_calls`, `finish_reason`
- Tool use: native via `tools=[…]` + `tool_choice`
- Grammar: vLLM supports `guided_json`; Together / Fireworks support `response_format`; we feature-detect and fall through
- Auth: `Authorization: Bearer <key>` from env or constructor

### 7.2 Ollama

Ollama has a near-OpenAI-compatible endpoint (`/v1/chat/completions`) plus its own native (`/api/chat` and `/api/generate`). Native gives us more control (raw mode, system field, prefix caching hints, etc.). Default to native; fall back to OpenAI-compat if version probe fails.

- Tools: native since Ollama 0.3 (`tools` array in `/api/chat`)
- Streaming: NDJSON (newline-delimited), not SSE — different parser
- Local default: `http://localhost:11434`

### 7.3 llama.cpp server

llama.cpp's HTTP server is closer to raw completion + Jinja template application server-side via `--jinja`. We support both modes:

- OpenAI-compat path: `--jinja` enabled, route to OpenAIcompat adapter with `base_url = http://host:8080/v1`
- Raw completion path: `/completion` endpoint. We apply the chat template *client-side* via the `templates/jinja.py` engine and post the rendered string. Tool calls parsed via Path B/C.

### 7.4 TGI

HF Text Generation Inference. OpenAI-compat surface, plus native `/generate` with grammar support. Use for grammar-constrained Path C on TGI deployments.

### 7.5 Modal-hosted inference

Convenience adapter for users serving via Modal. Wraps `Function.invoke()` with our normalized event protocol. Useful for fan-out workflows.

### 7.6 Raw HTTP custom

Escape hatch: user passes `request_fn` + `parse_event_fn` and we plug it into the event stream. For exotic deployments (Replicate webhooks, custom inference gateways).

### 7.7 Mock

For tests + offline development. Replays recorded SSE fixtures from `tests/recorded/`.

---

## 8. Model capability matrix (top 30 OSS models we explicitly support at GA)

| Model | Family | Ctx | Native tools | Grammar (vLLM) | Thinking | Notes |
|---|---|---|---|---|---|---|
| Llama 3.3 70B Instruct | llama3 | 128K | yes | yes | – | Default open recommendation |
| Llama 3.1 70B/8B Instruct | llama3 | 128K | yes | yes | – | |
| Llama 3 70B/8B Instruct | llama3 | 8K | no | yes | – | Path C; small ctx |
| Qwen 2.5 72B/32B/14B/7B Instruct | qwen2.5 | 128K | yes | yes | – | Strongest open tool use |
| Qwen 2.5 Coder 32B | qwen2.5 | 128K | yes | yes | – | |
| QwQ 32B Preview | qwen2.5 | 32K | partial | yes | inline | Reasoning model |
| DeepSeek-V3 | deepseek | 64K | yes | yes | – | Cheapest in class |
| DeepSeek-R1 | deepseek | 64K | partial | yes | inline+oob | |
| DeepSeek-R1-Distill-Llama-70B | llama3 | 128K | no | yes | inline | Path C+thinking |
| DeepSeek-R1-Distill-Qwen-32B | qwen2.5 | 128K | no | yes | inline | |
| Mixtral 8x22B Instruct | mistral | 64K | yes | yes | – | |
| Mixtral 8x7B Instruct | mistral | 32K | yes | yes | – | |
| Mistral Large 2 | mistral | 128K | yes | yes | – | |
| Mistral Nemo 12B | mistral | 128K | yes | yes | – | |
| Hermes 3 70B / 8B | llama3 | 128K | yes | yes | – | OSS community favorite |
| Hermes-Pro Llama 3.1 8B | llama3 | 128K | yes | yes | – | Function-calling specialist |
| Functionary-V3.2 | llama3 | 8K | yes | yes | – | |
| Command R+ | cohere | 128K | yes | yes | – | Native tool format |
| Phi-4 14B | phi | 16K | no | yes | – | Path C |
| Gemma 2 27B/9B/2B | gemma | 8K | no | yes | – | Path C; small ctx |
| Yi-Large | yi | 32K | yes | yes | – | |
| InternLM 2.5 20B | internlm | 32K | yes | yes | – | |
| Marco-o1 | qwen2.5 | 32K | partial | yes | inline | |
| OpenThinker-32B | qwen2.5 | 32K | partial | yes | inline | |
| Granite 3.1 8B | granite | 128K | yes | yes | – | IBM open |
| Aya Expanse 32B | cohere | 128K | yes | yes | – | Multilingual |
| OLMo 2 13B | olmo | 4K | no | yes | – | Path C |
| Falcon 180B | falcon | 8K | no | yes | – | Path C |
| StableLM 12B | stablelm | 16K | no | yes | – | Path C |
| SmolLM 3 1.7B | smollm | 128K | partial | yes | – | Edge-class |

"partial" = native tools sometimes supported via specific server build. We feature-detect.

For models not in this table, we fall back to a heuristic: detect family from `model_id` prefix; default capabilities = `{native_tools: False, grammar: True, thinking: False}` (Path C). Users can override with `Agent(capability_override=…)`.

---

## 9. Performance commitments

Same eight from v1 plan, all still binding. Plus four new:

9. **`<think>` parser is gated.** Zero cost when `emits_inline_thinking=False`.
10. **Tool-call text parser is gated.** Zero cost when `supports_native_tools=True` AND backend confirms native path was taken.
11. **Chat template rendering happens server-side when possible.** Client-side Jinja only fires for raw-completion endpoints.
12. **Capability lookup is O(1) and frozen onto the Agent at init.** No per-turn lookups.

Plus benchmarking targets we promise to hit at GA:

- Token-to-event latency overhead: **< 200 µs** per event versus a hand-written httpx + json-loads loop (measured on Qwen 2.5 7B on local vLLM).
- Memory: **< 50 MB resident** for an idle Agent + 5 tools, baseline Python.
- Tool dispatch latency: tool call kicked off **< 5 ms** after the closing `</tool_call>` token in Path B/C; **< 1 ms** after `tool_call.id` finalizes in Path A.
- Cold-start: `import mantis_agent` to `Agent` constructed and one request sent in **< 250 ms** (lazy provider imports do their job).

These are part of CI. Regressions fail the build.

---

## 10. Public API surface

### 10.1 The primary API — `Agent`

```python
from mantis_agent import Agent, UserMessage, tool

@tool
async def search_web(query: str) -> str: ...

agent = Agent(
    model="qwen2.5-72b-instruct",
    backend="https://api.together.xyz/v1",        # OpenAI-compat URL or env-detected
    tools=[search_web],
    system="You are a research assistant.",
    max_turns=10,
    max_usd=0.50,
)
messages = await agent.run([UserMessage(content="What is Spawn Labs?")])
```

### 10.2 The drop-in API — `query()`

For users porting from Claude Agent SDK without rewriting:

```python
from mantis_agent import query

async for msg in query(prompt="...", options={"model": "qwen2.5-72b-instruct", "tools": [...]}):
    print(msg)
```

`query` is a thin generator wrapper that constructs an Agent, runs `stream`, and yields normalized SDKMessages identical in shape to upstream's `SDKMessage`.

### 10.3 V2 sessions API (alpha)

```python
session = create_session(SessionConfig(model="qwen2.5-72b", store=SqliteSessionStore("./agent.db")))
async for msg in session.prompt("hello"):
    ...
forked = fork_session(session.id, store=...)
```

### 10.4 MCP server creation

Identical to upstream — accept `@tool` decorated functions or a list of `Tool` objects:

```python
from mantis_agent.mcp import create_sdk_server
server = create_sdk_server(name="my-tools", tools=[search_web, get_weather])
```

### 10.5 Remote control + scheduled tasks

Mirror upstream's `connect_remote_control(...)` and `watch_scheduled_tasks(...)` for parity with their public surface. v0 = stubs; M3 = implementations.

---

## 11. Milestones

### M0 — Skeleton (DONE, on `main`)
Already shipped: types, events, http, anthropic adapter (**will be deleted in M0.1**), agent loop (**will be rewritten**), tools registry, basic dispatcher.

### M0.1 — Pivot + hot-path (week 1)
- **Delete** `providers/anthropic.py` (out of scope per §1.3).
- Add `capabilities.py` + bundled `ModelCapability` table for top 30 OSS models.
- Add `streaming/executor.py` (streaming tool dispatch).
- Add `streaming/text_tool_parser.py` (Path B/C state machine).
- Add `streaming/thinking_parser.py` (`<think>` splitter).
- Refactor `tools.py`: `Tool.is_concurrency_safe: Callable[[dict], bool] | bool`, env-configurable cap, per-batch CancelScope for sibling abort.
- Update `agent.py` to drive `StreamingToolExecutor` instead of the post-message dispatcher.

### M0.2 — OpenAI-compat adapter (week 2)
- Implement `providers/openai_compat.py` with full streaming + native tool calls + grammar feature detect.
- Test against vLLM (Qwen 2.5 7B local), Together (Llama 3.3 70B), Fireworks (DeepSeek-V3), Groq (Llama 3.3 70B), OpenRouter (mixed).
- Recorded fixtures for each backend in `tests/recorded/openai_compat/`.

### M1 — Backend coverage (week 3)
- `providers/ollama.py` (native NDJSON parser, default to localhost:11434).
- `providers/llamacpp.py` (both Jinja-on and raw completion modes).
- `providers/tgi.py` with grammar.
- `providers/modal.py`.
- Bundled chat templates for top 30 models.

### M2 — Safety surface (week 4)
- `hooks.py` with all 28 events; v0 active set = PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop.
- `permissions.py` (modes + rules).
- `budget.py` (tokens + USD + per-model pricing JSON for hosted backends; zero-cost for local).
- Fallback model on `BudgetExceededError`.

### M3 — MCP (week 5)
- `mcp/types.py` (server config union) — pre-scaffold so already imported.
- `mcp/client.py`.
- All four transports: stdio, sse, http, in-process.
- Elicitation handler.
- Integration with `Tool` registry so MCP tools and local `@tool` tools dispatch identically.

### M4 — Multi-agent + sessions (week 6)
- `subagent.py` (asyncio_task / subprocess / remote isolation modes).
- `session.py` (InMemorySessionStore, SqliteSessionStore, fork, resume).
- `query()` drop-in wrapper.

### M5 — Compaction + skills + polish (week 7)
- `compact.py` with SimpleCompactor.
- `skills.py` with prefetch + search.
- CLI (`mantis-agent`).
- Docs site at `mantis-agent-sdk.dev`.
- Benchmark suite published.

### M6 — GA polish (week 8)
- Production-ready, semver 1.0, PyPI release.
- Migration guides from Claude Agent SDK, LangGraph, smolagents.
- Sample agents on top of each: code agent, research agent, customer-support agent.

---

## 12. Distribution + ecosystem

### 12.1 PyPI
- `mantis-agent-sdk` — core
- `mantis-agent-sdk[ollama]`, `[vllm]`, `[mcp]`, `[all]` — extras for optional deps

### 12.2 npm equivalent
Out of scope v1.0. Likely a future port driven by demand.

### 12.3 Examples repo
`mantis-agent-sdk-examples` — separate repo with 10+ working agents (code, research, support, etc.) so people can copy-paste and edit.

### 12.4 Compatibility shims
- `claude-agent-sdk-shim` — provides `from claude_agent_sdk import query` that wraps ours.
- `openai-agents-shim` — same for OpenAI Agents SDK signatures.

### 12.5 Benchmark + leaderboard
Publish a public benchmark of (model × backend) tool-use pass rates measured against a fixed 100-task suite. Updates weekly. Becomes the canonical reference for "what works."

---

## 13. Open decisions

| # | Decision | Recommendation | Status |
|---|---|---|---|
| 1 | License | Apache-2.0 | locked |
| 2 | Repo home | `teddyoweh/mantis-agent-sdk` | locked |
| 3 | Telemetry | Opt-in, clearly disclosed | pending |
| 4 | Drop Anthropic adapter? | YES — out of scope per §1.3 | pending (this plan recommends it) |
| 5 | Drop OpenAI/Gemini/Bedrock from v1 plan? | YES — out of scope | pending |
| 6 | Schema source: type hints vs. zod-like | Type hints by default, `input_schema=` override | locked |
| 7 | API shape | Both `Agent` class + `query()` wrapper | locked |
| 8 | Pre-scaffold MCP types in M0 | YES | locked |
| 9 | Default backend if none specified | Ollama at `localhost:11434` if reachable, else error | pending |
| 10 | Bundled tokenizer configs vs. fetched | Bundled (~50 KB total) for top 30 | locked |
| 11 | Prompt-engineered tool protocol | Hermes-Pro-derived `<tool_call>` XML | locked |
| 12 | Reasoning-model thinking handling | Stream as `ThinkingBlock`, opt out via flag | locked |
| 13 | Sub-agent default isolation | asyncio_task | locked |
| 14 | `is_concurrency_safe` default | True (read-only assumption); flagged tools opt out | locked |

---

## 14. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| OSS model tool-use reliability is too low for production | M | H | Ship a measured benchmark; recommend specific (model, backend) pairs at GA; provide Path C (grammar) as fallback for unreliable models |
| Server format fragmentation worsens as new providers appear | H | M | Adapter pattern absorbs new backends in <300 LOC each; bake in OpenAI-compat as default; maintain compatibility shims |
| MCP spec evolves and breaks our client | M | M | Pin to a specific MCP spec version; bump deliberately; copy upstream's MCP normalization layer |
| Capabilities table drifts from reality as models update | H | L | Capability table is a JSON the community can PR; CI runs a weekly probe against top providers and flags drift |
| Performance regressions creep in | M | H | Benchmark CI gates on (latency, memory) for top 5 (model, backend) pairs |
| Streaming tool parser misfires on unusual model outputs | M | M | Heavy fuzz suite in tests/; gracefully fall back to "no tool call detected, continue" |
| Users expect ChatGPT-style hosted experience | L | M | Loud README "this is local-first, byo-server" |

---

## 15. What we are NOT planning (yet)

- **Vision models / multimodal.** Add in 1.x — same agent loop, new ContentBlock variants. Llava, Qwen-VL, Pixtral, etc. Deferred because tool-use is the more urgent gap.
- **Voice / audio.** No.
- **Fine-tuning / RLHF.** Different product.
- **A hosted product.** No.
- **Reactive UI / TUI.** No — we ship a library. The CLI is just for diagnostics.
- **Workflows / DSL.** No — agents have prompts and tools. If you want a workflow DSL, use Inngest / Temporal / Airflow and call us from a step.

---

## 16. The acceptance test

We are done when this sentence is true:

> A new user can `pip install mantis-agent-sdk[ollama]`, `ollama pull qwen2.5:72b`, and run a 10-line script that does a 5-turn agent task with two tools — and it Just Works on the first try.

Then we add Together, Fireworks, vLLM, llama.cpp until that sentence holds across the entire model + backend matrix.

That's the bar.
