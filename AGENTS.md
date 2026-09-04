# AGENTS.md — working in `mantis-agent-sdk`

Orientation for AI agents (and humans) working in this repo. Read this first, then
the relevant module. The full build backlog lives in
[`docs/internals/PARITY_ROADMAP.md`](docs/internals/PARITY_ROADMAP.md) — that's the
"deeply build it all" spec.

---

## What this is

Two products from one package:

1. **The library** — a drop-in `claude-agent-sdk` for open-source models. Same
   Anthropic-shaped surface (`query`, `ClaudeAgentOptions`, `@tool`, hooks,
   permissions, MCP, sub-agents, sessions), but the loop runs against Ollama /
   vLLM / llama.cpp / TGI / Together / Fireworks / Groq / OpenRouter.
2. **The `mantis` terminal** — a full-screen, Claude-Code-style coding agent TUI.

The wire format underneath is OpenAI-compat or Ollama; the surface above is
Anthropic-shaped. `routing.py` infers the backend from the model-name shape.

## Repo map (where things live)

| Area | Files |
|---|---|
| **Agent loop** | `agent.py` (`run_iter` — the turn loop, tool dispatch, permission check), `query.py` (the `query()` generator + SDK message shapes), `compat_query.py`, `claude_compat.py` |
| **Types / events / errors** | `types.py`, `events.py` (streaming events), `errors.py` |
| **Tools** | `tools.py` (`@tool`, `ToolRegistry`), `builtin_tools/fs.py` (bash/read/write/edit/multi_edit/ls/glob/grep), `builtin_tools/web.py`, `subagent.py` |
| **Model routing / providers** | `routing.py`, `providers/`, `capabilities.py` (per-model tool-use path table), `http.py`, `retry.py`, `catalog.py` (saved keys + last model) |
| **Context / memory** | `compact.py` (`SimpleCompactor` — wired: the default `auto_compact=True` compactor, plus micro/emergency compaction on overflow), `system_reminder.py` (`<env>` block — wired via `render_environment_context` into the isMeta context head), `memory.py`, `project_memory.py`, `memory_recall.py` |
| **Sessions** | `session.py`, `session_tree.py` (fork/rewind/checkpoint — strong, ahead of the reference), `transcripts.py` |
| **Extensibility** | `hooks.py` (28-event taxonomy), `permissions.py`, `skills.py` (wired: catalog in the context head + `load_skill` tool; `skills="auto"` in the terminal, off by default for library callers), `mcp/` (stdio/sse/http/in-process), `settings.py`, `response_format.py` / `response_model.py` (structured output; native envelope or instruct-and-parse per backend) |
| **Observability / budget** | `tracing.py` (`InMemoryTracer`/`OTelTracer`), `budget.py` (pricing, `max_usd`) |
| **Terminal (TUI)** | `tui.py` (REPL, rendering, slash commands, model picker, `_render_diff`), `tui_fullscreen.py` (the default full-screen app), `clipboard.py` |
| **CLIs / setup** | `cli.py` (`mantis-agent` diagnostics — stdlib-only), `setup_wizard.py` (`mantis setup`), `setup_local.py`, `setup_local_llamacpp.py` |
| **Paths** | `paths.py` → `~/.mantis-agent/` |

## The one thing to internalize

**The engine's hard machinery IS wired — the remaining risk is cross-provider
edge behaviour, not missing connections.** `run_iter` (`agent.py`) builds a
`SimpleCompactor` by default (`auto_compact=True`; micro → summarize → emergency
clear on a real overflow, bounded to one retry per turn), injects the `<env>` +
git snapshot through `render_environment_context`, and registers `load_skill` with
the skill catalog. Don't re-implement any of that.

What the loop guarantees (there are tests for each — `tests/test_engine_*.py`):

- **Message shape.** Every list handed to a provider passes
  `_assert_message_invariants` (each `tool_use` answered by exactly one
  `tool_result` in the *immediately* following user message; no orphan / stale
  results; unique ids). `_repair_tool_call_history` makes it hold by construction,
  the assembler mints ids a provider left empty or duplicated (Gemini, some vLLM
  builds), and the exception / abandonment path heals the caller's list with
  `close_open_tool_calls`. A `MessageInvariantError` means an engine bug, not a
  provider quirk.
- **Thinking.** `_split_inline_thinking` moves any `<think>…</think>`-style span a
  provider left inside text into a `ThinkingBlock`, so answers, `query().result`
  and the text-channel tool salvage never see reasoning.
