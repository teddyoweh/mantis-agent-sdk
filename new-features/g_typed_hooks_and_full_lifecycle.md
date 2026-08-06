# Typed Hooks and Full Lifecycle — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/hooks.py`, `mantis_agent/agent.py`, and `mantis_agent/settings.py`
**Objective:** Turn hooks from an SDK-only Python callback registry into a configurable, typed, bounded extension system that fires the complete lifecycle, supports command/HTTP/MCP/prompt handlers, and fails closed when a blocking hook cannot render a verdict.

## 1. Executive summary

`mantis_agent/hooks.py` is 375 lines and unusually honest about its own limits. It declares 27 events in `HOOK_EVENTS`, then immediately narrows to a `DISPATCHED_EVENTS` frozenset of seven and documents why:

> Of the full upstream vocabulary above, these are the events the mantis agent loop actually *fires* today. […] Keeping this set explicit makes the contract honest: we don't silently advertise events that never fire.

That comment is exactly right, and it is also the work item. Twenty of twenty-seven declared events never fire. `HookDispatcher.is_dispatched()` exists so integrators can discover this at runtime, which is a good mitigation for a gap that should be closed rather than mitigated.

Five further limits matter as much as the missing events:

**Handlers are Python callables only.** `HookFn = Callable[[HookContext], Awaitable[HookResult | None]]`. There is no way to configure a hook from `settings.json`. Every other extension point in Mantis — skills, agents, MCP servers, workflows, permissions — is file-configurable. Hooks are the exception, which means a user cannot add one without writing Python and constructing the agent themselves. Command hooks (`"type": "command"`) are the single most-used hook form in the wider ecosystem and Mantis cannot express them at all.

**`HookResult` is binary.** `block: bool`. A hook can allow or cancel. It cannot ask the user, defer, or return a structured reason that the permission layer understands. The permission system has a three-valued `Allow | Deny | Ask` union; hooks have a boolean. These should meet.

**Hooks are unbounded.** `res = await fn(ctx)` has no timeout, no cancellation, and no concurrency control. A hook that hangs hangs the agent loop permanently, with no diagnostic beyond the absence of progress.

**Hook failure fails open.** This is the most serious issue:

```python
try:
    res = await fn(ctx)
except Exception:  # noqa: BLE001 — user code; never crash the loop
    logger.exception("hook %r raised; treating as no-op", event)
    continue
```

For an observability hook, treating a crash as a no-op is correct. For a `PreToolUse` hook whose entire purpose is to block dangerous calls, it is a security failure: the hook crashes, the exception is logged, and the tool call proceeds. A user who installs a hook to prevent writes outside a directory gets no protection the moment that hook hits an unexpected input. Failure mode must be declared per hook, and blocking hooks must default to fail-closed.

**Matchers only see the tool name.** `_matcher_hits` fnmatches `ctx.tool.name`. A hook cannot scope itself to "writes under `src/`" or "bash commands containing `git push`" without matching every call and filtering internally — which means paying the dispatch cost on every call and writing the filter logic in every hook.

This plan closes all of it while keeping `Hooks`, `HookContext`, `HookResult`, `HookMatcher`, and `HookDispatcher` importable and behaviorally compatible.

## 2. Goals

### User outcomes

- Configure a hook in `settings.json` without writing Python: run a command, call an HTTP endpoint, invoke an MCP tool, or inject a prompt.
- Register for any of the 27 declared events and have it actually fire.
- Have a formatter run after every edit, a linter gate every commit, a notifier fire when a long job finishes — all declaratively.
- Get a clear error when a hook times out, crashes, or returns malformed output, instead of silent no-op or an unexplained hang.
- Know that a security hook which crashes blocks the operation rather than waving it through.
- Let a slow hook run in the background and deliver its result into the conversation when it finishes.

### Engineering goals

- Preserve the existing SDK surface exactly. `Hooks(pre_tool_use=fn)` keeps working with identical semantics for non-blocking hooks.
- Keep `HookDispatcher.dispatch(event, ctx)` as the single funnel the agent loop calls.
- Keep `has(event)` cheap; the loop uses it to skip context assembly on the common no-hook path.
- Make `DISPATCHED_EVENTS` equal `HOOK_EVENTS` at the end of this work, so `is_dispatched()` becomes trivially true and the honesty comment can be deleted.
- Bound every handler: timeout, output size, concurrency, and retries.
- No new required dependencies. HTTP hooks reuse `mantis_agent/http.py`.
- Python 3.9–3.14.

