# Peer Agent Teams — Extensive Implementation Plan

**Status:** Proposed
**Target:** A new `mantis_agent/teams/` package, built on session, activity, and subagent primitives
**Objective:** Add long-lived peer agents with independent sessions, durable inboxes, direct messaging, a dependency-aware shared task DAG with atomic claims, and lead-to-peer plan approval — without modeling peers as nested subagents.

## 1. Executive summary

Mantis has three ways to run more than one agent, and none of them is a team.

**Subagents** (`subagent.py`) are parent-owned, single-shot, and hierarchical. A `task` call spawns a child, the child runs to completion, its final text returns as a tool result, and the child ceases to exist. It has no inbox, no ability to initiate contact, and no life beyond the call.

**The coordinator** (`coordinator.py`) fans out to workers and synthesizes their reports. `_build_workers` is explicit about the limitation in a comment: *"Self-contained prompt: the worker starts fresh with no memory of this conversation, so fold the overall objective into every task."* Workers are stateless, mutually invisible, and terminate at fan-in. This is map-reduce, not collaboration.

**Workflows** (`workflow.py`) orchestrate agents deterministically through phases. The script decides everything; agents do not talk to each other. Coordination is a program, not a conversation.

The missing shape is a peer: an agent with its own persistent session and transcript, its own context that accumulates, an inbox it can be messaged at, the ability to message others, and a lifetime that spans many turns. Three peers working a release — one on infrastructure, one on docs, one on tests — need to hand work to each other, ask each other questions, and see a shared task list. None of the three existing mechanisms expresses that.

There is a hint that this was anticipated: `HOOK_EVENTS` in `hooks.py` includes `TeammateIdle`, declared for upstream parity and listed in `RESERVED_EVENTS` because the runtime does not fire it. That event only makes sense in a peer model.

The design rule that matters most here is stated in the audit and bears repeating: **do not model teams as nested subagents.** Peers need independent context, transcript, inbox, and lifecycle. Building them as a variation on `task` would inherit the parent-owned, single-shot, tool-result-return shape that is precisely what makes subagents unsuitable.

What teams *should* reuse is the session layer. `session_tree.py` already provides independent transcripts (`SessionTranscript`), parent-linked entries, `branch_session`, `load_for_resume`, and `list_sessions`. A peer is a session. That is the whole architectural insight, and it means teams are mostly coordination glue over machinery that exists.

## 2. Goals

### User outcomes

- Start a team with named roles and give each a durable brief.
- Message a specific peer, or broadcast, and have them respond in their own context.
- See a shared task list where peers claim work, complete it, and unblock each other.
- Have a lead peer propose a plan and require approval before peers execute it.
- Watch all peers in one panel, drill into any one's transcript, and take over its input.
- Resume a team after closing the terminal, with each peer's context intact.
- Trust that a peer cannot escalate its own permissions or impersonate the user.

### Engineering goals

- A peer is a `Session`. Reuse `session_tree.py` rather than inventing peer persistence.
- Teams project into the activity graph from `a_activity_graph_and_inline_rail.md` as ordinary nodes; `/team` is a view, not a new registry.
- Messages are data, never authority — the same rule child reports follow.
- Reuse the shared agent concurrency ceiling from `e_subagent_trust_limits_and_isolation.md`; peers are agents and count against it.
- Durable by default. A team's task DAG and inboxes survive restart.
- In-process activity-panel mode first; split-pane later. No terminal multiplexer dependency.
- Python 3.9–3.14.

### Success metrics

- A three-peer team completes a dependency-ordered task set with no duplicated work, proven by atomic-claim stress tests.
- Team state survives `kill -9` and resumes with every peer's transcript and inbox intact.
- No message from one peer can widen another's permissions, approve a plan on its behalf, or forge a sender.
- Deadlock (all peers blocked on each other) is detected and reported within a bounded interval.
- Peer idle cost is near zero — an idle peer must not poll or consume tokens.

## 3. Non-goals

