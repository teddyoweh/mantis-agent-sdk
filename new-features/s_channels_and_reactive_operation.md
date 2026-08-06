# Channels and Reactive Operation — Extensive Implementation Plan

**Status:** Proposed
**Target:** A new `mantis_agent/channels/` package over the daemon, jobs, and workflow engines
**Objective:** Normalize inbound events — webhooks, chat messages, alerts, email, forge events, MCP notifications — into authenticated, deduplicated, durable triggers that start sessions, run named workflows, or post notifications, under explicit policy.

## 1. Executive summary

Mantis is reactive in one direction only: it reacts to things it decided to watch.

`watch.py` (393 lines) runs a command and streams its output as events, with real operational care — `_BATCH_WINDOW_S = 0.2` batching, `_MAX_BATCH_LINES = 40`, `_MAX_EVENT_CHARS = 4000`, `_DEFAULT_TIMEOUT_MS = 300_000` with `_MAX_TIMEOUT_MS = 3_600_000`, and rate limiting via `_RATE_WINDOW_S = 10.0` / `_RATE_MAX_EVENTS = 20`. Those bounds are the right instincts and should be the template for inbound events too.

`cron.py` (463 lines) provides time-based triggering: `parse_schedule` for `every N`, `daily HH:MM`, weekday, and cron expressions; `CronJob` persisted via `load_jobs`/`save_jobs`; `due_jobs`, `run_job`, `tick`, `daemon`; and OS integration through `launchd_plist` and `systemd_units` with `install_scheduler`. So Mantis can already run without a terminal on a schedule.

What does not exist is anything *inbound*. Nothing external can cause Mantis to act. A CI failure, a PR comment, a Slack message, a PagerDuty alert, an email — none of these can trigger anything. The only way to react to an external event today is to poll for it with a watch, which is expensive, high-latency, and impossible for events that leave no local trace.

The gap is not "add a webhook listener." A naive webhook listener attached to an agent is a remote code execution surface with extra steps: an unauthenticated HTTP endpoint whose payload becomes model instructions, running with the user's credentials on the user's machine. The plan therefore centers on four properties, in order of importance:

1. **Authentication before parsing.** A payload is verified — HMAC signature, token, or mTLS — before any of it is interpreted. Unverified bytes are discarded, never inspected.
2. **Events are data, never instructions.** A channel event is untrusted third-party content, handled exactly like MCP output, child reports, and PR content elsewhere in this plan set.
3. **Explicit, narrow routing.** An event does not "give the agent a prompt." It matches a declared rule that invokes a *named, pre-approved* workflow or notification with structured parameters. The event supplies data to a template; it does not author the instruction.
4. **Durability and exactly-once-ish delivery.** Events are journaled before acknowledgment, deduplicated by provider id, and retried on failure — otherwise a reactive system silently drops work.

The transport reuses the daemon from `m_session_event_api_and_remote_surfaces.md`. The routing target reuses named workflows from `d_workflow_safety_resume_and_scale.md`. This plan is mostly ingestion, verification, and policy.

## 2. Goals

### User outcomes

- A failing CI run opens a session that investigates it.
- A PR comment mentioning the bot triages the request.
- A Slack message routes to a named workflow with the message as a parameter.
- A monitoring alert triggers a diagnostic workflow and posts a summary back.
- Events that arrive while the machine is asleep are processed on wake, not lost.
- Full visibility: what arrived, what matched, what ran, what was rejected and why.

### Engineering goals

- Provider-neutral core; each provider is a thin adapter with a verification method.
- Reuse the daemon for the listener; no second server.
- Reuse named workflows as the only execution target; no ad-hoc prompt injection.
- Reuse `cron.py`'s persistence and scheduler-installation patterns for the durable queue.
- Bound everything, following `watch.py`'s existing discipline.
- Fail closed on verification, routing ambiguity, policy, and budget.
- Python 3.9–3.14, stdlib-only ingestion.

### Success metrics

- No unverified payload is parsed beyond its signature envelope — asserted by test.
- Duplicate delivery of the same provider event id executes exactly once.
- Events survive daemon restart and machine sleep with no loss.
- No channel event can cause an unapproved workflow or an unbounded prompt to run.
- The injection corpus passes: event content never alters what runs or what is reported.
- Idle listener cost under 5 MB RSS and negligible CPU.

## 3. Non-goals

