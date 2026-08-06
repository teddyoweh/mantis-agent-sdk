# Claude Code 2.1.220 feature audit for Mantis

Source inspected: `/Users/teddy/Documents/code/claude-code-2.1.220-source/cli.bundle.js` and `workflow-excerpts.txt`.

This is an implementation-oriented comparison of the shipped Claude Code bundle against Mantis. The bundle is minified, so its stable evidence is unique literals and extracted byte-offset ranges rather than source line numbers.

## Executive summary

Mantis already has most of the underlying runtime primitives: provider-neutral agents, foreground and background subagents, a job manager, deterministic workflows, workflow history/resume, session trees, rewind, worktree swarms, skills, MCP, hooks, cron, watch jobs, plan mode, sandboxing, and both classic and fullscreen TUIs.

The highest-value remaining work is not “add workflows.” It is to unify existing runtime state into one navigable activity workspace, then add the genuinely missing execution shapes and safety boundaries.

## Confirmed Claude Code feature inventory

1. **Unified task/background-job lifecycle** — task IDs, bounded output, cancellation, output retrieval, progress events, and terminal task notifications.
2. **Dedicated subagent view** — agent identity, status line, model, transcript, limits, forwarding, and agent-view feature gates.
3. **Workflow workspace** — `/workflows`, concurrent runs, phase groups, agent drill-down, live progress, pause/stop/retry, and task linkage.
4. **Workflow scripting** — deterministic `agent`, `parallel`, `pipeline`, `phase`, and `log` primitives with pure-literal metadata.
5. **Workflow replay** — `runId`, script persistence, journal files, and longest-unchanged-prefix caching.
6. **Agent teams** — independent peer sessions, direct messages, shared dependency-aware tasks, in-process or split-pane displays, and plan approval.
7. **Session continuity** — resume, continue, switch, fork, child sessions, JSONL import, and transcript GC.
8. **Permission state machine** — manual, plan, accept-edits, auto, bypass, and don't-ask modes with machine-readable decision provenance.
9. **Typed hooks** — command, prompt, HTTP, MCP, and agent hooks; asynchronous hooks can wake the model.
10. **Skills and commands** — layered sources, policy gates, allowlists, context budgets, and shell-execution controls.
11. **Plugins/marketplaces** — packaged skills, agents, hooks, MCP servers, workflows, binary assets, install/sync, and policy controls.
12. **MCP lifecycle** — strict config, timeouts, connection batching, dynamic tool refresh, backgrounding, and large-output spill files.
13. **Sandbox/worktree integration** — isolation participates in permission decisions and UI diagnostics, rather than being only a launch option.
14. **Context and cost UX** — context usage, compaction, per-model usage, prompt-cache-aware behavior, effort, and status-line integration.
15. **Remote and multi-surface sessions** — managed session/event APIs, event streaming, remote control, IDE/desktop/mobile entrypoints, and session teleport concepts.
16. **Voice and rich input** — shipped voice-related gates/modules plus clipboard/image/file input paths.
17. **Vim/keybinding modes** — modal editing and configurable interaction mechanics.
18. **Scheduled and reactive operation** — scheduled tasks, background tasks, hooks, and inbound event/channel concepts.
19. **Browser/IDE integrations** — Chrome/MCP and IDE entrypoints are represented in runtime source categories and feature controls.
20. **Advisor/escalation** — a stronger model may be consulted at hard decision points without replacing the main model.

## What Mantis already has

| Capability | Mantis implementation |
|---|---|
| Background jobs | `mantis_agent/jobs.py` |
| Foreground/background subagents | `mantis_agent/subagent.py` |
| Persistent read-only twins | `mantis_agent/subagent.py` (`pair`) |
| Coordinator | `mantis_agent/coordinator.py` |
| Workflow engine and controls | `mantis_agent/workflow.py` |
| Named workflows | `mantis_agent/workflow_defs.py` |
| Workflow persistence/replay | `mantis_agent/workflow_store.py` |
| Workflow tool | `mantis_agent/workflow_tool.py` |
| Workflow/job viewers | `mantis_agent/tui.py` |
| Session DAG, resume, branch, rewind | `mantis_agent/session_tree.py` |
| File checkpoint rewind | `mantis_agent/tui.py` |
| Worktree swarm | `mantis_agent/tui_fullscreen.py` |
| Watch jobs | `mantis_agent/watch.py` |
| Autopilot/goal loop | `mantis_agent/tui_fullscreen.py` |
| Cron/schedules | `mantis_agent/cron.py` |
| Skills, memory, hooks, MCP | corresponding `mantis_agent/*` modules |
| Advisor | command and tool infrastructure already present |
| Fullscreen and classic terminal UIs | `mantis_agent/tui_fullscreen.py`, `mantis_agent/tui.py` |

## The core product opportunity: one Activity workspace

Mantis currently exposes related work through separate commands and overlays: `/jobs`, `/agents`, `/workflows`, `/twin`, `/swarm`, `/watch`, and `/cron`. The strongest transferable Claude Code idea is a single activity model, not another isolated feature.

### Proposed hierarchy

