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

## [2.15.0] — 2026-06-30

### Changed

- **"Allow for session" now actually sticks for edits.** It was keyed on the
  exact tool input, so every `edit_file`/`write_file` — which always has a
  different old_string/new_string/content — re-prompted anyway, making the option
  useless for the highest-friction case. Edit/write/notebook tools are now keyed
  by the FILE PATH: approve editing `foo.py` once and further edits to `foo.py`
  this session don't re-prompt (`bar.py` still asks). `bash` and other tools stay
  scoped to the exact call, as before.

## [2.14.0] — 2026-06-30

### Changed

- **Malformed-history self-healing is now library-wide, not just the TUI.**
  `run_iter` closes any unanswered `tool_use` at the very start of a run, so a
  history left dangling by ANY path — a cancelled `Agent.run()`, a session saved
  mid-tool then resumed, or a hand-built message list — produces a well-formed
  first request instead of a provider error. `close_open_tool_calls` is now
  position-aware: it inserts the synthetic `tool_result` immediately after the
  assistant that opened it (correctly slotting BETWEEN the tool_use and a
  following user message), and augments a partially-answered result message.
  Idempotent.

## [2.13.0] — 2026-06-30

### Changed

- **Interrupting a turn now keeps the work.** Pressing Esc/Ctrl-C mid-reply used
  to discard the ENTIRE turn — your message and everything the agent had already
  done (files read, tools run) vanished. Now the completed work is kept; only the
  tool calls left unanswered by the interrupt are closed with a synthetic
  `[interrupted by user]` result, so the history stays well-formed and you can
  continue or redirect from where it stopped (the Claude Code behavior). New
  `agent.close_open_tool_calls`.

## [2.12.0] — 2026-06-30

### Added

- **`/learn` command** — memory consolidation. Have the agent review the current
  session and save the DURABLE facts worth keeping (your preferences and
  conventions, project gotchas, where things live, decisions + rationale) to
  persistent memory via the `remember` tool — the manual, on-demand form of
  auto-memory. `/learn` reviews everything; `/learn <focus>` steers it. Prompt is
  guarded against saving transient task state or duplicating existing memories.
  Recalled automatically in future sessions.

## [2.11.0] — 2026-06-30

### Added

- **`@`-file-mentions now inject file contents.** Previously `@`-mentions only
  autocompleted the path; the agent saw the literal `@foo.py` and had to do a
  separate `read_file` (or miss it). Now when you send a message mentioning
  `@path` files that exist, their current contents are injected inline (as an
  isMeta system-reminder, so your visible message stays clean) — the model has
  them immediately, no extra round-trip. Files too large to inline get a note
  pointing at `read_file`; non-file `@words` are ignored; duplicates deduped. New
  `tui.resolve_file_mentions` / `render_mention_block`.

## [2.10.1] — 2026-06-30

### Fixed

- **Friendly labels for recently-added tools.** `task`, `lsp`, `notebook_edit`,
  `remember`, `load_skill`, `ask_user_question`, `exit_plan_mode`, and
  `bash_output` were rendering in the terminal as a bare tool name with no target.
  They now show a human verb + target — e.g. `⚒ Delegate find the auth bug`,
  `⚒ Look up render_diff`, `⚒ Remember cache TTL` — matching the built-in tools.

## [2.10.0] — 2026-06-30

### Added

- **`task` tool — subagent delegation** in the terminal (Claude Code's Task
  primitive). The agent can now hand a focused, multi-step investigation to a
  fresh read-only subagent that runs to completion and returns just its findings —
  keeping the main context clean (no dozens of intermediate file dumps). The
  subagent shares the parent's model/provider but gets only a read-only kit
  (read_file, grep, glob, ls, lsp, web) — it cannot edit, run shell, recurse into
  another `task`, or prompt the user, so delegation is safe and unsupervised. Runs
  concurrently for parallel exploration. New `subagent.make_task_tool` (the
  underlying `SubAgentTool`/`as_subagent_tool` machinery already existed; this
  wires a general-purpose read-only variant into the `mantis` agent).