- Replacing subagents, the coordinator, or workflows. All three remain, and a peer may use all three internally.
- Multi-user collaboration. One user, several agents.
- Cross-machine teams. Peers are local processes or tasks; distribution is `m_session_event_api_and_remote_surfaces.md`'s problem later.
- Automatic team formation. The user (or a lead peer with approval) defines the roster.
- Consensus protocols or leader election. The lead is assigned, not elected.
- A general actor framework. Scope is bounded to agent peers with inboxes and a shared task list.

## 4. Current integration points

- `mantis_agent/session_tree.py` (630 lines) — `SessionTranscript` (`append_message`, `append_meta`, `set_title`, `record_last_prompt`, `load`, `messages`), `TranscriptEntry`, `new_session_id`, `branch_session`, `rewind_chain`, `list_sessions`, `load_for_resume`, `iter_session_files`. This is the peer persistence layer.
- `mantis_agent/session.py` (941 lines) — `Session`, `SessionStore`, `SqliteSessionStore`, `Checkpoint`. The SQLite pattern is the model for the team store.
- `mantis_agent/subagent.py` — `AgentType`, `discover_agent_types`, `resolve_agent_tools`, `_SUBAGENT_EXCLUDED_TOOLS`, `make_pair_tool` / `_TWIN_SYSTEM` (the persistent read-only twin — the closest existing thing to a peer, and a useful precedent for a long-lived secondary agent).
- `mantis_agent/coordinator.py` — `_build_workers`, `_format_report`, `make_coordinate_tool`; teams should be able to hand off to a coordinator fan-out.
- `mantis_agent/agent.py` — the agent loop each peer runs.
- `mantis_agent/permissions.py` — per-peer contexts, narrowing inheritance.
- `mantis_agent/hooks.py` — `TeammateIdle` (reserved), `SubagentStart` / `SubagentStop`.
- `mantis_agent/activity/` — team, peer, and task nodes.
- `mantis_agent/jobs.py` — peers run as durable jobs; `b_durable_jobs_and_reattachment.md` provides supervision.
- `mantis_agent/isolation/worktree.py` — peers editing in parallel need separate checkouts.
- `mantis_agent/tui_fullscreen.py` — the team panel.

## 5. Product model

### Objects

```text
Team
├── id, name, objective, created_at, status
├── roster: [Peer]
├── tasks: TaskDAG
├── channel: broadcast message log
└── policy: permissions, budget, isolation defaults

Peer
├── id, name, role, brief
├── session_id  ──────────────→ SessionTranscript (independent)
├── inbox: durable message queue
├── status: idle | working | blocked | awaiting_approval | stopped
├── current_task: task_id | None
├── agent_type, model, tools, permission_mode, isolation
└── budget allocation

Task
├── id, title, description, created_by
├── depends_on: [task_id]
├── status: open | claimed | in_progress | blocked | review | done | failed
├── assignee: peer_id | None
├── claim: (peer_id, claimed_at, lease_expires)
├── artifacts: [refs]
└── result
```

### Peers are sessions

Each peer gets a real session id from `new_session_id()` and a real `SessionTranscript`. This gives, for free:

- Independent, accumulating context.
- A durable transcript inspectable with existing tooling.
- Resume via `load_for_resume`.
- Branching and rewind via `branch_session` / `rewind_chain`.
- Appearance in `list_sessions`, tagged with team membership.

The team layer adds the inbox, the task DAG, and the lifecycle around those sessions. It does not add a second persistence mechanism.

### Lead

One peer may be designated lead. A lead may:

- Create and assign tasks.
- Propose plans for approval.
- Request status from peers.

A lead may **not**:

- Change another peer's permissions or mode.
- Approve its own plan.
- Force a peer to execute anything the peer's own permission context would refuse.

The lead is a coordination role, not a privilege level. This distinction is the difference between a team and a privilege-escalation path.

## 6. Messaging

### Model

