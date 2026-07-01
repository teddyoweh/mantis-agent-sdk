# Upstream comparison — Claude Code source vs. our v0

**Read date:** 2026-05-16. Source: vendored copy of the Claude Code TS source
(`~1,902` files). The public SDK surface is in `src/entrypoints/sdk/`; the
runtime engine is `src/QueryEngine.ts` + `src/query.ts` + `src/Tool.ts`
+ `src/services/tools/*` + `src/services/mcp/*`.

The honest framing: I designed v0 against the *public API shape* of the
Claude Agent SDK, not the actual source. Reading the source raises the bar.
Below is what I missed, what's correctly assumed, and what we should change
before M1.

## What the upstream actually is

A drop-in analog is not just "Anthropic client + tool loop." The upstream
engine carries a *production agent runtime*:

| Subsystem | What it does | Files |
|---|---|---|
| QueryEngine | Owns conversation state across turns. Constructs the system prompt from many parts, calls `query`, accumulates usage, handles compaction triggers, runs hooks. | `QueryEngine.ts` (1,295 LOC) |
| `query` loop | The hot path. Calls the model, streams events, drives the streaming tool executor, handles auto-compact + reactive compact + snip + context-collapse, retries on fallback. | `query.ts` (1,729 LOC) |
| StreamingToolExecutor | Starts tool calls **as they arrive in the assistant stream**, not after the assistant message finalizes. Concurrent-safe tools run in parallel up to a cap; non-safe tools serialize. | `services/tools/StreamingToolExecutor.ts` (530 LOC) |
| toolOrchestration | Partitions a tool batch by `tool.isConcurrencySafe(parsedInput)` and runs each partition. Concurrency cap default 10 (env). | `services/tools/toolOrchestration.ts` |
| toolExecution | Runs one tool: parse input, validate, ask permissions, execute, capture progress, format result. ~1,745 LOC because real tools generate progress messages mid-execution. | `services/tools/toolExecution.ts` |
| Permissions | `canUseTool` callback + 28 hook events (PreToolUse, PostToolUse, Stop, SubagentStart, …). Allow/deny/ask rules per source (user/project/local). | `Tool.ts`, `hooks/useCanUseTool.ts`, `services/tools/toolHooks.ts` |
| MCP client | stdio + sse + http + in-process ("sdk") + claudeai-proxy transports. Server discovery, auth, OAuth port routing, elicitation (URL params mid-tool via JSON-RPC error -32042), channel allowlist, vscode SDK bridge. | `services/mcp/*` (~24 files) |
| Compaction | Auto-compact when budget low, reactive compact, snip compaction, context-collapse. Multiple strategies behind feature flags. | `services/compact/*` |
| Skills | Separate from tools. `searchHint`, `alwaysLoad`, skill prefetch in query loop. | `services/SkillSearch/*`, `tools/SkillTool/*` |
| Memory | The MEMORY.md tree system — separate "memdir" subsystem with auto-loading + nested paths. | `memdir/*` |
| Sessions | Persistence to disk, sessionId tracking, fork/resume, replay, message-queue manager. | `bootstrap/state.ts`, `utils/sessionStorage.ts`, V2 session API |
| Cost tracking | Per-model pricing table, usage accumulation, `costUSD` reported in events. | `cost-tracker.ts`, `services/api/logging.ts` |
| File state cache | Tracks file mtimes/contents to detect external edits between turns. | `utils/fileStateCache.ts`, `utils/fileHistory.ts` |
| Thinking config | adaptive (Opus 4.6+) / enabled with budgetTokens / disabled | `utils/thinking.ts` |
| Output enforcement | JSON-schema-constrained output via `SYNTHETIC_OUTPUT_TOOL_NAME`. | `tools/SyntheticOutputTool/*` |
| Hooks | 28 events; each can mutate the request or block it. | `utils/hooks.ts`, `services/tools/toolHooks.ts` |
| Agent definitions | Filesystem-loaded sub-agent definitions (`AgentDefinition`), spawned via `AgentTool`. | `tools/AgentTool/*` |

Public SDK surface (`entrypoints/agentSdkTypes.ts`) is small by comparison:

```ts
function tool(name, description, zodSchema, handler, extras?) -> SdkMcpToolDefinition
function createSdkMcpServer({name, version?, tools?}) -> McpSdkServerConfigWithInstance
function query({prompt, options}) -> Query   // async iterable of SDKMessage
class AbortError

// V2 (alpha)
unstable_v2_createSession(options) -> SDKSession
unstable_v2_resumeSession(id, options) -> SDKSession
unstable_v2_prompt(message, options) -> Promise<SDKResultMessage>

// Session ops
listSessions, getSessionInfo, getSessionMessages, renameSession, tagSession,
forkSession

// Remote control + scheduled tasks
connectRemoteControl, watchScheduledTasks, buildMissedTaskNotification
```

