# Advisor Capability Ordering and Inheritance — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/advisor.py` (336 lines), `catalog.py`, `subagent.py`, `workflow.py`
**Objective:** Make the advisor provably an escalation rather than a configuration accident — through provider-neutral capability ordering, per-request validation, inheritance into child agents, prompt-cache-stable context assembly, explicit disable policy, and diagnostics when it is silently unavailable.

## 1. Executive summary

`mantis_agent/advisor.py` implements a genuinely differentiated idea, and its docstring states it well:

> Most turns of a long task are routine; a handful decide whether it works. The advisor pairs a cheap main model with a stronger second one that the agent consults at exactly those moments […] **the advisor doesn't have to live on the same provider as the main model.** Run Qwen on your own box and escalate three decisions an hour to Opus.

The implementation supports that: `AdvisorConfig` exposes a `cross_provider` property, `resolve_advisor` resolves provider, base URL, and key from the catalog independently of the session's model, and configuration comes from `/advisor <model>`, `--advisor`, the `advisorModel` setting, or `MANTIS_ADVISOR`. `make_advisor_tool` exposes `consult_advisor`, and `_render_transcript` feeds the conversation to the advisor bounded by `_MAX_TRANSCRIPT_CHARS = 60_000`.

Five gaps keep it from being reliable in the way the docstring promises.

**Nothing checks that the advisor is stronger.** `resolve_advisor` resolves whatever model string it is given. A user can configure a 7B local model as the advisor for an Opus main session, and the system will dutifully escalate hard decisions to the weaker model. There is no capability ordering, no warning, and no way for the agent to know that consulting the advisor is a downgrade. The entire premise — escalation — depends on an ordering that does not exist.

**Availability is validated once, if at all.** The advisor may be configured and unreachable: a local server that is down, an expired key, a model the provider has retired. Discovery happens when the tool is called, mid-task, at exactly the moment the agent decided it needed help.

**Children do not inherit it.** A subagent spawned by the `task` tool gets a model and a tool set. It does not get the advisor. Nor does a workflow agent, nor a coordinator worker. That is arguably backwards: a cheap child grinding through a hard subproblem is precisely where an escalation is most valuable, and the parent — which has the most context — is where it is least needed.

**Every consultation is a cache miss.** `_render_transcript(messages, limit=60_000)` re-renders the whole conversation on each call. The rendered text grows and changes between calls, so each advisor request presents a different prefix. With a 60,000-character context, a session consulting the advisor five times pays full input cost five times. Prompt caching cannot help because nothing stable is at the front.

**Failures are quiet.** If the advisor is unset, unreachable, or over budget, the tool is simply absent or fails at call time. There is no diagnostic explaining that the escalation path the user configured is not working — and a silently-missing advisor looks exactly like a session where the model chose not to consult one.

## 2. Goals

### User outcomes

- Configure an advisor and be told immediately if it is weaker than the main model, unreachable, or misconfigured.
- Have child agents inherit the escalation path, so the cheap models doing the grinding can escalate.
- Pay for the advisor's context once per session, not once per consultation.
- See when the advisor was consulted, what it cost, and whether it changed the outcome.
- Know when the advisor is unavailable, and why, rather than wondering why it was never used.
- Disable it per session, per agent type, or per workflow phase.

### Engineering goals

- Preserve `AdvisorConfig`, `resolve_advisor`, `advisor_status`, `save_advisor_model`, `clear_advisor_model`, and `make_advisor_tool` as public API.
- Capability ordering must be **provider-neutral**. A ranking that only understands one vendor's models would defeat the module's central premise.
- Ordering is data, refreshable, and overridable — never hardcoded model-name matching.
- Validation is cheap and cached; it must not add latency to every turn.
- Cache-stable context assembly without changing what the advisor sees semantically.
- Python 3.9–3.14.

### Success metrics

- Configuring a weaker advisor produces a warning at configuration time, not at call time.
- Advisor reachability is validated in the background at session start with no added startup latency.
- Child agents inherit the advisor and successfully consult it.
- A session with five consultations shows a measurable prompt-cache hit rate on the shared prefix.
- Every suppression reason is visible in `/advisor status`.
- Unknown models degrade to a documented default rather than blocking configuration.

