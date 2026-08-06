# Unified Activity Graph and Inline Rail — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis-agent-sdk` library and `mantis` terminal
**Objective:** Project every concurrent execution engine in Mantis into one typed activity tree with one normalized event stream, then expose that tree as a persistent rail beneath the fullscreen input and a drill-down cockpit above it.

## 1. Executive summary

Mantis runs concurrent work through at least six independent engines, each with its own identity scheme, its own status vocabulary, its own retention policy, and its own viewer:

| Engine | Module | Identity | Status vocabulary | Retention |
|---|---|---|---|---|
| Background jobs | `mantis_agent/jobs.py` | `Job.id: int` from `itertools.count(1)` | `running / done / error / cancelled / timeout` | `_MAX_RETAINED_JOBS = 100`, `events` is `deque(maxlen=40)` |
| Workflow runs | `mantis_agent/workflow.py` | base36 string from `_b36(next(_WF_SEQ))` | per-`AgentRun` state | `_RECENT_CAP = 5` activity lines |
| Subagent task runs | `mantis_agent/subagent.py` | `_RUN_COUNTER = itertools.count(1)` | progress text only | none |
| Watches | `mantis_agent/watch.py` | borrows a `Job.id` | job status | job retention |
| Swarm candidates | `mantis_agent/swarm.py` | `SwarmCandidate` index | `SwarmResult` | none |
| Schedules | `mantis_agent/cron.py` | `CronJob.id` string, persisted | enabled/disabled + last run | file-backed |

The only cross-engine link that exists today is `Job.workflow_id`, a single string field whose docstring already concedes the problem: *"One piece of work, two windows onto it — /jobs for lifecycle, /workflows for structure."* That comment is the specification for this document, generalized. There should be one piece of work and one window, with lenses over it.

The consequence of six engines is not merely cosmetic. It means:

- `/jobs`, `/agents`, `/workflows`, `/twin`, `/swarm`, `/watch`, and `/cron` cannot answer "what is running right now" as a single question.
- A workflow agent that spawns a subagent produces two unlinked records.
- Cancellation is per-engine; cancelling a parent does not provably reap descendants.
- There is no replay. `Job.events` is a bounded in-memory deque; when the TUI exits, the history is gone.
- Every future feature — teams, durable jobs, remote surfaces, IDE panels — must either integrate with six engines or add a seventh.

This plan introduces a **read model**, not a new runtime. Existing engines keep executing work exactly as they do today. They gain a thin emission call. A registry consumes those emissions, assigns namespaced IDs, maintains parent links, and serves one tree plus one append-only event journal. Every viewer becomes a projection.

Build order is deliberate: contracts, then registry, then emission from existing engines, then journal, then rail, then cockpit, then actions. The rail is the visible payoff but it is the fifth step, not the first.

## 2. Goals

### User outcomes

- One line under the input box always shows what is running, with counts by state, and never scrolls away.
- Pressing a single key focuses the rail; arrow keys walk the tree; Enter drills into any node.
- A workflow's phases and agents, a task tool's subagent, a watch's event stream, and a shell job appear as siblings and children of one tree rather than as unrelated overlays.
- Drilling into any agent shows prompt, model, provider, effort, current tool, token and cost usage, permission mode, isolation mode, recent transcript, and final result.
- Contextual actions are offered only when the node supports them: stop, pause, resume, retry, skip, message, approve plan, open transcript, copy result.
- After a crash or restart, the same tree can be reconstructed from the journal and browsed read-only.
- `/jobs`, `/agents`, and `/workflows` continue to work and become saved filters over the same model.

### Engineering goals

- Reuse `msgspec.Struct` tagged unions exactly as `mantis_agent/events.py` already does (`tag_field="type"`), so the envelope encodes and decodes with the existing encoder infrastructure in `mantis_agent/types.py`.
- Reuse the parent-pointer JSONL pattern already proven in `mantis_agent/session_tree.py` (`TranscriptEntry` with `uuid` / `parent_uuid`, `build_chain`, `latest_leaf`). The activity journal is the same idea applied to execution rather than conversation.
- Do not modify execution semantics in `jobs.py`, `workflow.py`, or `subagent.py`. Emission must be additive and failure-isolated.
- Zero cost when unused: no journal file, no background task, and no measurable import cost when the registry has no subscribers.
- Keep the registry UI-independent so headless, SDK, IDE, and future remote consumers share it.
- Preserve Python 3.9–3.14 support; no `match`, no PEP 604 unions at runtime in new modules unless `from __future__ import annotations` is present as elsewhere in the codebase.

### Success metrics

- All six engines emit; `/jobs`, `/workflows`, and `/agents` render from the registry with no direct engine access.
- Tree construction for 10,000 journal events completes under 100 ms.
- Rail render cost stays under 2 ms per frame at 200 live nodes.
- No unbounded growth: memory is provably capped by node and event budgets under a 24-hour soak.
- Cancelling any node terminates all descendants, verified by a leak test that asserts zero surviving `asyncio.Task` objects.
- Journal replay of a recorded session reproduces the identical final tree, asserted byte-for-byte on a canonical serialization.

## 3. Non-goals