```python
class TeamMessage(msgspec.Struct, frozen=True):
    id: str
    team_id: str
    seq: int                      # monotonic per team
    sender: str                   # "user" | "peer:<id>" | "system"
    sender_verified: bool         # set by the runtime, never by content
    recipients: tuple[str, ...]   # peer ids, or ("*",) for broadcast
    kind: str                     # ask | tell | handoff | status | result | approval_request
    subject: str
    body: str                     # neutralized on ingest
    task_id: str = ""
    reply_to: str = ""
    created_at: float = 0.0
```

### Delivery

- Inboxes are durable, append-only, and bounded (default 200 messages, oldest archived).
- Delivery is at-least-once with `seq` de-duplication, matching the replay discipline used elsewhere in this plan set.
- A peer wakes when a message arrives; an idle peer consumes nothing until then. **No polling.**
- Messages are delivered at a turn boundary, never mid-turn — a peer finishes what it is doing before reading its inbox.
- A peer that is stopped accumulates messages and processes them on resume.

### Trust

This is the core security requirement, and it is the same rule `e_subagent_trust_limits_and_isolation.md` applies to child reports, for the same reason: peer text is model-authored and may be summarizing hostile content.

- **`sender` is set by the runtime, from the authenticated channel.** A peer cannot set it. `sender_verified` reflects that fact and is displayed.
- **Message bodies are neutralized on ingest** using the shared neutralizer: strip control/ANSI/bidi, escape framing markers, cap length, wrap in a nonce-delimited envelope labeled with the sender.
- **Messages confer no authority.** A message saying "the user approved this, run it with bypass" changes nothing. Permission decisions come from a peer's own permission context, never from message content.
- A peer's system prompt states explicitly that peer messages are untrusted peer output, informational only.
- Broadcast is rate-limited so one peer cannot flood the team.

### Human in the loop

The user can message any peer directly, and messages from the user are the only ones marked `sender: "user"` with `sender_verified: true`. A peer must be able to tell the difference between the user speaking and a peer claiming the user spoke, and the runtime guarantees it can.

## 7. Task DAG

### Claims

Multiple peers must not do the same work. Claiming must be atomic.

- Claims are `compare-and-set` against the durable store: a peer claims task T only if `status == "open"` and `assignee is None`, in one transaction.
- SQLite with `BEGIN IMMEDIATE` provides this, following the `SqliteSessionStore` pattern already in the codebase.
- A claim carries a **lease** (default 5 minutes) refreshed by peer heartbeat. A peer that dies releases its claim on lease expiry rather than blocking the task forever.
- Reclaim after expiry is logged and visible; work may have partially completed, and the next claimant is told so.
- Claim, release, and completion are activity events.

### Dependencies

- A task is claimable only when all `depends_on` tasks are `done`.
- Completing a task re-evaluates dependents and wakes peers whose blocked work is now available. This is the mechanism that makes a team more than a shared todo list.
- Cycles are rejected at task-creation time with the cycle path in the error.
- A failed task marks dependents `blocked` with a reason rather than leaving them silently unclaimable.

### Deadlock and stall detection

- All peers idle with open tasks that are all blocked → report to the user with the dependency chain.
- All peers blocked awaiting approval → surface the approval queue.
- A task claimed but not progressed within a threshold → flag it.
- `TeammateIdle` (already declared in `HOOK_EVENTS`) fires when a peer has no claimable work, which is the natural hook for a user-supplied assignment policy. Wiring it moves the event from `RESERVED_EVENTS` to `DISPATCHED_EVENTS`.

## 8. Plan approval

A peer proposing consequential work should get sign-off before executing.

- A peer emits `approval_request` with a plan, affected files, and estimated cost.
- Routed to the user by default, or to the lead when `policy.approver == "lead"`.
- The peer's status becomes `awaiting_approval`; it does no work while waiting and consumes nothing.
- Approval is recorded with approver identity, timestamp, and the plan's content hash. **An approved plan that is then edited requires re-approval** — otherwise approval is meaningless.
- Denial returns feedback; the peer revises and may re-request, subject to a retry cap.
- Approval authorizes a *plan*, never a permission mode. Each tool call inside the plan still goes through the peer's permission context. This is the same layering the permission plan applies everywhere: approval at one level does not substitute for enforcement at another.

## 9. Execution and isolation