## 3. Non-goals

- Automatic advisor selection. The user chooses; the system validates and advises.
- Automatic consultation. The agent decides when to escalate; this plan does not add a policy that consults on its behalf.
- A general model-benchmarking framework. Ordering is coarse and declarative, not measured.
- Multi-advisor panels or voting. One advisor.
- Replacing the advisor's system prompt or changing its guidance semantics.
- Routing changes — `routing.py` handles model selection for the main loop and stays separate.

## 4. Current integration points

- `mantis_agent/advisor.py` — `_ENV = "MANTIS_ADVISOR"`, `_SETTING = "advisorModel"`, `_ADVISOR_SYSTEM`, `_MAX_TRANSCRIPT_CHARS = 60_000`, `AdvisorConfig` (+`cross_provider`), `_configured_model`, `resolve_advisor`, `save_advisor_model`, `clear_advisor_model`, `advisor_status`, `_render_transcript` (+`render_block`), `make_advisor_tool`.
- `mantis_agent/catalog.py` (703 lines) — model metadata, providers, base URLs, keys. Capability ordering data lives here.
- `mantis_agent/providers/base.py` — `Provider`; the advisor resolves its own.
- `mantis_agent/subagent.py` — `SubAgentSpec`, `AgentType`, `resolve_agent_tools`, `_SUBAGENT_EXCLUDED_TOOLS`, `make_task_tool`. Inheritance lands here.
- `mantis_agent/coordinator.py` — `_build_workers`; workers are children too.
- `mantis_agent/workflow.py` — `make_agent_runner`; workflow agents likewise.
- `mantis_agent/budget.py` — `BudgetTracker`; advisor calls are billable and currently accounted loosely.
- `mantis_agent/compact.py` — compaction changes the transcript, invalidating any advisor prefix cache.
- `mantis_agent/routing.py` (133 lines) — model selection; distinct but adjacent.
- `mantis_agent/activity/` — consultations as activity nodes.
- `mantis_agent/settings.py` — `advisorModel` and new policy keys.
- `mantis_agent/tui_fullscreen.py` — `/advisor` command and status.

## 5. Capability ordering

### The requirement

The advisor must be at least as capable as the main model, and the check must work across providers — including local models the catalog may know little about.

### Tier model

Rather than ranking individual models, assign coarse tiers. Coarse is a feature: it is robust to new releases, does not pretend to precision the data cannot support, and is easy to reason about.

```json
{
  "capabilityTiers": {
    "version": 3,
    "updated": "2026-08-01",
    "tiers": {
      "frontier":  {"rank": 5, "description": "Strongest available reasoning"},
      "advanced":  {"rank": 4},
      "standard":  {"rank": 3},
      "fast":      {"rank": 2},
      "small":     {"rank": 1}
    },
    "models": {
      "claude-opus-5": "frontier",
      "claude-sonnet-5": "advanced",
      "claude-haiku-4-5-20251001": "fast"
    },
    "patterns": [
      {"match": "*-opus-*", "tier": "frontier"},
      {"match": "*-sonnet-*", "tier": "advanced"},
      {"match": "*-haiku-*", "tier": "fast"},
      {"match": "*:70b*", "tier": "standard"},
      {"match": "*:7b*", "tier": "small"},
      {"match": "*:8b*", "tier": "small"}
    ],
    "default": "standard"
  }
}
```

Rules:

- **Explicit entries win over patterns; patterns win over the default.**
- Patterns are essential for local models, where names like `qwen3:32b` carry their own size information and no registry will ever be complete.
- Unknown models resolve to `standard` and are **flagged as unknown**, so the resulting comparison is reported as low-confidence rather than presented as fact.
- The table is data shipped in the package, overridable by user settings, and refreshable independently of a release — model landscapes change faster than release cycles.
- A user may pin a tier for a specific model: `"capabilityOverrides": {"my-local-model": "advanced"}`. Someone running a strong local model must be able to say so.

### Comparison

At configuration time and at session start:

| Relation | Behavior |
|---|---|
| advisor tier > main tier | Normal. Escalation confirmed. |
| advisor tier == main tier | Warn: "same tier; consultation may not add capability." Allowed — a same-tier second opinion from a different provider has real value. |
| advisor tier < main tier | **Warn prominently.** Allowed only with explicit confirmation or `allowDowngrade: true`. |
| either unknown | Warn that the comparison is low-confidence; proceed. |

Never block. A user may have knowledge the table lacks, and refusing a configuration on the basis of a coarse heuristic would be wrong. But the warning must be unmissable, because silently escalating to a weaker model defeats the entire feature.

The advisor's tier is also exposed to the agent in the tool description — "consult a `frontier`-tier advisor" versus "a `fast`-tier advisor" — so the model can weigh the advice appropriately. An agent told it is consulting something stronger will treat the answer differently from one told it is a peer.

## 6. Validation

### At configuration

`/advisor <model>` and `--advisor` validate immediately:

1. Model exists in the catalog or matches a known provider pattern.
2. Provider is resolvable; base URL well-formed; credential present.
3. Tier comparison against the current main model.
4. Optional live reachability probe (a minimal request), behind `--check`.

Report all findings at once rather than one per attempt:

```text
/advisor qwen3:7b
  ⚠ tier: small (rank 1) — main model claude-sonnet-5 is advanced (rank 4)
    Consulting this advisor is a downgrade. Confirm? [y/N]
  ✓ provider: ollama (http://localhost:11434) reachable
  ✓ credentials: not required
```

### At session start

- Background, non-blocking reachability check.
- Result cached for the session with a short TTL.
- Failure marks the advisor `unavailable` with a reason and surfaces it once in the status line — never as a modal interruption.
- The `consult_advisor` tool is **still registered** when the advisor is unreachable, and returns a structured error explaining why. Removing the tool would make the agent believe no escalation path exists, which is a worse failure than a clear error.

### Per request

- Health checked before each call; a stale-cached failure short-circuits with a clear error rather than a timeout.
- Timeout (default 120 s, configurable — advisor calls are legitimately slow).
- On failure: one retry for transient errors, then a structured error the agent can act on. The agent must be able to proceed without the advisor rather than stalling.

## 7. Inheritance

The advisor should flow to child agents, which is where escalation is most valuable.

### Model

Add `advisor` to the spawn context, alongside permissions and budget in `SubAgentSpec`:

```python
advisor: Any = INHERIT      # INHERIT | None | AdvisorConfig
```

Defaults, chosen so the useful case is the default:

| Child type | Default |
|---|---|
| `task` subagent | Inherit |
| Coordinator worker | Inherit |
| Workflow agent | Inherit, overridable per phase or per agent |
| Peer (teams) | Inherit |
| Persistent twin (`pair`) | Do not inherit — read-only twins are advisory themselves |

Controls:

- `AgentType` frontmatter may set `advisor: inherit | none | <model>`.
- A workflow `AgentSpec` and phase may override.
- Global `advisor.inherit: true|false`.

### Constraints

- **A child's advisor may never exceed the parent's tier.** A cheap child must not be able to escalate to something the parent could not. This mirrors the narrowing-only inheritance rule that `e_subagent_trust_limits_and_isolation.md` applies to permissions, and it exists for the same reason: a child should never be able to acquire capability its parent lacks.
- Advisor consultations by children count against the **parent's** budget, and against the shared advisor call budget.
- Per-child consultation caps (default 3) prevent a fan-out of ten children from producing thirty frontier-model calls. This is the cost failure mode this feature has, and it needs an explicit ceiling.
- Children's consultations appear in the activity graph under the child, so the cost is attributable.
- `consult_advisor` is not in `_SUBAGENT_EXCLUDED_TOOLS` and should not be — but it is only granted when inheritance resolves to an available advisor, so a child never receives a tool that cannot work.

## 8. Prompt-cache stability

### The problem

`_render_transcript(messages, limit=_MAX_TRANSCRIPT_CHARS)` produces a fresh rendering of the conversation for every consultation. Because the conversation grows, each call's prompt differs from the last from very early on. Nothing stable sits at the front, so no prefix can be cached, and every consultation pays full input price on up to 60,000 characters.