- A hosted webhook relay. Mantis listens locally; public reachability is the user's tunnel or their CI calling out.
- A general message-bus integration (Kafka, SQS). The adapter interface allows them later.
- Autonomous action on every event. Reactive runs are bounded, policy-gated, and default to read-only work.
- Replacing `cron.py`. Time triggers stay there; channels are event triggers, and both feed one routing layer.
- Replacing `watch.py`. Local polling remains the right tool for local file and command changes.
- Two-way chat presence. Posting a reply is supported; maintaining a conversational bot is not.

## 4. Current integration points

- `mantis_agent/watch.py` — `make_watch_tool`, `make_watch_stop_tool`, `_watch_env`, `_watch_cwd`, `_coerce_timeout_ms`, and the batching and rate-limiting constants that set the precedent for event bounds.
- `mantis_agent/cron.py` — `parse_schedule`, `CronJob`, `jobs_path`, `log_dir`, `load_jobs`, `save_jobs`, `due_jobs`, `run_job`, `tick`, `daemon`, `install_scheduler`, `launchd_plist`, `systemd_units`, `_LAUNCHD_LABEL`. The persistence and OS-integration model to reuse.
- `mantis_agent/daemon/` — from `m_session_event_api_and_remote_surfaces.md`. The listener runs inside it.
- `mantis_agent/workflow_defs.py` — `discover_workflow_definitions`, `load_workflow_definition`, `InputSpec`, `resolve_inputs`, `render_template`, `WorkflowDefinition`. Named workflows are the execution target and `InputSpec` is the parameter contract.
- `mantis_agent/jobs.py` — reactive runs are durable jobs.
- `mantis_agent/hooks.py` — `Notification`, `FileChanged`, `Elicitation`; channels dispatch `Notification`.
- `mantis_agent/http.py` — outbound replies with URL validation.
- `mantis_agent/mcp/` — MCP server notifications as a channel source.
- `mantis_agent/permissions.py`, `sandbox.py`, `budget.py` — reactive runs are the least-supervised execution in the product and need the tightest defaults.
- `mantis_agent/ci/forge/` — from `u_github_gitlab_ci_review_and_autofix.md`; forge adapters are reused for replies.

## 5. Architecture

```text
inbound
  ├─ HTTP listener (daemon, loopback or tunnel)
  ├─ pollers (IMAP, forge API, MCP notifications)
  └─ local sources (cron tick, watch event)
             │
             ▼
      ┌─────────────┐
      │  verify     │  signature / token / mTLS — before parsing
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │  normalize  │  provider payload → ChannelEvent
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │  journal    │  durable, before ack
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │  dedupe     │  by (channel, provider_event_id)
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │  route      │  match rules → action
      └──────┬──────┘
             ▼
   notify | workflow | session | ignore
```

Verification precedes normalization. Journaling precedes acknowledgment. Deduplication precedes routing. Each ordering is a correctness property, not an implementation detail.

### Listener

- Runs inside `mantisd`; no separate process.
- Binds loopback by default. Public exposure is the user's choice via their own tunnel, documented with its risks.
- Optional TLS with the daemon's certificate machinery.
- Bounded: max body size (default 1 MB), max concurrent requests, per-channel rate limit, request timeout.
- Returns `202 Accepted` after journaling, before processing. A provider must not wait on an agent run, and a timeout must not cause the provider to retry into a duplicate.
- Unknown paths return `404` with no information about configured channels.

## 6. Event model

```python
class ChannelEvent(msgspec.Struct, frozen=True):
    id: str                     # mantis-assigned
    channel: str                # configured channel name
    provider: str               # github | gitlab | slack | generic | imap | mcp
    provider_event_id: str      # for dedupe; from a provider header or a body hash
    kind: str                   # normalized: ci.failed, pr.comment, alert.firing, message
    received_at: float
    verified: bool              # always true past the gate; recorded for audit
    actor: str                  # provider-reported sender; untrusted
    subject: str                # neutralized
    body: str                   # neutralized
    fields: dict[str, str]      # normalized, whitelisted scalars
    raw_ref: str                # path to the stored raw payload
    signature_method: str
```

Rules:

