# Workflow Safety, Strict Resume, and Scale — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/workflow.py`, `workflow_script.py`, `workflow_store.py`, `workflow_defs.py`, `workflow_tool.py`
**Objective:** Close the correctness gap in resume, persist exact script sources with hashes, add pre-launch review and cost projection, enforce scale ceilings, harden determinism, and let a proven workflow become a saved command.

## 1. Executive summary

The workflow subsystem is the largest recent addition to Mantis — roughly 3,700 lines across five modules — and much of it is already careful work. `workflow_script.py` implements a genuine AST allowlist: `_ALLOWED_NODES` enumerates permitted node types so *"a new Python syntax feature can't quietly widen what a script can do,"* `_BANNED_NAMES` blocks `eval`/`exec`/`getattr`/`__import__`, imports are refused outright, underscore-prefixed attribute access is blocked (defeating the standard `x.__class__.__mro__[1].__subclasses__()` escape), and dunder strings are rejected so the same escape cannot be reassembled through a subscript. `workflow_store.redact_inputs` is deliberately conservative, documented with the right trade-off: *"a false positive costs a resume its input value, a false negative writes a live key into a file that outlives the session."* `replay_cache` refuses to replay non-`done` agents because *"replaying an errored or cancelled agent would launder a failure into a success."*

That reasoning is sound. The gaps are in places where the same rigor has not yet been applied.

**Resume is content-keyed, not prefix-keyed — and this is a correctness bug.** `replay_cache` builds `{cache_key(phase, label, prompt): result}` and `workflow_defs.cache_key` hashes those three values. On resume, any agent whose phase, label, and prompt are unchanged replays its cached result. Consider a three-stage pipeline where stage 1's prompt is edited and stages 2 and 3 are untouched. Stage 1 re-runs and produces a *different* result. Stages 2 and 3 match their cache keys — their prompts are unchanged as written — and replay stale results computed from the *old* stage 1 output. The workflow reports success with an internally inconsistent result set. The fix is the model the audit describes as "longest unchanged prefix": once any agent's identity or inputs change, everything downstream of it must re-run, regardless of whether its own prompt text changed. Content-keying is safe only for genuinely independent fan-out; it is unsafe for pipelines, which are the engine's headline feature.

**Script sources are not persisted with a hash.** `save_run` records a `definition` string and the run dict, but a script-mode workflow's actual source is not durably associated with the run by content hash. Resume therefore cannot detect that the script itself changed between runs — only that individual prompts did. A resumed run against an edited script may replay results produced by code that no longer exists.

**Records are not written atomically.** `save_run` ends with `target.write_text(json.dumps(record, indent=2), encoding="utf-8")`. A crash or a full disk mid-write leaves a truncated JSON file that `load_record` cannot parse, losing the entire run history rather than the last update. Every other durable store in this plan set uses temp-file-plus-rename; this one should too.

**There is no pre-launch review.** A model-authored script can fan out to many agents. Individual agents are permission-gated, so the blast radius per agent is bounded, but the *aggregate* — token spend, wall-clock, worktree count — is committed without the user seeing a plan. `MAX_DEFINITION_AGENTS = 64` bounds declarative definitions; script mode has `_default_cap()` and budget checks but no user-facing projection before launch.

**Scale limits are scattered.** `MAX_DEFINITION_AGENTS = 64`, `_default_cap()`, `_MAX_RETAINED_RUNS = 200`, `_RECENT_CAP = 5`, plus the budget tracker. There is no single lifetime cap on agents spawned by one run, no wall-clock ceiling on a run, and no coordination with the subagent concurrency cap that `e_subagent_trust_limits_and_isolation.md` introduces.

**A proven workflow cannot become a command.** Users iterate on a script until it works, then have no way to save it as `/my-review` short of hand-writing a definition file.

## 2. Goals

### User outcomes

- Resume a workflow after an edit and get results that are *correct*, with a clear statement of what replayed and what re-ran.
- See what a workflow will do — phases, agent count, projected tokens and cost — and approve it before it runs.
- Never lose run history to a crash mid-save.
- Get a hard stop when a run exceeds its agent, time, or token ceiling, with an actionable message.
- Save a working script as a named command and run it with arguments.
- Give workflow agents worktree isolation so parallel edits do not collide.