### Structure for caching

Restructure the advisor request into stable and volatile segments:

```text
[ stable ]  advisor system prompt (_ADVISOR_SYSTEM)          — never changes
[ stable ]  session preamble: project, task framing, rules   — set once per session
[ stable ]  transcript prefix through the last cache anchor  — grows in steps
[ volatile] recent turns since the anchor                    — small
[ volatile] the specific question                            — small
```

- **Anchor advancement.** The transcript prefix advances only at defined anchors — every N turns, or at a size threshold — rather than continuously. Between anchors the prefix is byte-identical, so it caches. This trades a small amount of freshness in the cached region for a large reduction in repeated input cost.
- **Deterministic rendering.** `_render_transcript` must produce byte-identical output for the same messages: stable ordering, no timestamps, no elapsed times, no run-specific identifiers. Any nondeterminism in the rendering defeats caching entirely and would be invisible without a test asserting it.
- **Explicit cache markers** where the provider supports them, so the boundary is declared rather than inferred.
- **Compaction invalidates the anchor.** `compact.py` rewrites history; the advisor's prefix must be rebuilt and the invalidation recorded rather than silently producing a miss.
- **Cross-provider caveat.** Not every provider supports prompt caching, and the advisor is explicitly often on a different provider than the main model. Where caching is unsupported, the structure still helps by keeping the volatile portion small; the plan must not claim savings that a given provider cannot deliver, and `/advisor status` should report whether caching is active.

### Truncation

`_MAX_TRANSCRIPT_CHARS = 60_000` currently truncates. Improve within the same budget:

- Preserve the head (task framing) and the tail (recent context); elide the middle with an explicit marker stating what was omitted.
- Always retain user turns and decisions in preference to tool output — a consultation about an approach needs the reasoning, not the file contents.
- State the omitted amount in the rendered transcript so the advisor knows it is seeing a partial record and can say so.

## 9. Observability

### Status

```text
/advisor
  model        claude-opus-5
  provider     anthropic (cross-provider: yes, main is ollama/qwen3:32b)
  tier         frontier (rank 5) vs main small (rank 1) — escalation ✓
  health       reachable (checked 2m ago)
  caching      supported, prefix anchored at turn 14
  inheritance  enabled — children may consult (max 3 each)
  usage        4 consultations · 128k in · 6.2k out · $2.14
  suppressed   —
```

`suppressed` is the field that fixes the quiet-failure problem. It reports why a consultation did not or could not happen:

```text
suppressed   budget exhausted (advisor spend $5.00 of $5.00)
suppressed   unreachable: connection refused to http://localhost:11434
suppressed   disabled for agent type 'explore'
suppressed   per-child cap reached (3/3) for sub:12
```

### Activity and logging

- Each consultation is an activity node with question summary, latency, tokens, and cost.
- Consultations by children appear under the child.
- `/advisor log` shows recent consultations with truncated questions and answers.
- Traces carry advisor spans with the transcript excluded and the question redacted.

### Cost

- Advisor spend is accounted separately from main-model spend, because they are frequently different providers with different prices and mixing them makes the escalation's cost invisible.
- A separate advisor budget (`advisor.maxCostUsd`) that, when exhausted, suppresses consultations with a visible reason rather than failing silently.

## 10. Disable policy

```json
{
  "advisor": {
    "model": null,
    "enabled": true,
    "allowDowngrade": false,
    "inherit": true,
    "maxConsultationsPerSession": 20,
    "maxConsultationsPerChild": 3,
    "maxCostUsd": 5.0,
    "timeoutMs": 120000,
    "transcript": {
      "maxChars": 60000,
      "anchorEveryTurns": 10,
      "preserveHeadChars": 8000,
      "preserveTailChars": 20000
    },
    "healthCheck": {"onStart": true, "ttlSeconds": 300},
    "disabledForAgentTypes": [],
    "capabilityOverrides": {}
  }
}
```

Precedence: `MANTIS_ADVISOR` env → `--advisor` flag → `advisorModel` setting → none. `/advisor off` clears for the session; `clear_advisor_model` clears persistently.