### Peer runtime

Two modes:

- **In-process** (phase 1): each peer is an `asyncio.Task` running its own agent loop against its own session. Cheap, simple, shares the provider connection pool.
- **Worker process** (phase 4): each peer is a supervised worker from `b_durable_jobs_and_reattachment.md`. Survives terminal exit; crash-isolated.

Both count against the shared `maxConcurrentAgents` ceiling.

### Filesystem isolation

Peers editing the same tree concurrently is a data race. Options per team policy:

| Mode | Behavior |
|---|---|
| `shared` | One tree; peers coordinate through tasks. Default for read-heavy teams |
| `worktree` | Each peer gets its own checkout via `isolation/worktree.py` |
| `worktree-per-task` | A fresh worktree per claimed task |

With `worktree`, integration is explicit: a peer completes a task producing a diff, and merging is a separate reviewed step. **Never auto-merge** — the same rule as subagent worktrees, for the same reason.

### Budget

- A team has a total budget; each peer receives an allocation.
- A peer exhausting its allocation goes `blocked` and requests more rather than silently stopping.
- Team budget is reserved against session budget so a team cannot consume everything unnoticed.
- Per-peer and per-team spend visible in `/team`.

### Permissions

- Each peer has its own `PermissionContext`, derived from the team policy, which is derived from the session's — **narrowing only**, never widening.
- `session_allows` are not shared between peers. An approval the user gave one peer does not authorize another; this is the same confused-deputy reasoning that keeps session allows out of subagent inheritance.
- A peer cannot modify any peer's permissions, including its own.
- Approval prompts state which peer is asking.

## 10. Surface

```text
/team new <name> --objective "..."
/team add <role> [--agent-type T] [--model M] [--isolation worktree]
/team start | pause | stop
/team status
/team msg <peer> <text>
/team broadcast <text>
/team tasks
/team task add "<title>" [--depends <id,...>] [--assign <peer>]
/team task claim|done|fail <id>
/team approve <request-id> | /team deny <request-id> "<feedback>"
/team focus <peer>          take over a peer's input directly
/team transcript <peer>
/team resume <team-id>
/team archive <team-id>
```

The panel, rendered through the activity graph:

```text
team release-2.62                                    running · 12m
  peers
    lead     ● working   task#4 cut release notes         $0.42
    infra    ● working   task#2 bump versions   worktree  $1.10
    docs     ○ blocked   waiting on task#2                $0.18
    tests    ◐ approval  plan: rewrite fixtures           $0.31
  tasks   2 done · 1 in progress · 1 blocked · 3 open
  inbox   docs←infra(1)  lead←tests(1)
  budget  $2.01 / $10.00
```

Drilling into a peer shows its transcript, inbox, current task, and permission mode. `/team focus` lets the user drive one peer directly, which is the escape hatch when a peer is stuck and explaining is slower than doing.

## 11. Persistence

```text
~/.mantis/teams/<team-id>/
  team.json           roster, objective, policy, status
  tasks.db            SQLite: tasks, claims, dependencies (atomic CAS)
  inbox/<peer>.jsonl  append-only durable inbox
  channel.jsonl       broadcast log
  approvals.jsonl     requests and decisions with content hashes
```

Peer transcripts live in the normal session location and are referenced by id, not duplicated.

Requirements:

- `team.json` written atomically (temp + fsync + rename), like every other durable record in this plan set.
- Inbox and channel are append-only with whole-line writes; truncated tails discarded on read.
- `tasks.db` uses `BEGIN IMMEDIATE` for claims; schema versioned like `session.py`'s `_SCHEMA`.
- Directory `0o700`, files `0o600`.
- Resume reconstructs peers from transcripts and replays inboxes from the last processed `seq`.
- Retention and archival, following `workflow_store.prune_runs`.

## 12. Configuration