### Engineering goals

- Preserve `Workflow`, `WorkflowRun`, `Phase`, `AgentRun`, `make_agent_runner`, `wrap_runner_with_progress`, `validate_script`, `extract_meta`, `save_run`, `load_record`, `list_runs`, `replay_cache`, `prune_runs`, and the `workflow_defs` public surface.
- Keep the AST allowlist as the security model; extend it, never relax it.
- Keep `to_dict`/`from_dict` round-trips stable, adding fields with defaults so old records still load.
- Make the journal the activity journal from `a_activity_graph_and_inline_rail.md`, not a fourth store.
- Share the concurrency ceiling with `e_subagent_trust_limits_and_isolation.md` rather than adding a fifth independent cap.
- Python 3.9–3.14.

### Success metrics

- The pipeline-staleness scenario in §1 is caught by a regression test and produces a correct re-run.
- Every persisted record survives an induced crash at any point during save.
- Script source hash mismatch is detected on resume and forces a full re-run with an explanation.
- A run cannot exceed its configured agent, wall-clock, or token ceiling.
- Cost projection is within 30% of actual for the benchmark workflows.
- No regression in existing workflow tests.

## 3. Non-goals

- Redesigning the script language. The primitives (`agent`, `parallel`, `pipeline`, `phase`, `log`, `workflow`) stay as they are.
- Relaxing the AST allowlist to support more Python. Scripts orchestrate; anything else is an agent's job — the existing error message says this well and should stand.
- Distributed workflow execution across machines.
- Replacing declarative definitions with scripts or vice versa; both remain first-class.
- General-purpose caching of agent results across runs. Replay is scoped to resuming one run.
- Building the activity graph — this plan consumes it.

## 4. Current integration points

- `mantis_agent/workflow.py` (1,150 lines) — `WorkflowError`, `AgentRun` (with `to_dict`/`from_dict`, `push_activity`, `elapsed_ms`), `Phase.roll_up`, `WorkflowRun`, `make_agent_runner`, `wrap_runner_with_progress`, `_default_cap`, `Workflow` (`phase`, `log`, `_emit`, `_ingest`, `_finalize`, `stop`, `cancel`, `pause`, `resume`, `skip_agent`, `_budget_exhausted`, `finish`), `_b36`, `_WF_SEQ`, `_RECENT_CAP`.
- `mantis_agent/workflow_script.py` (361 lines) — `ScriptError`, `_ALLOWED_NODES`, `_BANNED_NAMES`, `validate_script`, `extract_meta`, `_compile`, `_WRAPPER`, `_Json`, `_make_phase`, `_script_line`, `_BudgetView`.
- `mantis_agent/workflow_store.py` (234 lines) — `RECORD_VERSION`, `_MAX_RETAINED_RUNS`, `_SECRET_HINTS`, `_SAFE_ID`, `runs_dir`, `run_path`, `redact_inputs`, `save_run`, `_result_summary`, `load_record`, `list_runs`, `prune_runs`, `replay_cache`.
- `mantis_agent/workflow_defs.py` (1,108 lines) — `MAX_DEFINITION_AGENTS`, `WORKFLOWS_SUBDIR`, `_MODES`, `AgentSpec`, `PhaseSpec`, `InputSpec`, `WorkflowDefinition`, `validate_definition_data`, `parse_workflow_md`, `discover_workflow_definitions`, `render_template`, `resolve_inputs`, `cache_key`, `_make_stage`, `_pipeline_items`.
- `mantis_agent/workflow_tool.py` (753 lines) — `workflows_enabled`, `_DISABLE_ENV`, `WorkflowLaunch`, `prepare_workflow_launch`, `format_workflow_report`, `_workflow_schema`, `make_workflow_tool`, `attach_job_progress`, `_format_script_report`.
- `mantis_agent/workflow_view.py` (542 lines) — the viewer.
- `mantis_agent/budget.py` — `BudgetTracker`, consumed by `_BudgetView`.
- `mantis_agent/jobs.py` — workflow runs are jobs; `Job.workflow_id` links them.
- `mantis_agent/activity/` — journal and registry.
- `mantis_agent/isolation/worktree.py` — from `e_subagent_trust_limits_and_isolation.md`.
- `mantis_agent/skills.py` — the command/skill format that saved workflows target.