Disable scopes:

- Session: `/advisor off`.
- Agent type: `disabledForAgentTypes`.
- Workflow phase or agent: per-spec override.
- Global: `advisor.enabled: false`.

`advisor.model` may be set at project tier, but `capabilityOverrides` and `allowDowngrade` may not — a repository must not be able to assert that its preferred model is `frontier`, nor silently permit a downgrade.

## 11. Surface

```text
/advisor                     status (as above)
/advisor <model>             set, with validation
/advisor off                 disable for the session
/advisor check               live reachability and tier check
/advisor log [n]             recent consultations
/advisor tiers               the capability table and where a model resolves
```

`/advisor tiers <model>` answers "why is this considered `small`" by showing whether the tier came from an explicit entry, a pattern match (and which), an override, or the default — which is the only way tier disputes get resolved without reading source.

## 12. Errors

```text
AdvisorError                      (base)
├── AdvisorNotConfiguredError
├── AdvisorModelUnknownError
├── AdvisorProviderError           # unresolvable provider or base URL
├── AdvisorCredentialError
├── AdvisorUnreachableError        # with the underlying reason
├── AdvisorTimeoutError
├── AdvisorDowngradeError          # tier lower, not confirmed
├── AdvisorBudgetExhaustedError
├── AdvisorConsultationLimitError  # session or per-child
├── AdvisorDisabledError           # with the scope that disabled it
└── AdvisorTierUnknownError        # low-confidence comparison, warning only
```

Every error returned to the agent is structured and actionable: the agent should be able to note that escalation was unavailable and proceed, rather than retrying or stalling.

## 13. Delivery phases

### Phase 0 — Audit and data

1. Inventory the models users actually configure as advisors, across providers.
2. Draft the tier table and pattern set; validate coverage against catalog contents and common local model names.
3. Measure current advisor call cost and confirm the absence of cache hits.
4. Prototype the anchored-prefix structure and measure the savings on a real session.
5. Confirm which providers in the catalog support prompt caching.

**Exit:** tier table covers the common cases; cache savings measured and provider support mapped.

### Phase 1 — Capability ordering

1. Add the tier table to `catalog.py` with explicit entries, patterns, and default.
2. Implement resolution with confidence reporting and user overrides.
3. Implement comparison and warnings at configuration time.
4. Add `allowDowngrade` and the confirmation flow.
5. Add `/advisor tiers` explaining resolution.

**Exit:** a downgrade is impossible to configure unknowingly.

### Phase 2 — Validation and diagnostics

1. Implement configuration-time validation reporting all findings at once.
2. Implement background session-start health checks with TTL caching.
3. Keep the tool registered when unavailable; return structured errors.
4. Implement per-request health short-circuit, timeout, and single retry.
5. Implement `suppressed` reporting in `/advisor` and the status line.

**Exit:** the advisor is never silently absent; every suppression has a visible reason.

### Phase 3 — Cache stability

1. Make `_render_transcript` deterministic; add a byte-identity test.
2. Restructure requests into stable and volatile segments.
3. Implement anchor advancement and explicit cache markers.
4. Implement head/tail-preserving truncation with an omission marker.
5. Handle compaction invalidation; report caching state in status.

**Exit:** repeated consultations hit the cache where supported; savings measured.

### Phase 4 — Inheritance

1. Add `advisor` to `SubAgentSpec` with an `INHERIT` sentinel.
2. Implement tier-narrowing enforcement for children.
3. Wire coordinator workers and workflow agents.
4. Implement per-child consultation caps and parent budget accounting.
5. Add `AgentType` and workflow spec overrides.

**Exit:** children escalate, bounded and attributable.

### Phase 5 — Observability and hardening

1. Activity nodes for consultations, including children's.
2. `/advisor log` and separate cost accounting.
3. Advisor budget with visible suppression.
4. Adversarial review: project-tier override abuse, transcript leakage, cross-provider credential handling.
5. Remove experimental gating.

## 14. Testing strategy

### Unit