- Rewriting or merging the execution engines. `JobManager`, `Workflow`, and the task tool keep their own scheduling.
- Cross-session aggregation in phase 1. One session, one tree.
- Durable job supervision or process reattachment — that is `b_durable_jobs_and_reattachment.md`, which depends on this journal.
- Remote streaming of the event envelope — that is `m_session_event_api_and_remote_surfaces.md`.
- Replacing `SessionTranscript`. Conversation history and activity history stay separate stores with separate lifetimes.
- A general-purpose metrics or tracing backend. `mantis_agent/tracing.py` remains the tracing path; the registry emits spans into it rather than replacing it.
- Split-pane or multi-window layouts. The rail is one line; the cockpit is one overlay.

## 4. Current integration points

Modules that must be touched, and what each contributes:

- `mantis_agent/jobs.py` — `Job`, `JobManager.spawn/get/running/all/cancel/cancel_all`, `on_event` (terminal, once) and `on_stream` (mid-run, many). These two callbacks are already the emission seam; the registry subscribes to them.
- `mantis_agent/workflow.py` — `WorkflowRun`, `Phase`, `AgentRun` already implement `to_dict`/`from_dict` and `push_activity`. `Workflow._emit()` is the existing internal notification path; the registry hooks it. `stop/cancel/pause/resume/skip_agent/finish` are the action surface.
- `mantis_agent/workflow_tool.py` — `attach_job_progress(wf, job)` already bridges workflow runs to jobs; it becomes a registry emission instead of a bespoke bridge.
- `mantis_agent/subagent.py` — `_RUN_COUNTER`, `_update_job_progress(job, msg)`, `make_task_tool`, `make_pair_tool`, `make_job_output_tool`. Subagent runs currently borrow the parent job's progress field; they need their own nodes.
- `mantis_agent/coordinator.py` — `_with_progress`, `_build_workers` produce worker activity that is currently invisible outside the final report.
- `mantis_agent/watch.py` — `make_watch_tool(jobs)` streams via `JobManager.emit`; each batch becomes an event.
- `mantis_agent/swarm.py` — `SwarmCandidate` / `SwarmResult`; each candidate becomes a child node.
- `mantis_agent/cron.py` — `CronJob`, `due_jobs`, `run_job`, `tick`; scheduled runs attach as roots with a schedule provenance.
- `mantis_agent/events.py` — the tagged-union encoding precedent for the envelope.
- `mantis_agent/session_tree.py` — the parent-pointer JSONL precedent for the journal.
- `mantis_agent/tracing.py` — span emission.
- `mantis_agent/tui.py` (6,647 lines) and `mantis_agent/workflow_view.py` (542 lines) — existing viewers to be reimplemented as projections.
- `mantis_agent/tui_fullscreen.py` (4,232 lines) — the live UI; the rail lands here, below the input box.
- `mantis_agent/headless.py` — machine-readable projection.
- `mantis_agent/settings.py` — `load_settings`, `merge_settings`, `apply_settings_to_options` for the configuration layer.

Do not grow `tui.py` or `tui_fullscreen.py` further. Add `mantis_agent/activity/` and a thin render module.

## 5. Product model

### The tree

```text
session                                   ← root, one per Session
├── turn (foreground)                     ← one per user prompt
│   ├── tool: bash "pytest -q"
│   ├── tool: task → subagent "Explore"   ← child node, own transcript
│   └── tool: workflow → workflow run
│       ├── phase "Review"
│       │   ├── agent "review:bugs"
│       │   └── agent "review:perf"
│       └── phase "Verify"
│           └── agent "verify:auth.py"
├── job #3 (background shell)
├── job #4 (watch: pytest --lf)
│   └── stream batches (events, not nodes)
├── swarm "refactor-auth"
│   ├── candidate 1 (worktree)
│   └── candidate 2 (worktree)
├── team "release-cut"                    ← later; see c_agent_teams.md
│   ├── peer "infra"
│   └── peer "docs"
└── schedule "nightly-triage"
    └── run 2026-08-03T02:00
```

### Node envelope

Every node, regardless of engine, exposes the same fields. This is the single most important contract in the plan.

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | Namespaced, globally unique — see §6 |
| `parent_id` | `str \| None` | `None` only for the session root |
| `kind` | `str` | `session / turn / tool / subagent / workflow / phase / agent / job / watch / swarm / candidate / team / peer / schedule / run` |
| `title` | `str` | One-line human label, ≤ 80 chars |
| `detail` | `str` | Optional second line |
| `status` | `str` | Unified vocabulary — see below |
| `created_at` | `float` | Wall clock, epoch seconds |
| `started_at` | `float \| None` | Monotonic-derived, wall-clock-normalized |
| `ended_at` | `float \| None` | |
| `model` | `str` | Where applicable |
| `provider` | `str` | Where applicable |
| `effort` | `str` | Where applicable |
| `usage` | `Usage \| None` | Reuse `mantis_agent/types.py::Usage` |
| `cost_usd` | `float \| None` | |
| `activity` | `str` | Current one-line activity |
| `recent` | `tuple[str, ...]` | Bounded ring of recent activity lines |
| `transcript_ref` | `str \| None` | Path or session id for drill-down |
| `permission_mode` | `str` | From `mantis_agent/permissions.py` context |
| `isolation` | `str` | `none / worktree / subprocess / remote` |
| `source` | `str` | Provenance: `user / model / hook / schedule / channel / peer` |
| `actions` | `frozenset[str]` | What this node actually supports |
| `error` | `str \| None` | Terminal error text, redacted |

