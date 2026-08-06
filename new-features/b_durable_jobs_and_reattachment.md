# Durable Jobs and Reattachment — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/jobs.py` and a new supervised worker runtime
**Objective:** Let background work outlive the terminal that started it — through a durable job registry, append-only event journals, supervised worker processes, bounded output spill files, discovery, and safe reattachment after restart.

## 1. Executive summary

`mantis_agent/jobs.py` is 249 lines and is a well-built *in-process* job manager. `JobManager` owns `jobs: dict[int, Job]`, mints IDs from `itertools.count(1)`, enforces `_MAX_RUNTIME_S = 3600` as an absolute backstop, prunes to `_MAX_RETAINED_JOBS = 100`, and exposes two callbacks with deliberately different contracts — `on_event` fires exactly once on any terminal state, `on_stream` fires zero or more times before it. Both swallow callback errors, with the comment *"a broken notifier must never kill the job result."* That is the right instinct and it should be preserved everywhere.

Every part of that state is ephemeral. Specifically:

- `Job.task` holds an `asyncio.Task` in the current event loop. A job is a coroutine, not a process.
- `Job.events` is `deque(maxlen=40)` in memory.
- `Job.result` is a string in memory.
- `JobManager.jobs` is a plain dict with no backing store.
- `cancel_all()` is called on TUI exit, terminating everything.

The consequence: closing the terminal ends all background work, and there is no record that it ever ran. A 40-minute test suite, a long build, a background research subagent — all die with the session, and the next session cannot even discover that they existed. `_MAX_RUNTIME_S = 3600` is a sensible backstop for a coroutine but is also an upper bound on any background work the user can ever do, since nothing can outlive the session anyway.

There is a second, subtler limit. Because jobs are coroutines in the agent's own event loop, a job that blocks the loop degrades the whole session, and a job cannot survive a crash of the agent process. The `kind` field already distinguishes `task`, `workflow`, and `watch` work, which is exactly the seam where different execution strategies belong.

This plan adds durability in three separable layers, each independently valuable:

1. **A durable record and journal** — jobs survive as data even while execution stays in-process. This alone gives history, post-hoc inspection, and crash forensics.
2. **Output spill files** — results and event streams stop being memory-bound, so a job can produce megabytes without holding them in RAM or truncating at 40 events.
3. **Supervised worker processes** — jobs genuinely outlive the terminal, with detach, discovery, and reattachment.

The journal is not a new invention: `a_activity_graph_and_inline_rail.md` defines an append-only activity journal with the same crash-tolerance and rotation requirements. Durable jobs must consume that journal rather than opening a parallel one. Where this plan says "journal," it means the activity journal with job-specific record types.

## 2. Goals

### User outcomes

- Start a long build, close the laptop lid, come back, and see it finished.
- Close the terminal deliberately and be told which jobs will keep running and which will be cancelled.
- Open a new session and discover jobs still running from a previous one.
- Reattach to a running job and stream its output from where you left off, or from the beginning.
- Read the full output of a job that produced 200 MB, without the session holding it in memory.
- See a job's history after a crash, including what it was doing when the crash happened.
- Get a terminal notification when a detached job finishes, even if no session is open.

### Engineering goals

- Preserve `JobManager`, `Job`, `spawn`, `get`, `running`, `all`, `cancel`, `cancel_all`, `emit`, `on_event`, and `on_stream` with compatible semantics. In-process jobs must behave exactly as they do today.
- Keep the "broken notifier never kills the job" rule everywhere, including across process boundaries.
- Make durability opt-in per job, not global. A trivial 2-second job should not pay for a journal write and a process spawn.
- Never leave orphan processes. Every worker is supervised, reaped, and accounted for.
- Bound everything: output size, journal size, retained records, concurrent workers.
- Python 3.9–3.14; macOS and Linux first-class, Windows degraded but honest.

### Success metrics

- A detached job survives terminal exit, SIGHUP, and the parent process being killed with SIGKILL.
- Reattachment after restart replays the job's full event history with no gaps.
- A job producing 500 MB of output costs the session under 1 MB of memory.
- Zero orphan processes after a 500-job lifecycle test including crashes and cancellations.
- No measurable overhead for non-durable in-process jobs versus today.
- Job discovery across sessions completes in under 50 ms for 1,000 historical jobs.