### Success metrics

- All 27 events dispatch, each with an integration test asserting it fires with a populated context.
- Command, HTTP, MCP, and prompt handler types work from `settings.json`.
- A hanging hook is terminated at its timeout with a structured error; the agent loop continues.
- A crashing blocking hook denies the operation, proven by test.
- Hook dispatch adds under 50 µs when no hook is registered for the event.
- Zero regressions in the existing hook test suite.

## 3. Non-goals

- A plugin distribution format for hooks — that is `j_plugin_packages_and_marketplaces.md`, which will package hooks defined by this plan.
- Sandboxing hook commands beyond what `mantis_agent/sandbox.py` already provides. Command hooks inherit the session's sandbox posture; they do not get a bespoke one.
- Replacing `can_use_tool`. Hooks and the permission callback remain distinct; §8 defines how they compose.
- Remote hook delivery — `m_session_event_api_and_remote_surfaces.md` owns that transport.
- Hook authoring UI. Configuration is files plus `/hooks` inspection commands.

## 4. Current integration points

- `mantis_agent/hooks.py` — `HOOK_EVENTS`, `DISPATCHED_EVENTS`, `RESERVED_EVENTS`, `HookContext`, `HookResult`, `HookFn`, `HookMatcher`, `HookSpec`, `Hooks`, `HookDispatcher`, `_normalize_hooks`, `_matcher_hits`, `_camel_to_snake`, `_EVENT_TO_FIELD`.
- `mantis_agent/agent.py` — the seven live dispatch points: `PreToolUse`, `PostToolUse`, `PostToolUseFailure` around tool execution; `UserPromptSubmit` before a user turn; `PreCompact` before compaction; `Stop` at a stopping point; `PermissionDenied` on refusal. Twenty more dispatch points land here.
- `mantis_agent/permissions.py` — `recheck_mutated_input` already exists precisely because a `PreToolUse` hook can rewrite input. Every new mutation path must route through it.
- `mantis_agent/settings.py` — `load_settings`, `merge_settings`, `_deep_merge`, `_union_list`; hook configuration is loaded and trust-layered here.
- `mantis_agent/subagent.py` — `SubagentStart` / `SubagentStop` belong around `make_task_tool` and `make_pair_tool` execution.
- `mantis_agent/session.py`, `mantis_agent/session_tree.py` — `SessionStart` / `SessionEnd`.
- `mantis_agent/compact.py` (678 lines) — `PreCompact` fires; `PostCompact` does not.
- `mantis_agent/jobs.py` — `TaskCreated` / `TaskCompleted` map to `JobManager.spawn` and the terminal callback.
- `mantis_agent/swarm.py` — `WorktreeCreate` / `WorktreeRemove`.
- `mantis_agent/cron.py`, `mantis_agent/watch.py` — `Notification` sources.
- `mantis_agent/http.py` (206 lines) — the HTTP handler transport.
- `mantis_agent/tracing.py` — hook spans.
- `mantis_agent/paths.py` — resolving hook script paths.

## 5. Handler types

A hook becomes a tagged union. The Python callable stays as one variant among five.

### `python`

Unchanged. The existing `HookFn`. Registered through the `Hooks` dataclass in SDK use.

### `command`

```json
{
  "type": "command",
  "command": "./scripts/fmt.sh",
  "args": ["$TOOL_INPUT_PATH"],
  "timeoutMs": 5000,
  "cwd": "$PROJECT_ROOT",
  "onFailure": "block"
}
```

- Context is delivered on **stdin as JSON**, not as arguments. Arguments support a small, explicitly enumerated substitution set (`$TOOL_NAME`, `$TOOL_INPUT_PATH`, `$SESSION_ID`, `$PROJECT_ROOT`, `$EVENT`) and nothing else. No shell interpolation of arbitrary context fields — that is how a filename becomes a command.
- Executed with `argv` directly, never through a shell, unless the config sets `"shell": true`, which requires the command to originate from `user` or `managed` trust.
- **Exit-code contract:**

| Exit | Meaning |
|---|---|
| `0` | Allow; stdout parsed as an optional JSON `HookResult` |
| `1` | Non-blocking error; logged, treated as no-op |
| `2` | **Block**; stderr becomes the block reason shown to the model |
| other | Treated per `onFailure` (default `block` for blocking events, `ignore` otherwise) |