### Unified status vocabulary

Existing engines disagree. `Job.status` uses `running / done / error / cancelled / timeout`. Workflow agents carry a different set. Normalize to:

```text
pending    → created, not yet started
running    → actively executing
paused     → suspended, resumable
blocked    → waiting on permission, approval, or a dependency
done       → completed successfully
error      → completed with failure
cancelled  → terminated by user or parent
timeout    → terminated by deadline
skipped    → deliberately bypassed
```

Provide `mantis_agent/activity/status.py` with explicit mapping functions per engine — `from_job_status`, `from_agent_run` — rather than scattering translation at call sites. `blocked` and `skipped` are new and matter: `blocked` powers the "why is nothing happening" question, and `skipped` already exists implicitly via `Workflow.skip_agent`.

### Rollup semantics

A parent's displayed status derives from children when the parent has no intrinsic status of its own (`phase`, `swarm`, `team`). `Phase.roll_up()` in `workflow.py` already implements exactly this for one engine; generalize it:

- Any child `running` → parent `running`.
- Else any child `blocked` → parent `blocked`.
- Else any child `error` → parent `error`.
- Else all children terminal → parent `done`.
- Empty parent → `pending`.

Nodes with intrinsic status (`job`, `agent`) never roll up; they report their own.

## 6. Identity and ID scheme

Three incompatible ID spaces exist. Do not renumber them — external references (`/job 3`, `run 4f2a`) are user-visible and must keep working.

Namespace instead:

```text
<kind-prefix>:<engine-local-id>
```

```text
ses:01J8...          session (reuse session_tree.new_session_id())
trn:7                foreground turn ordinal
job:3                JobManager Job.id
wfr:4f2a             WorkflowRun base36 id
wfp:4f2a/Review      phase, scoped by run
wfa:4f2a/a7          AgentRun id, scoped by run
sub:12               subagent _RUN_COUNTER
swm:refactor-auth    swarm
cnd:refactor-auth/2  candidate
cro:nightly-triage   CronJob.id
run:nightly-triage/1722650400   scheduled execution
```

Rules:

- IDs are opaque strings to consumers. Never parse them outside `activity/ids.py`.
- Provide `parse_id(s) -> tuple[str, str]` and `make_id(kind, local) -> str` in one module with exhaustive tests.
- Engine-local IDs are reused across sessions (both `Job.id` and `_RUN_COUNTER` restart at 1 per process). The journal therefore keys on `(session_id, node_id)`; the registry is per-session and may use the short form internally.
- `Job.workflow_id` becomes redundant once emission exists but must remain populated for one release for backward compatibility, then be deprecated.
- Resolution helper `resolve_ref(text)` accepts user shorthand (`3`, `#3`, `4f2a`) and disambiguates against live nodes, so `/job 3` and rail navigation share one lookup.

## 7. Event envelope

### Encoding

Follow `mantis_agent/events.py` precisely: `msgspec.Struct`, `frozen=True`, `tag_field="type"`, one struct per event type. This gives free JSON encode/decode, discriminated decoding, and consistency with the streaming event types the codebase already ships.

```python
class NodeCreated(msgspec.Struct, frozen=True, tag="node_created", tag_field="type"):
    seq: int
    ts: float
    node_id: str
    parent_id: str | None
    kind: str
    title: str
    detail: str = ""
    source: str = "model"
    model: str = ""
    provider: str = ""
    isolation: str = "none"

class NodeStatus(msgspec.Struct, frozen=True, tag="node_status", tag_field="type"):
    seq: int
    ts: float
    node_id: str
    status: str
    error: str | None = None

class NodeActivity(msgspec.Struct, frozen=True, tag="node_activity", tag_field="type"):
    seq: int
    ts: float
    node_id: str
    text: str

class NodeUsage(msgspec.Struct, frozen=True, tag="node_usage", tag_field="type"):
    seq: int
    ts: float
    node_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

class NodeAction(msgspec.Struct, frozen=True, tag="node_action", tag_field="type"):
    seq: int
    ts: float
    node_id: str
    action: str          # stop | pause | resume | retry | skip | message | approve
    actor: str           # user | parent | policy | schedule
    detail: str = ""
```

Additional types: `NodeAttached` (late parent discovery), `NodeMetric`, `NodeArtifact`, `JournalHeader`.

### Ordering and sequencing

- `seq` is a monotonic per-session counter assigned by the registry, not by emitters. Emitters are unordered; the registry serializes.
- `ts` is wall-clock epoch seconds for display. Engines internally use `time.monotonic()` (`Job.started`, `AgentRun.elapsed_ms`); normalize once at the registry boundary using a captured `(monotonic_origin, wallclock_origin)` pair so replayed timestamps are stable.
- Events for a node may arrive before its `NodeCreated` if an engine emits progress eagerly. The registry buffers orphans in a bounded pending map keyed by `node_id` and attaches on creation. Orphans older than a configurable window are dropped with a counted diagnostic rather than retained forever.