## [2.9.0] — 2026-06-30

### Added

- **`/init` command** (parity roadmap T1.2, completing it). Bootstraps a project's
  `MANTIS.md` — `/init` expands into a canned prompt that has the agent explore the
  codebase (ls/glob/grep/read) and write a tight `MANTIS.md` with the build/lint/
  test/run commands, high-level architecture, key conventions, and gotchas. That
  file then auto-loads into context every future session (the load-bearing half,
  already shipped). Improves an existing `MANTIS.md` rather than clobbering it. New
  `tui.INIT_PROMPT` / `expand_slash_prompt`.

## [2.8.0] — 2026-06-30

### Added

- **Path-scoped conditional rules.** A `.mantis/rules/*.md` file may now declare
  `globs:` (or `paths:`) in frontmatter, and is injected into context ONLY when a
  matching file is active in the conversation — an `@`-mention or a file the agent
  just read/edited. So a SQL style rule rides only SQL work, a Go rule only Go
  work, keeping project instructions lean instead of spending context on rules
  that don't apply. Rules with no globs stay unconditional (loaded always, as
  before). Deduped per session. New `mantis_agent.rules` module. Mirrors Claude
  Code's path-specific instructions.

## [2.7.0] — 2026-06-30

### Added

- **`/compact` command.** Compress the conversation on demand instead of waiting
  for auto-compaction — frees context before a big next step. Takes an optional
  focus hint (`/compact the auth refactor`) that steers what the summary
  preserves. Keeps the last few turns verbatim, summarizes the rest with the
  current model, and reports the before→after message count. Short conversations
  are a no-op. New `compact.run_manual_compaction` helper.

## [2.6.0] — 2026-06-30

### Added

- **Inline image rendering** in the terminal (iTerm2 / WezTerm). When the agent
  reads an image with multimodal `read_file`, the `mantis` terminal now *shows*
  it inline (via the iTerm2 `OSC 1337;File=` protocol, with tmux passthrough)
  plus a `[media, size]` note — the visual counterpart to the model being able to
  see it (1.28). Terminals without support just get the note, so nothing breaks.
  New `mantis_agent.inline_image` module (`iterm2_image_escape`,
  `supports_inline_images`, `image_block_to_inline`).

## [2.5.0] — 2026-06-30

### Changed

- **Structured compaction summaries** (parity roadmap T1.7). When a long coding
  session auto-compacts, the summarizer now produces Claude's multi-section
  format — Primary Request · Key Technical Concepts · Files and Code Sections
  (with exact paths + snippets) · Errors and Fixes · Problem Solving · Pending
  Tasks · Current Work · Next Step — instead of 200–400 words of prose. This
  preserves file paths, symbol names, error messages, and the precise next action
  across a resumed turn, so the agent doesn't redo or break work after a
  compaction. The transcript fed to the summarizer already carries tool inputs
  (file paths) and errors as raw material.

## [2.4.1] — 2026-06-30

### Fixed

- **Diff word-highlighting no longer lights up every line on a re-indent.** The
  colored diff paired the i-th removed line with the i-th added line by position,
  so wrapping a block in `try:`/`except` (or any re-indent) shifted every line and
  word-diffed unrelated pairs — nearly every character showed as "changed." Now
  removed↔added lines are aligned by their stripped content (SequenceMatcher):
  lines that only moved/re-indented match as unchanged and get no char emphasis;
  only genuinely modified lines are word-diffed against their real counterpart. A
  one-char edit still highlights exactly one char. New `_compute_word_emphasis`.

## [2.4.0] — 2026-06-30

### Added

