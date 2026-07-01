# Parity roadmap — building mantis up to (and past) Claude Code

This is the **build backlog**: the gap between mantis and Claude Code, turned into
implementation tickets. Each item has **what · where to touch · how · effort ·
acceptance**. Built from a four-agent deep dive over Claude Code's decompiled source.
Read [`../../AGENTS.md`](../../AGENTS.md) first for orientation and the gotchas.

> **Theme:** much of the hard machinery already exists in the repo but is **never
> wired into `agent.py`'s `run_iter`**. Tier 0 is mostly *connecting* it. Cheap, huge.

Effort key: **S** trivial/<1h · **M** half-day · **L** multi-day. Impact: 🔴 critical · 🟠 high · 🟡 nice.

---

## 🔴 Tier 0 — Critical. A long session today either crashes or runs unsafe. Ship these first.

### T0.1 — Wire auto-compaction into the turn loop · S · 🔴 — ✅ SHIPPED (v1.7.0)
- **Why:** `SimpleCompactor` (`compact.py`) implements `should_compact()`/`compact()` (0.85 threshold, keep-last-K) but **nothing calls it** → sessions grow until the provider 413s. Local models have 8–32k windows; this is the #1 killer.
- **Where:** `agent.py` `run_iter`, top of the `for _ in range(max_steps)` loop. Model context window from `capabilities.py`.
- **How:** before each model call, if `compactor.should_compact(messages, ctx_window)`: summarize older turns, replace `messages` in place, emit a `SDKCompactBoundaryMessage`. Reserve ~20k tokens for the summary; keep a buffer below the window. Add a 3-strike circuit breaker for irrecoverable overflow (Claude does this).
- **Acceptance:** a scripted 100-turn session against a 8k-window model never errors; a `compact_boundary` appears in the transcript; `tests/` covers `should_compact` firing + history shrink.

### T0.2 — Interactive permission "Ask" + load rules into the TUI · M · 🔴 (safety)
- **Why:** the engine gates (`agent.py` `check_permission` before dispatch, `Ask`/`Deny` honored) **but the TUI default mode is allow-all**: no rules loaded (`tui.py` builds `PermissionContext` with none), `Ask`→`Allow`, and `default`/`accept edits` are identical. So bash/write/edit run with **zero confirmation**.
- **Where:** `tui.py` `_permit` + `_build_session`/context construction; `permissions.py`; `settings.py` (allow/deny rules).
- **How:** (1) load `settings.json` allow/deny rules into the permission context. (2) In `default` mode, for a non-allowlisted mutating tool, render an interactive prompt (reuse the `_pick`/prompt_toolkit infra): *Allow once / Allow for session / Deny*. (3) Make `accept edits` actually differ — auto-allow file edits, still prompt for bash. (4) Port a minimal bash-command classifier + dangerous-pattern check from Claude's `bashClassifier.ts`/`dangerousPatterns.ts`.
- **Acceptance:** in default mode, a `bash("rm -rf …")` triggers a confirm prompt; deny surfaces as a `ToolResultBlock(is_error)`; allow-for-session is remembered.

### T0.3 — Truncate/elide tool results · S · 🔴
- **Why:** tool results are appended whole (`agent.py`); one `cat bigfile` or noisy build log blows the window in one turn.
- **Where:** the tool executor / result wrapper in `agent.py` (and/or `tools.py`).
- **How:** cap each tool result to N chars (Claude caps even git-status at 2000 with a "run it yourself" note). Keep head+tail, insert `… [N chars elided]`. Make the cap tool-aware (read/bash bigger than grep counts).
- **Acceptance:** a tool returning 1MB lands in history < ~8k chars; the model still sees a usable head/tail.

### T0.4 — Request prompt caching (`cache_control`) · S · 🟠 (cost/latency)
- **Why:** `budget.py` *prices* `cache_creation`/`cache_read` tokens but the client never *sets* `cache_control` → every turn re-bills the full prefix (~5–10×).
- **Where:** `providers/anthropic_passthrough.py` (and any OpenAI-compat that supports caching).
- **How:** send the system block as a content array with `cache_control: {type: "ephemeral"}` breakpoints on the system prompt and the last stable message. Keep the cache prefix (system + user context) stable across turns; don't mutate it mid-session.
- **Acceptance:** a 2-turn run reports non-zero `cache_read` tokens on turn 2.