### Bounding

Unbounded event streams are the primary memory risk. Every bound is explicit and configurable:

| Bound | Default | Rationale |
|---|---|---|
| `maxLiveNodes` | 2000 | Live tree size |
| `maxRecentPerNode` | 8 | Generalizes `_RECENT_CAP = 5` and `deque(maxlen=40)` |
| `maxJournalBytes` | 32 MB | Rotate, do not truncate mid-line |
| `maxEventsPerSecondPerNode` | 50 | Coalesce activity beyond this |
| `terminalNodeRetention` | 200 | Generalizes `_MAX_RETAINED_JOBS = 100` |
| `orphanBufferSize` | 256 | Pending events awaiting a parent |

Activity coalescing matters for watches: `watch.py` already batches with `_BATCH_WINDOW_S = 0.2` and `_MAX_BATCH_LINES = 40`. The registry applies a second coalescing pass so a chatty watch cannot flood the journal.

## 8. Architecture

### Package layout

```text
mantis_agent/activity/
  __init__.py          # public surface: ActivityRegistry, ActivityNode, subscribe
  ids.py               # make_id / parse_id / resolve_ref
  status.py            # unified vocabulary + per-engine mappers
  events.py            # msgspec envelope structs
  registry.py          # ActivityRegistry: in-memory tree + subscribers
  journal.py           # append-only JSONL writer/reader, rotation, replay
  projections.py       # jobs view, workflows view, agents view, filters
  actions.py           # action dispatch to owning engines
  emit.py              # thin helpers engines call; no-op when no registry
  render.py            # text formatting shared by rail, cockpit, headless
```

### `ActivityRegistry`

Responsibilities, and nothing beyond them:

- Assign `seq`, normalize timestamps, validate envelopes.
- Maintain `nodes: dict[str, ActivityNode]` and `children: dict[str, list[str]]`.
- Apply rollup on ancestor chains when a child transitions.
- Fan out to subscribers (rail, cockpit, journal, tracing, headless, future remote).
- Enforce bounds and eviction.
- Serve queries: `tree()`, `node(id)`, `descendants(id)`, `roots()`, `filter(pred)`, `counts()`.

Explicitly not responsible for: executing anything, owning tasks, or deciding UI layout.

Construction is explicit, not a module-level singleton:

```python
registry = ActivityRegistry(session_id=session.id, config=ActivityConfig.from_settings(settings))
jobs = JobManager(on_event=registry.job_terminal, on_stream=registry.job_stream)
```

A process-level accessor may exist for convenience, but ownership must be traceable to a session so tests and SDK embedders can run isolated registries concurrently.

### Subscribers

```python
class ActivitySubscriber(Protocol):
    def on_event(self, ev: ActivityEvent) -> None: ...
```

Subscribers are synchronous and must be fast; slow work belongs in a queue owned by the subscriber. A raising subscriber is logged, counted, and — after a threshold — detached. Follow the precedent already set in `JobManager._fire_on_event`, which deliberately swallows callback errors so a broken notifier cannot turn a completed job into a failure. The registry adopts the same rule and adds the detach threshold.

### Emission helpers

`activity/emit.py` provides the only API engines touch:

```python
def node_created(reg, node_id, parent_id, kind, title, **kw) -> None
def node_status(reg, node_id, status, error=None) -> None
def node_activity(reg, node_id, text) -> None
def node_usage(reg, node_id, usage) -> None
```

Each accepts `reg=None` and returns immediately. That keeps the diff in `jobs.py` and `workflow.py` to single guarded lines and guarantees zero cost when the feature is off.

### Journal

Mirror `session_tree.SessionTranscript`:

- One JSONL file per session at `~/.mantis/activity/<session-id>.jsonl` (resolve via `mantis_agent/paths.py`).
- Append-only, one event per line, `msgspec` encoded.
- First line is a `JournalHeader` with schema version, session id, cwd, and the `(monotonic_origin, wallclock_origin)` pair.
- Atomic append with a single `write()` of a complete line; never partial-line flush.
- Rotation at `maxJournalBytes` to `<session-id>.1.jsonl`; retain two generations.
- Reader tolerates a truncated final line (crash during write) by discarding it, exactly as a JSONL transcript reader must.
- `replay(path) -> ActivityRegistry` reconstructs the tree read-only, with all nodes forced terminal-or-unknown since no engine is attached.

Writing is opt-in via `activity.journal: true`. When disabled there is no file handle and no I/O.

## 9. Engine integration

Each engine gets a minimal, reviewable diff.

### `jobs.py`

`JobManager` already has the two callbacks. Widen them rather than adding new ones:

- `spawn()` emits `NodeCreated(kind="job", parent_id=<current turn>)` and `NodeStatus("running")`.
- `emit()` — the existing mid-run streaming path — additionally emits `NodeActivity`.
- Terminal transition emits `NodeStatus` mapped through `status.from_job_status`.
- `cancel()` / `cancel_all()` emit `NodeAction(action="stop", actor="user")` before terminating.