## 5. Strict-prefix resume

### The model

Replace content-keyed replay with **dependency-ordered prefix replay**.

Every agent invocation gets a deterministic position and a chained identity:

```python
step_id   = <ordinal in deterministic execution order>
step_hash = H(script_hash, step_id, phase, label, prompt, model, agent_type,
              effort, isolation, tuple(sorted(input_refs)), parent_step_hash)
```

`parent_step_hash` is the chaining element and the whole fix. It folds in the hash of every step whose output this step consumed. If stage 1 changes, its `step_hash` changes, so stage 2's `parent_step_hash` changes, so stage 2's `step_hash` changes — even though stage 2's own prompt text is identical. The cache misses, and stage 2 re-runs correctly.

Replay rule: walk steps in deterministic order and replay while `step_hash` matches the recorded run. On the first mismatch, stop replaying; every subsequent step executes live, regardless of whether its own hash would have matched. This is the "longest unchanged prefix" semantics.

### Determinism requirements

Prefix replay is only sound if step ordering is deterministic across runs. That requires:

- **Stable ordinals.** Assign `step_id` at the point of the `agent()` call in program order. `parallel()` and `pipeline()` must assign ordinals deterministically by item index, not by completion order.
- **No wall-clock or randomness in scripts.** The AST allowlist already blocks imports, so `random` and `time` are unreachable. Confirm nothing in the injected namespace exposes them; `_BudgetView.spent()` is time-independent, but any future addition must be checked. Add an explicit test asserting the script namespace contains no time or randomness source.
- **Deterministic `args`.** Inputs are recorded and hashed. A resume with different args is a different run and must not replay.
- **Stable iteration.** Dict ordering is insertion-ordered in supported Pythons; sets are not. Sort any set-derived collection before it influences ordering, and warn at validation time when a `Set` or `SetComp` node feeds a `parallel`/`pipeline` argument.

### Independent fan-out exemption

Content-keying is genuinely correct for independent parallel work: ten reviewers over ten files do not depend on each other. Allow a step to declare independence:

```python
await parallel([... ], independent=True)
```

Independent steps use content-keyed replay and are not invalidated by a sibling's change — only by a change to themselves or to an ancestor they consumed. Default is **dependent** (strict), because the safe default must be the conservative one. A user opting into `independent=True` is asserting a property the engine cannot verify, and the documentation must say so plainly.

### Reporting

Resume must explain itself:

```text
Resuming wfr:4f2a (script hash changed: 8 steps replayed, 14 re-run)
  replayed  Scan/grep-tests            step 1–8
  changed   Fix/patch-auth             step 9  (prompt edited)
  re-run    Fix/*, Verify/*            step 9–22  (downstream of step 9)
```

Never silently replay. A user who does not know which results are fresh cannot trust any of them.

## 6. Source persistence and integrity

### What is recorded

Extend the record written by `save_run`:

```python
{
  "version": 2,
  "run_id": "4f2a",
  "kind": "script" | "definition",
  "script_source": "...",          # exact source, verbatim
  "script_hash": "sha256:...",
  "definition": "review-changes",
  "definition_hash": "sha256:...",
  "meta": {...},                    # the pure-literal meta block
  "args_hash": "sha256:...",
  "inputs": {...},                  # redacted, as today
  "steps": [                        # NEW: the ordered step ledger
    {"step_id": 1, "step_hash": "...", "phase": "Scan", "label": "grep",
     "status": "done", "result_ref": "...", "usage": {...}}
  ],
  "run": {...},                     # existing run dict, unchanged
  "summary": {...}
}
```