```json
{
  "teams": {
    "enabled": false,
    "maxPeers": 6,
    "maxTeams": 2,
    "defaultIsolation": "shared",
    "claimLeaseSeconds": 300,
    "heartbeatSeconds": 30,
    "inbox": {"maxMessages": 200, "maxBodyBytes": 16384, "broadcastPerMinute": 6},
    "approval": {"approver": "user", "maxRetries": 3, "requireForWrites": true},
    "budget": {"perTeamUsd": 10.0, "perPeerUsd": 3.0},
    "stall": {"detectSeconds": 300, "noProgressSeconds": 600},
    "runtime": "inproc",
    "persistDays": 30
  }
}
```

`teams.enabled` defaults to `false`. Teams are the most resource-intensive feature in this plan set and should be opted into.

Environment: `MANTIS_TEAMS=0|1`, `MANTIS_TEAMS_MAX_PEERS`.

## 13. Errors

```text
TeamError                        (base)
├── TeamNotFoundError
├── PeerNotFoundError
├── PeerLimitExceededError
├── TaskCycleError                # carries the cycle path
├── TaskClaimConflictError
├── TaskLeaseExpiredError
├── TaskDependencyBlockedError
├── InboxFullError
├── MessageTooLargeError
├── SenderForgeryError            # a peer tried to set sender
├── ApprovalRequiredError
├── ApprovalStaleError            # plan changed after approval
├── PeerPermissionWideningError
├── TeamBudgetExhaustedError
├── TeamDeadlockError
└── TeamStoreCorruptError
```

## 14. Delivery phases

### Phase 0 — Design spike

1. Prototype a peer as a `Session` + agent loop and confirm transcript independence.
2. Validate atomic claims under 20 concurrent claimants with SQLite `BEGIN IMMEDIATE`.
3. Measure idle peer cost; confirm zero polling.
4. Design message neutralization reusing the child-report neutralizer.
5. Decide in-process versus worker for phase 1.

**Exit:** peers persist independently; claims are provably atomic; idle cost is zero.

### Phase 1 — Team runtime and peers

1. Add `teams/` with team, peer, and store modules.
2. Peers as sessions with independent transcripts.
3. In-process peer execution against the shared concurrency ceiling.
4. Per-peer permission contexts with narrowing-only inheritance.
5. Team, peer, and task nodes in the activity graph.

**Exit:** a two-peer team runs, each with its own accumulating context.

### Phase 2 — Messaging

1. Durable inboxes with bounded size and archival.
2. Runtime-set, verified senders; forgery refused.
3. Neutralization on ingest with the shared neutralizer.
4. Turn-boundary delivery and wake-on-message.
5. `/team msg`, `/team broadcast`, user-to-peer messaging.

**Exit:** peers communicate; no message confers authority; senders cannot be forged.

### Phase 3 — Task DAG

1. SQLite task store with atomic CAS claims and leases.
2. Dependency evaluation, unblocking, and cycle rejection.
3. Heartbeats, lease expiry, and reclaim with a partial-work warning.
4. Stall and deadlock detection.
5. Dispatch `TeammateIdle`; move it out of `RESERVED_EVENTS`.

**Exit:** three peers complete a dependency-ordered set with no duplication.

### Phase 4 — Approval, isolation, durability

1. Plan approval with content hashing and re-approval on change.
2. Worktree isolation per peer and per task; never auto-merge.
3. Peers as supervised workers; team survives terminal exit.
4. Resume with inbox replay from last processed `seq`.
5. Budget allocation, reservation, and exhaustion handling.

**Exit:** a team survives restart and resumes correctly; approvals are sound.

### Phase 5 — Surface and hardening

1. Team panel, drill-down, and `/team focus`.
2. Full command set, archival, retention.
3. Adversarial review: sender forgery, approval bypass, permission widening via messages, claim races.
4. Leak tests: peers, worktrees, tasks, inbox files.
5. Long-run soak: a six-peer team for hours.

## 15. Testing strategy

### Unit