- `fields` contains only **whitelisted, typed, scalar** values the adapter extracted — repository name, PR number, branch, alert name, severity, sender id. Not the whole payload. Routing conditions may only reference `fields`, never `body`.
- `subject` and `body` are neutralized on ingest with the shared neutralizer (control characters, ANSI, bidi, framing markers, length caps) and stored quoted.
- The raw payload is stored separately, redacted, with bounded retention, for debugging. It is never fed to a model.
- `actor` is what the provider claims. It is a routing input only when the provider's signature covers it, and that is recorded per adapter.

## 7. Verification

Non-negotiable and adapter-specific.

| Provider | Method |
|---|---|
| GitHub | `X-Hub-Signature-256` HMAC-SHA256 over the raw body |
| GitLab | `X-Gitlab-Token` constant-time compare |
| Slack | `v0` HMAC with timestamp, plus a 5-minute freshness window |
| Generic | HMAC over the raw body with a configured header and secret |
| mTLS | Client certificate pinned by fingerprint |
| IMAP | Poller; authenticity from the account, plus optional DKIM/sender allowlist |
| MCP | Trusted server connection already authenticated |

Requirements:

- **Verify over the raw bytes**, before JSON parsing. Parsing unverified input is itself an attack surface, and re-serializing changes the bytes the signature covered.
- `hmac.compare_digest` everywhere; never `==`.
- Timestamp freshness where the provider supplies it, to bound replay.
- Replay defense: `(channel, provider_event_id)` recorded and refused on repeat, independent of dedupe-for-idempotency.
- Secrets from the OS keychain or a `0o600` file, never from project settings. A repository must not be able to configure a channel secret.
- A channel with no verification method configured **refuses to start**, with an explicit error. There is no unauthenticated mode, not even for loopback — a local unauthenticated port is reachable by any process on the machine.
- Verification failures are counted, rate-limited, and logged with source address, never with payload content.

## 8. Routing

```json
{
  "channels": {
    "ci": {
      "provider": "github",
      "verify": {"method": "hmac-sha256", "secretRef": "keychain:mantis/ci"},
      "path": "/hooks/ci",
      "rules": [
        {
          "when": {"kind": "ci.failed", "fields.repo": "teddyoweh/mantis", "fields.branch": "main"},
          "action": "workflow",
          "workflow": "diagnose-ci-failure",
          "inputs": {"run_id": "{fields.run_id}", "repo": "{fields.repo}"},
          "policy": "readonly",
          "cooldownSeconds": 300
        },
        {
          "when": {"kind": "pr.comment", "fields.mentions_bot": "true"},
          "action": "notify",
          "message": "PR #{fields.pr} mentions you"
        }
      ]
    }
  }
}
```

Rules:

- Conditions are exact or glob matches on `kind` and `fields.*` only. No expression language, no regex over `body`. Matching against attacker-controlled free text is how a routing rule becomes a bypass.
- First match wins; unmatched events are journaled and ignored, which is the default.
- `inputs` interpolate `fields` into the workflow's declared `InputSpec` parameters. Values are validated against the spec and sanitized; a value that fails validation fails the rule rather than being coerced.
- **Only named, discovered workflows may be invoked** — never an inline prompt, never a script from the payload. This is the single most important routing rule.
- `cooldownSeconds` per rule prevents an event storm from starting a hundred runs.
- Concurrency cap per channel and globally.

### Actions

| Action | Behavior |
|---|---|
| `notify` | Desktop or push notification. No agent runs. **The safe default.** |
| `workflow` | Run a named workflow with validated inputs, as a durable job |
| `session` | Start a headless session from a **template** with the event as labeled data |
| `ignore` | Journal only |

`session` is the most powerful and is off unless explicitly enabled. Even then, the event never becomes the prompt: a stored template is the prompt, and the event is attached as untrusted labeled data.

## 9. Execution policy

Reactive runs are the least-supervised execution Mantis performs — no human is present, by definition. Defaults must reflect that.

| Policy | Permissions | Sandbox | Network |
|---|---|---|---|
| `readonly` (default) | Read-only tools only; all mutations denied | `hardened` | Provider + declared hosts |
| `standard` | Deny-by-default rules; explicit allows | `hardened` | Declared hosts |
| `elevated` | Configured rules; requires per-channel opt-in | `workspace` | Declared hosts |

Enforced regardless of policy:

- **No interactive asker.** Per the existing `_resolve_ask` behavior, an explicit ask fails closed — which is exactly right here.
- `bypass` mode is unavailable to reactive runs, unconditionally.
- `dangerouslyDisableSandbox` is refused.
- Hard token, cost, and wall-clock budgets per run and per channel per hour.
- Provider API key scrubbed from subprocesses; channel secrets never in the run's environment.
- Every reactive run appears in the activity graph tagged `source="channel"` so it is never mistaken for user-initiated work.

### Outbound replies

- Reply targets are declared per channel, not taken from the payload. A webhook that says "post the result to `https://attacker.example`" is ignored — the reply URL comes from configuration.
- Reply content is structured and template-rendered, following the publication discipline in `u_github_gitlab_ci_review_and_autofix.md`: validated fields into fixed templates, no model-authored markdown posted verbatim.
- Rate-limited, with failures retried and eventually surfaced.

## 10. Durability

Reuse `cron.py`'s file-backed model and `b_durable_jobs_and_reattachment.md`'s journal discipline.

```text
~/.mantis/channels/
  config.json
  queue.db              SQLite: events, status, attempts, dedupe index
  journal/<date>.jsonl  append-only received events
  raw/<event-id>.json   redacted raw payloads, bounded retention
  replies/              outbound queue with retry state
```

- Journal write and queue insert happen **before** the HTTP response. An acknowledged event is durable.
- `(channel, provider_event_id)` is a unique index; a duplicate insert is detected and the event acknowledged without re-processing.
- Processing is at-least-once with idempotent routing; combined with dedupe, effective behavior is once per provider event.
- Failed runs retry with backoff up to a cap, then move to a dead-letter state that is visible and manually retryable.
- Events arriving while the daemon is down are missed at the HTTP layer — providers retry, and pollers catch up by cursor. Document this precisely rather than implying guaranteed capture.
- Machine sleep: on wake, the queue is drained and pollers resume from their cursors.
- Retention: journal 30 days, raw payloads 7 days, dead letters until cleared.

## 11. Pollers

Not everything can push.

- **IMAP** — poll a folder, filter by sender allowlist and subject pattern, mark processed, cursor by UID. Email is the least authenticated channel and defaults to `notify` only.
- **Forge polling** — for environments without webhook reachability; cursor by event id or timestamp, honoring rate limits.
- **MCP notifications** — a connected server's notifications become channel events; the server's existing trust decision governs.

Pollers share the same normalize → journal → dedupe → route pipeline. Only ingestion differs.

## 12. Surface

```text
mantis channel list
mantis channel add <name> --provider P --path /hooks/x --secret-ref R
mantis channel test <name> --file payload.json     # verify + route, no execute
mantis channel enable|disable <name>
mantis channel log [--channel C] [--status S]
mantis channel retry <event-id>
mantis channel dead-letters
mantis channel url                                  # local URL + setup instructions
mantis channel secret set <name>
```

`mantis channel test` is essential: it verifies a signature, normalizes, and shows which rule would match and what would run — without running it. Configuring routing rules by triggering real events is not viable.

```text
$ mantis channel test ci --file ci-failed.json
verified   hmac-sha256 ✓
normalized kind=ci.failed  repo=teddyoweh/mantis  branch=main  run_id=1842
matched    rule 1 → workflow diagnose-ci-failure
inputs     run_id=1842  repo=teddyoweh/mantis
policy     readonly · hardened sandbox · budget 100k tokens
cooldown   ok (last run 41m ago)
```

In the TUI, `/channels` shows configured channels, recent events, and reactive runs; reactive runs appear in the activity rail tagged by source.

## 13. Configuration

```json
{
  "channels": {
    "enabled": false,
    "listener": {
      "enabled": false,
      "host": "127.0.0.1",
      "port": 0,
      "tls": false,
      "maxBodyBytes": 1048576,
      "maxConcurrent": 8,
      "requestTimeoutMs": 5000
    },
    "defaults": {
      "action": "notify",
      "policy": "readonly",
      "cooldownSeconds": 60,
      "maxRunsPerHour": 20,
      "budget": {"maxTokens": 150000, "maxCostUsd": 1.5, "maxWallSeconds": 600}
    },
    "retention": {"journalDays": 30, "rawDays": 7},
    "retry": {"maxAttempts": 4, "initialMs": 5000, "maxMs": 300000},
    "allowSessionAction": false,
    "channels": {}
  }
}
```