## 3. Non-goals

- A general-purpose distributed job queue. This is local background work for one user on one machine.
- Cross-machine job execution — `m_session_event_api_and_remote_surfaces.md` owns remote surfaces, and a remote worker is a later composition of the two.
- Replacing `cron.py`. Scheduled jobs already persist; they become *producers* of durable jobs rather than a competing system.
- Replacing the workflow engine. A workflow run remains a workflow run; it gains a durable job record like everything else.
- Guaranteed exactly-once execution. A supervised worker that dies mid-write may leave a partial side effect; the plan makes that *detectable*, not impossible.
- Resource limits beyond timeouts (CPU/memory cgroups). Noted as future work.

## 4. Current integration points

- `mantis_agent/jobs.py` — the entire module. `Job` dataclass fields (`id`, `desc`, `kind`, `started`, `status`, `result`, `task`, `tool_count`, `turn_count`, `stream_count`, `last_event`, `last_tool`, `events`, `workflow_id`), `elapsed_s`, `record_event`, `summary`, and `JobManager`'s full surface.
- `mantis_agent/activity/` — the journal, envelope, and registry from `a_activity_graph_and_inline_rail.md`. Durable job records are journal records.
- `mantis_agent/subagent.py` — `make_job_output_tool(jobs)` reads job results; `_update_job_progress` writes progress. Both must work against durable jobs.
- `mantis_agent/watch.py` — `make_watch_tool(jobs)` and `make_watch_stop_tool(jobs)`; watches are long-lived streaming jobs and the most obvious detach candidates.
- `mantis_agent/workflow_tool.py` — `attach_job_progress(wf, job)` links a workflow run to a job; `WorkflowLaunch._persist`.
- `mantis_agent/workflow_store.py` — `save_run`, `load_record`, `list_runs`, `prune_runs`, `runs_dir`, `_MAX_RETAINED_RUNS = 200`, `_SAFE_ID`, `redact_inputs`. This is the closest existing precedent for durable execution records and its patterns should be reused directly.
- `mantis_agent/cron.py` — `CronJob`, `load_jobs`, `save_jobs`, `run_job`, `tick`, `daemon`, `install_scheduler`, `launchd_plist`, `systemd_units`. Cron already solves "run without a terminal" and its scheduler installation is directly relevant.
- `mantis_agent/tui.py` / `tui_fullscreen.py` — `/jobs` viewer, exit handling that currently calls `cancel_all`.
- `mantis_agent/paths.py` — state directory resolution.
- `mantis_agent/sandbox.py` — workers inherit the session sandbox policy.
- `mantis_agent/hooks.py` — `TaskCreated` / `TaskCompleted` from `g_typed_hooks_and_full_lifecycle.md`.

## 5. Product model

### Execution modes

`Job` gains a `mode`, defaulting to today's behavior:

| Mode | Execution | Survives terminal exit | Use |
|---|---|---|---|
| `inproc` | `asyncio.Task` (today) | No | Short work, tool calls, quick subagents |
| `durable` | `asyncio.Task` + journal | No, but fully recorded | Medium work where history matters |
| `worker` | Supervised child process | Yes | Long builds, test suites, long agents |
| `detached` | Worker, reparented | Yes, explicitly | Work the user chooses to keep |

Promotion is allowed and is the expected flow: a job starts `inproc`, exceeds a threshold, and the user is offered promotion — or a policy promotes it automatically by `kind` and predicted duration.

Demotion is not allowed. A worker cannot become in-process.

### Lifecycle

```text
pending → starting → running → ┬→ done
                               ├→ error
                               ├→ cancelled
                               ├→ timeout
                               └→ orphaned      (worker lost its supervisor)
                                    ↓
                                 adopted        (a new session took ownership)
```

`orphaned` and `adopted` are new and necessary. A worker whose supervisor died is not failed — it is running without an owner, and the correct action is adoption, not cancellation.