- Tier resolution: explicit entry, pattern match, override, default, unknown-with-flag.
- Pattern precedence and specificity.
- Comparison outcomes for every tier relation, including unknowns.
- `allowDowngrade` gating and confirmation.
- Configuration validation: unknown model, unresolvable provider, missing credential, malformed base URL.
- Health check caching, TTL expiry, and short-circuit behavior.
- Structured errors for every failure with actionable text.
- `_render_transcript` byte-identity across repeated calls on identical input.
- Anchor advancement: stable prefix between anchors, correct advancement at thresholds.
- Truncation preserving head and tail with an accurate omission marker.
- Inheritance resolution for each child type and each default.
- Child tier-narrowing enforcement.
- Consultation caps: session, per-child; budget exhaustion.

### Integration

- Cross-provider advisor: local main model, hosted advisor, real resolution of provider, URL, and key.
- Unreachable advisor: tool present, structured error, agent proceeds.
- Subagent inherits and consults; cost attributed to the parent.
- Workflow agent with a phase-level override.
- Compaction invalidates the anchor and the next call rebuilds correctly.
- Downgrade configuration warns and requires confirmation.
- Budget exhaustion suppresses with a visible reason.

### End-to-end

- Full session with five consultations; measured cache hits where supported.
- `/advisor` status accuracy across states: healthy, unreachable, suppressed, disabled.
- `/advisor tiers` explains resolution correctly for explicit, pattern, override, and unknown models.
- Fan-out of five children each capped at three consultations.

### Security

- Project settings cannot set `capabilityOverrides` or `allowDowngrade`.
- Advisor credentials are never logged, traced, or included in status output.
- The transcript sent to the advisor is redacted with the shared redactor — it is a full conversation crossing a provider boundary, frequently to a *different vendor* than the main model, which makes redaction more important here than almost anywhere else.
- A child cannot escalate above its parent's tier.
- Advisor responses are untrusted model output: neutralized and labeled before entering the parent's context, exactly as child reports are.

### Performance

- Health check cost at session start (must not delay the first prompt).
- Tier resolution cost (cached, effectively zero).
- Transcript rendering cost at 60,000 characters.
- Measured input-token reduction across a five-consultation session.

## 15. Documentation

- `docs/guides/advisor.md` — update with tiers, validation, inheritance, cost.
- `docs/guides/advisor-tiers.md` — the tier model, pattern matching, overrides, why coarse.
- `docs/api/advisor.md` — `AdvisorConfig`, tier API, inheritance.
- Cross-provider recipes: local main with hosted advisor, and the reverse.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 16. File-level implementation map

New:

- `mantis_agent/advisor/__init__.py` (re-exports the existing `__all__` verbatim)
- `mantis_agent/advisor/tiers.py` — table, resolution, comparison
- `mantis_agent/advisor/validate.py` — configuration and health checks
- `mantis_agent/advisor/context.py` — cache-stable assembly, anchors, truncation
- `mantis_agent/advisor/inherit.py` — child resolution and narrowing
- `mantis_agent/advisor/usage.py` — accounting and suppression
- `mantis_agent/data/capability_tiers.json`
- `tests/test_advisor_tiers.py`
- `tests/test_advisor_validate.py`
- `tests/test_advisor_context_stability.py`
- `tests/test_advisor_inheritance.py`
- `tests/test_advisor_suppression.py`
- `tests/test_advisor_security.py`
- `docs/guides/advisor-tiers.md`

Modified:

- `mantis_agent/advisor.py` → package `__init__` preserving `__all__`
- `mantis_agent/catalog.py` — tier data and lookup
- `mantis_agent/subagent.py` — `advisor` on `SubAgentSpec`, `AgentType` frontmatter
- `mantis_agent/coordinator.py` — worker inheritance
- `mantis_agent/workflow.py`, `workflow_defs.py` — agent and phase overrides
- `mantis_agent/budget.py` — separate advisor accounting
- `mantis_agent/compact.py` — anchor invalidation
- `mantis_agent/activity/` — consultation nodes
- `mantis_agent/settings.py` — advisor policy keys and tier restrictions
- `mantis_agent/tui_fullscreen.py` — `/advisor` commands and status
- `tests/public_api_surface.txt` — intentional update

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Tier table becomes stale as models ship | Data file, pattern fallbacks, user overrides, refreshable independently of releases |
| Coarse tiers are wrong for a specific model | `capabilityOverrides`; warnings never block; `/advisor tiers` explains resolution |
| Provider bias in the ordering | Patterns cover local and third-party names; unknown resolves to a neutral default and is flagged |
| Inheritance multiplies cost | Per-child caps, session caps, separate advisor budget, visible suppression |
| Child escalates beyond the parent | Tier narrowing enforced, tested |
| Cache anchoring stales the advisor's view | Anchor cadence configurable; volatile tail always current; omission marked |
| Nondeterministic rendering defeats caching silently | Byte-identity test on `_render_transcript` |
| Provider does not support caching | Structure still shrinks the volatile portion; status reports whether caching is active; no overclaimed savings |
| Transcript crosses a vendor boundary | Shared redactor applied; documented explicitly as a cross-provider data flow |
| Advisor output steers the parent unsafely | Treated as untrusted model output: neutralized and labeled |
| Health checks delay startup | Background, TTL-cached, non-blocking |
| Splitting the module breaks imports | Package `__init__` re-exports `__all__`; snapshot test |
| Project settings assert a false tier | `capabilityOverrides` and `allowDowngrade` are not project-configurable |

## 18. Acceptance checklist

- [ ] Capability tiers resolve from explicit entries, patterns, overrides, and a default, with confidence reported.
- [ ] Tier data is provider-neutral and covers local model naming conventions.
- [ ] Configuring a weaker advisor warns prominently and requires confirmation.
- [ ] Configuration validation reports all findings at once.
- [ ] Health is checked in the background at session start and cached with a TTL.
- [ ] The tool stays registered when the advisor is unavailable and returns a structured error.
- [ ] Every suppression reason is visible in `/advisor status`.
- [ ] `_render_transcript` is byte-identical for identical input, asserted by test.
- [ ] Requests are split into stable and volatile segments with anchored prefixes.
- [ ] Compaction invalidates and rebuilds the anchor.
- [ ] Truncation preserves head and tail and states what was omitted.
- [ ] Caching state — including unsupported providers — is reported honestly.
- [ ] Children inherit the advisor by default, with documented per-type exceptions.
- [ ] A child's advisor can never exceed the parent's tier.
- [ ] Per-child and per-session consultation caps are enforced.
- [ ] Advisor cost is accounted separately and bounded by its own budget.
- [ ] Consultations appear as activity nodes, attributed to the consulting agent.
- [ ] Advisor responses are neutralized and labeled as untrusted model output.
- [ ] The transcript sent cross-provider is redacted.
- [ ] Project settings cannot set tier overrides or allow downgrades.
- [ ] `ruff check` and the full pytest suite pass.

## 19. Recommended implementation order

1. **Ship capability ordering first.** The advisor's premise is escalation, and today nothing verifies that escalation is what happens. A user who has configured a downgrade is getting the opposite of the feature, silently, and finding out is the highest-value change here.
2. **Ship validation and the `suppressed` field second.** Both are small, and together they end the quiet-failure class: a configured advisor either works or explains why not.
3. **Make `_render_transcript` deterministic third, with the byte-identity test**, before any caching work. Nondeterministic rendering would make every later measurement meaningless and the failure would be invisible.
4. **Ship cache-stable assembly fourth.** It is the largest cost win and it changes nothing the user sees. Report caching state honestly, including for providers that do not support it — the module's cross-provider premise makes this a common case rather than an edge one.
5. **Ship inheritance fifth, with caps in the same commit.** Inheritance without per-child limits turns a ten-way fan-out into thirty frontier-model calls, and the ceiling must exist before the capability does.
6. **Add separate cost accounting alongside inheritance**, since that is the point at which advisor spend stops being obviously attributable.
7. **Add observability last** — activity nodes and `/advisor log` are the polish that makes the rest legible, and they depend on the accounting existing.
8. Coordinate the untrusted-output handling with `e_subagent_trust_limits_and_isolation.md` so the advisor's response passes through the same neutralizer as a child report. It is model output crossing a provider boundary into the parent's reasoning context, and it deserves the same treatment.
