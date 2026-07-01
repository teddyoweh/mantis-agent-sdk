# Changelog

All notable changes to `mantis-agent-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and from 1.0.0 on the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The full versioning policy is in [SEMVER.md](SEMVER.md).

## [1.8.1] - 2026-06-30

### Added

- **`mantis` full-screen: a live, navigable slash-command menu.** Typing `/`
  now shows a real layout window (not a fragile completion float) listing the
  matching commands with descriptions — arrow ↑/↓ to select, Tab/Enter to fill.
- **`/models` is a selectable model picker.** It lists only *chat* models from
  the active backend (embeddings, tts, whisper, moderation, and legacy models
  are filtered out), navigable with the arrow keys; Enter switches and rebuilds
  the agent so the change takes effect immediately, and persists the choice.
  `/model <partial>` filters as you type.

### Fixed

- `/model <id>` in the full-screen UI now rebuilds the live agent (previously it
  set the model string but the running agent kept the old model).

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

## [1.8.0] — 2026-06-30

### Added

- **Interactive permission prompts — the terminal no longer runs bash/write/edit
  unconfirmed** (parity roadmap T0.2). `default` mode now *asks* before every
  mutating tool (Allow once / Allow for session / Deny), rendered as an in-pane
  prompt in the full-screen app (resolved by a keypress, no nested prompt).
  `accept edits on` auto-approves file edits but still asks for bash; `plan mode`
  still denies mutations; `bypass` and read-only tools never prompt. A bash
  danger classifier annotates the prompt (`rm -rf`, `curl|sh`, `sudo`, …).
  `settings.json` `permissions.allow/deny/ask` rules are now loaded and enforced.

### Changed

- `check_permission` resolves `Ask` through a new `PermissionContext.asker`
  callback with per-`(tool, input)` "allow for session" memory; `PermissionMode`
  gains `acceptEdits`. Library/headless callers without an asker keep the old
  non-blocking behavior, so nothing hangs. New `classify_bash_command`.

## [1.7.0] — 2026-06-30

### Added

- **Auto-compaction is now wired into the agent loop** (parity roadmap T0.1).
  When a conversation approaches the model's context window, `Agent` summarizes
  older turns at a safe boundary and continues — so long sessions no longer grow
  until the provider 413s. On by default (`auto_compact=True`); pass
  `auto_compact=False` or a custom `compactor=` to override. The summary is a
  plain `UserMessage` (serializes through providers/`query()`/sessions), the
  split is tool-pair-aware (never orphans a `tool_use`), the summarizer call is
  billed through the budget tracker, and a per-run cap guards against a
  non-converging summary. Covered by `tests/test_compaction.py`.

### Fixed

- `SimpleCompactor` now detects a leading system message by `role` rather than
  `isinstance`, so the SDK-shaped `SystemMessage` (from `claude_compat`) is
  correctly preserved outside the compaction boundary.

## [1.6.0] — 2026-06-30

### Added

- **`mantis setup` — a real first-run experience.** Detects your machine
  (RAM / Apple Silicon / NVIDIA VRAM) and recommends the best *coding* model
  that fits, from a curated coding-first catalog (Qwen2.5-Coder 0.5B→32B plus
  DeepSeek-R1 for code reasoning). Pick from the list, take the ★ recommendation,
  or `--auto`; it installs Ollama if missing, pulls the model, and sets it as
  your default so `mantis` opens straight into a working agent.
  `mantis setup --list` prints the catalog; `mantis setup --model <tag>` pulls
  a specific one. (The older `mantis-agent setup-local` still works.)

## [1.5.1] — 2026-06-30

### Docs

- **README polished end to end** — sharper intro hook (terminal + library, one
  install), the terminal section rewritten as natural prose, and the stale
  pre-1.0 "acceptance test"/test-count copy refreshed (831 tests, 3.11–3.13).

## [1.5.0] — 2026-06-30

### Changed

- **`pip install mantis-agent-sdk` now ships the terminal out of the box** — no
  `[cli]` extra needed. `prompt_toolkit` and `rich` moved into core
  dependencies (both lazy-imported, so the stdlib-only `mantis-agent`
  diagnostics CLI keeps its snappy cold start). `[cli]` is kept as a no-op for
  back-compat.

## [1.4.1] — 2026-06-30

### Changed

- **Diff colors now match Claude Code exactly** — bright green/red gutter
  markers + line numbers (`rgb(105,219,124)` / `rgb(255,168,180)`, Claude's
  `diffAdded`/`diffRemoved`) over the dark-blend row fill, with the dimmed
  variants as the fallback fg.

## [1.4.0] — 2026-06-30

### Changed

- **Syntax-highlighted diffs (sexier than the line-numbered blocks).** Diff rows
  now render the code with full syntax highlighting *on top of* the full-width
  green/red background — `def` keywords, identifiers, types, strings all
  colored inside the added/removed rows (language detected from the file
  extension). Plus a Claude-Code-style `<file>  +N -M` summary line.

## [1.3.3] — 2026-06-30

### Changed

- **Diffs now render like Claude Code** — full-width dark-green/dark-red
  background rows for additions/deletions (not just colored text), with a
  line-number gutter (additions show new-file numbers, deletions show
  old-file numbers) and dim context lines.
- Fixed remaining `Text` styles that used `ansi*` names (rendered white): the
  tool-result branch, error lines, and the todo checklist now use valid rich
  colors.

## [1.3.2] — 2026-06-30

### Fixed

- **Diffs now render in color.** `Text(style="ansigreen"/"ansired"/…)` silently
  produced white text (rich's `Text` doesn't accept the `ansi*` color names that
  its markup parser does) — so edit diffs showed `+`/`-` with no green/red.
  Converted all `Text` styles to valid rich names (`green`/`red`/`bright_black`).
  Also: `multi_edit` now returns a unified diff like `edit_file`/`write_file`, so
  multi-edit operations render colored diffs too.

## [1.3.1] — 2026-06-30

### Docs

- **README documents the `mantis` terminal** — install (`[cli]` extra), the
  full-screen agent TUI, edit diffs, tool calls, clipboard paste, slash
  commands, keys, and configuration env vars — alongside the existing library
  (API) docs.

## [1.3.0] — 2026-06-30

### Added

- **Clipboard paste (Ctrl+V) in the terminal** — paste a copied image, or a
  copied file path, straight into the prompt as an attachment. New
  `mantis_agent.clipboard` module (macOS/Linux/Windows) with image + file
  detection; wired into the TUI input.

This release also bundles all the interactive-terminal work from 1.1.x–1.2.x:
the `mantis` Claude-Code-style terminal — praying-mantis mascot, Markdown +
syntax-highlighted code, line-numbered edit diffs, friendly tool-call headers,
the animated thinking spinner, dark slash-command menu, and full-screen mode
(input pinned to the bottom, always visible while the agent works).

## [1.2.2] — 2026-06-30

### Fixed

- **Blank line between your message and the reply** in full-screen mode. Switched
  to a trailing-blank spacing model (each block emits its own separator; tool
  calls emit none so their result hugs) so the gap is reliable.
- **Ctrl+C now quits when idle** (and still interrupts a running reply).

## [1.2.1] — 2026-06-30

### Fixed

- **Consistent spacing in full-screen mode.** One blank line between blocks
  (user message, assistant text, tool call) with tool results hugging their
  call — fixing assistant text that was cramped right under a tool result and
  the doubled gap before the next prompt.

## [1.2.0] — 2026-06-30

### Added

- **Full-screen mode — the input is pinned to the bottom and always visible,
  even while the agent is working.** `mantis` now runs as a `prompt_toolkit`
  app whose bottom region (rule · input · rule · footer) stays fixed while the
  conversation scrolls above it (the Claude Code layout). The thinking spinner
  lives in the footer; Esc / Ctrl+C interrupts a running reply, Ctrl+D quits.
  All existing rich rendering (banner, markdown, diffs, tool calls) is reused.
  Set `MANTIS_CLASSIC=1` to force the classic scrolling REPL; full-screen also
  auto-falls-back to it if it can't start.

## [1.1.28] — 2026-06-30

### Changed

- **Input frame uses a solid rule** (`─`) again instead of the dashed `┄`.

## [1.1.27] — 2026-06-30

### Changed

- **Removed the "? for shortcuts" footer hint** (default mode shows no footer
  text), and dropped the toolbar's reverse/white background so the footer is
  plain text on the terminal background.

## [1.1.26] — 2026-06-30

### Fixed

- **Bottom rule hugs the input at launch.** The input is padded toward the
  bottom of the screen so its framing rules + footer hug it, instead of the
  toolbar floating to the screen floor with a gap. Safe with `erase_when_done`
  (the frame is wiped and `› message` echoed in place, so the first message
  scrolls naturally instead of being buried).

## [1.1.25] — 2026-06-30

### Fixed

- **Bottom rule now hugs the input, and the Enter flicker is gone.** Dropped
  `reserve_space_for_menu` from 8 to 0: it had inserted 8 blank rows between the
  input and the bottom rule/footer (rule floated far below the input), and that
  large reserved region repainted on submit (the ~1s flicker). Now both dashed
  rules sit directly above and below the input.

## [1.1.24] — 2026-06-30

### Changed

- **Input frame uses a dashed rule** (`┄`) instead of a solid line.

## [1.1.23] — 2026-06-30

### Fixed

- **Input rules now frame only the live input**, not every past message. The
  framed prompt (top rule + input + bottom rule + footer) is erased on submit
  (`erase_when_done`) and the submitted line is echoed as a clean `› message`,
  so scrollback has no stray rules.

## [1.1.22] — 2026-06-30

### Changed

- **Input is framed with horizontal rules** above and below it (the toolbar
  draws the lower rule), matching Claude Code's prompt framing.

## [1.1.21] — 2026-06-30

### Fixed

- **Removed the grey highlight box around inline `code`/filenames.** rich's
  default markdown code style uses a reverse/background that read like a
  stray text selection; inline code and code blocks now render as plain green
  text.

## [1.1.20] — 2026-06-30

### Added

- **Real diffs for edits.** `edit_file` and `write_file` now return a compact
  unified diff, and the TUI renders it as a line-numbered green/red diff block
  under the tool call (additions green, deletions red, context dim) — like
  Claude Code's edit view.

## [1.1.19] — 2026-06-30

### Fixed

- **Hotfix:** 1.1.18's wheel was built mid-edit and shipped `builtin_tools/fs.py`
  without its `import re`, so the package failed to import. Rebuilt with the
  import in place.

## [1.1.18] — 2026-06-30

### Fixed

- **Doubled spacing between messages.** Both the render and the pre-spinner
  step were emitting blank lines, so blocks were separated by two blank lines
  (and bullets could orphan above code). Now the single pre-spinner blank is
  the only separator and content lands on the spinner's cleared line — exactly
  one blank line between blocks, call+result still hugged.

## [1.1.17] — 2026-06-30

### Changed

- **Tool call and its result are hugged together** (no blank/spinner gap
  between `⚒ write foo.py` and its `└ …` result). Spacing is kept above the
  call and below the result group.

## [1.1.16] — 2026-06-30

### Changed

- **Tighter Markdown rendering.** Code blocks no longer carry rich's large
  vertical padding / grey box — replies are compact (one blank line around
  code instead of three).
- **Spinner spacing.** The thinking spinner now gets a blank line above it
  after tool results too (not just at turn start), so it isn't cramped.

## [1.1.15] — 2026-06-30

### Changed

- **Blank line between a reply and the next prompt** so turns don't jam together.
- **`/clear` now blanks the screen** (clears scrollback) and redraws the banner —
  a clean fresh start — instead of just printing "(history cleared)".

## [1.1.14] — 2026-06-30

### Changed

- **Tool calls render Claude-Code-style.** Instead of `⚒ read(path=...)` and a
  raw output dump, tool calls now show a friendly verb + target
  (`⚒ Read foo.py`, `⚒ Edit foo.py`, `⚒ Run date +%H:%M`, `⚒ Search "pat"`)
  with the result hanging off a `└` branch and overflow capped.

## [1.1.13] — 2026-06-30

### Fixed

- **Your messages no longer vanish after sending.** The launch bottom-padding
  pushed the prompt below a wall of blank lines, so the first message scrolled
  up into that emptiness and looked gone. Removed the padding: the banner sits
  at the top, the input right beneath it, and the conversation flows downward
  with every message visible.

## [1.1.12] — 2026-06-30

### Changed

- **Breathing room above tool calls and the loading spinner.** Tool-call lines
  (`⚒ grep(...)`) and the thinking spinner now get a blank line above them
  instead of being cramped against the previous output.

## [1.1.11] — 2026-06-30

### Fixed

- **Mascot no longer clipped, and the input is back at the bottom.** The banner
  height is now *measured* at the real terminal width (handling wrapping on
  narrow windows) instead of estimated, and `mantis` clears the screen +
  scrollback before drawing — so the banner sits fully at the top and the
  input is padded to the bottom row at any size. Fixes the case where a narrow
  window scrolled the mascot's head/antennae off the top.

## [1.1.10] — 2026-06-30

### Changed

- **Slash-command menu restyled** to a dark panel with a description column
  and a bright-green selected row — replacing prompt_toolkit's default
  white-background menu. Each command (`/help`, `/model`, `/clear`, `/cwd`,
  `/exit`, `/quit`) now shows a one-line description.

## [1.1.9] — 2026-06-30

### Fixed

- **Banner no longer scrolls off the top into a huge empty void.** The old
  bottom-padding overflowed on tall/narrow windows (wrapped banner text made
  the line math under-count), pushing the mascot off-screen and stranding the
  prompt at the bottom. `mantis` now clears to a fresh screen, prints the
  banner at the top, and puts the input right beneath it — robust at any
  terminal size.

## [1.1.8] — 2026-06-30

### Changed

- **Thinking spinner is now mantis green** (was coral) to match the mascot.

## [1.1.7] — 2026-06-30

### Changed

- **Assistant replies are rendered as Markdown** (code blocks with syntax
  highlighting, bold/italics, lists, tables) instead of raw text — using an
  ANSI colour theme so it looks right in Terminal.app. No more literal
  ```` ``` ```` fences in the output.

## [1.1.6] — 2026-06-30

### Added

- **Animated "thinking" status line** while the model works: a pulsing star,
  a random whimsical gerund, and a live elapsed timer — e.g.
  `✻ Undulating… (34s)` — rendered on a transient row that clears itself the
  instant output arrives. The input has no border/separator lines around it.

## [1.1.5] — 2026-06-30

### Changed

- **Input is pinned to the bottom of the terminal on launch** (Claude-Code
  style): the banner stays at the top and the prompt is pushed down to the
  bottom row, instead of sitting right under the banner with a large empty
  gap below. After the first turn, output scrolls naturally.

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