The existing `Job.status` vocabulary (`running / done / error / cancelled / timeout`) is preserved and extended, mapping into the unified activity vocabulary via `activity/status.py`.

### Detach semantics

Detach is an explicit user decision, made at the moment it matters — session exit:

```text
3 jobs are running:
  #3  worker    pytest -q                     4m12s   [keep running]
  #4  watch     src/**/*.py                   9m01s   [cancel]
  #5  inproc    subagent: Explore auth flow      42s   [cannot detach]

[k] keep all detachable   [c] cancel all   [enter] use choices above
```

Rules:

- `inproc` jobs cannot detach and say so plainly rather than silently dying.
- Detached jobs are reparented and survive.
- Default per kind is configurable; the prompt is skippable with a remembered choice.
- Non-interactive exit uses the configured default and logs what it did.

## 6. Durable records

### Record shape

Reuse `workflow_store.py`'s design directly — it already solves this problem for workflow runs, with `RECORD_VERSION`, `_SAFE_ID` filename sanitization, `redact_inputs`, atomic save, `list_runs` with a limit, and `prune_runs` with retention.

```python
class JobRecord(msgspec.Struct, omit_defaults=True):
    version: int = 1
    job_id: str                 # namespaced, e.g. "job:ses-01J8/3"
    session_id: str
    seq: int                    # per-session ordinal, preserves Job.id
    kind: str                   # task | workflow | watch | shell | agent
    mode: str                   # inproc | durable | worker | detached
    desc: str
    status: str
    created_at: float           # wall clock
    started_at: float | None
    ended_at: float | None
    cwd: str
    pid: int | None             # worker only
    pgid: int | None
    exit_code: int | None
    tool_count: int = 0
    turn_count: int = 0
    stream_count: int = 0
    last_event: str = ""
    last_tool: str = ""
    result_ref: str = ""        # spill file path
    result_bytes: int = 0
    events_ref: str = ""        # journal path
    workflow_id: str = ""       # preserved for compatibility
    error: str = ""
    spawn_spec: dict | None = None   # for retry; redacted
```

### Identity

`Job.id` is an `int` from a per-process counter, restarting at 1 in every session. It is user-visible (`/job 3`) and must keep working. Namespace it exactly as `a_activity_graph_and_inline_rail.md` specifies: the durable key is `job:<session-id>/<seq>`, while `/job 3` resolves within the current session. Cross-session references use the full form, and `resolve_ref` handles both.

### Storage layout

```text
~/.mantis/jobs/
  index.db                      # SQLite: discovery and status queries
  <session-id>/
    <seq>/
      record.json               # JobRecord, atomically written
      events.jsonl              # append-only event journal
      stdout.log                # spilled output, rotated
      stderr.log
      result.txt                # final text if large
      spawn.json                # redacted spawn spec for retry
```

SQLite for the index is a deliberate choice: `session.py` already ships `SqliteSessionStore` with `_SCHEMA`, `_connect`, and sync helpers run off-thread, so the pattern, the dependency, and the concurrency discipline all exist in the codebase. Scanning per-job JSON files for discovery would be O(n) file opens; the index makes "what is running right now, across all sessions" one query.

The files remain the source of truth. The index is a cache and must be rebuildable from the directory tree by a `mantis jobs reindex` command, because an index that can become authoritative is an index that can lose data.

### Atomicity

- `record.json` is written to a temp file in the same directory and `os.replace`d — atomic on both platforms.
- `events.jsonl` is append-only, one complete line per `write()`, never partial-line flushed. A truncated final line from a crash is discarded on read, matching the activity journal's rule.
- Status transitions are journaled *before* they are attempted where the transition has a side effect, so a crash mid-transition is detectable.

## 7. Output spill

`Job.result` is a string and `Job.events` is a 40-entry deque. Both must become file-backed with a bounded in-memory window.

### Design