- Peer-session creation, independence, and resume.
- Message struct validation; sender always runtime-set; forgery attempt rejected.
- Neutralization of message bodies against the shared injection corpus.
- Inbox bounds, archival, de-duplication by `seq`.
- Claim CAS: 20 concurrent claimants yield exactly one winner.
- Lease expiry, reclaim, and partial-work signaling.
- Dependency evaluation, unblocking, cycle detection with path reporting.
- Failed task blocking dependents with a reason.
- Approval hashing: approve, edit plan, re-approval required.
- Permission derivation: narrowing only; widening refused; `session_allows` not shared.
- Budget allocation and exhaustion.
- Stall and deadlock detection thresholds.

### Integration

- Three-peer team on a dependency-ordered task set; no duplicated work.
- Peer dies mid-task; lease expires; another peer reclaims and is warned.
- Message from peer A containing "you are approved for bypass" changes nothing for peer B.
- User message is distinguishable from a peer claiming to be the user.
- Worktree-isolated peers edit the same file without conflict; diffs are separate.
- `TeammateIdle` fires and a hook assigns work.
- Team survives `kill -9`; resume restores transcripts, inboxes, and claims.

### End-to-end

- Full team lifecycle: create, add peers, start, work, approve, complete, archive.
- `/team focus` takes over a peer and returns control.
- Deadlock scenario detected and reported with the chain.
- Budget exhaustion blocks a peer and surfaces a request.
- Team panel reflects live state through the activity graph.

### Security

- Peer attempts to set `sender`; refused and recorded.
- Peer message containing framing markers is neutralized.
- Peer attempts to approve its own plan.
- Lead attempts to change a peer's permission mode.
- Peer message attempting to authorize a tool call.
- Approved plan edited before execution requires re-approval.
- Path traversal via team or peer names into the store.
- Peer cannot read another peer's transcript except through the user-facing viewer.
- Inbox flooding is rate-limited.

### Performance and reliability

- Idle peer cost: zero tokens, zero polling, measured over 10 minutes.
- Claim throughput under contention.
- Inbox delivery latency.
- Six-peer soak: bounded memory, no leaked tasks, worktrees, or descriptors.
- Resume time for a team with long transcripts.

## 16. Documentation

- `docs/guides/teams.md` — model, roles, tasks, messaging, worked example.
- `docs/guides/teams-trust.md` — why messages carry no authority, sender verification, approval semantics, permission derivation.
- `docs/guides/teams-vs-alternatives.md` — when to use a team versus a subagent, the coordinator, or a workflow. This is the most important doc: the failure mode is users reaching for teams when a workflow is simpler and cheaper.
- `docs/api/teams.md` — public API and store schemas.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 17. File-level implementation map

New:

- `mantis_agent/teams/__init__.py`
- `mantis_agent/teams/types.py` — `Team`, `Peer`, `Task`, `TeamMessage`
- `mantis_agent/teams/store.py` — team.json, tasks.db, inbox files
- `mantis_agent/teams/runtime.py` — peer lifecycle and scheduling
- `mantis_agent/teams/mailbox.py` — inboxes, delivery, de-duplication
- `mantis_agent/teams/tasks.py` — DAG, claims, leases, dependencies
- `mantis_agent/teams/approval.py`
- `mantis_agent/teams/policy.py` — permissions, budget, isolation derivation
- `mantis_agent/teams/panel.py` — rendering
- `mantis_agent/teams/commands.py`
- `tests/test_team_store.py`
- `tests/test_team_peers.py`
- `tests/test_team_mailbox.py`
- `tests/test_team_claims.py`
- `tests/test_team_dependencies.py`
- `tests/test_team_approval.py`
- `tests/test_team_permissions.py`
- `tests/test_team_security.py`
- `tests/test_team_resume.py`
- `docs/guides/teams.md`
- `docs/guides/teams-trust.md`
- `docs/guides/teams-vs-alternatives.md`

Modified:

- `mantis_agent/hooks.py` — dispatch `TeammateIdle`
- `mantis_agent/activity/` — team, peer, task node kinds
- `mantis_agent/session_tree.py` — team tagging in `SessionInfo`
- `mantis_agent/subagent.py` — share the neutralizer and concurrency ceiling
- `mantis_agent/jobs.py` — peers as supervised workers
- `mantis_agent/isolation/worktree.py` — per-peer worktrees
- `mantis_agent/permissions.py` — per-peer context derivation
- `mantis_agent/budget.py` — team and peer allocations
- `mantis_agent/tui_fullscreen.py` — team panel and commands
- `tests/public_api_surface.txt` — intentional update

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Teams modeled as nested subagents | Peers are sessions; enforced by construction and reviewed in Phase 1 |
| Peer messages become an injection channel | Runtime-set senders, shared neutralizer, explicit no-authority rule, dedicated security suite |
| Duplicate work from claim races | Atomic CAS with `BEGIN IMMEDIATE`; 20-claimant stress test |
| Dead peer blocks a task forever | Leases with heartbeat; expiry, reclaim, and partial-work warning |
| Deadlock goes unnoticed | Stall and deadlock detection with chain reporting; `TeammateIdle` |
| Cost explodes | Team and per-peer budgets, reservation, `enabled: false` default, max peers |
| Peers corrupt each other's edits | Worktree isolation; never auto-merge |
| Approval becomes a rubber stamp | Content hashing forces re-approval on change; approval never substitutes for per-call permissions |
| Lead becomes a privilege level | Lead has coordination powers only; cannot alter permissions or approve its own plan |
| Users reach for teams when a workflow fits | Explicit comparison doc; `/team` suggests alternatives for simple fan-out |
| State corruption on crash | Atomic writes, append-only logs, versioned schema, truncated-tail tolerance |
| Idle peers burn tokens | Wake-on-message only; zero-polling assertion in tests |

## 19. Acceptance checklist

- [ ] A peer is a `Session` with an independent transcript; no second persistence layer.
- [ ] Peers accumulate context across many turns and resume after restart.
- [ ] Inboxes are durable, bounded, de-duplicated, and replayed on resume.
- [ ] `sender` is runtime-set and unforgeable; `sender_verified` is displayed.
- [ ] Message bodies are neutralized with the shared neutralizer.
- [ ] No message can widen permissions, approve a plan, or authorize a call.
- [ ] Task claims are atomic; concurrent claimants yield exactly one winner.
- [ ] Leases expire, reclaim works, and partial work is signaled.
- [ ] Dependencies unblock dependents; cycles are rejected with the path.
- [ ] Deadlock and stalls are detected and reported.
- [ ] `TeammateIdle` is dispatched, not reserved.
- [ ] Plan approval is content-hashed; edits force re-approval.
- [ ] Approval never substitutes for per-call permission checks.
- [ ] Peer permissions derive by narrowing only; `session_allows` are not shared.
- [ ] Worktree isolation works; diffs are never auto-merged.
- [ ] Teams count against the shared agent concurrency ceiling.
- [ ] Idle peers consume nothing.
- [ ] Team state survives `kill -9` and resumes correctly.
- [ ] `ruff check` and the full pytest suite pass.

## 20. Recommended implementation order

1. **Build the peer-as-session layer first and stop there for a release.** Two peers with independent contexts that the user can message individually is already useful, and it validates the central architectural bet before any coordination machinery exists.
2. **Add messaging with trust from the first commit.** Sender verification and neutralization are not hardening to add later; a messaging layer without them is a channel for durable injection between agents.
3. **Add the task DAG third**, and get atomic claims right before anything else in it. Everything else in the DAG is bookkeeping; the claim is the correctness property.
4. **Add leases and stall detection immediately after claims** — a claim without a lease converts a crashed peer into a permanently blocked task.
5. **Wire `TeammateIdle`** once the DAG exists; the event has been declared and unfired since the hooks module was written, and this is what it was for.
6. **Add approval fifth**, with content hashing from the start.
7. **Add worktree isolation sixth**, reusing the extraction from `e_subagent_trust_limits_and_isolation.md`.
8. **Move peers to supervised workers last**, once `b_durable_jobs_and_reattachment.md` has proven the supervisor. In-process peers are enough to validate the entire design, and durability is a deployment concern rather than a design one.
9. Write `teams-vs-alternatives.md` before the feature ships, not after. The most likely failure of this feature is overuse, and documentation is the mitigation.