### T0.5 — System-prompt env + git + directory block · M · 🔴 (coding quality)
- **Why:** the model runs **blind to repo state** — `_build_user_context` (`tui.py`) injects only `MEMORY.md`/`MANTIS.md`. No `<env>` (cwd/platform/OS), no git status/branch/recent commits, no directory tree. `build_live_context_block` exists in `system_reminder.py` but is **unwired**.
- **Where:** `tui.py` `_build_user_context` / `_default_system`; reuse `system_reminder.py`.
- **How:** prepend an `<env>` block (cwd, platform, today's date) + a git snapshot (`git branch`, `status --short`, last 5 commits, user) + a shallow directory listing. Cache per session; refresh git status on demand.
- **Acceptance:** the system prompt contains the current branch + cwd; verified by capturing `_build_user_context` output.

---

## 🟠 Tier 1 — High value. The biggest capability/UX jumps.

### T1.1 — `@`-file-mention autocomplete · M · 🟠
Type `@` to fuzzy-find a file and inject its path (and optionally content). **Absent** today (completer only handles `/`). **Where:** the prompt_toolkit completer in `tui.py`/`tui_fullscreen.py`. **How:** add an `@`-triggered completer backed by a fast file walk (respect `.gitignore`); on selection insert the relative path. **Acceptance:** typing `@ma` lists matching files; picking one inserts the path.

### T1.2 — `/init` + project-memory auto-load · M · 🟠
`/init` injects a canned prompt → agent writes `AGENTS.md`/`MANTIS.md` (build/test cmds + architecture). The **load-bearing half**: auto-load that file into the system prompt every session (cwd-walk, hierarchical like Claude's CLAUDE.md). **Where:** new slash handler in `tui.py`; loader in `project_memory.py` → `_build_user_context`. **Acceptance:** `/init` produces a file; next launch shows it injected.

### T1.3 — Wire skills (progressive disclosure + `load_skill` tool) · M · 🟠
`SkillRegistry` exists but the TUI never loads/injects it, and it dumps **full bodies** (expensive) instead of frontmatter-only. **Where:** `skills.py` (add SKILL.md frontmatter loader: `name`/`description`/`whenToUse`/`allowed-tools`), `agent.py`/`tui.py` (inject only the frontmatter catalog), `tools.py` (add a `load_skill` tool that pulls the body on demand). **Acceptance:** skills directory is discovered; only descriptions sit in context until `load_skill` is called.

### T1.4 — Cheap tool upgrades · S · 🟠
In `builtin_tools/fs.py`: **grep** → add `output_mode` (`content`/`files_with_matches`/`count`), `-A/-B/-C` context, `multiline`, `type` filter, `head_limit` (don't hardcode `-m 50`). **glob** → sort results by mtime (most-recent first). **bash** → `run_in_background` + a way to read backgrounded output. **Acceptance:** grep honors `output_mode=count`; glob returns newest-first; a backgrounded bash returns a handle.

### T1.5 — `/context` budget view + cost/context status line · M · 🟠
Show "how full is the window" + a running token tally (drop the `$`). **Where:** a `/context` handler that renders a per-category token breakdown; the `bottom_toolbar` (`tui.py`) gains tokens-used / context-% (data from `budget.py`/usage). **Acceptance:** `/context` prints token usage by category; the footer shows `N/Mk tokens`.

### T1.6 — Plan-mode approval handoff · M · 🟠
Read-only gating already works; add the **present-plan → user-approve → execute** transition (Claude's `ExitPlanMode` reads a plan file + flips out of read-only on approval). **Where:** `tui.py` mode handling + a plan file under `~/.mantis-agent/`. **Acceptance:** exiting plan mode shows the plan and asks to proceed; approving enables mutating tools.

### T1.7 — Structured compaction summary · S · 🟠
Swap `compact.py`'s "200–400 words of prose" prompt for Claude's 9-section format (Primary Request · Technical Concepts · Files & Code *with snippets* · Errors & Fixes · Pending Tasks · Current Work · Next Step). Preserves file/line/error fidelity across a resumed coding turn. **Acceptance:** a compacted summary contains file paths + the pending task.

### T1.8 — LSP tool · L · 🟠 (highest capability ceiling)
Semantic navigation grep can't do: goto-definition, find-references, hover, document/workspace symbols, call hierarchy. **Where:** new `builtin_tools/lsp.py` + an LSP client + per-language server discovery (pyright, rust-analyzer, tsserver…). Big, but the single largest capability gap for a coding agent. **Acceptance:** `lsp(op="references", symbol=…)` returns real references in a Python project.

---

## 🟡 Tier 2 — Nice-to-have / polish

**Near-free (S):** vim input mode + external editor ✅ SHIPPED (v1.19.0) · `/copy` last reply ✅ SHIPPED (v1.24.0) · todo state re-injected each turn ✅ SHIPPED (v1.25.0) · model fallback (`Agent(fallback_model=…)`, switch on overload via `retry.py`).

**Moderate (M):** `/diff` aggregate session changes (reuse `_render_diff`) · `/cost` token counter · `/export` transcript ✅ SHIPPED (v1.24.0) · `/memory` edit memory files in `$EDITOR` · `AskUserQuestion` tool ✅ SHIPPED (v1.12.0) · multimodal/notebook Read (image/PDF/`.ipynb`) · `NotebookEdit` tool · MCP **resources** (`resources/list`/`read`), **prompts** (`prompts/list`/`get`), **OAuth/bearer auth**, **progress** notifications · hooks: ✅ matchers + multiple-per-event SHIPPED (v1.23.0); shell hooks + fire-all-events + **shell/command hooks** + actually *fire* all 28 events (today only PreToolUse/PostToolUse/PermissionDenied dispatch) · microcompaction (cache-aware tool-result eliding, runs every turn, cheaper than full compaction) · word-level intra-line diff highlighting ✅ SHIPPED (v1.22.0) · distinct styled panel for API `ThinkingBlock`s · inline image rendering (iTerm/kitty protocol).

**Worktree / Sleep / Config tools (M/S):** `EnterWorktree`/`ExitWorktree` (isolated git worktree under `.mantis-agent/worktrees/`), `Sleep` (interruptible wait, no held shell), `Config` (model-driven settings get/set).

---

## ⚫ Skip — Anthropic-infra-specific, not worth it for an OSS local agent

Team/Task/Coordinator swarm tools · `SendMessage` (inter-agent sockets) · `ScheduleCron`/`RemoteTrigger` (hosted cron) · `Brief` (Kairos proactive channel) · `REPL` ("code-mode") · login/logout/feedback/extra-usage/install-slack-app/install-github-app/billing commands · PowerShell (unless Windows-native is a goal).

---

## Where mantis already matches or beats the reference (don't "fix" these)

Sessions fork/rewind/checkpoint (`session_tree.py`) · mid-stream **parallel** tool dispatch · OSS-model **text-tool-call salvage** + repeated-call circuit breaker (genuinely ahead) · tracing (`InMemoryTracer`/`OTelTracer`) · per-model `modelUsage` + budget · the diff/markdown/streaming rendering · plan-mode read-only gating (the *enforcement* exists; only the approval handoff is missing) · SDK message shapes 1:1.

---

## Suggested execution order

1. **T0.1 → T0.5** (one focused, tested release each — small diffs, plumbing exists).
2. **T1.1 (@-mentions) + T1.2 (/init + project memory)** — biggest UX jump.
3. **T1.4 (tool upgrades) + T1.5 (/context) + T1.7 (structured summary)** — quick quality lifts.
4. Then T1.3 (skills), T1.6 (plan handoff), and T1.8 (LSP) as larger efforts.

Ship each as its own version per the release workflow in `AGENTS.md`. Verify the wheel imports before publishing.