- Output streams to `stdout.log` / `stderr.log` as it is produced.
- The in-memory ring keeps the last N lines (default 200, generalizing the current 40) for instant `/jobs` rendering.
- Files rotate at a size cap (default 64 MB) with a bounded generation count; rotation preserves the head, because the beginning of a build log is usually more diagnostic than the middle.
- `Job.result` becomes a property reading a bounded tail from the spill file, so existing callers keep working.
- `make_job_output_tool` gains offset, limit, and grep parameters so the model can query a large log rather than receiving it whole. Model-visible output is capped and states what was omitted and how to narrow — the same discipline the browser plan applies to snapshots.
- Total disk per job is capped; exceeding it marks the job `error` with a distinct reason rather than filling the disk.

### Redaction

Spill files persist to disk and are read back into model context. Redact on write, not on read, using the shared recursive redactor consolidated in `h_sandbox_egress_credentials_and_escape_controls.md`. `workflow_store.redact_inputs` and its `_SECRET_HINTS` are the existing precedent and should be the same code.

## 8. Supervised workers

### Process model

```text
mantis session (parent)
   │ spawn, then reparent
   ▼
mantis-jobd (supervisor, one per user)
   ├── worker: pytest -q          (pgid 4411)
   ├── worker: agent subagent     (pgid 4412)
   └── worker: watch src/**       (pgid 4413)
```

A single per-user supervisor rather than one supervisor per job:

- Workers are reparented to it, so they survive the session.
- It reaps children, writes terminal records, and fires notifications.
- It is restartable and rediscovers running workers from the index plus liveness checks.
- It exits when idle for a configurable period.

This mirrors `m_session_event_api_and_remote_surfaces.md`'s daemon and should share its lifecycle plumbing (socket location, idle shutdown, `mantis daemon` command family) without sharing its responsibilities.

### Spawning

- New session leader via `os.setsid()` so the worker is detached from the controlling terminal and immune to SIGHUP.
- Process group recorded so cancellation can signal the whole group — a worker that spawns `make` which spawns compilers must be fully reapable.
- `stdin` from `/dev/null`; `stdout`/`stderr` to spill files opened by the supervisor.
- Environment built by the sandbox environment builder from `h_sandbox_egress_credentials_and_escape_controls.md`. A detached worker running for an hour with the session's API key in its environment is exactly the exposure that plan closes.
- Sandbox policy inherited and re-applied via `wrap_command`; a worker must not be a sandbox escape.
- CWD validated to still exist; a worker whose directory was deleted fails fast with a clear reason.

### Agent workers

A worker running a Mantis agent (not just a shell command) needs more:

- Serialize the spawn spec: prompt, agent type, model, tool allowlist, permission mode, rule set.
- **Permission handling is the hard part.** A detached agent has no interactive asker. Rules:
  - Default to `default` mode with **no asker**, which per the existing `_resolve_ask` logic fails closed for explicit asks and dangerous commands — exactly the desired behavior.
  - Optionally allow a pre-approved rule set captured at detach time.
  - A worker that hits an `Ask` it cannot resolve records a `blocked` state, notifies the user, and waits for a bounded period; if a session reattaches, the ask is surfaced there. Otherwise it times out and fails closed.
  - A detached agent must never run in `bypass` or with a classifier `auto` mode that could approve writes unattended, unless explicitly configured with `worker.allowUnattendedAuto`.
- Transcript written to the standard session transcript location so the work is inspectable and resumable.

### Liveness and orphans

- Supervisor heartbeats into the index.
- Workers are checked with `os.kill(pid, 0)` plus a recorded start time to defend against PID reuse — checking the PID alone is a classic bug, and a PID that was reused by an unrelated process must not be signalled.
- A worker whose supervisor is gone is `orphaned`; the next session or supervisor start adopts it.
- A worker whose process is gone without a terminal record is marked `error` with `reason="lost"`, never silently `done`.
- Startup sweep reconciles the index against reality and cleans stale directories.

## 9. Reattachment

### Discovery

```text
$ mantis jobs
ID              KIND     STATUS    ELAPSED  SESSION      DESC
job:01J8/3      worker   running     12m4s  (this)       pytest -q
job:01J7/1      worker   running     41m2s  laptop-2     nightly build
job:01J7/2      watch    orphaned    41m0s  laptop-2     src/**/*.py
job:01J6/5      worker   done         3m1s  laptop-1     ruff format
```