`Job.workflow_id` stops being the linkage mechanism; `parent_id` is. Keep the field populated for one release.

### `workflow.py`

`Workflow._emit()` already fires on every material change and `AgentRun.to_dict()` already produces a serializable snapshot. Convert:

- `Workflow.__init__` → `NodeCreated(kind="workflow")`, parented to the spawning tool node.
- `phase()` / `_get_phase()` → `NodeCreated(kind="phase")`.
- Agent start → `NodeCreated(kind="agent")` carrying model, provider, effort, isolation.
- `AgentRun.push_activity` → `NodeActivity`.
- `_ingest` → `NodeUsage` from the `BudgetTracker`.
- `_finalize` → terminal `NodeStatus`.
- `stop/pause/resume/cancel/skip_agent/finish` → `NodeAction` plus resulting `NodeStatus`.

`attach_job_progress` in `workflow_tool.py` becomes a two-line registry parenting call.

### `subagent.py`

This is where the real gap is: subagent runs have `_RUN_COUNTER` identity but no node. `_update_job_progress(job, msg)` currently writes progress onto the *parent job*, collapsing child into parent.

- `make_task_tool` emits `NodeCreated(kind="subagent")` with `parent_id` = the tool node that invoked it, plus the resolved `AgentType.name`, model, and tool allowlist size.
- `_update_job_progress` additionally emits `NodeActivity` against the subagent's own node.
- `make_pair_tool` (persistent read-only twins) emits a long-lived node so `/twin` becomes a filter.
- `make_job_output_tool` reads through the registry rather than `JobManager` directly.

### `watch.py`, `swarm.py`, `coordinator.py`, `cron.py`

- Watches: each `_BATCH_WINDOW_S` batch → one coalesced `NodeActivity`; timeout → `NodeStatus("timeout")`.
- Swarm: `SwarmCandidate` → child node with `isolation="worktree"`; `SwarmResult` → terminal status plus `NodeMetric` for files-changed from `_count_files_changed`.
- Coordinator: `_with_progress` already wraps a progress callback; route it to `NodeActivity` for each worker.
- Cron: `run_job` creates a root node with `source="schedule"` and a `parent_id` of `None`, since a scheduled run is not caused by a turn.

## 10. Security and trust

Activity data is displayed prominently and persisted. Treat it as untrusted.

- **Model-controlled strings.** `title`, `detail`, and `activity` originate from tool arguments, agent prompts, and child agent output. Sanitize before display: strip ANSI escapes, C0/C1 control characters, bidi overrides (U+202A–U+202E, U+2066–U+2069), and zero-width characters; cap length; collapse newlines. A subagent must not be able to redraw the rail or forge a second node's line.
- **No authority from activity.** Nothing read out of the registry may widen a permission, approve a plan, or select a tool. The registry is display and coordination state only. This is the same rule `e_subagent_trust_limits_and_isolation.md` applies to child reports, and it must hold here because the rail is a place where child-authored text reaches the user.
- **Secret redaction.** Reuse and extend the redaction already present in `workflow_store.redact_inputs` and its `_SECRET_HINTS`. Every string field is redacted on the way *into* the registry, not on the way out, so a subscriber cannot observe an unredacted value.
- **Path and ID safety.** Journal filenames derive from session IDs; sanitize with the existing `_SAFE_ID` pattern from `workflow_store.py` (`[^A-Za-z0-9._-]`) before touching the filesystem. Never interpolate a node title into a path.
- **Action authorization.** `NodeAction` records who acted. Model-initiated stop/retry of a node it does not own must be rejected; a node may act on itself and its descendants only. Record refusals as `NodeAction` with a denial detail so the audit trail shows attempts.
- **Journal permissions.** Create the activity directory `0o700` and files `0o600`, consistent with other Mantis state.
- **Fail open for display, closed for control.** A registry failure must never break execution — emission is wrapped and swallowed. But if the registry cannot confirm ownership for an action, the action is refused.

## 11. Configuration

```json
{
  "activity": {
    "enabled": true,
    "rail": true,
    "railPosition": "below-input",
    "journal": false,
    "journalDir": null,
    "maxLiveNodes": 2000,
    "maxRecentPerNode": 8,
    "maxJournalBytes": 33554432,
    "journalGenerations": 2,
    "terminalNodeRetention": 200,
    "coalesceWindowMs": 200,
    "maxEventsPerSecondPerNode": 50,
    "orphanBufferSize": 256,
    "showCost": true,
    "collapseCompleted": true,
    "defaultFilter": "active"
  }
}
```

Environment overrides, following existing convention (`MANTIS_AGENT_DISABLE_WORKFLOWS` in `workflow_tool.py` is the precedent):

- `MANTIS_ACTIVITY=0|1`
- `MANTIS_ACTIVITY_RAIL=0|1`
- `MANTIS_ACTIVITY_JOURNAL=0|1`
- `MANTIS_ACTIVITY_DIR`

Resolve through `settings.load_settings` / `merge_settings` so the standard precedence applies. Project-level settings may narrow but never widen retention or enable the journal in a directory the user has not trusted.

## 12. TUI integration

### The rail

One line, always present, directly beneath the input box in `tui_fullscreen.py`:

```text
▸ 3 running · 1 blocked · 12 done      wfr:4f2a Review 2/5 · job:3 pytest · sub:12 Explore
```

Rules:

- Renders in under 2 ms; precomputed by the registry's `counts()` and a bounded top-N slice, never by walking the full tree per frame.
- Blocked nodes always win a slot — the rail's most valuable job is answering "why is nothing happening."
- Truncates from the right with an explicit `+N` overflow marker.
- Hidden entirely when nothing has ever run, so a plain chat session is unchanged.
- Respects `NO_COLOR` and degrades to ASCII markers on terminals without box drawing, consistent with existing detection in the TUI.

### Focus and navigation

- `Ctrl+G` (or a configurable action; see `q_keybindings_and_modal_editing.md`) focuses the rail.
- `Down` from an empty input focuses the rail — cheap and discoverable.
- `Left` / `Escape` → parent. `Right` / `Enter` → drill down. `Tab` → next sibling.
- `Escape` at root returns focus to the input.
- Focus state lives in the view, never in the registry.

### The cockpit

An overlay showing one node in full:

```text
agent wfa:4f2a/a7 — review:bugs                       running · 42s
  parent   wfp:4f2a/Review
  model    claude-opus-5   provider anthropic   effort high
  perms    acceptEdits     isolation worktree
  usage    12.4k in · 3.1k out · 0.9k cache · $0.14
  tool     read_file mantis_agent/agent.py
  recent
    reading mantis_agent/agent.py
    reading mantis_agent/permissions.py
    scanning for unchecked tool results
  actions  [s]top  [m]essage  [t]ranscript  [c]opy result
```

Only actions in the node's `actions` set are drawn. Never advertise an operation the owning engine cannot perform — that rule is inherited directly from the browser plan's "avoid advertising unavailable operations."

### Feed and filters

One chronological feed across all nodes, filterable by `run`, `phase`, `agent`, `kind`, `status`, and `source`. `/jobs`, `/agents`, and `/workflows` become named filters:

- `/jobs` → `kind in {job, watch}`
- `/agents` → `kind in {subagent, agent, peer}`
- `/workflows` → `kind == workflow`, grouped by phase

Keep the commands. Users should not notice the reimplementation except that the views now agree with each other.

### Headless and SDK

- `headless.py` gains `--activity-events` emitting the envelope as newline-delimited JSON on the existing stream.
- SDK consumers subscribe directly: `registry.subscribe(fn)`.
- Never print the rail to a non-TTY.

## 13. Actions

`activity/actions.py` maps a node to its owning engine:

| Action | Applicable kinds | Implementation |
|---|---|---|
| `stop` | job, watch, workflow, agent, subagent, swarm, peer | `JobManager.cancel`, `Workflow.stop`, `Workflow.cancel(agent_id)` |
| `pause` | workflow | `Workflow.pause` |
| `resume` | workflow | `Workflow.resume` |
| `skip` | agent | `Workflow.skip_agent` |
| `retry` | agent, job | Re-dispatch with recorded inputs |
| `message` | subagent, peer | Deliver to the target's inbox |
| `approve` | node in `blocked` on a plan | Resolve the pending approval |
| `transcript` | any with `transcript_ref` | Open in the transcript viewer |
| `copy` | any terminal | Copy result via `mantis_agent/clipboard.py` |

Requirements:

- Actions are idempotent. Stopping a stopped node succeeds silently.
- Cancellation is recursive and provable: `stop(node)` cancels descendants depth-first, then the node, then asserts no descendant remains non-terminal within a timeout, escalating to a hard cancel and a logged diagnostic.
- Every action emits `NodeAction` before attempting and a resulting `NodeStatus` after, so the journal explains state changes.
- `retry` requires the engine to have recorded sufficient inputs. Where it has not (arbitrary coroutines in `JobManager.spawn`), `retry` is absent from `actions` rather than failing at press time.

## 14. Errors

```text
ActivityError                    (base)
├── UnknownNodeError
├── DuplicateNodeError
├── InvalidParentError           # cycle or wrong-kind parent
├── NodeLimitExceededError
├── ActionNotSupportedError
├── ActionNotAuthorizedError
├── JournalWriteError
├── JournalCorruptError          # recoverable; truncate tail and continue
├── JournalVersionError          # schema version newer than reader
└── SubscriberError              # counted, then detach
```

Registry errors must be non-fatal to execution. Journal errors disable journaling for the session with one user-visible warning rather than repeated noise.

## 15. Delivery phases

### Phase 0 — Design spike

1. Prototype `ActivityNode` / envelope structs and confirm `msgspec` tagged-union round-trip across the Python matrix.
2. Instrument `JobManager` alone; drive a real session; measure event volume for a chatty watch.
3. Prototype the rail render and measure per-frame cost at 200 nodes.
4. Confirm the monotonic-to-wallclock normalization survives suspend/resume on macOS.
5. Decide rollup ownership: registry-computed versus engine-reported.

**Exit:** stable envelope; measured event volume; rail cost under budget; no execution regression.

### Phase 1 — Registry and contracts

