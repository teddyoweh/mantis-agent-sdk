# Changelog

All notable changes to `mantis-agent-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and from 1.0.0 on the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The full versioning policy is in [SEMVER.md](SEMVER.md).

## [Unreleased]

### Added

- **Built-in tracing — span tree of every agent run, no required dependency.**
  New `Agent(tracer=...)` field accepts any object satisfying the new
  `Tracer` protocol. The agent loop emits four span kinds:
  - `agent.run` (one per `Agent.run()` / `run_iter()` call) carrying
    `agent.model`, `agent.turns`, and aggregate token / cost totals.
  - `agent.turn` (one per turn) with `turn.stop_reason`,
    `turn.input_tokens`, `turn.output_tokens`, `turn.tool_uses`.
  - `llm.call` (one per provider stream) with `llm.input_tokens`,
    `llm.output_tokens`, `llm.cache_read_tokens`,
    `llm.cache_creation_tokens`, `llm.stop_reason`, `llm.first_token_ms`.
  - `tool.call` (one per dispatched tool) with `tool.name`, `tool.id`,
    `tool.input.keys` (sorted KEYS only — never values, so PII can't
    leak into observability backends), `tool.is_error`,
    `tool.result.len`.

  Ships two implementations:
  - `InMemoryTracer` — zero-dep, records to a list; `tracer.tree()`
    reconstructs the span forest, `tracer.summary()` aggregates by
    span name, `tracer.write_jsonl(path)` exports to disk.
  - `OTelTracer` — lazy-imports `opentelemetry.trace`. Plugs into any
    existing OpenTelemetry pipeline (Datadog, Honeycomb, Tempo,
    Jaeger, ...). Raises a clear `ImportError` at construction if
    `opentelemetry-api` isn't installed.

  Span ids are 16-char hex (OTel SpanID width); trace ids are 32-char
  hex (OTel TraceID width) so exporters can use them verbatim. When
  `Agent.tracer is None` the loop pays zero overhead — every span call
  site is gated by a single `if tracer is None`. Tool-input keys are
  recorded but **values are never copied into spans** — opinionated
  privacy default that matches what production teams ship to SaaS
  observability backends. Four new public exports: `Tracer`, `Span`,
  `InMemoryTracer`, `OTelTracer`. Example:
  `python -m mantis_agent.examples.with_tracing`.

- **Structured output via `response_format`.** New `Agent(response_format=...)`
  and `ClaudeAgentOptions(response_format=...)` fields accept the OpenAI
  `response_format` shape — `{"type": "json_object"}` for free-form JSON, or
  `{"type": "json_schema", "json_schema": {"name": ..., "schema": ..., "strict": ...}}`
  for schema-constrained output. The agent layer translates per backend:
  OpenAI-compat / Modal / llama.cpp pass the envelope through verbatim,
  Ollama maps to its native top-level `format` field, TGI maps to
  `parameters.grammar`. `anthropic_passthrough` raises a loud
  `ResponseFormatError` (real Anthropic API has no `response_format`).
  Three new public exports: `ResponseFormatError`,
  `normalize_response_format`, `translate_response_format`.

## [1.1.4] — 2026-06-30

### Changed

- **Mascot reworked so it reads as a mantis, not a lizard.** The body now
  rears up steeply (instead of lying horizontal) and the raptorial forelegs
  are drawn bold and folded in front — the posture + arms are what
  distinguish a praying mantis from a generic green creature. Slimmer
  abdomen and thin legs.

## [1.1.3] — 2026-06-30

### Fixed

- **`mantis` now auto-selects an installed model instead of dying on a missing
  default.** On startup it probes the backend (Ollama `/api/tags`, else
  OpenAI-compat `/v1/models`); if the configured model isn't installed it picks
  the closest one that is (same base family → any chat model → first available)
  and notes the swap. When nothing is installed or the backend is unreachable
  it prints an actionable hint (`ollama serve` / `ollama pull <model>`). The
  per-turn "model not found" error now also suggests the exact pull command.

## [1.1.3] — 2026-06-30

### Changed

- **Mascot redrawn to match a real praying mantis.** Reared-up alert stance,
  facing right: abdomen low-left, prothorax rearing up to a triangular head
  with a compound eye and long antennae, raptorial forelegs folded in the
  "praying" pose, standing on bent legs. Smaller footprint (7 rows), with a
  pale highlight ridge and a paler folded forearm for depth.

## [1.1.2] — 2026-06-30

### Changed

- **Redrew the `mantis` mascot as a side-profile praying mantis.** The
  front-facing sprite read as a face; the new mascot is a pixel *bitmap*
  rasterized with half-blocks (2× vertical resolution, two-color cells) —
  triangular head with a compound eye, swept antennae, the raptorial
  forelegs folded in the "praying" pose, an arched body, and three legs.

## [1.1.1] — 2026-06-30

### Changed

- **`mantis` banner mascot is now a praying mantis.** Replaced the reused
  placeholder sprite with a purpose-drawn 5-row pixel praying mantis
  (antennae, triangular head, two compound eyes, folded raptorial forelegs).

## [1.1.0] — 2026-06-30

### Added

- **`mantis` — an interactive, Claude-Code-style agent terminal.** Run
  `mantis` in any directory for a banner (pixel mascot + version + model +
  cwd), a bordered input with a rotating `Try "…"` placeholder, a mode
  footer cycled with `shift+tab`, slash commands (`/help`, `/model`,
  `/clear`, `/cwd`, `/exit`), and token-level streaming from any configured
  backend. Configuration reads the standard `MANTIS_AGENT_MODEL`,
  `MANTIS_AGENT_BASE_URL`, and `MANTIS_AGENT_API_KEY` env vars.
  - New module `mantis_agent.tui` and a new `mantis` console entry point.
  - New `[cli]` optional extra (`prompt_toolkit`, `rich`) keeps the core
    SDK dependency-light; the stdlib-only `mantis-agent` CLI is unchanged.
    Install with `pip install 'mantis-agent-sdk[cli]'`.

## [1.0.0] — 2026-05-17

First stable release. The public API — the set of names in
`mantis_agent.__all__` — is now covered by the SemVer guarantee
documented in [SEMVER.md](SEMVER.md).

### Added

- **Locked public API surface.** `mantis_agent.__all__` is now the
  single source of truth for what is covered by SemVer. A new test
  (`tests/test_public_api_surface.py`) snapshots the set and fails on
  unintentional drift.
- **`__version__` from package metadata.** `mantis_agent.__version__`
  now reads from `importlib.metadata.version("mantis-agent-sdk")` when the
  package is installed, so it always tracks `pyproject.toml`.
- **`SEMVER.md`** — the versioning policy.
- **`RELEASING.md`** — the release runbook.
- **`.github/workflows/release.yml`** — tag-driven PyPI publish using
  trusted publishing (OIDC). No long-lived API token required.
- **`.github/workflows/test.yml`** — CI matrix on Python 3.11, 3.12, 3.13.
- Expanded `__all__` to include every Claude SDK parity symbol that was
  previously imported at the top of `mantis_agent` but only
  conventionally public: `ClaudeAgentOptions`, `ClaudeSDKClient`,
  `ClaudeSDKError`, `CLIConnectionError`, `AgentDefinition`,
  `HookMatcher`, `HookInput`, `HookJSONOutput`, `ClaudeHookContext`,
  `ClaudePermissionResult`, `PermissionResultAllow`,
  `PermissionResultDeny`, `ToolPermissionContext`, `ResultMessage`,
  `IsolationMode`, `create_sdk_mcp_server`.
- PyPI metadata: `readme`, `authors`, `keywords`, `classifiers`,
  `project.urls`, and explicit hatchling `wheel`/`sdist` targets so the
  built sdist contains tests, docs, and policy files.

### Highlights of the road to 1.0

The pre-1.0 series shipped the building blocks the 1.0 surface relies on.
A non-exhaustive summary:

- **Multi-model**: Ollama (native + auto-routing), OpenAI-compat
  (vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras), llama.cpp,
  TGI, OpenAI native (`gpt-*`, `o1`/`o3`/`o4`), Gemini OpenAI-compat,
  Modal serverless adapter, `anthropic_passthrough` for parity testing.
- **Tool use**: three paths (native, prompt-engineered `<tool_call>`,
  grammar-constrained JSON), capability-table-driven selection across
  30+ models, parallel dispatch, mid-stream dispatch, mid-stream
  cancellation via `ToolPermissionContext.signal`.
- **Streaming**: full `ContentBlockStart`/`Delta`/`Stop` plus
  `MessageStart`/`Delta`/`Stop` event surface; tools fire on
  `ContentBlockStop`, not after `MessageStop`.
- **Thinking**: inline `<think>` for DeepSeek-R1, QwQ, Marco-o1, R1-distill;
  out-of-band thinking blocks for the DeepSeek API; `ThinkingBlock`
  in `AssistantMessage.content`.
- **MCP**: stdio / sse / http transports, in-process server via
  `create_sdk_mcp_server`, elicitation, sampling.
- **Sessions**: JSONL transcript persistence, `~/.mantis-agent/` layout,
  fork + resume from arbitrary checkpoint, memory entries + index,
  `<system-reminder>` and `isMeta` injection, auto-compaction.
- **Budget**: per-model pricing, `max_usd` ceiling →
  `BudgetExceededError`, `total_cost_usd` and `modelUsage` on
  `ResultMessage`, `max_turns` ceiling.
- **Local setup**: `mantis-agent setup-local` for Ollama (Linux/macOS/Windows)
  and `mantis-agent setup-local-llamacpp` for llama.cpp.
- **Examples**: 16 verified examples across ≥3 backends, including
  `quickstart`, `ollama_local`, `with_thinking`, `tools_option`,
  `mcp_calculator`, `system_prompt`, `fireworks_hosted`,
  `vllm_self_hosted`, `multi_agent_research`.
- **Docs site**: mkdocs-material at `docs/`.

[Unreleased]: https://github.com/teddyoweh/mantis-agent-sdk/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/teddyoweh/mantis-agent-sdk/releases/tag/v1.0.0