- **Stop controls don't fight.** `Agent(max_usd=…)` no longer mirrors
  `max_steps` into `Budget.max_turns`; a step-cap cutoff reports `error_max_turns`
  with or without a USD budget; after a "wrap up NOW" reminder a natural stop is
  final (persist mode won't answer it with "keep working").
- **Permissions fail closed.** A crashing `can_use_tool` is a recorded denial;
  `_permission_denials` is per run.
- **Structured output.** `response_format` / `response_model` send the
  `json_schema` envelope only where the backend enforces it, else fall back to a
  prompt instruction + `parse_response` (`Agent._structured_output_mode`).

**True remaining gaps** (verified, not folklore):

1. `BackendCapability` has no structured-output flag; the engine reads an optional
   `structured_output` attribute (`"json_schema" | "json_object" | "none"`) and
   otherwise falls back to a provider-name rule (only Anthropic passthrough is
   forced to instruct mode). Hosted `json_object`-only backends (DeepSeek, Moonshot)
   still get the `json_schema` envelope until the table carries the flag.
2. The OpenAI-compat *native* translator (`_translate_native`) doesn't peel inline
   `<think>` tags — only the prompt-engineered path does. The engine-level split
   above is the net; per-token streaming UIs still see the raw tags mid-stream.
3. Two turn caps with different semantics: `max_steps`/`max_turns` is a clean stop
   (final-turn wrap-up, then break); an explicit `Budget(max_turns=N)` is a hard
   `BudgetExceededError` after the Nth turn (pinned by `test_budget_wrapup.py`).
4. The `Stop` hook fires on a natural stop and on cancellation, **not** on a
   step-cap cutoff; `HookDispatcher.dispatch_run_end` (`StopFailure`) exists but
   the loop never calls it.
5. `Agent.cancel()` is one-way — `cancellation_signal` is never reset, so a
   cancelled Agent can't run again (the terminal builds a fresh one).
6. Headless `Ask` in `mode="auto"` is treated as Allow at the loop layer
   (`_preflight_call`); every other headless Ask already denies.
7. The TUI default permission mode is allow-all (Tier 0 on the roadmap).

## Conventions & gotchas (don't trip on these)

- **rich color names**: `Text(style="ansigreen")` renders **white** — rich's `Text`/
  `Style.parse` rejects `ansi*` names (only its *markup* parser accepts them). Use
  `"green"` / `"red"` / `"bright_black"` in `Text` styles and `Style`/`Theme`. Markup
  (`"[ansired]x[/]"`) and prompt_toolkit HTML (`fg="ansired"`) *do* accept `ansi*`.
- **Lazy imports**: `rich` / `prompt_toolkit` / `query` deps are imported inside
  functions, not at module top — keeps the stdlib-only `mantis-agent` cold start snappy.
  Keep it that way.
- **TUI spacing model** (`tui_fullscreen.py`): output is printed *above* a pinned input
  via `run_in_terminal`. Each block prints a **trailing** blank (separation can't be
  eaten); a tool **call** prints none so its **result hugs** it. Don't add leading blanks.
- **Diffs**: `_render_diff` renders full-width green/red rows with syntax-highlighted
  code (token bg stripped, row bg layered) on Claude's exact palette
  (`rgb(105,219,124)` / `rgb(255,168,180)`). Lang from file extension.
- **Permissions are real at the engine** (`agent.py` checks before dispatch) but the
  **TUI default mode is allow-all** — fixing that is Tier 0 (safety). See roadmap.

## Reference source

The north star is **Claude Code's own source** (Teddy's `claude agent sdk.zip` —
decompiled TS; prompts/descriptions are intact even where logic is minified). Unzip it
and compare `src/tools/`, `src/commands/`, `src/query/`, `src/utils/`,
`src/components/` against the mantis equivalents above. The
[parity roadmap](docs/internals/PARITY_ROADMAP.md) already maps the diff.

## Doing a deep dive

For broad "what does Claude do here vs us" questions, fan out **parallel read-only
agents**, one per subsystem (tools · slash-commands/TUI · core engine · hooks/perms/
MCP/skills/rendering), each told to return a *ranked gap list* (name · what Claude does ·
mantis status · effort · impact), not file dumps. Synthesize. That's how the roadmap
was built.

## Dev & release workflow

```bash
. .venv/bin/activate
uv pip install -e .                 # editable install (refreshes metadata for tests)
ruff check mantis_agent tests       # lint (CI runs this with `|| true`)
python -m pytest -q                 # 831 tests; MANTIS_AGENT_MOCK=1 for offline;
                                    # live-ollama tests skip if Ollama unreachable
```

**Shipping a release** (do this when the user asks to publish):
1. Bump `pyproject.toml` `version` **and** the fallback literal in `__init__.py`
   (`_detect_version`), add a `CHANGELOG.md` entry.
2. `uv build`, then **verify the wheel imports** —
   `uv pip install <wheel>` in a throwaway venv and `import mantis_agent`. *(A broken
   wheel once shipped because an `import` was added to a file after the build; always
   re-verify.)* `python -m twine check dist/*` if the README changed.
3. `uv publish` (PyPI), `git push`, then `uv tool install --force --reinstall <wheel>`
   to update the global `mantis`.

Semantic versioning; the public API is `mantis_agent.__all__` (snapshotted by
`tests/test_public_api_surface.py` — update it intentionally).