On session start, if jobs from other sessions are running, show a one-line notice rather than a blocking prompt. The notice is the discovery mechanism; it must not interrupt.

### Attaching

```text
/jobs attach job:01J7/1        stream live output from now
/jobs attach job:01J7/1 --from-start   replay journal then go live
/jobs adopt job:01J7/2         take ownership of an orphan
/jobs detach job:01J8/3        promote and detach
/jobs output job:01J7/1 [--tail N] [--grep P]
/jobs stop job:01J7/1
```

Reattachment semantics mirror the replay rules in `m_session_event_api_and_remote_surfaces.md`, and for the same reason: reading a journal then switching to live is where gaps and duplicates are introduced. Buffer live events during journal read and de-duplicate by sequence number.

Multiple sessions may observe one job. Only one may control it, following the same lease model as remote control. Two sessions racing to cancel one job is the same class of bug as two surfaces answering one permission prompt.

### Notifications

- Terminal state fires a notification even when no session is attached: OS notification on the host, plus a durable "unread" flag surfaced at the next session start.
- The existing `on_event` contract — fires exactly once on any terminal state — extends across the process boundary. The supervisor writes the terminal record; any attached session delivers it; an unattached completion is delivered at next attach.
- Deduplicate: a job's completion must be announced once, not once per session that later opens.

## 10. Security

- **Path safety.** Job directories derive from session IDs and sequence numbers, sanitized with `workflow_store`'s `_SAFE_ID` pattern before any filesystem use. Never interpolate a description into a path.
- **Spill redaction.** Applied on write. A job log is a durable, on-disk artifact that later re-enters model context; an unredacted token in it is a persistent leak.
- **Directory permissions.** `~/.mantis/jobs` at `0o700`, files at `0o600`.
- **Worker environment.** Built by allowlist, provider API key never passed. This is stronger than the in-process case because a detached worker's lifetime is unbounded.
- **Sandbox inheritance.** Workers run under the session's sandbox policy. A job may not be used to escape confinement, and `dangerouslyDisableSandbox` is refused for detached workers entirely.
- **Signal safety.** Cancellation signals the recorded process group, verified by start time to defend against PID reuse. Escalation is SIGTERM, grace period, SIGKILL.
- **Spawn spec redaction.** `spawn.json` supports retry and therefore contains prompts and arguments; redact before writing, and never store credentials in it.
- **Adoption authorization.** Only the same UID may adopt a job. Verified from directory ownership, not from the record's contents.
- **Untrusted content.** Job descriptions and output are model- and tool-authored. Sanitize control characters, ANSI, and bidi before rendering, per the activity plan.

## 11. Configuration

```json
{
  "jobs": {
    "durable": {"enabled": true, "defaultMode": "inproc"},
    "promote": {
      "auto": true,
      "afterSeconds": 120,
      "kinds": ["workflow", "watch", "shell"]
    },
    "detachOnExit": {
      "prompt": true,
      "default": {"worker": "keep", "watch": "cancel", "inproc": "cancel"}
    },
    "maxRuntimeSeconds": 3600,
    "maxDetachedRuntimeSeconds": 86400,
    "maxConcurrentWorkers": 8,
    "output": {
      "memoryLines": 200,
      "spillMaxBytes": 67108864,
      "rotations": 2,
      "totalPerJobBytes": 268435456
    },
    "retention": {"records": 500, "days": 30, "completedOutputDays": 7},
    "supervisor": {"enabled": true, "idleShutdownMinutes": 30},
    "worker": {
      "allowUnattendedAuto": false,
      "blockedAskTimeoutSeconds": 900
    },
    "notifications": {"onComplete": true, "onFailure": true}
  }
}
```

Environment:

- `MANTIS_JOBS_DURABLE=0|1`
- `MANTIS_JOBS_NO_DETACH=1`
- `MANTIS_JOBS_DIR`

`maxRuntimeSeconds` keeps `_MAX_RUNTIME_S = 3600` as the in-process backstop; detached workers get a separate, longer bound, because the current one-hour cap exists only because nothing could outlive the session anyway.