Bump `RECORD_VERSION` to 2. `load_record` must read version 1 records and treat them as replay-ineligible rather than failing — an old record can still be *viewed*, it just cannot be safely resumed under the new semantics. Silently resuming a v1 record with prefix rules would be unsound because v1 has no step ledger.

### Source size and secrets

- Script source is capped (default 256 KB); larger scripts are stored truncated with the hash of the full source, and marked replay-ineligible.
- Script source is redacted with the shared redactor before writing. A model-authored script may contain a literal credential in a prompt string, and this file outlives the session — the same reasoning `redact_inputs` already documents.
- `args` are hashed *after* redaction so a redacted value does not change the hash between runs.

### Atomic writes

Replace `target.write_text(...)` with:

1. Write to `<target>.tmp-<pid>` in the same directory.
2. `flush()` and `os.fsync()` the file descriptor.
3. `os.replace(tmp, target)` — atomic on POSIX and Windows.
4. On failure, remove the temp file; never leave a partial `.json`.

`load_record` gains tolerance: a `.tmp-*` file is ignored, and an unparseable record is reported as corrupt with the run id rather than raising into the caller. `save_run`'s existing contract — *"the caller should treat a failure here as a logging problem, never as a failed workflow"* — is preserved.

Large step results move to sibling files (`steps/<step_id>.txt`) referenced by `result_ref`, so the record itself stays small and a single large agent output cannot make the whole record slow to write.

## 7. Pre-launch review

### The approval card

Before a script or definition runs, render a card and require confirmation when the projection exceeds a threshold:

```text
Workflow: review-changes                              script · 41 lines
  Scan      1 agent    grep test logs
  Review    5 agents   parallel, one per dimension
  Verify    ≤15 agents pipeline, per finding
  ────────────────────────────────────────────────
  agents    7–21           worktrees 0
  tokens    ~180k–520k     est. cost $2.10–$6.40
  wall      ~4–11 min      concurrency cap 8
  isolation none
  [r]un   [e]dit   [s]ave as command   [c]ancel
```

Rules:

- Shown when projected agents, tokens, or cost exceed configured thresholds; below them, run without friction. A three-agent workflow should not require ceremony.
- The projection is derived from the script's static structure where possible (`meta.phases`, literal list lengths, `parallel`/`pipeline` argument shapes) and marked as a range when loops or budget-driven counts make it unbounded.
- An **unbounded** projection (a `while` loop over `budget.remaining()`) is stated as unbounded rather than guessed, and always requires confirmation.
- The card shows the script source on request, since it is model-authored code the user may want to read.
- Non-interactive contexts use configured thresholds and fail closed above them, consistent with the permission layer's headless behavior.

### Cost projection

Add `workflow/projection.py`:

- Static analysis of the validated AST: count `agent()` call sites, resolve literal-length `parallel`/`pipeline` arguments, detect loops and mark their contribution unbounded.
- Per-agent token estimate from historical run data for the same definition or script hash, falling back to a configured default.
- Pricing from the model catalog (`catalog.py`).
- Report a range, never a single number, and record actuals afterward so estimates improve.

Accuracy target is deliberately modest — 30% — because the purpose is preventing a surprise 50-agent run, not billing.

## 8. Scale limits

### Ceilings

| Limit | Default | Scope |
|---|---|---|
| `maxAgentsPerRun` | 200 | Lifetime of one run |
| `maxAgentsPerPhase` | 64 | Matches `MAX_DEFINITION_AGENTS` |
| `maxConcurrentAgents` | shared with subagents (8) | Session-wide |
| `maxRunWallSeconds` | 3600 | One run |
| `maxRunTokens` | from budget | One run |
| `maxWorktrees` | 4 | Concurrent, shared |
| `maxScriptBytes` | 262144 | One script |
| `maxScriptSteps` | 4096 | Static call-site expansion |
| `maxNestedWorkflows` | 1 | Existing `workflow()` nesting rule |
| `maxRetainedRuns` | 200 | Existing `_MAX_RETAINED_RUNS` |

Notes:

- `maxConcurrentAgents` must be the *same* semaphore `e_subagent_trust_limits_and_isolation.md` defines. `_default_cap()` currently derives its own value; it should read the shared configuration so a user setting one number gets one behavior.
- Exceeding a ceiling stops the run cleanly at a phase boundary where possible, records the partial result, and reports which limit fired and how to raise it. It must never silently truncate a fan-out — a workflow that quietly ran 8 of 20 reviewers and reported success is worse than one that stopped.
- `log()` what was dropped whenever any bound is applied, per the engine's own documented rule about silent caps.

### Budget integration

`_BudgetView` already exposes `total`, `spent()`, and `remaining()`, and `_budget_exhausted()` gates further spawns. Extend:

- A run reserves its projected budget at launch so two concurrent runs cannot both believe they have the full remainder.
- Budget exhaustion mid-run stops at the next boundary and marks the run `error` with a distinct reason, not `done`.
- The reservation is released on completion or cancellation.

## 9. Isolation for workflow agents

Workflow agents that edit files currently share one working tree. Two agents editing the same file in the same run is a data race that produces silently wrong results.

- Add `isolation` to `AgentSpec` and to the `agent()` script primitive, accepting the modes from `e_subagent_trust_limits_and_isolation.md`.
- `isolation="worktree"` gives an agent its own checkout, subject to the shared worktree cap.
- Diffs are returned as data; the workflow script decides what to do with them. **Never auto-merge** — same rule, same reason.
- Record isolation per step so it participates in `step_hash`: changing an agent's isolation changes its identity and must invalidate replay.
- Cleanup is part of run finalization and of cancellation, with a startup sweep for leaks.

## 10. Save as command

```text
[s]ave as command  →  name: review-changes
                      scope: project | user
```

Writes a definition or script file to the standard workflows directory (`WORKFLOWS_SUBDIR`), with:

- The exact script source and its `meta` block.
- Declared inputs derived from observed `args` usage, so the saved command takes parameters.
- Provenance: originating run id, script hash, date, and the fact that it was model-authored.
- Availability as `/name` through the existing command discovery path.