- stdout over the size cap is truncated and the hook is treated as malformed.
- Inherits the session sandbox. A command hook is not a sandbox escape.

### `http`

```json
{
  "type": "http",
  "url": "https://policy.internal/hook",
  "method": "POST",
  "headers": {"Authorization": "Bearer ${env:POLICY_TOKEN}"},
  "timeoutMs": 3000,
  "retries": 1,
  "onFailure": "block"
}
```

- Context posted as JSON; response parsed as a JSON `HookResult`.
- URL validated at configuration time and at every call: HTTPS required unless the host is loopback; no redirects followed to a different origin without revalidation; private-network and metadata addresses blocked by the same rules `h_sandbox_egress_credentials_and_escape_controls.md` defines.
- Secrets in headers come from `${env:...}` references resolved at call time and never persisted into logs, traces, or the decision record.
- Bounded retries with jittered backoff, only for idempotent (non-blocking) events. A blocking event is never retried, because a retry after a timeout risks double-executing a side effect.

### `mcp`

```json
{"type": "mcp", "server": "policy", "tool": "check_edit", "timeoutMs": 5000}
```

Invokes a tool on a connected MCP server. Depends on `i_mcp_oauth_and_dynamic_lifecycle.md` for connection lifecycle. If the server is disconnected, `onFailure` governs.

### `prompt`

```json
{"type": "prompt", "text": "Remember: this repo requires conventional commits.", "once": true}
```

Injects text into the model's context at the event point. Valid only for `UserPromptSubmit`, `SessionStart`, `InstructionsLoaded`, `PreCompact`, and `PostCompact`. The injected text is labeled as configuration-sourced, not user-sourced, so the model can weigh it correctly — and so a project-level prompt hook cannot impersonate the user. `once: true` injects only on first occurrence per session.