`channels.enabled` and `listener.enabled` both default to `false`. `allowSessionAction` defaults to `false`. Channel definitions are **user- or managed-tier only**; a project settings file may not define a channel, configure a secret, or change a policy. A cloned repository must not be able to open a listening port or route events.

Environment: `MANTIS_CHANNELS=0|1`, `MANTIS_CHANNELS_NO_EXEC=1` (forces every rule to `notify`).

`MANTIS_CHANNELS_NO_EXEC=1` is the panic switch: events still arrive and journal, but nothing executes.

## 14. Errors

```text
ChannelError                      (base)
├── ChannelConfigError
├── ChannelNoVerificationError    # refuses to start
├── SignatureInvalidError
├── SignatureStaleError           # timestamp outside the window
├── ReplayDetectedError
├── PayloadTooLargeError
├── PayloadMalformedError
├── ChannelRateLimitedError
├── NoMatchingRuleError           # journaled, not an error path
├── WorkflowNotFoundError         # rule names an unknown workflow
├── InputValidationError
├── CooldownActiveError
├── ChannelBudgetExceededError
├── ReplyTargetInvalidError       # target not in configuration
├── DeadLetterError
└── PollerAuthError
```

Verification failures return a generic `401` with no detail — a verbose error is an oracle for forging signatures.

## 15. Delivery phases

### Phase 0 — Threat model and design

1. Write the threat model: an inbound endpoint on a developer machine with the user's credentials.
2. Enumerate provider verification methods and confirm raw-body access is available in the listener.
3. Build the injection corpus for event content.
4. Prototype journal-before-ack and measure latency.
5. Confirm named-workflow invocation with `InputSpec` validation covers the real use cases.

**Exit:** threat model reviewed; verification feasible for each provider; corpus ready.

### Phase 1 — Ingestion and journal, notify only

1. Add `channels/` with the event model, journal, and SQLite queue.
2. Implement the listener in the daemon with bounds and `202`-after-journal.
3. Implement HMAC verification over raw bytes for generic, GitHub, GitLab, and Slack.
4. Implement normalization with neutralization and whitelisted `fields`.
5. Implement dedupe, replay defense, and the `notify` action only.

**Exit:** verified events arrive, journal, deduplicate, and notify. Nothing executes.

### Phase 2 — Routing and workflows

1. Implement rule matching on `kind` and `fields` only.
2. Implement the `workflow` action against named workflows with `InputSpec` validation.
3. Implement cooldowns, per-channel concurrency, and hourly caps.
4. Implement the `readonly` policy with hardened sandbox and no asker.
5. Implement `mantis channel test`.

**Exit:** a CI failure runs a pre-approved diagnostic workflow under a read-only policy.

### Phase 3 — Durability and operations

1. Implement retry with backoff and dead letters.
2. Implement retention and pruning.
3. Implement sleep/wake recovery and poller cursors.
4. Implement `channel log`, `retry`, `dead-letters`.
5. Surface reactive runs in the activity graph tagged by source.

**Exit:** no acknowledged event is lost; failures are visible and retryable.

### Phase 4 — Pollers and replies

1. Implement the IMAP poller with sender allowlist, defaulting to `notify`.
2. Implement forge polling with cursors and rate-limit handling.
3. Implement MCP notification ingestion.
4. Implement outbound replies to configured targets with structured templates.
5. Implement reply retry and failure surfacing.

**Exit:** channels work without inbound reachability; results post back safely.

### Phase 5 — Elevated policy and hardening

1. Implement `standard` and `elevated` policies with per-channel opt-in.
2. Implement the `session` action behind `allowSessionAction`.
3. Adversarial review: signature bypass, replay, SSRF via reply targets, injection via event content, routing bypass via `body`.
4. Fuzz payload parsing and rule matching.
5. Load test: event storms, cooldown and rate-limit behavior.

## 16. Testing strategy

### Unit

- Signature verification per provider: valid, invalid, truncated, wrong algorithm, timing-safe comparison.
- Verification over raw bytes, not re-serialized JSON.
- Timestamp freshness windows and replay refusal.
- Normalization: whitelisted `fields` only; `body` neutralized against the injection corpus.
- Dedupe on `(channel, provider_event_id)`; concurrent duplicate inserts.
- Rule matching: exact, glob, no-match, first-match-wins; conditions referencing `body` rejected at config load.
- Input interpolation and `InputSpec` validation, including values that fail validation.
- Cooldown, per-channel concurrency, hourly caps.
- Policy enforcement: `bypass` refused, sandbox escape refused, no asker.
- Reply target from configuration only; payload-supplied target ignored.
- Retry backoff, attempt cap, dead-letter transition.
- Retention pruning.