`SDKMessage` is a discriminated union — assistant chunks, user echoes,
tool-use, tool-result, system init, compact boundaries, status changes, permission denials.

## What I got right in v0

* **Universal Message + ContentBlock model with tagged unions.** Their `Message` is also a tagged union (`AssistantMessage | UserMessage | SystemMessage | ProgressMessage | AttachmentMessage | TombstoneMessage | ToolUseSummaryMessage | SystemLocalCommandMessage`). Same idea.
* **Normalized streaming event model that all providers fold into.** They don't multi-provider, but their `StreamEvent` shape is the same Anthropic SSE taxonomy I mirrored.
* **Provider adapter as a protocol; lazy registry.** Right call — they don't have to do this because they're Anthropic-only, but for a multi-provider SDK it's the correct abstraction.
* **Shared `httpx.AsyncClient` with HTTP/2.** Mirrors the long-lived `@anthropic-ai/sdk` client they hold open.
* **`@tool` decorator + schema auto-derivation.** They use `zod` schemas passed explicitly; I auto-derive from type hints. Theirs is more explicit, mine is more ergonomic. **I should support both** — explicit `input_schema=` override is already there, just need to document.
* **anyio + task group for parallel dispatch.** Right primitive. They use a generator merge helper (`all`) with concurrency limit; same thing different shape.

## What I got wrong or missed (rank-ordered)

### 1. Tool dispatch happens AFTER the assistant message finalizes. Wrong.

**Their model:** `StreamingToolExecutor` watches the assistant stream and kicks off each `tool_use` block as soon as it finishes streaming, *while later content is still arriving*. Concurrent-safe tools run in parallel up to a cap; non-safe ones serialize.

**My model:** I assemble the full `AssistantMessage`, then dispatch all tool calls in a `task_group`. That's a real latency tax on multi-tool turns — if a tool takes 5 s and the assistant streams 3 more tool calls after it, my version waits 5 s before starting any.

**Fix:** Move tool dispatch into the stream consumer. When a `ContentBlockStop` event fires for a `tool_use` block, kick the dispatch task immediately. The agent loop becomes: stream → for each tool_use block that closes, start it → after `message_stop`, await all and assemble results → next turn.

### 2. `parallel_safe: bool` is too coarse.

**Theirs:** `Tool.isConcurrencySafe(parsedInput) -> bool`. Function of the inputs. Two `bash` calls writing to different paths can run in parallel; two writing to the same path can't.

**Mine:** static `parallel_safe` per tool.

**Fix:** Change `Tool.parallel_safe` from `bool` to `Callable[[dict], bool] | bool` (bool stays for backwards-compat). Partition runs by walking the input list.

### 3. No concurrency cap.

**Theirs:** `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY=10` default, env-overridable.

**Mine:** `task_group.start_soon` for every call — unbounded.

**Fix:** Wrap in `anyio.Semaphore(cap)` or use `anyio.create_memory_object_stream` + a worker pool. Cap configurable per-Agent.

### 4. No sibling-abort.

**Theirs:** Each tool batch runs under a child `AbortController` rooted at the turn's. If one tool errors (esp. bash), siblings get aborted so subprocesses die immediately rather than running to completion.

**Mine:** Errors are caught per-task and turned into `ToolResultBlock(is_error=True)`; siblings keep running.

**Fix:** Open an `anyio.CancelScope` per concurrent batch; cancel on first error if a per-tool flag (`abort_siblings_on_error`) is set.

### 5. No permission / hook system.

**Theirs:** `canUseTool(tool, input, ctx, message, useID, force?)` is required on every tool call. Returns `PermissionResult` (allow / deny / ask). Hook events fire at 28 named points (PreToolUse, PostToolUse, Stop, SubagentStart/Stop, …) — each hook can mutate the request or block.

**Mine:** Tools just run. No gating, no hooks.

**Fix:** Add `Agent(can_use_tool=async_fn, hooks=Hooks(...))`. Start with a minimal hook set (PreToolUse, PostToolUse, Stop) and grow. The 28-event taxonomy in their `HOOK_EVENTS` const is a good steal — copy it verbatim.

### 6. No token budget / max-budget-usd / fallback model.

**Theirs:** `maxTurns`, `maxBudgetUsd`, `taskBudget.total`, `fallbackModel`. Auto-fallback when primary errors with `FallbackTriggeredError`.

**Mine:** `max_steps` only.

**Fix:** Add `max_budget_usd` + a per-model pricing table (Anthropic publishes them; ship a JSON). Track `total_cost` on the Agent; abort when exceeded. Add `fallback_model` knob and an error class to trigger it.

### 7. No thinking config.

**Theirs:** `{type: 'adaptive'} | {type: 'enabled', budgetTokens?} | {type: 'disabled'}`.

**Mine:** Nothing. Just `temperature`.

**Fix:** Add `thinking: ThinkingConfig`. Pass through to Anthropic adapter as `thinking` param; ignore on other providers.

