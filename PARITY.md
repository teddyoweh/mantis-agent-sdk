# Claude Code / Agent SDK — parity notes

Research snapshot, August 2026, against the live docs at
[code.claude.com/docs](https://code.claude.com/docs) (247 pages; the docs moved
off `docs.claude.com` and the old URLs 301 to the new host) plus the
deobfuscated CLI source.

This is a working document for deciding what mantis builds next. It records
what they have, what we have, and — where it matters — the exact design
decision they made, because several of those are worth copying verbatim rather
than re-deriving.

---

## 1. Dynamic workflows

Their headline orchestration feature.
[docs](https://code.claude.com/docs/en/workflows)

**Shape.** Claude writes a **JavaScript script**; a runtime executes it in the
background, isolated from the conversation, while the session stays
responsive. Intermediate results live in *script variables*, not in a context
window — that's the whole point.

Their own comparison table is the clearest statement of why it exists:

|                       | Subagents        | Skills            | Agent teams        | Workflows              |
|-----------------------|------------------|-------------------|--------------------|------------------------|
| Who decides next step | Claude, per turn | Claude            | Lead agent         | **The script**         |
| Intermediate results  | Context window   | Context window    | Shared task list   | **Script variables**   |
| What's repeatable     | Worker defn      | Instructions      | Team defn          | **The orchestration**  |
| Scale                 | A few per turn   | Same              | A handful of peers | **Dozens to hundreds** |

**Script API** (from the saved-script example and the tool contract):

```javascript
export const meta = {
  name: 'audit-routes',
  description: 'Audit every route handler for missing auth checks',
}

const found = await agent('List every .ts file under src/routes/.', {
  schema: { type: 'object', required: ['files'],
            properties: { files: { type: 'array', items: { type: 'string' } } } },
})

const audits = await pipeline(found.files, file =>
  agent(`Audit ${file}.`, { label: file }),
)

return audits.filter(Boolean)
```

Plain JS with top-level `await`. `agent()` spawns one subagent (with `schema`
it returns a validated object); `parallel()` is a barrier; `pipeline()` runs
each item through all stages with no barrier between them; `phase()` groups for
the progress UI.

**Details worth copying exactly:**

- **Saved workflows become slash commands.** `s` in the `/workflows` view saves
  the run's script to `.claude/workflows/` (project, shared via git) or
  `~/.claude/workflows/` (personal). It then runs as `/<name>`. Plugins can
  ship them, namespaced `/<plugin>:<name>`. In a monorepo, the closest
  `.claude/workflows/` to the cwd wins.
- **`args`** — a saved workflow reads invocation input from a global `args`,
  passed as *structured data* (so `args.map(...)` works without parsing).
- **Resume rule.** Cached results stop at **the first agent that didn't
  finish**, and *every agent that started after it re-runs even if it
  completed*. Consequence they call out explicitly: many small agents preserve
  more progress across a stop than a few long ones. Resume only works within
  the same session.
- **Caps**: 16 concurrent agents (fewer on small machines), **1,000 agents per
  run**.
- **Size guideline** (`workflowSizeGuideline` setting or `/config`):
  `unrestricted` / `small` (<5) / `medium` (<15, the default) / `large` (<50).
  Advice to the model, not a cap.
- **Large-run warning** at >25 agents or >1.5M projected tokens, shown on the
  progress line. Advisory only; suppressed under ultracode.
- **`ultracode`** = `xhigh` effort + automatic workflow orchestration for every
  substantive task. The keyword is honoured **only from human-typed input** —
  not `-p`, not a scheduled task, not a webhook or PR comment relayed into the
  conversation. That boundary is a deliberate injection defence and is worth
  copying.
- **Approval**: a card listing the planned phases, with *Yes* / *Yes, don't ask
  again for this workflow in this project* / *View raw script* / *No*, and
  `Ctrl+G` to open the script in `$EDITOR`. Skipped entirely under bypass,
  `-p`, and the SDK.
- **Subagents inside a workflow always run `acceptEdits`** and inherit the tool
  allowlist, regardless of the session's mode. File edits auto-approve; shell,
  web fetch and MCP tools not in the allowlist can still prompt mid-run.
- Every run **writes its script to the session directory** and hands Claude the
  path, so you can read it, diff it against a prior run, or edit and relaunch.
- `/deep-research` ships as a bundled workflow.
- Off switches: `disableWorkflows` setting, `CLAUDE_CODE_DISABLE_WORKFLOWS=1`,
  `/config`, plus a managed-settings switch for orgs.

**Progress UI.** `/workflows` lists runs; selecting one shows phases with agent
count, token total and elapsed time; drilling in shows an agent's prompt,
recent tool calls and result. Keys: `↑↓` select, `Enter`/`→` drill,
`Esc`/`←` back, `j`/`k` scroll, `f` filter by status, `p` pause/resume, `x`
stop agent or run, `r` restart agent, `s` save as command. **And a one-line
progress summary lives in the task panel below the input box** — down-arrow to
focus, Enter to expand. That inline line is the thing to copy; our viewer is
currently only an overlay you have to open.

**Where mantis stands.** The engine (`workflow.py`) already has
`agent/parallel/pipeline/phase`, per-agent budgets, pause/resume/cancel/skip/
retry, a concurrency cap, run persistence and a real viewer. What's missing is
the *shape*: the model can only reach it through `coordinate(objective,
subtasks[])`, a fixed Research → Synthesis → Verification form. It cannot
express "loop finders until two consecutive dry rounds, dedup against
everything seen, then judge each survivor through three lenses."

Gaps, in dependency order:

1. Model-authored scripts (everything else depends on this)
2. Resume with a cached prefix + a journal of agent results
3. Saved, named, parameterized workflows as slash commands
4. Nested workflows; per-agent worktree isolation; lifetime caps; determinism
   guards (they ban `Date.now`/`Math.random` inside scripts *because* those
   would break resume)

Already at parity: per-agent structured output (`schema` → `response_format`),
phases, budgets, the viewer.

---

## 2. Agent teams

[docs](https://code.claude.com/docs/en/agent-teams) — experimental, gated
behind `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.

A second multi-agent primitive, **distinct from both subagents and workflows**:
peers, not workers. A lead session spawns teammates that are *full independent
Claude Code sessions*, each with its own context window, that **message each
other directly** and coordinate through a **shared task list** they self-claim
from (file locking prevents double-claims; completing a task auto-unblocks its
dependents).

Mechanics worth noting:

- Mailboxes are JSON files: `~/.claude/teams/{team}/inboxes/{agent}.json`.
  Team config at `~/.claude/teams/{team}/config.json`, task list at
  `~/.claude/tasks/{team}/`. Team name is `session-` + first 8 chars of the
  session id. Config is removed at session end; the task list persists so
  resumed sessions keep their tasks.
- Two display modes: **in-process** (teammates in an agent panel below the
  prompt — `↑↓` select, `Enter` to open a teammate's transcript *and type to
  message it*, `x` to stop, `Ctrl+T` for the task list) or **split panes**
  (tmux / iTerm2). `teammateMode` setting, `--teammate-mode` flag.
- Teammates can be instantiated **from subagent definitions** — define a role
  once, use it as both a delegated worker and a teammate.
- **Plan approval between agents**: a teammate can be required to plan in
  read-only mode and submit to the lead, which approves or rejects with
  feedback, autonomously.
- Hooks: `TeammateIdle`, `TaskCreated`, `TaskCompleted` — exit code 2 blocks
  the transition and feeds text back.
- Security detail worth copying: a message arriving over `SendMessage` is
  labelled as coming from *another Claude session, not the user*. A teammate
  can't approve a permission prompt on your behalf, and a denied teammate can't
  relay the action to another teammate to get it done.
- Honest limitations documented: no resume of in-process teammates, one team
  per session, no nested teams, lead is fixed, permissions set at spawn.

**mantis has nothing equivalent.** `/swarm` runs N isolated attempts and picks
a winner; `task` spawns workers that report back. Neither gives peers that talk
to each other over a shared task list.

---

## 3. The advisor tool

[docs](https://code.claude.com/docs/en/advisor) — experimental, Anthropic API
only.

Pair a cheap main model with a **stronger advisor model** that the main model
consults *at decision points it chooses*: before committing to an approach,
when an error keeps recurring, before declaring a task done. The advisor
receives the full conversation and returns guidance.

- `/advisor [model]`, `advisorModel` setting, `--advisor` flag,
  `/advisor off`, `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`.
- The advisor must be **at least as capable** as the main model; the pairing is
  validated before every request and silently not attached if it isn't.
- Subagents inherit the advisor and re-check the pairing against their own
  model.
- Toggling it does **not** invalidate the main model's prompt cache.
- Their pitch: "Haiku main + Opus advisor — lowest-cost main model with strong
  planning… lower than switching the main model to Sonnet or Opus."

**This is the single best fit for mantis on the whole list.** The product's
entire thesis is *any model, any provider, any self-host*, and its users run
small local models. "Run Qwen locally, escalate the three hard moments to Opus"
is a better version of this feature than Anthropic can ship, because we can
pair *across providers* — a local main model with a hosted advisor — which they
structurally cannot. It's also small: a tool that ships the transcript to a
second provider and returns guidance.

---

## 4. Everything else worth knowing

Confirmed **already shipped in mantis** (no action): structured outputs,
`modelUsage` cost breakdown, fallback model, budget ceilings, session
fork/resume, a session-store abstraction, compaction with a `PreCompact` hook,
the full hook event set, custom agent definitions, MCP (stdio/http/sse,
resources, prompts, elicitation, sampling), plan mode, checkpoint/rewind,
`AskUserQuestion`, background bash, `/goal`, `/loop`, notebook editing, vim
mode — and, as of this session, **tool search / deferred schemas**,
**OS sandboxing**, **scheduled runs**, and **headless `-p`** with
text/json/stream-json.

Genuinely missing, roughly by value:

| Thing | What it is | Why it might matter here |
|---|---|---|
| **Advisor** | Stronger model consulted at decision points | Cross-provider escalation; perfect fit for local-model users |
| **MCP OAuth** (`claude mcp login`) | Auth flow for remote MCP servers | Without it the entire hosted-MCP ecosystem is unreachable |
| **Agent teams** | Peer sessions, shared task list, direct messaging | The one multi-agent shape we don't have |
| **Auto mode** | A classifier decides permissions instead of prompting, with org-configurable trusted repos/domains and hard deny rules | The "no prompts, still safe" middle ground |
| **Auto memory** | Claude accumulates durable learnings across sessions unprompted | We have `/learn`; theirs is automatic |
| **Channels** | Webhooks / chat / alerts pushed *into* a live session via MCP | Turns the agent into something that reacts, not just runs |
| **Worktree flags** | `--worktree`, `.worktreeinclude`, per-subagent isolation | We have worktrees only inside `/swarm` |
| **Plugins + marketplaces** | Packaged skills/agents/hooks/MCP/workflows, with dependencies, relevance hints, `.zip`/URL install | How third parties extend it |
| **GitHub/GitLab CI** | PR review, autofix, issue triage, `/code-review`, `/security-review` | Distribution: "review every PR with a model you host" |
| **Statusline / output styles / themes** | Presentation customization | Polish |
| **Remote/mobile/web/teleport/Dispatch** | Move a session between surfaces | Large surface, large effort |

Two smaller things from the SDK reference worth stealing cheaply:

- **Subagent output scanning** — they neutralize control tags and turn markers
  in subagent output before it re-enters the parent's context, so a subagent
  (or a tool it read) can't inject instructions upward. We should do this.
- **Hook lifecycle events** (`HookStarted` / `HookCompleted`) and
  `ToolSearchResult` as a hook event.

---

## 5. Findings from the page-by-page sweep

Detail that a summary would have lost, and that changes what's worth building.

### Hooks: 32 events, and four handler *types*

We have 10 events and one handler type (a shell command). They have 32 events —
including `PermissionRequest`, `PermissionDenied`, `PostToolBatch`,
`InstructionsLoaded`, `MessageDisplay`, `ConfigChange`, `CwdChanged`,
`FileChanged`, `WorktreeCreate`/`Remove`, `PostCompact`, `Elicitation`,
`StopFailure`, `DirectoryAdded` — and four handler types:

| type | what it is |
|---|---|
| `command` | a shell command (what we have) |
| `http` | POST the payload to a URL, gated by `allowedHttpHookUrls` |
| `mcp_tool` | call an MCP tool, with `${tool_input.file_path}` substitution |
| `prompt` / `agent` | **run a model call as the hook** and read `{"ok":…,"reason":…}` |

Exit-code contract worth copying exactly: `0` = ok (stdout parsed as JSON;
plain stdout only becomes context for `UserPromptSubmit`/`SessionStart`),
`2` = **block, and stderr is fed to Claude**, anything else = non-blocking
error — *including 1*, which trips everyone up.

`PreToolUse` can return `permissionDecision` ∈ `allow|deny|ask|defer` with
precedence `deny > defer > ask > allow`, plus `updatedInput` to rewrite the
call. Matchers are exact-string when they contain only `[A-Za-z0-9_\- ,|]`
(with `|`/`,` as alternation) and an **unanchored regex** otherwise — so
`mcp__memory` matches nothing and you need `mcp__memory__.*`.

### Auto mode — their biggest 2026 behavioural addition

A **classifier model** (Sonnet 5, independent of `/model`) decides permissions
instead of prompting. Order: allow/ask/deny rules → read-only and
working-directory edits auto-approved → everything else to the classifier.

The design details are the interesting part:

- On entering auto mode, broad allow rules that grant arbitrary code execution
  (`Bash(*)`, `Bash(python*)`, package-manager runs, `Agent` allows) are
  **dropped**, and restored on exit.
- The classifier sees user messages, tool calls and CLAUDE.md — **tool results
  are stripped** (so a malicious file can't argue its way past it).
- Config (`autoMode.{environment,allow,soft_deny,hard_deny}`) is read **only**
  from user settings, managed settings and `--settings` — *never* from project
  or local settings, because a repo shouldn't be able to widen its own trust.
- Fallback: 3 consecutive or 20 total blocks pause auto mode.

This is the "no prompts, still safe" middle ground between our default posture
and `--godmode`, and it's a category we don't have at all.

### Permission rules are much richer than ours

- `Tool(param:value)` matching for deny/ask: `Agent(model:opus)`,
  `Bash(run_in_background:true)`, `Bash(dangerouslyDisableSandbox:true)`.
- `Edit(path)` rules cover **every** file-writing tool; a `Write(...)` path rule
  is accepted but never consulted (with a startup warning). A `Read` deny also
  blocks `Edit` on that path.
- Read/Edit specifiers use **gitignore syntax with four anchors** — `//abs`,
  `~/home`, `/relative-to-the-settings-file`, and bare/`./` relative to cwd.
  Note `/Users/alice/file` is *not* absolute in this scheme.
- Bash rules split on `&&`, `||`, `;`, `|`, `&` and newlines and must match
  **every** subcommand; wrappers `timeout`, `nice`, `nohup`, `xargs` etc. are
  stripped, but `npx`, `docker exec`, `find -exec` deliberately are not.
- A **protected path list** (`.git`, `.claude`, shell rc files, `.npmrc`,
  `.mcp.json`, pre-commit config…) that `permissions.allow` **cannot**
  pre-approve — the check runs before allow rules.

### Sandboxing goes deeper than what we shipped

Ours confines filesystem writes and optionally network. Theirs adds:
`credentials.envVars` with `mask` (requires TLS termination), `filesystem.
disabled` (network isolation only), `network.allowedDomains`/`strictAllowlist`,
`$TMPDIR` remapping per session, and a `dangerouslyDisableSandbox` escape hatch
on the Bash tool that Claude retries with — gated by
`Bash(dangerouslyDisableSandbox:true)` ask rules and
`allowUnsandboxedCommands: false`. Domain allowlisting is the obvious next step
for us.

### Subagents

Background-by-default with a restricted tool set; depth cap 3
(`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`), 200 per session, 20 concurrent.
Per-agent **memory scopes** (`~/.claude/agent-memory/<name>/`). `/subtask`
forks share the parent's prompt cache. `Explore`/`Plan` skip CLAUDE.md and git
status entirely.

**Output scanning (v2.1.210)**: subagent reports are scanned before the parent
reads them; control-tag imitations get a backslash inserted and a
`[harness: subagent output matched instruction-shaped pattern(s): …]` marker is
prepended. Nothing is removed or reworded. Cheap, and we should do it.

### Skills absorbed slash commands

`.claude/commands/deploy.md` and `.claude/skills/deploy/SKILL.md` both produce
`/deploy`; the skill wins a clash. They follow the [agentskills.io](https://agentskills.io)
open standard. Skills support `` !`command` `` blocks that execute *before* the
content reaches the model, a per-session listing budget
(`skillListingBudgetFraction`, default 1% of context), and **stacking**
(`/write-tests /fix-issue 123` loads both).

### How the live agent feed actually works

Three verified `Options` fields in the TypeScript SDK explain the progress UI
we were trying to reverse-engineer from the outside — they're the mechanism
behind per-agent rows updating mid-run:

| Option | Default | What it does |
|---|---|---|
| `agentProgressSummaries` | `false` | Generates **one-line progress summaries** for subagents and forwards them on `task_progress` events via a `summary` field. Foreground and background alike |
| `forwardSubagentText` | `false` | Forwards subagent **text and thinking blocks** as assistant/user messages with `parent_tool_use_id` set, so a consumer can render a nested transcript. Off by default, only `tool_use`/`tool_result` are emitted. All nesting depths from v2.1.219 |
| `includePartialMessages` | `false` | Streams token-level `stream_event`s |

Plus `Query.streamInput(stream: AsyncIterable<SDKUserMessage>): Promise<void>`
— how a message typed mid-turn merges into the *running* turn rather than
queueing as the next one.

That's the shape to copy for our own viewer: a per-agent one-line summary
channel separate from the transcript, and an explicit opt-in to forwarding
subagent prose upward (defaulting to off, so the parent's context isn't
flooded by workers' reasoning).

### Other deltas worth knowing

- **`settingSources` flipped default** in the SDK: omitting it now means
  `["user","project","local"]`, not "load nothing". Pass `[]` for a clean run.
- **Auto memory**: `~/.claude/projects/<project>/memory/` with a `MEMORY.md`
  index; only the first **200 lines or 25 KB** loads per session.
- New CLI flags worth stealing: `--bare` (skip all discovery), `--safe-mode`,
  `--max-budget-usd`, `--json-schema`, `--exec` (PTY-backed background job).
- Checkpoints: one per **user prompt**, newest 100 kept, `Esc Esc` on an empty
  prompt, and "Summarize from here / up to here" as rewind actions.
- The statusline command receives a **large JSON payload** on stdin — context
  window usage, rate-limit percentages and reset times, PR review state,
  worktree info. If we ever build a statusline, copy that payload shape.
- `settings.json` has ~128 top-level keys and there are ~280 environment
  variables. Managed settings **fail closed** on security fields and open on
  version pins.

---

## Recommended order for mantis

1. **Advisor** — smallest, best fit, and differentiated because we can pair
   across providers (a local main model with a hosted advisor is something they
   structurally can't ship).
2. **Model-authored workflow scripts + resume + saved workflows** — the big
   one; makes the engine we already have reachable by the model.
3. **MCP OAuth** (`mantis mcp login`) — unlocks the half of the MCP ecosystem
   we currently can't touch at all.
4. **Subagent output scanning** — small, security-relevant, no user-facing cost.
5. **Hook handler types** (`http`, `mcp_tool`, `prompt`) and the ~20 missing
   events — our hook engine is the right shape, it's just narrow.
6. **Auto mode** — a classifier-decided permission tier between "ask me" and
   "godmode". The largest single behavioural gap, and the design is documented
   well enough to copy (drop broad allow rules on entry, strip tool results
   from the classifier's view, never read the config from project settings).

Deliberately not chasing: statusline, output styles, themes, remote/mobile/web,
teleport, plugin marketplaces, Slack/Chrome integrations, artifacts. Large
surface, small return for this codebase's audience.