1. Add `activity/` with `ids.py`, `status.py`, `events.py`, `registry.py`, `emit.py`.
2. Implement bounds, eviction, orphan buffering, and rollup.
3. Implement subscriber fan-out with error counting and detach.
4. Unit-test every state transition, bound, and mapping function.
5. No UI, no journal, no engine changes yet.

**Exit:** registry is fully tested standalone; imports cost nothing when unused.

### Phase 2 — Engine emission

1. Wire `jobs.py` through the existing `on_event` / `on_stream` seam.
2. Wire `workflow.py` through `_emit`, `push_activity`, `_ingest`, `_finalize`.
3. Wire `subagent.py` with genuine child nodes.
4. Wire `watch.py`, `swarm.py`, `coordinator.py`, `cron.py`.
5. Assert parity: every record previously visible in `/jobs` and `/workflows` is present in the registry.

**Exit:** one tree contains all work; existing viewers still read their own engines; both agree.

### Phase 3 — Projections replace viewers

1. Implement `projections.py` and `render.py`.
2. Reimplement `/jobs`, `/agents`, `/workflows` as filters.
3. Remove direct engine access from `tui.py` and `workflow_view.py`.
4. Add the feed and filter grammar.
5. Add headless JSON output.

**Exit:** no viewer touches an engine directly; `Job.workflow_id` is unused by UI.

### Phase 4 — Rail and cockpit

1. Add the rail to `tui_fullscreen.py` with focus handling.
2. Add the cockpit overlay and contextual actions.
3. Add `actions.py` with recursive cancellation and authorization.
4. Add accessibility behavior: screen-reader announcements for state changes, ASCII fallback, `NO_COLOR`.
5. Add configuration and environment overrides.

**Exit:** navigate and control every engine from the rail; actions are authorized and recursive.

### Phase 5 — Journal and replay

1. Implement `journal.py` with rotation, atomic append, and truncated-tail tolerance.
2. Implement `replay()` and a read-only browsing mode.
3. Test abrupt termination during every mutating transition.
4. Add retention and pruning, following `workflow_store.prune_runs`.
5. Add schema versioning and forward-compatibility rules.

**Exit:** a killed session's tree is fully reconstructable; replay is byte-stable.

### Phase 6 — Hardening

1. Adversarial review: control-character and bidi injection through titles; forged node IDs; action authorization bypass.
2. Fuzz the journal reader with truncated, reordered, and malformed lines.
3. Soak test for 24 hours; assert bounded memory and no task leaks.
4. Load test 200 concurrent nodes with a chatty watch.
5. Remove experimental status.

## 16. Testing strategy

### Unit

- `ids.py`: round-trip, malformed input, `resolve_ref` ambiguity.
- `status.py`: exhaustive mapping from every engine vocabulary, including unknown values.
- Rollup: all nine status combinations across empty, mixed, and terminal children.
- Bounds: node limit, recent ring, orphan buffer overflow, rate coalescing.
- Envelope: msgspec encode/decode for every struct on Python 3.9 and 3.14.
- Sanitization: ANSI, C0/C1, bidi, zero-width, oversize, newline collapse.
- Redaction: secrets in titles, details, activity, and errors.
- Subscriber failure: raises, counts, detaches at threshold.

### Integration

- Real `JobManager` with a fake coroutine: spawn, stream, terminal, cancel.
- Real `Workflow` with a deterministic fake runner: phases, parallel agents, pause/resume/skip.
- Task tool producing a genuine child node under its invoking tool node.
- Watch producing coalesced activity under load.
- Cron run appearing as a root with `source="schedule"`.
- Cross-engine: a workflow agent that spawns a subagent that spawns a shell job — three levels, correct parents.

### End-to-end

- Full TUI session: rail appears, focuses, navigates, drills, acts.
- `/jobs`, `/agents`, `/workflows` agree with the tree.
- Headless JSON stream is well-formed and complete.
- Kill -9 mid-workflow; replay reconstructs the tree.
- Concurrent workflows plus watches plus swarm without ID collision.

### Security

- Subagent emits a title containing ANSI cursor movement and a fake second rail line; assert neutralized.
- Node attempts an action on a non-descendant; assert refused and recorded.
- Journal path traversal via a hostile session id; assert sanitized.
- Secrets in workflow inputs never reach the journal.

### Performance and reliability

- Rail render at 200 nodes under 2 ms.
- Tree build from 10,000 events under 100 ms.
- 24-hour soak: bounded memory, bounded journal, zero leaked tasks.
- Cancellation storm: cancel 100 nodes with descendants; assert full reaping.

### CI

- Registry and projection tests run in the default suite.
- PTY-based rail tests follow the existing convention of synchronizing on footer hints rather than the boot summary, which is a known race in the fullscreen PTY tests.
- Soak and load tests run in a separate nightly job.

## 17. Documentation

- `docs/guides/activity.md` — the tree, the rail, navigation, filters, actions.
- `docs/guides/activity-journal.md` — enabling, retention, replay, privacy.
- `docs/api/activity.md` — `ActivityRegistry`, `ActivityNode`, envelope structs, subscriber protocol.
- Update `docs/guides/workflows.md` to describe workflows as a projection.
- Migration note for SDK users reading `JobManager` directly.
- Update `web/lib/docsNav.ts` and `mkdocs.yml`.