- **Refusal recovery.** When the model ends a turn with a bare, no-tool-call
  refusal ("I'm sorry, but I can't complete that request") — the spurious
  over-refusals small/aligned models emit on perfectly legitimate local work
  (listing processes/ports, reading your own files, running builds) — the agent
  now nudges it ONCE with a reminder that it's operating in the user's own
  authorized environment and re-prompts, instead of dead-ending the task. Capped
  at one retry per run, so a genuinely harmful request is simply refused again
  and stops. New `Agent.recover_refusals` flag (default True; set False to opt
  out). New `_looks_like_refusal` detector (length-capped + precise, so a long
  answer or an "I can't find that file" isn't misread).

## [2.3.0] — 2026-06-30

### Added

- **Read-before-write guard** (Claude Code's readFileState). `write_file` now
  refuses to clobber an existing file the tools haven't *seen* this session, or
  one that changed on disk since it was read — so unseen or newer content is
  never silently destroyed by a blind overwrite. The tools (`read_file`,
  `write_file`, `edit_file`, `multi_edit`) track each file's mtime; new files and
  read-then-write / write-then-overwrite flows pass freely, and the error tells
  the model to read first (recoverable in one step).

## [2.2.0] — 2026-06-30

### Fixed

- **`web_fetch` no longer depends on BeautifulSoup.** Its default (non-Exa) path
  called `bs4` — not a dependency — so a plain `web_fetch(url)` returned raw HTML
  (tags, `<script>`, CSS) as the model's "readable text". Rewritten with a
  stdlib HTML→text extractor (drops script/style/head, block-closes → newlines,
  strips tags, unescapes entities, collapses whitespace). Non-HTML bodies (JSON,
  plain text, markdown, source) are now returned verbatim by content-type instead
  of being tag-stripped. Same dependency-free treatment `web_search` got in 1.25.

## [2.1.0] — 2026-06-30

### Added

- **`lsp` is now multi-language.** Goto-definition and the `symbols` outline
  work across JavaScript, TypeScript, Go, Rust, Java, Ruby, and C/C++ (in
  addition to Python's precise ast path) via targeted declaration-syntax regex —
  so a function call or a control-flow brace is never mistaken for a definition
  the way plain grep would. TS interfaces/types/enums, Go/Rust types, Ruby
  modules, etc. are recognized with their kind. References stay Python-only
  (ast-precise).

## [2.0.0] — 2026-06-30

### Changed (BREAKING)

- **`ClaudeAgentOptions` is renamed to `MantisAgentOptions`.** The options class
  is now natively mantis-branded across the whole codebase, docs, and examples.
  `ClaudeAgentOptions` is **removed** (no alias) — update imports to
  `from mantis_agent import MantisAgentOptions`. All other drop-in symbols
  (`query`, `tool`, `AssistantMessage`, …) are unchanged.

### Added

- **Session-resume context freshness.** `Session.load` (and `resume_session`)
  now drops the synthetic `isMeta` context/reminder messages (env + git + memory
  head, recall, todo) by default so a resumed session RE-DERIVES current context
  instead of replaying a stale snapshot from when it was created. New
  `strip_context_messages` helper; pass `fresh_context=False` to keep the frozen
  head.

## [1.36.0] — 2026-06-30

### Added

- **Thinking-block rendering in the terminal** (parity roadmap T2 polish).
  Reasoning models (DeepSeek-R1, QwQ, API extended-thinking) emit a thinking
  block; previously the terminal dropped it entirely. Now it's shown dimmed above
  the answer under a `✻ thinking` header, capped at 12 lines (with a `… (N more
  lines)` note) so a long chain-of-thought doesn't bury the reply. New pure
  `_thinking_lines` helper.

## [1.35.0] — 2026-06-30

### Added

- **`lsp` gained a `symbols` operation** — a file/project outline: classes with
  their methods (indented) plus top-level functions, each with a line number.
  `lsp(operation="symbols", path=...)`, with an optional `symbol` substring to
  filter a large tree. The fast "show me the structure of this file / where's
  everything" view, ast-based (async methods and nested scopes handled). New
  `find_symbols` helper.

## [1.34.0] — 2026-06-30

### Added

- **`lsp` tool — semantic code navigation** (parity roadmap T1.8). Goto-definition
  and find-references for Python, done the mantis way: via the stdlib `ast` module
  instead of an external language server, so it has zero dependencies and works
  out of the box. Unlike grep it distinguishes a *definition* (function / class /
  method / module-level assignment) from a *mention*, resolves attribute accesses
  (`x.method`), and skips names in comments/strings. `lsp(operation="definition"
  | "references", symbol=..., path=...)`. Wired into the `mantis` tool belt. New
  `find_definitions` / `find_references` helpers.

## [1.33.0] — 2026-06-30

### Added

- **`/memory`** (parity roadmap T2). Open your instruction-memory files in
  `$EDITOR` to curate what the agent knows: `/memory` (project `MANTIS.md`),
  `/memory agents` (`AGENTS.md`), `/memory user` (user-level `MANTIS.md`).
  Creates the file with a template if missing and rebuilds the context head so
  edits apply on the next turn. New pure `resolve_memory_target` helper.

## [1.32.0] — 2026-06-30

### Added

- **`notebook_edit` tool** (parity roadmap T2). Edit a Jupyter notebook cell:
  `replace` (default), `insert` (a new code/markdown cell before an index), or
  `delete`, addressed by 0-based `cell_number`. Replacing a code cell clears its
  now-stale outputs and execution count; writes nbformat-style JSON back. Pairs
  with notebook reading (1.31.0) to complete notebook support.

## [1.31.0] — 2026-06-30

### Added

- **Notebook (`.ipynb`) reading** (parity roadmap T2). `read_file` on a Jupyter
  notebook now renders readable cells — markdown, code, and text outputs (stream,
  execute_result, and errors as `EName: value`; image outputs noted) — instead of
  dumping raw JSON. Falls back to plain text if the file isn't valid notebook
  JSON. New `_render_notebook` helper.

## [1.30.0] — 2026-06-30

### Added

- **`/diff`** (parity roadmap T2). Review every change the agent made this
  session in one view — runs `git diff HEAD` and renders each file with the same
  full-width syntax-highlighted, word-level-highlighted diff renderer used inline,
  plus a list of new (untracked) files. New pure `split_git_diff()` parser (splits
  `git diff` output into per-file hunks, stripping git headers). Notes when the
  directory isn't a git repo.

## [1.29.0] — 2026-06-30

### Added

- **Microcompaction** (parity roadmap T2). A cheap first line of context defense
  that runs before full compaction: once the window passes 60%, the bodies of
  tool results older than the last 8 (only those over ~800 chars) are cleared to
  `[old tool result cleared to save context]` — no summarizer call. It keeps the
  blocks and their `tool_use_id` intact (pairing untouched) and is idempotent, so
  a long chain of `read`/`grep`/`bash` dumps you've already acted on stops
  bloating the window, deferring the expensive summarizing compaction (which
  still fires at 85% as the fallback). New `SimpleCompactor.should_microcompact`
  / `microcompact`.

## [1.28.0] — 2026-06-30

### Added

- **Multimodal `read_file`** (parity roadmap T2). Reading an image
  (png/jpg/gif/webp/bmp) now returns it as an image the model can actually see —
  on vision-capable backends — instead of dumping mojibake; PDFs and other
  binaries get a helpful note. Under the hood, the tool executor now passes a
  tool that returns an `ImageBlock`/`TextBlock` (or a list of them) straight
  through as the tool-result content instead of stringifying it, so any tool can
  return rich content. (Anthropic serializes images in tool results; other
  backends vary by model.)

## [1.27.0] — 2026-06-30

### Added

- **MCP resources + prompts** (parity roadmap T2). The MCP client gained
  `list_resources()` / `read_resource(uri)` (the readable blobs a server exposes)
  and `list_prompts()` / `get_prompt(name, arguments)` (reusable named prompt
  templates, rendered to `[role] text`). Both list calls are paginated; binary
  resource parts are noted rather than dumped as base64. New `MCPResource` /
  `MCPPrompt` types. Previously the client only spoke `tools/list` + `tools/call`.

## [1.26.0] — 2026-06-30

### Added

- **Model fallback** (parity roadmap T2). `Agent(fallback_model=...)` — if the
  primary model call fails *before producing any output* (overload,
  model-not-found, connection drop), the turn is retried once on the fallback
  model (same provider/backend), so a transient outage doesn't kill the run. A
  failure *after* tokens have streamed is re-raised (no unsafe partial-output
  retry); the fallback is one-shot per run (no retry loop). In the terminal, set
  `MANTIS_AGENT_FALLBACK_MODEL`.

## [1.25.0] — 2026-06-30

### Fixed

- **Web search works out of the box again.** The keyless DuckDuckGo fallback
  required `beautifulsoup4` (not a dependency), so `web_search` returned an error
  unless you set an API key. It's now dependency-free (stdlib HTML parsing),
  unwraps DuckDuckGo's `/l/?uddg=` redirector to real URLs, and falls back to the
  `lite` endpoint when the html one is empty. Set `EXA_API_KEY` / `BRAVE_API_KEY`
  / `TAVILY_API_KEY` for higher-quality results as before.

### Added

- **Todo re-injection** (parity roadmap T2). When an `Agent` is given a live
  `todos` list (the one `todo_write` mutates), the current state is re-injected
  as a `<system-reminder>` at the top of each turn — refreshed, not accumulated —
  so the model keeps its plan in view over a long task. Wired in the terminal.

### Changed

- The terminal input prompt is now `❯` (was `›`).

## [1.24.0] — 2026-06-30

### Added

- **`/export` and `/copy`** (parity roadmap T2). `/export [path]` saves the
  conversation to a shareable markdown file (default `mantis-conversation.md`);
  `/copy` copies the last assistant reply to the system clipboard (pbcopy /
  wl-copy / xclip / clip). New pure `render_transcript()` helper and
  `clipboard.copy_to_clipboard()`.

## [1.23.0] — 2026-06-30

### Added

- **Hooks: multiple hooks per event + tool-name matchers** (parity roadmap T2).
  A hook field now accepts a list of callables and/or `HookMatcher(hook=fn,
  matcher="Bash")` — the dispatcher runs every *matching* hook in order (fnmatch
  against the tool name; non-tool events always run), chains input mutations, and
  short-circuits on the first block. Backward compatible: a bare callable still
  works. The `claude_compat` SDK-shaped `HookMatcher(matcher=..., hooks=[...])`
  now works end to end with real matcher semantics (previously only the first
  callable per event was honored).

## [1.22.0] — 2026-06-30

### Added

- **Word-level diff highlighting** (parity roadmap T2). On a modified line the
  diff renderer now brightens just the characters that actually changed
  (Claude's `diffAddedWord` / `diffRemovedWord` green/red), so a one-character
  edit lights up one character instead of the whole line reading as changed.
  Lines are paired within each change block and char-diffed; a wholesale rewrite
  skips the emphasis (the row colour already tells that story). New pure
  `_word_diff_spans` helper.

## [1.21.0] — 2026-06-30

### Added

- **Skills are now live in the product** (parity roadmap T1.3). The SKILL.md
  progressive-disclosure system was built but dead — now it works end to end.
  Drop a skill at `~/.mantis-agent/skills/<slug>/SKILL.md` (or
  `./.mantis/skills/...` per project) with `name`/`description` frontmatter and a
  markdown body. Each session injects only the **catalog** (name + one-line
  description) into context; when a task matches, the agent calls the new
  **`load_skill`** tool to pull the full instructions on demand — so N skills
  cost N one-liners, not N documents. `skills.discover_skills` /
  `render_skill_catalog` / `load_skill_body`.

## [1.20.0] — 2026-06-30

### Added

- **`bash(run_in_background=True)` + the `bash_output` tool** (parity roadmap
  T1.4 complete). Long-running commands — a dev server, a file watcher, a slow
  build — can now run detached: bash returns a background id immediately instead
  of blocking or timing out, streams stdout+stderr to a temp log, and
  `bash_output(bash_id=...)` reads the accumulated output plus whether it's still
  running or has exited (with its code). Processes start in their own session so
  they survive independently.

## [1.19.0] — 2026-06-30

### Added

- **Vim editing mode + external editor in the terminal** (parity roadmap T2).
  Toggle vim keybindings on the input line with **`/vim`** (or start with
  `MANTIS_VIM=1`). Press **Ctrl-X Ctrl-E** to compose a long or multi-line prompt
  in `$EDITOR` — the classic shell ergonomic. Both are near-free wins for anyone
  who lives in the terminal.

## [1.18.0] — 2026-06-30

### Added

- **Prompt caching for the Anthropic backend** (parity roadmap T0.4 — Tier 0
  complete). The passthrough now sets `cache_control: ephemeral` breakpoints on
  the system prompt and the last message, so Anthropic reads the stable prefix
  (system + conversation-so-far) from cache instead of re-billing it every turn
  — a large cost/latency win on multi-turn sessions. On by default; the provider
  already tallied `cache_read`/`cache_creation` tokens, now it actually requests
  the cache. Set `cache_prompts=False` on the provider to opt out.

## [1.17.0] — 2026-06-30

### Added

- **Plan-mode approval handoff** (parity roadmap T1.6). Plan mode already gated
  mutations read-only; now there's the missing present-plan → approve → execute
  flow. In plan mode the agent researches, then calls the new **`exit_plan_mode`**
  tool with its plan; the terminal renders it and asks you to approve via the
  same picker as AskUserQuestion. On approval plan mode is lifted so the agent
  can start editing; otherwise it stays read-only and revises. The plan-mode
  denial message now points the model at `exit_plan_mode`.

## [1.16.0] — 2026-06-30

### Added

- **Tool-result truncation backstop** (parity roadmap T0.3). A single huge tool
  result — `cat`-ing a big file, a noisy build log, an MCP tool dumping JSON —
  can no longer blow the whole context window in one turn. The executor caps each
  result (tool-aware: reads/shell/web-fetch get more room than a generic tool),
  keeping head + tail and eliding the middle with a note that says how much was
  dropped and to narrow the query. Default 30k chars; override with
  `MANTIS_AGENT_MAX_TOOL_RESULT`.

## [1.15.0] — 2026-06-30

### Added

- **`grep` gained real search modes** (parity roadmap T1.4). New args:
  `output_mode` (`content` / `files_with_matches` / `count`), `context_lines`
  (show lines around each match), `file_type` (restrict to a language like `py`,
  `rust`, `js` — maps to `rg --type` and to extensions in the Python fallback),
  `head_limit` (cap output; replaces the hardcoded 50-match limit), and
  `multiline` (patterns spanning line boundaries). Both the ripgrep path and the
  dependency-free Python fallback honor every mode. (`glob` already sorted results
  by mtime, newest-first.)

## [1.14.0] — 2026-06-30

### Added

- **Context-window awareness in the terminal** (parity roadmap T1.5). The footer
  now shows a live fill indicator (`12k/32k 38%`, coloured green→yellow→red as it
  fills) so you can see how full the window is at a glance. A new **`/context`**
  command renders a bar plus an estimated split across system prompt, memory/env
  context head, and conversation. New `context_breakdown()` helper.

## [1.13.0] — 2026-06-30

### Added

- **`@`-file-mentions in the terminal** (parity roadmap T1.1). Type `@` anywhere
  in the prompt to fuzzy-find a file under the cwd and drop its path in — no more
  pasting paths by hand. The completer ranks basename-prefix matches first, skips
  VCS/build dirs and dotfiles, and is bounded so it stays snappy on big repos.
  Navigate with ↑/↓, accept with Tab/Enter.

## [1.12.0] — 2026-06-30

### Added

- **`ask_user_question` tool — the agent can ask *you* structured multiple-choice
  questions mid-task** (Claude Code's AskUserQuestion). It proposes 1-4 questions,
  each with 2-4 labelled options (label + description); you pick with number keys
  or arrows, toggle multiple with space when `multiSelect` is set, or choose
  "Other" to type free text. Rendered as an in-pane picker in the full-screen
  terminal (Future-bridged like the permission prompt), with a numbered fallback
  in the classic REPL and a graceful no-op when headless. The chosen answers come
  back as the tool result, so the agent acts on real preferences instead of
  guessing. Wired into the `mantis` tool belt.

## [1.11.0] — 2026-06-30

### Added

- **`mantis --dangerously-skip-permissions` (alias `mantis --godmode`)** starts
  the terminal in engine-level bypass: every tool runs with no confirmation
  prompt — including dangerous shell commands (`rm -rf`, `curl|sh`, `sudo`),
  which are otherwise always gated. Sets the permission context's `mode=bypass`
  so the whole permission pipeline short-circuits to Allow, and prints a red
  warning banner on start. For trusted, unattended runs where you accept all
  risk.

## [1.10.0] — 2026-06-30

### Added

- **Memory recall is now wired into the run loop — the agent surfaces the *right*
  memories.** Before each turn it scores the `~/.mantis-agent/memory/` topic
  files against the latest user message (keyword-overlap, fully offline) and
  injects the top matches as an isMeta `<system-reminder>`, deduped across the
  session, with a staleness caveat for notes older than a day. Previously the
  whole `MEMORY.md` index was dumped regardless of relevance; the rich
  `memory_recall.py` engine was dead code. New `Agent.include_recall` (default
  True; disabled by `MANTIS_AGENT_NO_CONTEXT=1`).
- **A `remember` tool** gives the agent a write path into persistent memory — it
  can save durable facts (project conventions, preferences, gotchas) that recall
  then surfaces automatically in future sessions. Wired into the `mantis`
  terminal's tool belt. Read + write now form a closed loop.

## [1.9.1] — 2026-06-30

### Changed

- **The `mantis` system prompt is rebuilt to Claude-Code quality.** It keeps
  mantis's local-model tuning (act immediately, call tools instead of describing,
  never refuse a normal engineering task) and adds the engineering discipline a
  real coding agent needs: read before you change, make the smallest diff (no
  speculative abstractions / over-engineering / needless comments), prefer
  editing over creating files, diagnose failures before switching tactics, and
  **verify before reporting done + report outcomes faithfully**. New "Acting with
  care" section (confirm destructive / shared-state actions; approval once ≠
  approval always; no `--no-verify` shortcuts) and output conventions
  (`file_path:line_number`, lead with the answer, no emojis unless asked). The
  static environment line is dropped — the richer `<env>` context head (1.9.0)
  covers it.

## [1.9.0] — 2026-06-30

### Added

- **The agent is now oriented in your repo** (parity roadmap T0.5). Every
  session injects an `<env>` + git snapshot into the context head: working
  directory, platform, OS version, today's date, a shallow directory listing,
  and git branch / main branch / user / status / recent commits (Claude Code's
  format, incl. the "snapshot in time" disclaimer and 2k status truncation).
  Built once and memoized (`Agent._env_context`) so the prompt-cache prefix
  stays stable across turns; rides in the same isMeta head compaction preserves.
  New `include_env` field (default True); `MANTIS_AGENT_NO_CONTEXT=1` disables
  all context injection. New `system_reminder.build_env_context_block` /
  `build_git_context` / `render_environment_context`.
- **`AGENTS.md` is now auto-loaded** alongside `MANTIS.md` in the project-memory
  cwd-walk (same tier/precedence) — a project's existing AGENTS.md is picked up
  with no config.

## [1.8.1] — 2026-06-30

### Security

- **Dangerous shell commands can no longer skip the permission prompt.** A bash
  command flagged by the danger classifier (`rm -rf`, `curl|sh`, `sudo`, raw
  disk writes, …) now always requires live confirmation — a broad `allow` rule,
  `acceptEdits`, or the mode default can't auto-run it. With no interactive
  approver (library / headless), such a command is denied rather than run. Only
  an explicit `deny` rule or `bypass` mode overrides it.

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