### Integration

- Real GitHub webhook payload verified and routed end to end.
- Slack payload with a stale timestamp refused.
- Duplicate delivery executes exactly once.
- Daemon restart mid-queue; events drain correctly.
- Machine sleep simulation; pollers resume by cursor.
- Workflow run appears as a durable job tagged `source="channel"`.
- Failed workflow retries and dead-letters.
- `channel test` matches real routing behavior.

### End-to-end

- CI failure → diagnostic workflow → reply posted to the configured target.
- PR comment mentioning the bot → notification.
- IMAP message → notification.
- MCP notification → channel event.
- `MANTIS_CHANNELS_NO_EXEC=1` stops all execution while events still journal.

### Security

- Unsigned or wrongly-signed payload is discarded **before parsing** — asserted by instrumenting the parser.
- Replayed payload with a valid signature is refused.
- Event body containing injection text does not change what runs or what is reported.
- A routing rule cannot be configured to match against `body`.
- Payload-supplied reply URL is never used.
- Reply target pointing at a private or metadata address is refused.
- Project settings attempting to define a channel or set a secret are rejected.
- A rule naming an unknown or non-discovered workflow fails at load.
- Reactive run cannot set `bypass`, approve a sandbox escape, or resolve an ask.
- Channel secrets absent from run environments, logs, traces, and journals.
- Verification failure responses leak no detail.

### Performance and reliability

- Listener throughput and journal-before-ack latency.
- Event storm: 1,000 events, cooldowns and caps hold, no unbounded fan-out.
- Idle listener memory and CPU.
- Queue drain after a long outage.
- Leak test: 1,000 events with no leaked connections, tasks, or descriptors.

## 17. Documentation

- `docs/guides/channels.md` — concepts, providers, setup, routing, testing.
- `docs/guides/channels-security.md` — the threat model, why verification precedes parsing, why events are never prompts, why routing cannot match on body, policy tiers, exposure risks of tunneling.
- `docs/guides/channels-recipes.md` — CI failure diagnosis, PR triage, alert response, email notification.
- `docs/api/channels.md` — `ChannelEvent`, adapter interface, configuration schema.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 18. File-level implementation map

New:

- `mantis_agent/channels/__init__.py`
- `mantis_agent/channels/types.py` — `ChannelEvent`, rules, config
- `mantis_agent/channels/listener.py` — daemon HTTP endpoint
- `mantis_agent/channels/verify.py` — per-provider verification
- `mantis_agent/channels/normalize.py` — payload → event, neutralization
- `mantis_agent/channels/queue.py` — SQLite queue, dedupe, retry, dead letters
- `mantis_agent/channels/journal.py`
- `mantis_agent/channels/route.py` — matching and action dispatch
- `mantis_agent/channels/policy.py` — execution policy tiers
- `mantis_agent/channels/reply.py` — outbound
- `mantis_agent/channels/providers/github.py`, `gitlab.py`, `slack.py`, `generic.py`, `imap.py`, `mcp.py`
- `mantis_agent/channels/commands.py`
- `tests/test_channel_verify.py`
- `tests/test_channel_normalize.py`
- `tests/test_channel_dedupe.py`
- `tests/test_channel_routing.py`
- `tests/test_channel_policy.py`
- `tests/test_channel_durability.py`
- `tests/test_channel_reply.py`
- `tests/test_channel_security.py`
- `tests/fixtures/channels/**`
- `docs/guides/channels.md`
- `docs/guides/channels-security.md`

Modified:

- `mantis_agent/daemon/server.py` — listener registration
- `mantis_agent/cron.py` — share the trigger/routing layer
- `mantis_agent/jobs.py` — reactive runs as durable jobs
- `mantis_agent/workflow_defs.py` — invocation with validated inputs
- `mantis_agent/hooks.py` — `Notification` dispatch
- `mantis_agent/activity/` — `source="channel"` tagging
- `mantis_agent/sandbox.py`, `permissions.py`, `budget.py` — reactive policy
- `mantis_agent/http.py` — reply URL validation
- `mantis_agent/cli.py` — `channel` command family
- `mantis_agent/tui_fullscreen.py` — `/channels`
- `tests/public_api_surface.txt` — intentional update

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Inbound endpoint becomes remote code execution | Mandatory verification before parsing; named workflows only; read-only default policy; no `bypass`, no sandbox escape |
| Payload parsed before verification | Raw-byte HMAC; parser instrumented in tests to prove ordering |
| Event content steers the agent | Neutralization, labeled envelope, routing cannot match on body, injection corpus |
| Routing rule matching free text becomes a bypass | Conditions restricted to `kind` and whitelisted `fields`; rejected at config load otherwise |
| Reply becomes an SSRF or exfiltration channel | Targets from configuration only; URL validation; payload-supplied targets ignored |
| Event storm causes runaway cost | Cooldowns, per-channel and hourly caps, budgets, concurrency limits |
| Duplicate delivery duplicates work | Unique dedupe index; journal-before-ack; idempotent routing |
| Acknowledged events lost on crash | Durable journal and queue before the HTTP response |
| Project repository configures a channel | Channels are user/managed tier only; enforced at load |
| Secrets in project settings | `secretRef` to keychain or `0o600` file; project settings rejected |
| Verbose errors help forge signatures | Generic `401`, no detail, rate-limited |
| Email is weakly authenticated | Sender allowlist, `notify`-only default, documented as the weakest channel |
| Users expose the listener publicly without understanding | Loopback default, explicit documentation of tunnel risk, TLS support |
| Silent failure of reactive work | Dead letters visible, retry surfaced, activity-graph tagging |

## 20. Acceptance checklist

- [ ] No payload is parsed before its signature verifies, proven by instrumentation.
- [ ] Every channel requires a verification method or refuses to start.
- [ ] Comparisons use `hmac.compare_digest`; freshness windows and replay defense enforced.
- [ ] Events journal durably before acknowledgment.
- [ ] Dedupe by `(channel, provider_event_id)` yields exactly-once execution.
- [ ] Event content is neutralized and stored quoted; raw payloads never reach a model.
- [ ] Routing conditions may reference only `kind` and whitelisted `fields`.
- [ ] Only named, discovered workflows can be invoked; no inline prompts.
- [ ] Inputs validate against `InputSpec`; invalid values fail the rule.
- [ ] `notify` and `readonly` are the defaults; `session` requires explicit opt-in.
- [ ] Reactive runs cannot use `bypass`, escape the sandbox, or resolve an ask.
- [ ] Reply targets come from configuration only and are URL-validated.
- [ ] Cooldowns, concurrency, hourly caps, and budgets are enforced.
- [ ] Failures retry and dead-letter visibly.
- [ ] Project settings cannot define channels or secrets.
- [ ] `MANTIS_CHANNELS_NO_EXEC=1` stops all execution while journaling continues.
- [ ] `mantis channel test` matches real routing behavior.
- [ ] Reactive runs are tagged `source="channel"` in the activity graph.
- [ ] `ruff check` and the full pytest suite pass.

## 21. Recommended implementation order

1. **Write the threat model first.** An inbound endpoint on a developer machine holding the user's credentials is a fundamentally different posture from anything else Mantis does, and the design must start from that rather than from the feature.
2. **Ship ingestion with `notify` as the only action.** Verified events that arrive, journal, deduplicate, and produce a desktop notification are genuinely useful, carry almost no risk, and prove the entire pipeline. This should be a full release on its own.
3. **Get verification right before anything executes** — raw-byte HMAC, constant-time comparison, freshness, replay defense — and instrument the parser in tests to prove nothing is parsed first.
4. **Add routing restricted to `kind` and `fields` fourth.** The temptation to allow regex over `body` will be strong and must be refused; matching on attacker-controlled free text turns every rule into a potential bypass.
5. **Add the `workflow` action fifth, with `readonly` policy and named workflows only.** Never ship a path where a payload contributes to a prompt.
6. **Add durability, retry, and dead letters sixth.** A reactive system that silently drops work is worse than no reactive system, because the user stops checking.
7. **Add pollers seventh** — they need no inbound reachability and widen adoption considerably.
8. **Add replies eighth**, reusing the structured-template publication discipline from the CI plan rather than posting model-authored text.
9. **Add `elevated` policy and the `session` action last, if at all.** Most value is available at `readonly`, and each step above that removes a safeguard from the least-supervised execution path in the product.