## 18. File-level implementation map

New:

- `mantis_agent/activity/__init__.py`
- `mantis_agent/activity/ids.py`
- `mantis_agent/activity/status.py`
- `mantis_agent/activity/events.py`
- `mantis_agent/activity/registry.py`
- `mantis_agent/activity/journal.py`
- `mantis_agent/activity/projections.py`
- `mantis_agent/activity/actions.py`
- `mantis_agent/activity/emit.py`
- `mantis_agent/activity/render.py`
- `tests/test_activity_ids.py`
- `tests/test_activity_registry.py`
- `tests/test_activity_rollup.py`
- `tests/test_activity_bounds.py`
- `tests/test_activity_journal.py`
- `tests/test_activity_emission.py`
- `tests/test_activity_actions.py`
- `tests/test_activity_security.py`
- `tests/test_activity_rail_pty.py`
- `docs/guides/activity.md`
- `docs/api/activity.md`

Modified:

- `mantis_agent/jobs.py` — emission at spawn, emit, terminal, cancel
- `mantis_agent/workflow.py` — emission at phase, agent, ingest, finalize, controls
- `mantis_agent/workflow_tool.py` — replace `attach_job_progress` bridging
- `mantis_agent/subagent.py` — real child nodes
- `mantis_agent/coordinator.py`, `watch.py`, `swarm.py`, `cron.py` — emission
- `mantis_agent/tui.py` — viewers become projections
- `mantis_agent/workflow_view.py` — projection
- `mantis_agent/tui_fullscreen.py` — rail and cockpit
- `mantis_agent/headless.py` — JSON events
- `mantis_agent/settings.py` — activity config
- `mantis_agent/tracing.py` — span bridge
- `mantis_agent/__init__.py` — public exports
- `tests/public_api_surface.txt` — intentional update

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Emission slows the hot loop | Guarded no-op helpers; synchronous subscribers must be trivial; measured in Phase 0 |
| Event volume explodes on watches | Coalescing at both the watch batch window and the registry rate limit |
| Memory grows unbounded | Explicit node, recent, orphan, and retention caps, all tested |
| Three ID spaces collide | Namespaced IDs; single parse/format module; collision tests |
| Monotonic/wallclock skew corrupts replay | Normalize once at the boundary with a recorded origin pair |
| Rail steals input focus | Focus is explicit; Escape always returns to input; no autofocus on new nodes |
| Child-authored text redraws the terminal | Sanitize on ingest, not on render |
| Registry becomes a god object | Strict scope: no execution, no ownership of tasks, no UI decisions |
| Viewers regress during reimplementation | Phase 2 runs both paths and asserts parity before Phase 3 removes the old one |
| Journal corrupts on crash | Whole-line atomic append; reader discards truncated tail |
| Existing PTY tests flake harder | Synchronize on footer hints, per the known boot-summary race |
| `Job.workflow_id` removal breaks integrators | Keep populated one release, then deprecate with a changelog note |

## 20. Acceptance checklist

- [ ] Every engine emits; the tree contains all concurrent work.
- [ ] IDs are namespaced, stable, and never parsed outside `ids.py`.
- [ ] Status vocabulary is unified with tested per-engine mappers.
- [ ] Rollup matches `Phase.roll_up` behavior and generalizes correctly.
- [ ] Bounds are enforced and tested for nodes, recents, orphans, rate, and retention.
- [ ] Subscriber errors are isolated, counted, and detached.
- [ ] Titles and activity are sanitized against control, ANSI, and bidi injection.
- [ ] Secrets are redacted on ingest.
- [ ] Actions are authorized, idempotent, recursive, and never advertised when unsupported.
- [ ] `/jobs`, `/agents`, `/workflows` are projections and agree with each other.
- [ ] Rail renders under budget, degrades on limited terminals, and hides when idle.
- [ ] Cockpit shows prompt, model, usage, permissions, isolation, recents, and result.
- [ ] Journal is optional, rotated, crash-tolerant, and replays byte-stably.
- [ ] Headless emits machine-readable events.
- [ ] Core imports work with activity disabled at zero cost.
- [ ] Docs, changelog, and public API snapshot updated intentionally.
- [ ] `ruff check` and the full pytest suite pass.

## 21. Recommended implementation order

1. Land `ids.py`, `status.py`, and `events.py` alone, with exhaustive tests. These are pure and cheap to get right.
2. Land `registry.py` with bounds and rollup, still with no consumers.
3. Wire `jobs.py` only. Ship it. Verify parity against `/jobs` in real use.
4. Wire `workflow.py` and `subagent.py` — the two engines whose missing linkage motivates the whole plan.
5. Wire the remaining engines.
6. Reimplement the three existing viewers as projections and delete the duplicated traversal code.
7. Add the rail. It is now cheap because the model already exists.
8. Add the cockpit and actions, with recursive cancellation proven by leak tests.
9. Add the journal last; it is the only piece with durability semantics and it benefits from a settled envelope.
10. Harden, then unlock `b_durable_jobs_and_reattachment.md` and `m_session_event_api_and_remote_surfaces.md`, both of which consume this journal rather than inventing their own.