## 12. TUI and CLI surface

```text
mantis jobs [--all] [--running] [--session <id>]
mantis jobs show <id>
mantis jobs output <id> [--tail N] [--grep P] [--follow]
mantis jobs stop <id>
mantis jobs adopt <id>
mantis jobs reindex
mantis jobs prune [--days N]
mantis jobd start|stop|status
```

In the TUI, `/jobs` becomes a projection over the activity registry (per `a_activity_graph_and_inline_rail.md`) with durability columns added:

```text
/jobs
  #3   worker    running    12m4s   pytest -q                    [detachable]
  #4   watch     running     9m1s   src/**/*.py                  [detachable]
  #5   inproc    running      42s   subagent: Explore auth flow
  #2   worker    done        3m1s   ruff format        → 1.2 MB output
```

The `[detachable]` marker matters: users need to know before exit which work will survive.

## 13. Errors

```text
JobError                       (base)
├── JobNotFoundError
├── JobNotDetachableError      # inproc job asked to detach
├── JobSpawnError
├── JobCwdMissingError
├── SupervisorUnavailableError
├── WorkerLostError            # process gone, no terminal record
├── AdoptionDeniedError        # different UID
├── JobOutputTooLargeError
├── JobDiskQuotaError
├── JournalCorruptError        # truncated tail; recoverable
├── IndexRebuildRequiredError
└── JobControlHeldError        # another session holds control
```

Errors are structured and actionable. `WorkerLostError` in particular must never be reported as success; a job whose process vanished is an error with a specific reason, not a completion.

## 14. Delivery phases

### Phase 0 — Spike

1. Prototype `setsid` reparenting and verify survival across terminal close, SIGHUP, and parent SIGKILL on macOS and Linux.
2. Verify process-group signalling reaps a `make`-style tree.
3. Measure journal write cost per event at realistic watch rates.
4. Validate SQLite index concurrency with the `session.py` off-thread pattern.
5. Decide supervisor-per-user versus supervisor-per-job with a leak test on both.

**Exit:** workers provably survive; no orphans in the harness; journal cost acceptable.

### Phase 1 — Durable records

1. Add `jobs/records.py` with `JobRecord`, atomic write, and the storage layout.
2. Add the SQLite index with a rebuild path.
3. Journal job lifecycle into the activity journal.
4. Namespace job IDs; keep `/job 3` resolution working.
5. Add `mantis jobs` and `mantis jobs show` reading from durable records.

**Exit:** every job is recorded and inspectable after the session ends; execution unchanged.

### Phase 2 — Output spill

1. Stream output to spill files with rotation.
2. Keep a bounded in-memory ring; make `Job.result` a bounded property.
3. Extend `make_job_output_tool` with offset, limit, and grep.
4. Add redaction on write.
5. Add per-job disk quotas.

**Exit:** a 500 MB job costs the session under 1 MB of memory; output is queryable.

### Phase 3 — Supervisor and workers

1. Implement `mantis-jobd` with registration, reaping, and terminal records.
2. Implement worker spawn with `setsid`, process groups, sandbox, and scrubbed environment.
3. Implement liveness with PID-plus-start-time verification.
4. Implement orphan detection and the startup sweep.
5. Add `mantis jobd` commands and idle shutdown.

**Exit:** a shell job survives terminal exit and records its terminal state.

### Phase 4 — Detach and reattach

1. Add mode promotion and the exit prompt.
2. Implement attach with journal replay and gapless live switchover.
3. Implement adoption with UID verification.
4. Implement control leases for multi-session observation.
5. Add notifications with once-only delivery.

**Exit:** close a terminal, reopen, reattach, and stream from where you left off.

### Phase 5 — Agent workers

1. Serialize and restore agent spawn specs.
2. Implement the no-asker permission posture and `blocked` handling.
3. Route worker transcripts to standard session transcript locations.
4. Surface a blocked worker's pending ask to a reattaching session.
5. Enforce `allowUnattendedAuto` and the `bypass` refusal.