```text
Session
├── foreground turn
├── jobs
│   ├── subagent
│   ├── shell/background command
│   ├── watch
│   └── workflow
│       ├── phase
│       │   └── agent
│       └── phase
├── peer team
│   ├── teammate
│   └── shared tasks
└── schedules
```

Every node should expose a common envelope:

- stable ID and parent ID;
- kind, status, title, and description;
- start/end/elapsed time;
- model, provider, effort, tokens, and cost where applicable;
- current activity and recent events;
- transcript/event-log location;
- permissions and isolation mode;
- actions supported by that node.

### TUI behavior to copy

- Persistent one-line activity rail below the input box.
- Down-arrow or a dedicated key focuses the rail.
- Enter drills into the selected node.
- Left/Escape moves to its parent; right/Enter drills down.
- One feed can show all agents; filters narrow by run, phase, agent, status, or event type.
- Agent detail displays prompt, model/provider, current tool, tokens/cost, recent transcript, final result, and errors.
- Workflow detail shows phase groups and makes concurrent workflows ordinary sibling nodes.
- Actions are contextual: stop, pause, resume, retry, skip, message, approve plan, open transcript, or copy result.

## Highest-value gaps, in order

### 1. Unified activity registry and inline rail

Mantis has separate registries for jobs, workflow runs, and live subagent progress. Introduce a read model that projects all of them into one tree/event stream. Keep the existing engines; do not rewrite execution first.

Why first: it immediately improves workflows, tasks, watches, swarms, and future teams. It also creates the observability substrate needed to debug every later feature.

### 2. Durable, reattachable jobs

Current `JobManager` is session-local asyncio state and cancels leftovers when the TUI exits. Add append-only job event logs and a small durable registry, then permit reattachment after restart. Separate process execution is needed for jobs that truly outlive the terminal.

### 3. Peer agent teams

This is genuinely absent. Add independent peer sessions with:

- direct peer-to-peer messages;
- a durable shared task DAG with atomic claim/unclaim;
- dependency unblocking;
- lead-to-peer plan approval;
- source-labelled messages that cannot grant permissions;
- in-process activity-panel mode first, split-pane mode later.

Do not model teams as nested subagents. Peers need independent context, transcript, inbox, and lifecycle.

### 4. Model-authored workflow control flow

Mantis named workflows are declarative static graphs. Claude's dynamic workflow scripts add loops, conditionals, deduplication, voting, staged escalation, and novel orchestration. Implement only after establishing a constrained deterministic runtime:

- no ambient filesystem/network/process access;
- only orchestration primitives;
- deterministic inputs;
- script hashing and persisted source;
- append-only agent-result journal;
- cached-prefix replay;
- explicit lifetime and spawn limits.

### 5. Subagent-output boundary hardening

Before child output re-enters the parent, neutralize control tags, fake system/user/tool markers, and other instruction-shaped framing. Preserve the text as data and label it as an untrusted child report. This is small and security-critical.

### 6. Auto permission mode

Add a classifier-driven middle mode between prompting and bypassing. Preserve hard deny rules and record decision provenance. Sandbox state, repository trust, command decomposition, domain policy, and interaction requirements should be explicit classifier inputs.

### 7. Per-agent isolation everywhere

Generalize worktrees and subprocess isolation beyond `/swarm`:

- `task(... isolation="worktree")`;
- workflow agent isolation;
- optional subprocess execution;
- explicit cleanup and artifact merge behavior.

The existing `subprocess` and `remote` isolation enum values should not remain declarations that raise `NotImplementedError`.

### 8. MCP OAuth

Implement login, token refresh, secure storage, logout, and per-server auth status. Static headers are insufficient for the hosted MCP ecosystem.

### 9. Plugin packages and marketplaces

Package existing Mantis extension points—skills, commands, agents, hooks, MCP, and workflows—into a signed/versioned install unit with project/user scopes and policy controls. The mechanisms already exist; distribution and trust metadata are missing.

### 10. Reactive channels and remote surfaces

After durable jobs and the activity event model exist, expose controlled inbound channels and event streaming. Remote/mobile/browser control should consume the same session and activity event API rather than inventing another runtime.

## Ten concrete expert-level feature bases

1. **Activity graph:** one typed tree for all running work.
2. **Event journal:** append-only normalized lifecycle events for replay and UI.
3. **Inline task rail:** persistent compact status under the input.
4. **Agent cockpit:** drill-down transcript, prompt, tools, usage, permissions, and actions.
5. **Multi-run workflow board:** concurrent runs, phase grouping, filters, and controls.
6. **Durable job daemon:** survives terminal exit and supports reattachment.
7. **Peer team room:** inboxes, direct messages, shared task DAG, and plan approvals.
8. **Deterministic workflow VM:** constrained scripts with journals and cached replay.
9. **Trust/provenance engine:** permissions, sandbox, message source, and isolation surfaced uniformly.
10. **Remote event API:** one protocol for browser, IDE, mobile, and automation clients.

## Important design rule

Do not copy Claude Code's feature names as disconnected commands. Build shared primitives first:

- one event schema;
- one activity registry;
- one transcript abstraction;
- one permission provenance model;
- one isolation interface;
- one extension package format.

That turns workflows, teams, background jobs, remote control, and the TUI into views over the same system rather than five competing systems.