### 8. No auto-compact.

**Theirs:** Multiple strategies — `autoCompact` (default), `reactiveCompact`, `snipCompact`, `contextCollapse` — all feature-gated. When token usage crosses a threshold, summarize older messages into a single `SDKCompactBoundaryMessage` and drop them.

**Mine:** None. Long sessions will OOM the context.

**Fix:** Ship `SimpleCompactor` for v0 — when total tokens > `context_window * 0.85`, summarize the oldest N messages with a cheap model. Boundary message in the history preserves audit trail. More strategies can plug in later.

### 9. MCP is not implemented.

**Theirs:** Four transports (stdio, sse, http, in-process "sdk"), OAuth flows, server registry, channel allowlist, elicitation, connection manager, vscode SDK bridge.

**Mine:** Nothing. My plan mentions M2 = MCP client.

**Fix:** Keep MCP at M2 but write the **types** now (server config union, transport enum) so the agent loop already handles MCP-provided tools alongside local ones. Otherwise the integration is going to require an invasive refactor.

### 10. Skills ≠ tools.

**Theirs:** Skills are *first-class* alongside tools and have a separate registry, `searchHint`, `alwaysLoad`, skill-prefetch in the query loop. The agent might "discover" a skill at turn start and inject it as additional context.

**Mine:** Skills don't exist.

**Fix:** Defer to M3. Note it in the plan so users know the data model accommodates it later.

### 11. Smaller misses

| Miss | Where | Fix priority |
|---|---|---|
| `webSearchRequests` in `Usage` | their `ModelUsageSchema` | low — keep our Usage minimal, add as needed |
| `costUSD` in usage | their schema | medium — emit from a pricing table |
| `contextWindow`/`maxOutputTokens` in usage | their schema | low |
| File state cache (detect external edits) | `utils/fileStateCache.ts` | low — agents that touch the FS need this |
| Permission modes (default/auto/bypass) | `Tool.ts` | medium — needed for any non-trivial deployment |
| `SystemPrompt` from parts (not a single string) | `utils/queryContext.ts` | low — string works for v0 |
| Cost tracker per-model | `cost-tracker.ts` | medium |
| Session forking | V2 SDK | low — defer |
| Output JSON-schema enforcement (synthetic tool) | `tools/SyntheticOutputTool` | low — defer |

## Revised milestones

Original M0 was "Anthropic-only single-turn done." That's still right but with three corrections **before** moving to M1:

**M0.1 — Hot-path fixes (this week)**
1. Streaming tool dispatch (kick off tool tasks at `content_block_stop`, not after `message_stop`).
2. `is_concurrency_safe: Callable[[input], bool]` + concurrency cap (default 10, env-overridable).
3. Sibling-abort via per-batch `CancelScope`.
4. Tests + a recorded SSE fixture (vcr-style).

**M0.2 — Safety surface**
5. `can_use_tool` callback wiring.
6. Hooks dataclass with the same 28 event names as upstream. v0 implements 3: `pre_tool_use`, `post_tool_use`, `stop`.
7. `max_budget_usd` + per-model pricing JSON.

**M1 — Multi-provider** (as before)

**M1.5 — Compaction & thinking**
8. `thinking: ThinkingConfig` knob.
9. `SimpleCompactor`.

**M2 — MCP + sub-agents** (as before)

**M3 — Production polish** + skills, output-schema enforcement, session fork.

## Decision asks

1. **Drop-in API shape.** Upstream's public surface is `query({prompt, options}) -> Query` (async iterable). Mine is `Agent(...).run(messages) -> messages`. The Agent class is cleaner for typed code; their `query()` is more declarative. **Recommendation:** ship both. `Agent` is the primary, `query()` is a thin wrapper for drop-in compatibility.
2. **Zod-equivalent schema.** Upstream uses `zod` for tool schemas (gives JSON schema for free + runtime validation). I'm using type-hint introspection. **Recommendation:** keep my approach as the default, add an explicit `input_schema=` override (already exists), document that users who want zod-style validation can pass a `msgspec.Struct` and we'll derive the schema from it (cleaner than adding a pydantic/zod dep).
3. **MCP types in v0 even though MCP itself ships M2.** I want to write the `McpServerConfig` union, the `Tool` registry to accept MCP-provided tools, and the transport enum *now* so the agent loop already polymorphic. Cost: a few hundred lines of typed scaffolding that does nothing. Benefit: M2 is purely additive instead of an invasive refactor. **Recommendation:** yes, do it.

## What I'm going to do next unless you say otherwise

1. Land the four M0.1 fixes on a branch and PR them.
2. Write the comparison this doc captures into a structured changelog.
3. Add MCP type scaffolding to `types.py` so the agent loop is already polymorphic.

If you want me to also wire OpenAI/Gemini *before* M0.1, say so — but I think the speed + safety hot-path fixes are more important than a second provider with a slow tool loop.
