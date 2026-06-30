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
| **Context / memory** | `compact.py` (`SimpleCompactor` — ⚠️ built but unwired), `system_reminder.py` (env/live-context helpers — ⚠️ unwired), `memory.py`, `project_memory.py`, `memory_recall.py` |
| **Sessions** | `session.py`, `session_tree.py` (fork/rewind/checkpoint — strong, ahead of the reference), `transcripts.py` |
| **Extensibility** | `hooks.py` (28-event taxonomy), `permissions.py`, `skills.py` (`SkillRegistry` — ⚠️ unwired into TUI), `mcp/` (stdio/sse/http/in-process), `settings.py`, `response_format.py` |
| **Observability / budget** | `tracing.py` (`InMemoryTracer`/`OTelTracer`), `budget.py` (pricing, `max_usd`) |
| **Terminal (TUI)** | `tui.py` (REPL, rendering, slash commands, model picker, `_render_diff`), `tui_fullscreen.py` (the default full-screen app), `clipboard.py` |
| **CLIs / setup** | `cli.py` (`mantis-agent` diagnostics — stdlib-only), `setup_wizard.py` (`mantis setup`), `setup_local.py`, `setup_local_llamacpp.py` |
| **Paths** | `paths.py` → `~/.mantis-agent/` |

## The one thing to internalize

**A lot of hard machinery is built but never wired into the loop.** `SimpleCompactor`
(`compact.py`), `build_live_context_block` / the `<env>` block (`system_reminder.py`),
and `SkillRegistry` (`skills.py`) all exist as standalone utilities that
`agent.py`'s `run_iter` never calls. Closing most of the Claude-Code gap is
*connection, not construction* — so it's cheap. See the roadmap.

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