Trust rule: a saved workflow in a **project** directory is untrusted on first use in a fresh clone and requires approval, using the same content-hash trust machinery as MCP servers (`mcp/manager.py`'s `project_mcp_is_trusted` / `_file_hash`) and project agent personas. A repository must not be able to define a `/deploy` command that runs on first invocation.

## 11. Determinism and script hardening

The AST allowlist is strong. Additions rather than relaxations:

- **Namespace audit test.** Assert the exact set of names available to a script. Any addition must be a deliberate, reviewed change — a test that fails when the namespace grows is the mechanism.
- **No time, no randomness.** Assert neither is reachable, including transitively through injected objects.
- **Attribute access on results.** Agent results are strings or validated schema objects; ensure a returned object cannot expose dunder access through a path the AST check does not see (the check is static; a dynamically returned object is not).
- **Resource guards at runtime.** The static check cannot bound a `while True` loop. Add: step-count ceiling, wall-clock ceiling, and a cooperative cancellation check between steps so `stop()` is honored inside a hot loop.
- **Recursion.** `workflow()` nesting is already one level; enforce it in the runtime, not only by documentation.
- **`meta` purity.** `extract_meta` already requires a pure literal; keep it and add a test for the failure message quality, since this is the most common authoring mistake.

## 12. Configuration

```json
{
  "workflows": {
    "enabled": true,
    "review": {
      "requireApproval": "threshold",
      "thresholdAgents": 10,
      "thresholdTokens": 200000,
      "thresholdCostUsd": 2.0,
      "alwaysApproveUnbounded": true
    },
    "limits": {
      "maxAgentsPerRun": 200,
      "maxAgentsPerPhase": 64,
      "maxRunWallSeconds": 3600,
      "maxScriptBytes": 262144,
      "maxScriptSteps": 4096,
      "maxWorktrees": 4
    },
    "resume": {
      "mode": "strict-prefix",
      "allowIndependentContentCache": true,
      "requireScriptHashMatch": true
    },
    "store": {
      "retainRuns": 200,
      "retainDays": 30,
      "maxRecordBytes": 4194304,
      "spillResults": true
    },
    "saveAsCommand": {"enabled": true, "trustProject": "prompt"}
  }
}
```

Environment: the existing `MANTIS_AGENT_DISABLE_WORKFLOWS` is preserved. Add `MANTIS_WORKFLOW_NO_APPROVAL=1` for automation contexts, which must be `session`-trust and therefore incapable of widening beyond what user policy allows.

## 13. Surface

```text
/workflows                     runs: id, definition, status, agents, cost
/workflows show <id>           phases, steps, replay status
/workflows resume <id>         strict-prefix resume with a diff report
/workflows diff <id>           what changed since the recorded run
/workflows project <script>    projection card without running
/workflows save <id> <name>    save as command
/workflows prune [--days N]
```

The viewer (`workflow_view.py`) becomes a projection over the activity registry, gaining per-step replay status:

```text
wfr:4f2a  review-changes            running   14/22 steps
  Scan     ✓ 1/1     replayed
  Review   ✓ 5/5     replayed
  Verify   ● 8/16    live (downstream of edited step 9)
```

## 14. Errors

Extend the existing `WorkflowError(code, message)` shape rather than replacing it:

```text
WorkflowError
├── ScriptError                    (existing; keep line reporting)
├── WorkflowDefinitionError        (existing)
├── ReplayHashMismatchError        # script changed; full re-run
├── ReplayVersionUnsupportedError  # v1 record under v2 semantics
├── ReplayNondeterministicError    # ordering could not be reproduced
├── RecordCorruptError
├── AgentLimitExceededError
├── PhaseLimitExceededError
├── WallClockExceededError
├── StepLimitExceededError
├── BudgetReservationError
├── WorktreeLimitError
├── ApprovalRequiredError          # non-interactive above threshold
└── SavedWorkflowUntrustedError
```

Every limit error names the setting that governs it.

## 15. Delivery phases

### Phase 0 — Reproduce and design

1. Write the pipeline-staleness regression test; confirm it fails today.
2. Enumerate every determinism hazard in the current script namespace.
3. Design `step_hash` chaining and validate it by hand against three real workflows.
4. Measure record sizes to size the spill threshold.
5. Decide the v1→v2 record migration story.

**Exit:** the correctness bug is demonstrated by a failing test; hash design reviewed.

### Phase 1 — Durable record correctness

1. Atomic writes with temp-plus-rename and fsync.
2. Bump `RECORD_VERSION` to 2; add the step ledger; keep v1 readable and replay-ineligible.
3. Persist script source and hash, redacted, with a size cap.
4. Spill large step results to sibling files.
5. Harden `load_record` against corruption and temp files.

**Exit:** no record is lost to a crash; sources are durably associated with runs.

### Phase 2 — Strict-prefix resume

1. Assign deterministic `step_id` and compute chained `step_hash`.
2. Implement prefix replay; stop at first mismatch.
3. Add `independent=True` for genuine fan-out.
4. Add the resume report explaining replayed versus re-run.
5. Add determinism guards and the namespace audit test.

**Exit:** the staleness test passes; resume explains itself.

### Phase 3 — Scale limits

1. Add per-run agent, phase, wall-clock, and step ceilings.
2. Share the concurrency semaphore with the subagent limiter; make `_default_cap` read shared config.
3. Add budget reservation and release.
4. Ensure every bound logs what was dropped and stops cleanly at a boundary.
5. Add cooperative cancellation checks inside script loops.

**Exit:** no run can exceed its ceilings; no silent truncation.

### Phase 4 — Review and projection

1. Implement static projection from the validated AST.
2. Implement historical token estimation and catalog pricing.
3. Implement the approval card with thresholds and unbounded detection.
4. Add `/workflows project`.
5. Add non-interactive fail-closed behavior.

**Exit:** large runs require approval; projections are within target accuracy.

### Phase 5 — Isolation and save-as-command

1. Add `isolation` to `AgentSpec` and the `agent()` primitive; include it in `step_hash`.
2. Wire worktree isolation with the shared cap and cleanup.
3. Implement save-as-command with input derivation and provenance.
4. Add project-workflow trust gating.
5. Add `/workflows save` and command discovery.

### Phase 6 — Hardening

1. Adversarial review of the script sandbox with the extended namespace.
2. Fuzz record readers with truncated, reordered, and version-shifted input.
3. Leak tests for worktrees, budget reservations, and cancelled runs.
4. Soak: concurrent runs with watches and subagents under one shared cap.
5. Remove experimental gating.

## 16. Testing strategy

### Unit

- `step_hash` chaining: a change at each position invalidates exactly the correct suffix.
- Prefix replay stops at first mismatch and never resumes replaying afterward.
- `independent=True` content-cache behavior and its interaction with ancestor changes.
- Determinism: ordinal assignment under `parallel` and `pipeline` with out-of-order completion.
- Namespace audit: exact name set; no time or randomness reachable.
- Atomic save: induced failure before rename, after rename, mid-fsync.
- v1 record loads, views, and refuses replay.
- Source redaction and size capping.
- Every ceiling and its error message naming its setting.
- Projection: literal fan-out counts, loop detection, unbounded marking.
- `cache_key` compatibility for records that still use it.

### Integration

- Real `Workflow` with a deterministic fake runner exercising the staleness scenario end to end.
- Resume across an edited script: correct steps re-run, report accurate.
- Resume across an edited *definition*.
- Budget exhaustion mid-run stops at a boundary and records `error`.
- Concurrency cap shared with subagents under simultaneous load.
- Worktree isolation for two agents editing the same file.
- Crash during `save_run`; history intact on restart.

### End-to-end

- Approval card shown, edited, approved, run.
- Non-interactive above threshold fails closed.
- Save as command, then invoke it with arguments.
- Project-scoped saved workflow requires trust in a fresh clone.
- `/workflows diff` matches what resume actually does.

### Security

- Script attempting `__class__` via attribute, subscript, and f-string.
- Script attempting to reach `time` or `random` through any injected object.
- Script with a literal credential in a prompt — redacted in the record.
- Saved project workflow does not execute before approval.
- Path traversal via run id into `run_path` (guarded by `_SAFE_ID`).
- Unbounded loop honors cancellation and the step ceiling.

### Performance and reliability

- `step_hash` computation cost on a 4,000-step expansion.
- Record save cost with spilled results.
- Resume of a 200-step run.
- Leak test: 100 runs with worktrees and cancellations.

## 17. Documentation

- `docs/guides/workflows.md` — update the existing guide with resume semantics, limits, approval.
- `docs/guides/workflow-resume.md` — strict-prefix model, what invalidates what, `independent=True` and its risk.
- `docs/guides/workflow-scripting.md` — the allowlist, determinism rules, why imports are unavailable.
- `docs/api/workflows.md` — record schema v2, step ledger, public API.
- Migration note: v1 records are viewable but not resumable.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 18. File-level implementation map

New:

- `mantis_agent/workflow/steps.py` — step identity and hashing
- `mantis_agent/workflow/replay.py` — prefix replay
- `mantis_agent/workflow/projection.py` — static analysis and cost
- `mantis_agent/workflow/review.py` — approval card
- `mantis_agent/workflow/limits.py`
- `mantis_agent/workflow/save.py` — save-as-command
- `tests/test_workflow_step_hash.py`
- `tests/test_workflow_prefix_replay.py`
- `tests/test_workflow_staleness_regression.py`
- `tests/test_workflow_record_atomicity.py`
- `tests/test_workflow_determinism.py`
- `tests/test_workflow_namespace_audit.py`
- `tests/test_workflow_limits.py`
- `tests/test_workflow_projection.py`
- `tests/test_workflow_save_command.py`
- `docs/guides/workflow-resume.md`
- `docs/guides/workflow-scripting.md`

Modified:

- `mantis_agent/workflow.py` — step ledger, ceilings, isolation, cancellation checks
- `mantis_agent/workflow_script.py` — namespace audit hooks, runtime guards
- `mantis_agent/workflow_store.py` — atomic save, v2 record, spill, corruption tolerance
- `mantis_agent/workflow_defs.py` — `isolation` on `AgentSpec`, shared caps
- `mantis_agent/workflow_tool.py` — approval flow, projection, resume reporting
- `mantis_agent/workflow_view.py` — projection over the activity registry
- `mantis_agent/budget.py` — reservations
- `mantis_agent/isolation/worktree.py` — shared with subagents
- `mantis_agent/catalog.py` — pricing for projection
- `tests/public_api_surface.txt` — intentional update

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Strict prefix invalidates more than users expect | Clear resume report; `independent=True` escape; documentation of the trade-off |
| `independent=True` is misused and reintroduces staleness | Default is strict; docs state the user is asserting an unverifiable property; recorded in the ledger |
| Hash chaining is subtly wrong | Hand-validated against real workflows; per-position invalidation tests |
| Nondeterministic ordering breaks replay | Ordinals by program order and item index; set-usage warning; explicit error when order cannot be reproduced |
| v2 records break existing tooling | Version bump with v1 readable; schema documented |
| Atomic writes change failure behavior | `save_run` keeps its best-effort contract; failures remain logging problems |
| Approval card adds friction | Threshold-based; small runs unaffected; remembered per definition hash |
| Projections are inaccurate and erode trust | Always a range; unbounded stated as unbounded; actuals recorded to improve estimates |
| Ceilings stop legitimate large runs | Actionable errors naming the setting; clean stop at a boundary with partial results retained |
| Five separate caps persist | One shared semaphore; `_default_cap` reads shared config; test exercising subagent + workflow together |
| Worktrees leak across cancelled runs | Finalization and cancellation cleanup, plus startup sweep |
| Saved project workflows execute on clone | Content-hash trust gating, same machinery as MCP and personas |
| Script sandbox weakened by new namespace entries | Namespace audit test fails on any addition |

## 20. Acceptance checklist

- [ ] The pipeline-staleness scenario re-runs correctly and has a regression test.
- [ ] `step_hash` chains parent identities; changing any step invalidates its whole suffix.
- [ ] Resume reports exactly what replayed and what re-ran.
- [ ] `independent=True` is opt-in and documented as an unverified assertion.
- [ ] Script source and hash are persisted, redacted, and size-capped.
- [ ] Hash mismatch forces a full re-run with an explanation.
- [ ] Records are written atomically; no crash loses history.
- [ ] v1 records load and view but refuse replay.
- [ ] Large step results spill to sibling files.
- [ ] Per-run agent, phase, step, and wall-clock ceilings are enforced.
- [ ] Concurrency is one shared cap across subagents and workflows.
- [ ] No bound is applied silently; every one is logged.
- [ ] Approval card appears above thresholds and states unbounded projections honestly.
- [ ] Non-interactive runs fail closed above thresholds.
- [ ] Workflow agents support worktree isolation; diffs are never auto-merged.
- [ ] Save-as-command works and gates project-scoped workflows behind trust.
- [ ] The script namespace audit test passes and fails on any addition.
- [ ] `ruff check` and the full pytest suite pass.

## 21. Recommended implementation order

1. **Write the staleness regression test first and let it fail.** This plan's headline item is a correctness bug, and the test is the specification.
2. **Fix atomic writes immediately.** It is a five-line change that prevents permanent data loss and blocks nothing.
3. **Land the v2 record with the step ledger and script hash.** Prefix replay cannot be built without a place to record step identity.
4. **Implement `step_hash` chaining and prefix replay**, with the resume report shipping in the same release — replay that does not explain itself is not an improvement.
5. **Add determinism guards and the namespace audit test** alongside replay, since replay soundness depends on them.
6. **Consolidate the caps** onto the shared subagent semaphore before adding new ceilings, so the new ones are not a sixth independent number.
7. **Add ceilings and budget reservation.**
8. **Add projection and the approval card** — user-visible, but safe to defer because per-agent permissions already bound the blast radius of any single step.
9. **Add worktree isolation** once `e_subagent_trust_limits_and_isolation.md` has extracted the lifecycle from `swarm.py`.
10. **Add save-as-command last**, with trust gating included from the first commit rather than added afterward.