### Declaration

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": {"tool": "Write|Edit", "path": "src/**"},
        "handlers": [{"type": "command", "command": "./scripts/check.sh", "onFailure": "block"}]
      }
    ],
    "PostToolUse": [
      {"matcher": {"tool": "Edit"}, "handlers": [{"type": "command", "command": "ruff format $TOOL_INPUT_PATH"}]}
    ],
    "SessionEnd": [
      {"handlers": [{"type": "command", "command": "./scripts/cleanup.sh"}]}
    ]
  }
}
```

## 6. Matchers

Today `HookMatcher.matcher` is one fnmatch pattern against the tool name. Widen it to a structured, still-cheap predicate:

```python
@dataclass(frozen=True)
class HookMatcher:
    hook: HookFn | HandlerSpec
    matcher: str | None = None          # legacy: fnmatch on tool name
    tool: str | None = None             # regex alternation, e.g. "Write|Edit"
    path: str | None = None             # gitignore-style, resolved before match
    command: str | None = None          # substring/regex against shell command
    agent: str | None = None            # subagent type name
    source: str | None = None           # user | model | hook | schedule
    when: str | None = None             # tiny expression, see below
```

Rules:

- All specified fields must match (AND). Unspecified fields are ignored.
- `matcher` alone preserves exact current behavior, including "non-tool events always match."
- `path` reuses the gitignore matcher and realpath resolution from `f_permission_policy_engine_and_auto_mode.md`. The two features must share one implementation; two path matchers that disagree is a bug generator.
- `when` supports only `field op value` with `==`, `!=`, `in`, `contains` over a fixed field allowlist. No arbitrary evaluation, ever.
- Matching happens before context assembly where possible, so a narrow matcher costs almost nothing on non-matching calls.

## 7. The complete lifecycle

Twenty events need dispatch points. Each entry names the module and the semantic moment.

| Event | Where | Fires when | Blocking |
|---|---|---|---|
| `SessionStart` | `session.py` / TUI boot | Session created or resumed | No |
| `SessionEnd` | TUI shutdown, SDK close | Session closing; must run on cancellation too | No |
| `Setup` | `cli.py` / `setup_wizard.py` | First run in a project | No |
| `InstructionsLoaded` | agent init | CLAUDE.md / rules loaded | Yes (may amend) |
| `UserPromptSubmit` | `agent.py` | Already fires | Yes |
| `PreToolUse` | `agent.py` | Already fires | Yes |
| `PostToolUse` | `agent.py` | Already fires | No |
| `PostToolUseFailure` | `agent.py` | Already fires | No |
| `PermissionRequest` | `permissions.py` | An `Ask` is raised, before the user sees it | Yes |
| `PermissionDenied` | `agent.py` | Already fires | No |
| `Stop` | `agent.py` | Already fires | Yes |
| `StopFailure` | `agent.py` | Run ends in error | No |
| `PreCompact` | `compact.py` | Already fires | Yes |
| `PostCompact` | `compact.py` | After compaction, with the new message list | No |
| `SubagentStart` | `subagent.py` | `make_task_tool` / `make_pair_tool` begins a child | Yes |
| `SubagentStop` | `subagent.py` | Child terminal, before its report is ingested | Yes |
| `TaskCreated` | `jobs.py` | `JobManager.spawn` | No |
| `TaskCompleted` | `jobs.py` | Terminal `on_event` | No |
| `Notification` | `watch.py`, `cron.py`, `jobs.py` | User-facing notification raised | No |
| `Elicitation` | MCP layer | Server requests user input | Yes |
| `ElicitationResult` | MCP layer | User responded | No |
| `ConfigChange` | `settings.py` | Settings reloaded | No |
| `FileChanged` | `watch.py` | Watched file changed | No |
| `CwdChanged` | TUI / `cli.py` | Working directory changed | No |
| `WorktreeCreate` | `swarm.py` | Worktree created | No |
| `WorktreeRemove` | `swarm.py` | Worktree removed | No |
| `TeammateIdle` | teams runtime | Peer has no work | No |

`SubagentStop` is the highest-value addition and deserves emphasis: it is the natural enforcement point for the child-output neutralization that `e_subagent_trust_limits_and_isolation.md` specifies. A blocking `SubagentStop` hook that inspects a child's report before it re-enters the parent's context is the difference between a trust boundary and a comment about one.

`TeammateIdle` depends on `c_agent_teams.md` and may remain reserved until teams ship. That is acceptable *provided* `is_dispatched()` continues to report it honestly.

### Paired events

Add `HookStarted` and `HookCompleted` as internal observability events (not in `HOOK_EVENTS`, not user-registrable) emitted to the activity registry and tracing. They carry event name, handler type, duration, outcome, and truncated note. This is how a user answers "which hook is making my session slow" without instrumenting each hook.

## 8. Result model

### Widening `HookResult`

```python
class HookResult(msgspec.Struct, omit_defaults=True):
    block: bool = False                       # unchanged
    mutated_input: dict[str, Any] | None = None
    note: str | None = None
    # new
    decision: Literal["allow", "deny", "ask", "defer", ""] = ""
    reason: str = ""
    inject: str = ""                          # context to add
    inject_role: Literal["system", "user"] = "system"
    defer_token: str = ""                     # for async rewake
    metadata: dict[str, Any] = msgspec.field(default_factory=dict)
```

Compatibility: `block=True` is exactly equivalent to `decision="deny"`. Existing hooks returning `block` keep working; the dispatcher normalizes.

### Precedence across multiple hooks

The existing loop chains mutations and short-circuits on the first block. Formalize:

1. `deny` from any hook wins immediately; remaining hooks are skipped.
2. `ask` beats `allow`. If any hook asks and none deny, the operation goes to the permission asker.
3. `defer` is recorded and the operation proceeds only if the event is non-blocking; on a blocking event, `defer` degrades to `ask`.
4. Mutations chain in registration order, each hook seeing the previous result — as today.
5. `inject` accumulates from all hooks, in order, deduplicated.
6. Notes accumulate, as today.

### Mandatory revalidation

`recheck_mutated_input` already exists and already documents the threat: a callback "approved some *other* input and handed back this rewritten one; the rewrite itself was never vetted."

Extend that discipline. **Any** hook mutation, at any event, must trigger permission revalidation before the mutated input is used. Today only the `PreToolUse` path is covered because it is the only mutating path. As `InstructionsLoaded`, `SubagentStart`, and `PermissionRequest` gain mutation ability, each needs the same recheck. Encode this as a single helper the dispatcher calls, not as a rule each call site remembers.

### Async hooks and rewake

A hook may return `decision="defer"` with a `defer_token`. The dispatcher:

- Records a pending deferral in the activity registry as a `blocked` node when the event is blocking.
- Runs the handler in the background with its own timeout.
- On completion, delivers the result as a `Notification`-class injection into the next model turn, tagged with the token and the originating event.
- On timeout, resolves per `onFailure`.

Deferral must never silently vanish. Every deferred hook resolves to exactly one of: delivered, timed out, cancelled by session end.

## 9. Bounding and failure policy

### Per-handler configuration

```json
{
  "timeoutMs": 5000,
  "maxOutputBytes": 65536,
  "onFailure": "block",
  "onTimeout": "block",
  "retries": 0,
  "concurrency": 1
}
```

### Defaults by event class

| Event class | `onFailure` | `onTimeout` | Timeout |
|---|---|---|---|
| Blocking (`PreToolUse`, `PermissionRequest`, `SubagentStop`, `Stop`, `PreCompact`, `InstructionsLoaded`, `UserPromptSubmit`) | `block` | `block` | 5 s |
| Non-blocking (everything else) | `ignore` | `ignore` | 10 s |

This is the fix for the fail-open bug. A blocking hook that raises now denies. The existing behavior — swallow and continue — remains the default only for non-blocking events, where it is correct.

Migration matters here: existing SDK users have Python hooks registered on `PreToolUse` that may raise on edge inputs and currently no-op. Changing the default to `block` will surface real bugs as blocked operations. Ship it behind `hooks.failClosedOnBlocking` defaulting to `false` for one release, log a warning on every swallowed blocking-hook exception naming the hook, then flip the default with a changelog entry.

### Timeout and cancellation

- Every handler runs under `asyncio.wait_for` with its timeout.
- Command handlers are terminated with SIGTERM, then SIGKILL after a grace period, and their process group is reaped so a hook cannot leave orphans.
- Session cancellation cancels all in-flight hooks, including deferred ones.
- Cleanup is idempotent and shielded from cancellation, matching the pattern used elsewhere in the codebase.

### Reentrancy

A hook must not trigger its own event. Track an in-dispatch event set per task; a nested dispatch of an already-active event is refused and logged. Without this, a `FileChanged` hook that writes a file loops forever.

## 10. Security

Hooks execute code and see everything. The trust model is the plan.

- **Trust layering.** Hook configuration follows the layering from `f_permission_policy_engine_and_auto_mode.md`. A `project` or `local` settings file may define hooks, but a project-level hook is **untrusted by default**: it prompts for approval on first use, showing the command or URL, and the approval is remembered per project with a content hash. Editing the hook re-prompts. A cloned repository must not be able to execute a command the moment the user opens it.
- **`"shell": true` requires user or managed trust.** Argv execution is the default everywhere else.
- **No context interpolation into commands.** Only the enumerated substitution variables, each shell-escaped. Tool inputs are model-controlled strings; interpolating one into a command line is a direct injection path.
- **Secrets.** Hook stdin, HTTP bodies, logs, traces, and notes are redacted with the shared recursive redactor. `${env:...}` values are resolved at call time, never logged, and never included in the activity record.
- **Hooks receive data, not authority.** A hook's `inject` text enters the model's context labeled by source; it cannot grant a permission, change the mode, or approve a plan. A hook's `decision` is consumed by the permission layer as an input to `_decide`, and can only narrow — a hook returning `allow` does not override a deny rule.
- **URL validation** for HTTP handlers at config time and per call, including redirect revalidation.
- **Output bounds** on every handler; oversize output is truncated and treated as malformed rather than parsed partially.
- **Path safety.** Hook script paths resolve through `paths.py` and must live inside the project or a user-trusted directory; a hook path pointing into `/tmp` or a downloads directory is refused.

## 11. Configuration

```json
{
  "hooks": {
    "enabled": true,
    "failClosedOnBlocking": false,
    "defaultTimeoutMs": 5000,
    "maxOutputBytes": 65536,
    "maxConcurrent": 8,
    "allowProjectHooks": "prompt",
    "allowShell": false,
    "deferrals": {"enabled": true, "maxPending": 16, "timeoutMs": 120000},
    "PreToolUse": [],
    "PostToolUse": []
  }
}
```

Environment:

- `MANTIS_HOOKS=0|1`
- `MANTIS_HOOKS_TIMEOUT_MS`
- `MANTIS_HOOKS_NO_PROJECT=1`

`MANTIS_HOOKS=0` must disable every hook including managed ones — a user must always be able to boot without third-party code running. This is an availability escape hatch, and it is safe because disabling hooks can only remove capability, never grant it.

## 12. TUI and CLI surface

```text
/hooks                     list configured hooks by event, with source and trust
/hooks events              all 27 events, dispatch status, registered count
/hooks test <event>        dispatch a synthetic context, print results
/hooks log [n]             recent HookStarted/HookCompleted with durations
/hooks trust <id>          approve a project hook after showing its content
/hooks disable <id>        session-scoped disable
/hooks reload              re-read hook configuration
```

`/hooks events` directly replaces the need for the `DISPATCHED_EVENTS` documentation comment:

```text
event                fires  handlers  source
PreToolUse           yes    2         user, project(untrusted)
PostToolUse          yes    1         user
SubagentStop         yes    0         —
TeammateIdle         no     0         — (requires teams)
```

Rendering rules: hook activity appears in the activity rail as child nodes of the operation they gate, so a slow hook is visibly the reason a tool call is pending rather than an unexplained stall.

## 13. Errors

```text
HookError                     (base)
├── HookConfigError           # malformed declaration
├── HookHandlerTypeError      # unknown type
├── HookTimeoutError
├── HookOutputError           # unparseable or oversize output
├── HookCommandError          # spawn failed, non-contract exit
├── HookHttpError
├── HookMcpUnavailableError
├── HookUntrustedError        # project hook not approved
├── HookReentrancyError
├── HookDeferralLostError
└── HookBlockedOperation      # normal control flow, not a fault
```

Errors carry event, handler id, handler type, and duration. Configuration errors are reported at load with file and line; a malformed handler on a **blocking** event fails the load rather than being dropped, mirroring the deny-rule asymmetry in the permissions plan and for the same reason.

## 14. Delivery phases

### Phase 0 — Design and audit

1. Enumerate the exact call sites for all 20 undispatched events; confirm each has a well-defined moment and a populated context.
2. Decide which contexts need new `HookContext` fields versus `arbitrary`.
3. Prototype the command handler and validate the exit-code contract on macOS and Linux.
4. Measure dispatch overhead for the no-hook path to protect the `has()` fast path.
5. Survey existing SDK hook users for fail-closed migration risk.

**Exit:** dispatch-point map approved; no measurable overhead regression.

### Phase 1 — Bounding and failure policy

1. Add per-handler timeout, output cap, and concurrency.
2. Add `onFailure` / `onTimeout` with event-class defaults.
3. Add `failClosedOnBlocking` behind a flag defaulting to `false`, with warnings.
4. Add reentrancy guarding.
5. Add cancellation and process-group reaping.

**Exit:** a hanging hook cannot hang the loop; a crashing blocking hook can be made to block.

### Phase 2 — Result model

1. Widen `HookResult` with `decision`, `reason`, `inject`, `defer_token`, `metadata`.
2. Normalize `block` ↔ `decision="deny"`.
3. Implement precedence across multiple hooks.
4. Route every mutation through mandatory permission revalidation.
5. Add `HookStarted` / `HookCompleted` observability.

**Exit:** hooks can ask rather than only block; every mutation is revalidated.

### Phase 3 — Handler types

1. Add the handler tagged union and `HandlerSpec` parsing.
2. Implement `command` with stdin JSON, argv execution, and the exit-code contract.
3. Implement `http` over `mantis_agent/http.py` with URL validation.
4. Implement `prompt` with source labeling.
5. Implement `mcp` (gated on MCP lifecycle work).

**Exit:** hooks are configurable from `settings.json`; no Python required.

### Phase 4 — Full lifecycle

1. Add the 20 missing dispatch points, module by module, each with a test.
2. Extend `HookContext` where needed.
3. Set `DISPATCHED_EVENTS = frozenset(HOOK_EVENTS)` and delete the honesty comment.
4. Keep `is_dispatched()` as public API — it stays useful for custom events.
5. Add `/hooks events`.

**Exit:** every declared event fires.

### Phase 5 — Trust and configuration

1. Add trust layering and project-hook approval with content hashing.
2. Add `${env:...}` resolution and redaction.
3. Add path safety for hook scripts.
4. Add `/hooks trust`, `/hooks disable`, `/hooks reload`.
5. Add settings validation with per-line errors.

**Exit:** a cloned repository cannot execute code without explicit approval.

### Phase 6 — Deferrals and hardening

1. Implement deferred execution and rewake delivery.
2. Adversarial review: injection through substitution variables, SSRF through HTTP handlers, reentrancy loops, deferral leaks.
3. Flip `failClosedOnBlocking` to `true`.
4. Fuzz handler output parsing.
5. Remove experimental gating.

## 15. Testing strategy

### Unit

- `_normalize_hooks` across all `HookSpec` shapes including the SDK-shaped `{matcher, hooks: [...]}` form.
- Matcher evaluation for every field and combination, including the legacy `matcher`-only path.
- Precedence: deny > ask > defer > allow; mutation chaining; note and inject accumulation.
- Exit-code contract for every code including out-of-range.
- Output parsing: valid, malformed, oversize, empty, non-UTF-8.
- Timeout and cancellation for each handler type.
- Reentrancy refusal.
- `onFailure` / `onTimeout` for both event classes.
- Substitution variables: escaping, unknown variable, injection attempts.
- Trust: project hook untrusted, approved, content changed → re-prompt.

### Integration

- Each of the 27 events fires with a populated context, asserted individually.
- `PreToolUse` mutation triggers `recheck_mutated_input`.
- A `SubagentStop` hook inspects and can block a child report.
- A `PostToolUse` command hook formats a file and the change is visible.
- An HTTP hook denies an operation and the reason reaches the model.
- A deferred hook resolves into a later turn.
- Hook failure with `failClosedOnBlocking` on and off.

### End-to-end

- `settings.json`-only configuration, no Python, full session.
- `/hooks test` matches real dispatch behavior.
- Hook activity appears on the activity rail as the reason for a pending call.
- Session cancellation reaps command-hook processes; leak test asserts zero orphans.

### Security

- Tool input containing `; rm -rf ~` reaches a command hook without executing.
- HTTP handler pointed at `169.254.169.254` is refused.
- HTTP handler redirected cross-origin is revalidated.
- Project hook executes only after approval; modified hook re-prompts.
- `${env:TOKEN}` never appears in logs, traces, notes, or activity records.
- A hook returning `allow` cannot override a deny rule.
- A `prompt` handler cannot impersonate the user role.
- `FileChanged` hook that writes a file does not loop.

### Performance

- No-hook dispatch under 50 µs.
- 8 concurrent hooks respect the concurrency cap.
- Command handler spawn latency measured and bounded.
- No agent-loop throughput regression with observability enabled.

## 16. Documentation

- `docs/guides/hooks.md` — concepts, all 27 events with when they fire and whether they block, handler types, worked examples.
- `docs/guides/hooks-security.md` — trust model, project approval, injection defenses, what a hook can and cannot do.
- `docs/api/hooks.md` — `Hooks`, `HookContext`, `HookResult`, `HookMatcher`, `HookDispatcher`, handler specs.
- Migration note for `failClosedOnBlocking`.
- Cookbook: format-on-edit, lint gate, commit-message check, desktop notification, policy-server integration.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 17. File-level implementation map

New:

- `mantis_agent/hooks/__init__.py` (re-exports the current `__all__` verbatim)
- `mantis_agent/hooks/events.py` — vocabulary and dispatch registry
- `mantis_agent/hooks/matchers.py`
- `mantis_agent/hooks/handlers/__init__.py`
- `mantis_agent/hooks/handlers/command.py`
- `mantis_agent/hooks/handlers/http.py`
- `mantis_agent/hooks/handlers/mcp.py`
- `mantis_agent/hooks/handlers/prompt.py`
- `mantis_agent/hooks/dispatcher.py`
- `mantis_agent/hooks/config.py`
- `mantis_agent/hooks/trust.py`
- `mantis_agent/hooks/deferrals.py`
- `tests/test_hook_matchers.py`
- `tests/test_hook_handlers_command.py`
- `tests/test_hook_handlers_http.py`
- `tests/test_hook_precedence.py`
- `tests/test_hook_lifecycle_events.py`
- `tests/test_hook_timeout_cancel.py`
- `tests/test_hook_trust.py`
- `tests/test_hook_security.py`
- `docs/guides/hooks.md`
- `docs/guides/hooks-security.md`

Modified:

- `mantis_agent/hooks.py` → package `__init__` re-exporting `__all__` unchanged
- `mantis_agent/agent.py` — new dispatch points, mandatory revalidation
- `mantis_agent/permissions.py` — `PermissionRequest` dispatch, hook decision as input
- `mantis_agent/subagent.py` — `SubagentStart` / `SubagentStop`
- `mantis_agent/compact.py` — `PostCompact`
- `mantis_agent/jobs.py` — `TaskCreated` / `TaskCompleted`
- `mantis_agent/swarm.py` — worktree events
- `mantis_agent/watch.py`, `mantis_agent/cron.py` — `Notification`, `FileChanged`
- `mantis_agent/session.py`, `session_tree.py` — session events
- `mantis_agent/settings.py` — hook config and trust
- `mantis_agent/http.py` — reused by the HTTP handler
- `mantis_agent/tui_fullscreen.py` — `/hooks` commands
- `mantis_agent/tracing.py` — hook spans
- `tests/public_api_surface.txt` — intentional update

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Fail-closed default breaks existing SDK users | Flag defaults `false` for one release, warn on every swallowed blocking exception, then flip with changelog |
| Command hooks become an injection vector | Argv execution, stdin JSON, enumerated escaped substitutions, no shell by default |
| Project hooks execute on clone | Untrusted by default, content-hash approval, path restrictions |
| HTTP hooks enable SSRF | Shared URL validation, redirect revalidation, private/metadata blocking |
| Hooks hang the loop | Per-handler timeout, cancellation, process-group reaping |
| Reentrant hook loops | In-dispatch event set, refusal with diagnostic |
| Twenty new dispatch points destabilize the loop | One module per phase step, each with a test; non-blocking events first |
| Hook overhead on every tool call | `has()` fast path preserved; matchers evaluated before context assembly |
| Deferred hooks leak | Bounded pending set; every deferral resolves to delivered, timed out, or cancelled |
| Splitting the module breaks imports | Package `__init__` re-exports `__all__`; snapshot test |
| Hooks gain authority through `inject` | Injection is labeled by source and cannot alter permissions |
| MCP handler blocked on MCP work | Ships last; `is_dispatched`-style honesty until then |

## 19. Acceptance checklist

- [ ] All 27 events dispatch with populated contexts; `DISPATCHED_EVENTS == HOOK_EVENTS`.
- [ ] `command`, `http`, `prompt`, and `mcp` handlers work from `settings.json`.
- [ ] Exit-code contract implemented and tested for every code.
- [ ] Every handler is bounded by timeout, output cap, and concurrency.
- [ ] Blocking-hook failure blocks; non-blocking-hook failure is ignored.
- [ ] Every hook mutation triggers permission revalidation.
- [ ] `decision` supports allow/deny/ask/defer with defined precedence.
- [ ] Deferrals always resolve; none leak.
- [ ] Project hooks require approval; content changes re-prompt.
- [ ] No context field is interpolated into a command line.
- [ ] Secrets never reach logs, traces, notes, or activity records.
- [ ] Reentrancy is refused.
- [ ] Cancellation reaps hook processes; leak test passes.
- [ ] `/hooks events` reports dispatch status accurately.
- [ ] Existing SDK hook registration is unchanged and tested.
- [ ] Public API surface unchanged except intentional additions.
- [ ] `ruff check` and the full pytest suite pass.

## 20. Recommended implementation order

1. Bounding first — timeout, cancellation, output caps, reentrancy. This is pure hardening of code that already runs and needs no new concepts.
2. Failure policy next, behind the compatibility flag. Fixing fail-open on blocking hooks is the highest-severity item in this plan and should not wait for handler types.
3. Widen `HookResult` and formalize precedence. Everything downstream depends on the richer verdict.
4. Add mandatory revalidation on all mutation paths while the result model is fresh.
5. Ship the `command` handler alone. It is the most requested form and validates the whole configuration path.
6. Add trust and project approval **in the same release as `command`** — never after. Shipping configurable command execution without the trust gate is not acceptable even for one version.
7. Add `http` and `prompt`.
8. Add the 20 dispatch points in two batches: non-blocking events first (low risk), blocking events second.
9. Add deferrals last; they are the only piece with cross-turn state.
10. Flip `failClosedOnBlocking`, then unblock `e_subagent_trust_limits_and_isolation.md`, which enforces child-report neutralization at the now-real `SubagentStop`.