**Exit:** a detached agent runs safely unattended and fails closed on approvals.

### Phase 6 — Hardening

1. Adversarial review: PID reuse, signal escalation, adoption across UIDs, path traversal, spill redaction.
2. Fuzz journal and record readers with truncated and malformed input.
3. Leak tests: 500-job lifecycle with induced crashes.
4. Soak: 24 hours of watches and workers with bounded disk and memory.
5. Remove experimental gating.

## 15. Testing strategy

### Unit

- `JobRecord` round-trip, version handling, and atomic write with an induced crash between temp write and rename.
- ID namespacing and `/job N` resolution within and across sessions.
- Index rebuild from a directory tree; index/reality divergence reconciliation.
- Spill rotation, head preservation, quota enforcement, bounded tail reads.
- Redaction of secrets in output, results, and spawn specs.
- Liveness: live PID, dead PID, reused PID with mismatched start time.
- Status mapping into the unified activity vocabulary for every value.
- Detach decision defaults per kind.
- Retention and pruning, mirroring `workflow_store.prune_runs`.

### Integration

- In-process job behaves exactly as today, including `on_event` once and `on_stream` many.
- Durable job records a complete journal.
- Worker spawn, run, and terminal record via the supervisor.
- Cancellation reaps a process tree (`sh -c 'sleep 300 & wait'`).
- Supervisor restart rediscovers running workers.
- Orphan adoption by a new session.
- Watch job detached and reattached with a full event history.
- Workflow run as a durable job; `workflow_id` linkage preserved.

### End-to-end

- Start a job, exit the TUI with keep, reopen, discover, attach, and see completion.
- Kill -9 the session; worker survives; state is correct on next start.
- Kill -9 the supervisor; worker becomes orphaned; next start adopts it.
- Agent worker blocked on an approval; reattaching session surfaces the ask.
- 500 MB output job: memory bounded, output queryable, quota respected.

### Security

- Adoption attempt from a different UID is refused.
- Path traversal via a hostile session id or description.
- Secrets in job output do not persist to spill files.
- Provider API key absent from every worker environment.
- Detached worker cannot run with `bypass` or approve a sandbox escape.
- Cancellation does not signal a reused PID belonging to an unrelated process.
- Spill files and job directories have restrictive permissions.

### Performance and reliability

- Journal write cost at 50 events/second.
- Discovery query at 1,000 historical jobs.
- 500-job lifecycle leak test: zero orphan processes, zero leaked file descriptors.
- Supervisor idle memory and CPU.
- Concurrent worker cap enforcement.

## 16. Documentation

- `docs/guides/jobs.md` — modes, detaching, reattaching, discovery, output querying.
- `docs/guides/jobs-durability.md` — what survives what, storage layout, retention, recovery after crashes.
- `docs/guides/jobs-security.md` — worker environment, sandbox inheritance, unattended agent posture.
- `docs/api/jobs.md` — `Job`, `JobManager`, `JobRecord`, supervisor client.
- Troubleshooting: orphaned jobs, stale index, disk quota, blocked workers.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 17. File-level implementation map

New:

- `mantis_agent/jobs/__init__.py` (re-exports `Job`, `JobManager` unchanged)
- `mantis_agent/jobs/records.py`
- `mantis_agent/jobs/index.py`
- `mantis_agent/jobs/spill.py`
- `mantis_agent/jobs/supervisor.py`
- `mantis_agent/jobs/worker.py`
- `mantis_agent/jobs/attach.py`
- `mantis_agent/jobs/discovery.py`
- `mantis_agent/jobs/agent_worker.py`
- `tests/test_job_records.py`
- `tests/test_job_index.py`
- `tests/test_job_spill.py`
- `tests/test_job_supervisor.py`
- `tests/test_job_worker_lifecycle.py`
- `tests/test_job_attach.py`
- `tests/test_job_adoption.py`
- `tests/test_job_security.py`
- `docs/guides/jobs.md`
- `docs/guides/jobs-durability.md`

Modified:

- `mantis_agent/jobs.py` → package `__init__` preserving the public surface
- `mantis_agent/activity/journal.py` — job record types
- `mantis_agent/subagent.py` — `make_job_output_tool` with query parameters
- `mantis_agent/watch.py` — watches as detachable jobs
- `mantis_agent/workflow_tool.py` — durable job linkage
- `mantis_agent/cron.py` — scheduled runs produce durable jobs
- `mantis_agent/tui.py`, `tui_fullscreen.py` — `/jobs` projection, exit prompt
- `mantis_agent/cli.py` — `jobs` and `jobd` commands
- `mantis_agent/sandbox.py` — worker environment and policy inheritance
- `mantis_agent/paths.py` — jobs directory
- `tests/public_api_surface.txt` — intentional update

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Orphan processes accumulate | Supervisor reaping, process groups, startup sweep, 500-job leak test |
| PID reuse causes wrong signal | PID plus recorded start time verified before any signal |
| Detached agent runs unattended and does damage | No asker → fail closed; `bypass` refused; `allowUnattendedAuto` off by default |
| Detached worker holds credentials for hours | Allowlist environment; provider key never passed |
| Spill files fill the disk | Per-job and global quotas; rotation; retention pruning |
| Secrets persist in job logs | Redaction on write, shared with the sandbox and workflow redactors |
| Index diverges from reality | Files are authoritative; index rebuildable; startup reconciliation |
| Journal corrupted by a crash | Whole-line appends; truncated tail discarded on read |
| Two sessions fight over one job | Observation is free; control requires a lease |
| Users lose work by cancelling on exit | Explicit prompt showing what survives; remembered defaults; `[detachable]` marker |
| Supervisor becomes a second runtime | Strict scope: spawn, reap, record, notify. No agent logic |
| Windows lacks setsid and process groups | Job objects where possible; otherwise report `worker` mode unsupported honestly |
| Complexity regresses simple jobs | `inproc` remains the default and its code path is unchanged |

## 19. Acceptance checklist

- [ ] In-process jobs behave exactly as before; `on_event` and `on_stream` contracts preserved.
- [ ] Every job produces a durable, versioned record.
- [ ] Job IDs are namespaced; `/job N` still resolves.
- [ ] The index is a rebuildable cache; files are authoritative.
- [ ] Output spills to disk with rotation, quotas, and redaction on write.
- [ ] `job_output` supports tail, offset, and grep with bounded model-visible output.
- [ ] Workers survive terminal exit, SIGHUP, and parent SIGKILL.
- [ ] Cancellation reaps whole process trees without PID-reuse hazards.
- [ ] Orphans are detected and adoptable by the same UID only.
- [ ] Reattachment replays with no gaps or duplicates.
- [ ] Control requires a lease; observation does not.
- [ ] Completion is notified exactly once, even with no session attached.
- [ ] Detached agents fail closed on approvals and never run in `bypass`.
- [ ] Worker environments contain no provider key and inherit the sandbox.
- [ ] Zero orphan processes and file descriptors in the lifecycle leak test.
- [ ] Docs, changelog, and public API snapshot updated intentionally.
- [ ] `ruff check` and the full pytest suite pass.

## 20. Recommended implementation order

1. **Durable records first, with execution unchanged.** This is the highest value per unit of risk: history, forensics, and cross-session discovery arrive without touching how jobs run.
2. **Output spill second.** Also independent of process work, and it removes the memory ceiling that makes long jobs impractical today.
3. Reuse `workflow_store.py`'s patterns verbatim for both — atomic writes, `_SAFE_ID`, redaction, retention. Do not invent a second convention.
4. **Supervisor and workers third.** This is the first genuinely risky component; it lands only after records and spill are proven, so a worker has somewhere to write.
5. Prove reaping and orphan handling with leak tests *before* exposing detach to users.
6. **Detach and reattach fourth** — the visible feature, built on infrastructure that is already correct.
7. **Agent workers last**, because the unattended-permission posture is the most security-sensitive decision in this plan and deserves its own review.
8. Once workers exist, revisit `_MAX_RUNTIME_S`: the one-hour cap was a consequence of ephemerality and should be re-derived, not inherited.
