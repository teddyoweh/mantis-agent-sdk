# Skills and Commands Standard, Policy, and Shell Blocks — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/skills.py`, `mantis_agent/rules.py`, and the slash-command surface
**Objective:** Unify skills, commands, and rules into one layered extension model with standard metadata, deterministic collision and stacking rules, a listing token budget, per-skill tool allowlists, source provenance and trust, and permission-gated pre-execution shell blocks.

## 1. Executive summary

`mantis_agent/skills.py` (391 lines) is deliberately minimal and says so. Its module docstring draws the right distinction — *"A tool is something the agent calls and gets data back from. A skill is a chunk of instructions / context the agent reads *before* deciding what to do"* — and its matching is honestly labeled: *"The matching is intentionally dumb in v0 — substring search over `name + search_hint`. A real embedding-based ranker is M5 work."*

`Skill` carries `name`, `description`, `body`, `search_hint`, `always_load`, and `category`. `SkillRegistry` provides `add`, `remove`, `get`, `always_loaded`, `match`, and `by_category`. Discovery walks `_skill_dirs` and parses `*.md` frontmatter through `_parse_skill_md` — the same parser `subagent.py::_parse_agent_md` reuses for agent personas, which is a good economy.

`rules.py` (161 lines) handles a third mechanism: conditional rules from `.mantis/rules/` with glob frontmatter, activated by which files are in play (`active_files_from_messages`, `select_matching_rules`, `render_rules_reminder`) and `@mention` detection via `_MENTION_RE`.

So Mantis has three overlapping context-injection systems — skills, rules, and project memory files — plus slash commands as a fourth surface, and they do not share metadata, trust, precedence, or budget. The gaps:

**No standard metadata.** `Skill` has six fields. There is no version, no license, no author, no declared tool requirements, no argument schema, no allowed-tools list, no compatibility range. A skill cannot say "I need `bash` and `read_file`" or "I take a `--scope` argument," so nothing can validate or constrain it.

**No collision or stacking policy.** `_skill_dirs` returns user and project directories; `discover_skills` walks them. What happens when both define `review`? Whatever the iteration order produces. There is no documented nearest-project-wins rule, no way to see which one won, and no way to stack a project skill on top of a user skill rather than replacing it.

**No listing budget.** `render_skill_catalog` renders every discovered skill's name and description into the prompt. Twenty skills is fine; two hundred is a significant, invisible, per-turn context cost. `always_load` skills inject their full bodies with no aggregate cap at all — the docstring warns *"The agent pays this cost on every request"* but nothing enforces a ceiling.

**No tool allowlist.** A skill is instructions injected into the system prompt. It can tell the model to do anything the model can do. There is no way to say "this skill may only cause read-only tool calls."

**No provenance or trust.** `discover_skills` loads project-level `.mantis/skills/*.md` with no gate. A cloned repository's skill is injected into the system prompt on first run. Compare this to the same repository's MCP config, which `mcp/manager.py` correctly gates behind `project_mcp_is_trusted` with content hashing — and to `project_memory.py`, which restricts `@` imports to `_TRUSTED_IMPORT_TIERS = {"user", "managed"}`. The trust model exists twice in this codebase already and skills do not participate in it. **A project skill is arguably a higher-value injection target than a project MCP server, because it lands directly in the system prompt.**

**No shell blocks.** A command that wants to include `git status` output must ask the model to run it, costing a round trip and leaving the model to decide. There is no way to declare "run these commands and substitute their output before the prompt is assembled."

**Commands and skills are separate.** Slash commands and skills are the same thing viewed differently — a named, discoverable unit of instructions. Keeping them separate doubles the discovery, trust, and precedence work.

## 2. Goals

### User outcomes

- Write one markdown file that works as both a `/command` and a loadable skill.
- Know which definition won when two sources define the same name, and why.
- Layer a project addition on top of a user skill instead of replacing it.
- Constrain a skill to read-only tools.
- Have `!git status` in a command substitute real output before the model sees the prompt.
- Be asked before a cloned repository's skill enters the system prompt.
- See and control how much context skills are consuming.

### Engineering goals

- Preserve `Skill`, `SkillRegistry`, `discover_skills`, `match_skills`, `render_skills`, `render_skill_catalog`, `load_skill_body`, and `_parse_skill_md` (shared with agent personas).
- Add metadata fields with defaults so existing skill files keep working untouched.
- One resolver for skills, commands, and rules — three front doors, one loader.
- Reuse the trust machinery from `mcp/manager.py` rather than writing a third variant.
- Keep matching pluggable; this plan does not change the ranker, only what it ranks over.
- Python 3.9–3.14.

### Success metrics

- Every existing skill and rule file loads unchanged.
- Collision resolution is deterministic and explained by `/skills why <name>`.
- Total skill-derived context stays under a configured budget in every session, with truncation reported.
- No project-scoped skill, command, or rule enters the prompt before approval.
- Shell blocks execute under the permission layer with zero interpolation-injection paths.
- Catalog rendering cost is bounded at 500 discovered skills.

## 3. Non-goals

- Embedding-based skill matching. Explicitly deferred, as the current docstring already says.
- A marketplace or distribution format — that is `j_plugin_packages_and_marketplaces.md`, which packages what this plan standardizes.
- Replacing `project_memory.py`'s `MANTIS.md` mechanism. Project instructions stay file-based; skills are the named, discoverable, invocable unit.
- Executable skills in arbitrary languages. Shell blocks are bounded substitution, not a scripting runtime.
- Changing how `always_load` bodies are injected, beyond adding a budget.

## 4. Current integration points

- `mantis_agent/skills.py` — `Skill`, `SkillRegistry` (+`add`/`remove`/`get`/`always_loaded`/`match`/`by_category`/dunders), `render_skills`, `SKILLS_SUBDIR`, `_parse_skill_md`, `_skill_dirs`, `discover_skills`, `render_skill_catalog`, `_WORD_RE`, `_tokens`, `match_skills`, `render_relevant_skills`, `load_skill_body`.
- `mantis_agent/rules.py` — `WORKSPACE_DIR`, `RULES_SUBDIR`, `_PATH_KEYS`, `_MENTION_RE`, `parse_rule_frontmatter`, `_split_glob_value`, `discover_conditional_rules`, `rule_file_has_globs`, `active_files_from_messages`, `_matches`, `select_matching_rules`, `render_rules_reminder`.
- `mantis_agent/subagent.py` — `_parse_agent_md` reuses `_parse_skill_md`; agent personas share the format and therefore the trust problem.
- `mantis_agent/project_memory.py` — `_TRUSTED_IMPORT_TIERS`, `MAX_IMPORT_DEPTH`, `_MAX_FILE_BYTES`, `_within`, `_TIER_LABEL`. The tier model to generalize.
- `mantis_agent/mcp/manager.py` — `project_mcp_is_trusted`, `trust_project_mcp`, `_file_hash`, `_mcp_trust_path`, `filter_untrusted_project_servers`, `_TRUST_ENV`. The trust machinery to reuse.
- `mantis_agent/paths.py` — `get_mantis_agent_dir`, `get_agents_dir`, `get_project_dir`.
- `mantis_agent/permissions.py` — shell blocks run through `check_permission` and the shell decomposer.
- `mantis_agent/tools.py` — tool allowlists resolved against the registry, reusing `resolve_agent_tools`-style policy.
- `mantis_agent/system_reminder.py` — injection framing and budget.
- `mantis_agent/serve.py` — `skills_state`, `add_skill`, `delete_skill`, `_read_skill`, `_skill_md`, `_slugify` in the dashboard.
- `mantis_agent/tui_fullscreen.py` — the slash-command surface.

## 5. Unified metadata

One frontmatter schema serves skills, commands, and rules. All fields optional; existing files parse unchanged.

```yaml
---
name: review-changes
description: Review the working diff for correctness and security
version: 1.2.0
kind: skill | command | rule        # default: skill; command if invocable
invocable: true                     # exposes /review-changes
license: MIT
author: teddy
homepage: https://…

# discovery
search_hint: diff review audit lint security
category: engineering/review
always_load: false
globs: ["src/**/*.py"]              # rule-style conditional activation

# constraints
allowed_tools: ["read_file", "grep", "glob", "bash(git diff:*)"]
disallowed_tools: ["write_file", "edit_file"]
model: inherit
max_context_tokens: 4000

# arguments (command mode)
args:
  - name: scope
    type: string
    required: false
    default: staged
    choices: [staged, branch, all]

# composition
extends: user:review-changes        # stack rather than replace
requires: ["mantis>=2.62"]

# pre-execution
shell:
  - id: diff
    run: git diff --stat
    timeout_ms: 5000
---

Body. `{{args.scope}}` and `{{shell.diff}}` substitute here.
```

`Skill` gains these as defaulted fields. `_parse_skill_md` continues to return `(meta, body)`; a new `parse_definition` layer validates and types the metadata, so the shared parser stays shared with `_parse_agent_md`.

Unknown keys are preserved and ignored, so a file written for a newer version still loads.

## 6. Sources, precedence, and stacking

### Source tiers

Generalize `project_memory.py`'s tiering:

| Tier | Location | Trust |
|---|---|---|
| `builtin` | Shipped with Mantis | Trusted |
| `managed` | `/etc/mantis-agent/skills/` | Trusted, highest precedence |
| `user` | `~/.mantis/skills/` | Trusted |
| `project` | `<repo>/.mantis/skills/` | **Untrusted until approved** |
| `local` | `<repo>/.mantis/skills.local/` | **Untrusted until approved** |
| `plugin` | Installed packages | Trust from the package |
| `runtime` | `SkillRegistry.add()` | Trusted (the embedder chose it) |

### Nearest-project-wins

For project tiers, walk up from the cwd as `project_memory._walk_up` does. The nearest definition wins. In a monorepo, `packages/api/.mantis/skills/deploy.md` beats the repository root's.

### Collision resolution

Deterministic order: `managed` > `runtime` > `user` > nearest `project` > farther `project` > `plugin` > `builtin`.

Requirements:

- The winner is recorded with its full path; losers are retained as shadowed and inspectable.
- `/skills why <name>` prints the resolution chain. Silent shadowing is the failure mode this fixes.
- A collision between two same-tier definitions (two plugins) is an error naming both, not an arbitrary pick.

### Stacking

`extends: user:review-changes` composes rather than replaces:

- The parent's body is included, then the child's, with a clear separator.
- Metadata merges shallowly; the child wins per key.
- `allowed_tools` **intersects** rather than unions — a child may narrow but never widen its parent's tool grant. This is the same narrowing rule the permission and sandbox plans apply, and it is what keeps a project skill from widening a user skill.
- Cycles are detected with the chain reported.
- Depth capped (default 3).

## 7. Context budget

Skills consume context in three places, none currently bounded in aggregate.

| Consumer | Current | Proposed |
|---|---|---|
| `always_load` bodies | Unbounded | Budgeted, priority by tier |
| Catalog (`render_skill_catalog`) | All skills | Budgeted, truncated with a count |
| Matched skills (`render_relevant_skills`) | `limit=3` count-based | Byte-budgeted |
| Conditional rules | Unbounded | Budgeted |

Design:

- One `skills.maxContextTokens` budget (default 6,000), separate from but coordinated with the memory budget in `l_auto_memory_lifecycle.md`. Both plans must ultimately report into one total-injection accounting; neither should be able to silently consume the other's share.
- Allocation by priority: `always_load` from `managed`/`user`, then matched skills by score, then the catalog, then conditional rules.
- The catalog degrades gracefully: full descriptions, then names only, then a count plus an instruction to use `skill_search`. It must never be silently dropped, because a truncated catalog is a capability the model does not know it has.
- Every truncation is stated in the injected text and surfaced in `/skills budget`.
- `max_context_tokens` per skill caps any single body.

Beyond a threshold (default 40 skills), switch the catalog to search-first: register a `skill_search` tool and surface only `always_load` skills plus matched ones. This mirrors the deferred-tool mechanism `ToolRegistry.defer` already provides and keeps a large skill library affordable.

## 8. Tool allowlists

A skill injects instructions into the system prompt, so it can steer any tool the model has. `allowed_tools` makes that grant explicit and enforceable.

Semantics:

- Resolved against the active registry at load, following the policy shapes `AgentType.tools` already uses (`read-only`, `all`, explicit names) plus the `Tool(param:value)` grammar from `f_permission_policy_engine_and_auto_mode.md`.
- Enforcement is **advisory in phase 1, enforced in phase 2**:
  - Phase 1: the constraint is stated in the injected body and recorded, so the model is told and the user can audit.
  - Phase 2: while a skill is the *active invoked command*, tool calls outside its allowlist are denied with a reason naming the skill.
- Enforcement cannot apply to `always_load` skills, since they are ambient — those may declare `allowed_tools` for documentation but it is not enforced, and this limitation must be documented rather than papered over.
- `disallowed_tools` is a hard subtraction applied after allowlisting.
- Untrusted project skills may only ever narrow; a project skill declaring `allowed_tools: all` is rejected.

Being honest about the advisory-versus-enforced boundary matters. An allowlist that silently fails to constrain an ambient skill is worse than no allowlist, because it implies protection that is not there.

## 9. Shell blocks

### Purpose

Substitute real command output into a command's body before the model sees it, replacing a round trip.

```yaml
shell:
  - id: status
    run: git status --short
    timeout_ms: 3000
  - id: branch
    run: git rev-parse --abbrev-ref HEAD
```

Body: `Current branch: {{shell.branch}}` / `Working tree:\n{{shell.status}}`.

### Execution rules

Every one of these is required; shell blocks are the largest new attack surface in this plan.

- **Argv, never a shell**, unless the definition sets `shell: true` *and* originates from `user` or `managed` tier.
- **No interpolation of arguments into commands.** `{{args.x}}` may not appear in a `run` string. Arguments reach commands only as explicitly declared `argv` entries, each passed as a single argument with no word splitting. This is the difference between a feature and a command-injection hole.
- **Full permission check.** Each block runs through `check_permission` with the shell decomposer from `f_permission_policy_engine_and_auto_mode.md`. A shell block is a `bash` call that happens to be declared in a file; it gets no exemption.
- **Untrusted tiers may not declare shell blocks at all** until approved — and approval displays every command verbatim.
- Sandbox policy applies, inherited from the session.
- Timeout (default 5 s), output cap (default 8 KB, truncated with notice), and a block count cap (default 5).
- Blocks run sequentially; a failing block substitutes an error marker rather than aborting, unless `required: true`.
- Output is **sanitized before substitution**: control characters, ANSI, bidi, and framing markers stripped or escaped. Command output is untrusted content entering a prompt, and it must be treated exactly as MCP results and child reports are.
- Substituted output is labeled in the assembled body so the model can tell command output from authored instructions.

### Argument substitution

- `{{args.name}}` substitutes into the body only, never into a command.
- Values are validated against the declared `args` schema (type, `choices`, `required`).
- Values are sanitized like shell output before substitution.
- Unknown placeholders are an error at load time, not a silent empty string.

## 10. Trust

### The gate

Project- and local-tier definitions — skills, commands, rules, and agent personas — are untrusted until approved.

Reuse `mcp/manager.py`'s machinery directly: `_file_hash` for content hashing, a trust file alongside `_mcp_trust_path()`, `filter_untrusted_*` for exclusion, and an environment override mirroring `MANTIS_MCP_TRUST_PROJECT`.

Behavior:

- On first encounter, the definition is **excluded entirely** — not listed in the catalog, not matched, not invocable. A skill the model cannot see cannot be used, which is stronger than gating at use time.
- The user is shown one notice per project listing untrusted definitions, with `/skills trust` to review.
- Review displays name, tier, path, declared tools, shell blocks (verbatim), and the body.
- Approval is per file, keyed by content hash. Editing re-prompts.
- Bulk `/skills trust --all` is available but shows every definition first.
- `always_load` from an untrusted source is refused even after trust unless separately confirmed — an ambient skill is a permanent prompt modification and deserves its own decision.

### Why this matters most here

A project MCP server adds tools the model may call, gated by permissions. A project skill adds text to the system prompt, which shapes every decision the model makes, including which permissions to request and how to describe them to the user. It is the higher-leverage injection point, and it is currently ungated while MCP is gated. Closing this is the single most important item in the plan.

## 11. Unifying commands

A command is an invocable definition. One loader, three front doors:

- `invocable: true` (or `kind: command`) registers `/name`.
- Arguments parsed per the `args` schema; `/review-changes --scope branch`.
- Invocation assembles the body with argument and shell substitution and injects it as the turn's instruction.
- The same file remains loadable as a skill by name.
- `kind: rule` with `globs` activates conditionally through the existing `rules.py` path, now sharing metadata, trust, and budget.

Namespacing: plugin-provided commands are `/plugin:command`, matching the convention MCP tools already use with `mcp__server__tool`.

Discovery for the user: `/skills` lists everything with tier, trust, and invocability; `/help` includes invocable definitions with their descriptions.

## 12. Configuration

```json
{
  "skills": {
    "enabled": true,
    "maxContextTokens": 6000,
    "catalogMode": "auto",
    "catalogThreshold": 40,
    "maxAlwaysLoad": 8,
    "maxSkillTokens": 4000,
    "trustProject": "prompt",
    "allowProjectAlwaysLoad": false,
    "stacking": {"enabled": true, "maxDepth": 3},
    "shell": {
      "enabled": true,
      "maxBlocks": 5,
      "timeoutMs": 5000,
      "maxOutputBytes": 8192,
      "allowShellTrue": ["user", "managed"],
      "requirePermission": true
    },
    "allowedTools": {"enforce": "invoked-only"},
    "disabled": []
  }
}
```

`shell.requirePermission` may only be `true`, consistent with the other security baselines in this plan set: the key exists for inspectability, not to be turned off.

Environment: `MANTIS_SKILLS=0|1`, `MANTIS_SKILLS_TRUST_PROJECT=0|1`, `MANTIS_SKILLS_NO_SHELL=1`.

## 13. Surface

```text
/skills                       all definitions: name, tier, trust, invocable, tokens
/skills show <name>           metadata, body, resolution chain, shadowed versions
/skills why <name>            why this definition won
/skills trust [--all]         review and approve project definitions
/skills budget                context consumption by category
/skills search <query>        matcher results with scores
/skills disable <name>        session-scoped
/skills reload
/skills validate <path>       lint a definition file
```

```text
$ /skills why review-changes
review-changes                                              → project (nearest)
  ✓ project   ./packages/api/.mantis/skills/review-changes.md   trusted
    shadows
  ·  project  ./.mantis/skills/review-changes.md                 (farther)
  ·  user     ~/.mantis/skills/review-changes.md
  extends     user:review-changes
  tools       read_file, grep, glob, bash(git diff:*)   [narrowed from parent]
  shell       1 block (git diff --stat)
```

`/skills validate` is what makes authoring tractable — it catches unknown placeholders, tool names that do not resolve, cycles, and shell blocks that would be refused, before runtime.

## 14. Errors

```text
SkillError                        (base)
├── SkillParseError               # malformed frontmatter, with line
├── SkillMetadataError            # unknown/invalid field value
├── SkillCollisionError           # same-tier ambiguity
├── SkillCycleError               # extends cycle, with chain
├── SkillDepthExceededError
├── SkillUntrustedError
├── SkillToolUnknownError
├── SkillToolWideningError        # child widened parent's allowlist
├── SkillBudgetExceededError      # reported, not fatal
├── ShellBlockDeniedError
├── ShellBlockTimeoutError
├── ShellBlockOutputTooLargeError
├── ShellInterpolationError       # {{args}} used inside a run string
└── SkillArgumentError
```

Parse errors report file and line and drop that one definition rather than failing discovery — except in a `managed` tier, where a malformed policy definition fails loudly.

## 15. Delivery phases

### Phase 0 — Audit

1. Inventory real skill, rule, and command files; measure current injected token cost.
2. Determine collision frequency in practice.
3. Prototype the trust gate against `mcp/manager.py`'s machinery.
4. Prototype shell-block execution through `check_permission`.
5. Confirm `_parse_skill_md` can stay shared with `_parse_agent_md` under the extended schema.

**Exit:** current cost measured; trust reuse validated; parser sharing confirmed.

### Phase 1 — Trust

1. Add tiers, provenance, and content-hash trust reusing MCP's implementation.
2. Exclude untrusted project definitions from catalog, matching, and invocation.
3. Add `/skills trust` with full disclosure.
4. Gate `always_load` from project tiers separately.
5. Extend the gate to agent personas, coordinated with `e_subagent_trust_limits_and_isolation.md`.

**Exit:** a cloned repository's skill cannot reach the system prompt unapproved. **This ships first and alone.**

### Phase 2 — Metadata and resolution

1. Add `parse_definition` over `_parse_skill_md` with the extended schema.
2. Add defaulted fields to `Skill`; preserve unknown keys.
3. Implement tier precedence and nearest-project-wins.
4. Implement shadowing records and `/skills why`.
5. Implement stacking with intersecting tool allowlists and cycle detection.

**Exit:** collisions are deterministic and explained; existing files load unchanged.

### Phase 3 — Budget

1. Implement the shared skills context budget with priority allocation.
2. Implement graceful catalog degradation and search-first mode over `ToolRegistry.defer`.
3. Add per-skill token caps.
4. Report truncation in-band and in `/skills budget`.
5. Coordinate accounting with the memory budget.

**Exit:** skill context is bounded and visible; large libraries stay affordable.

### Phase 4 — Commands and arguments

1. Unify invocable definitions into the slash-command surface.
2. Implement the `args` schema, validation, and sanitized substitution.
3. Add plugin namespacing.
4. Integrate with `/help`.
5. Add `/skills validate`.

**Exit:** one file serves as skill and command.

### Phase 5 — Shell blocks

1. Implement blocks with argv execution and full permission checks.
2. Enforce the no-interpolation rule at parse time.
3. Add timeouts, output caps, block caps, and sanitization.
4. Gate `shell: true` to trusted tiers; require verbatim display at approval.
5. Add labeled substitution so the model can distinguish command output.

**Exit:** shell blocks work with no injection path.

### Phase 6 — Tool allowlists and hardening

1. Advisory allowlists, then enforcement for invoked skills.
2. Document the ambient limitation explicitly.
3. Adversarial review: injection through bodies, shell output, arguments, and stacking.
4. Fuzz frontmatter and placeholder parsing.
5. Remove experimental gating.

## 16. Testing strategy

### Unit

- Existing skill and rule files parse unchanged with correct defaults.
- Extended metadata: every field, invalid values, unknown keys preserved.
- Tier precedence across all pairs; nearest-project-wins in a nested layout.
- Same-tier collision raises with both paths.
- Stacking: merge, narrowing intersection, widening rejection, cycle with chain, depth cap.
- Budget allocation, priority, degradation steps, truncation reporting.
- Catalog mode switching at the threshold.
- Trust: untrusted excluded from catalog/match/invoke; approval; content change re-prompts; `always_load` gated separately.
- Shell blocks: argv construction, `{{args}}` in `run` rejected at parse, timeout, output cap, block cap, sanitization, permission denial.
- Argument validation: type, choices, required, unknown placeholder.
- Tool allowlist resolution and unknown-tool errors.

### Integration

- Full discovery across all six tiers with collisions and stacking.
- Untrusted project skill invisible to the model, then approved and usable.
- Shell block executing `git status` in a real repository with real permission checks.
- Shell block denied by a deny rule.
- Command invoked with arguments; body assembled correctly.
- Conditional rules activating via `active_files_from_messages`.
- 200-skill library staying within budget in search-first mode.

### End-to-end

- `/skills why` matches actual resolution.
- `/skills validate` catches every authoring error class.
- Fresh clone: notice shown, definitions excluded, approval flow works.
- `/skills budget` reflects real consumption.

### Security

- **The core test:** a cloned repository's `.mantis/skills/*.md` containing prompt-injection text does not reach the system prompt before approval.
- Agent persona from the same repository is likewise gated.
- `{{args.x}}` inside a `run` string is rejected at parse time.
- An argument value of `; rm -rf ~` reaches a shell block without executing.
- Shell output containing framing markers is sanitized before substitution.
- A project skill cannot widen a user skill's `allowed_tools` via `extends`.
- A project skill declaring `allowed_tools: all` is rejected.
- `shell: true` from a project tier is refused.
- Path traversal via `extends` or a skill name.
- A skill body cannot forge system-reminder framing.

### Performance

- Discovery at 500 definitions.
- Catalog render cost in both modes.
- Matcher cost at 500 definitions.
- Shell block execution overhead per invocation.
- Trust hash checking cost at startup.

## 17. Documentation

- `docs/guides/skills.md` — authoring, metadata reference, tiers, precedence, stacking.
- `docs/guides/skills-commands.md` — invocable definitions, arguments, shell blocks.
- `docs/guides/skills-security.md` — the trust model, why project definitions are gated, what allowlists do and do not enforce.
- `docs/api/skills.md` — `Skill`, `SkillRegistry`, resolution API.
- Migration: existing files work unchanged; new fields are opt-in; project definitions now require approval (the one behavior change, with a clear changelog entry).
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 18. File-level implementation map

New:

- `mantis_agent/skills/__init__.py` (re-exports the current surface)
- `mantis_agent/skills/schema.py` — metadata model and validation
- `mantis_agent/skills/resolve.py` — tiers, precedence, stacking, shadowing
- `mantis_agent/skills/trust.py` — reuses MCP trust machinery
- `mantis_agent/skills/budget.py`
- `mantis_agent/skills/shell.py` — shell blocks
- `mantis_agent/skills/args.py`
- `mantis_agent/skills/commands.py` — invocable registration
- `mantis_agent/skills/validate.py`
- `tests/test_skill_schema.py`
- `tests/test_skill_resolution.py`
- `tests/test_skill_stacking.py`
- `tests/test_skill_trust.py`
- `tests/test_skill_budget.py`
- `tests/test_skill_shell_blocks.py`
- `tests/test_skill_args.py`
- `tests/test_skill_security.py`
- `docs/guides/skills-commands.md`
- `docs/guides/skills-security.md`

Modified:

- `mantis_agent/skills.py` → package `__init__`
- `mantis_agent/rules.py` — share metadata, trust, and budget
- `mantis_agent/subagent.py` — persona trust via the shared gate
- `mantis_agent/project_memory.py` — coordinate budget accounting
- `mantis_agent/permissions.py` — shell-block checks
- `mantis_agent/tools.py` — allowlist resolution, deferred catalog
- `mantis_agent/system_reminder.py` — labeled injection
- `mantis_agent/serve.py` — trust and tier in `skills_state`
- `mantis_agent/tui_fullscreen.py` — `/skills` commands, invocable registration
- `tests/public_api_surface.txt` — intentional update

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Project skills injected before approval | Full exclusion until trusted; reuses proven MCP trust machinery |
| Trust prompt annoys users in every repository | One notice per project, bulk approve with disclosure, content-hash memory |
| Shell blocks become command injection | Argv only, no argument interpolation into `run`, parse-time rejection, permission checks, trusted-tier gating |
| Shell output injects instructions | Sanitized and labeled before substitution |
| Metadata change breaks existing files | All fields defaulted, unknown keys preserved, compatibility tests on real files |
| Collision resolution surprises users | Deterministic order, `/skills why`, shadowed versions retained |
| Stacking widens permissions | Tool allowlists intersect, never union; widening rejected |
| Allowlists imply protection they do not give | Ambient limitation documented explicitly; enforcement scoped to invoked skills |
| Budget silently drops capability | Graceful degradation with in-band notice; catalog never fully dropped |
| Large libraries cost context every turn | Search-first mode over the existing deferred-tool mechanism |
| Splitting the module breaks imports | Package `__init__` re-exports; snapshot test |
| Three systems remain three systems | One resolver, one trust gate, one budget; `rules.py` migrated in the same work |

## 20. Acceptance checklist

- [ ] Existing skill, rule, and persona files load unchanged.
- [ ] Project and local definitions are excluded until approved, keyed by content hash.
- [ ] Agent personas share the same trust gate.
- [ ] `always_load` from project tiers requires separate confirmation.
- [ ] Tier precedence and nearest-project-wins are deterministic and explained by `/skills why`.
- [ ] Same-tier collisions error with both paths.
- [ ] Stacking merges correctly; tool allowlists intersect and never widen.
- [ ] One budget bounds `always_load`, matched skills, catalog, and rules, with reported truncation.
- [ ] The catalog degrades gracefully and is never silently dropped.
- [ ] Search-first mode engages above the threshold using deferred tools.
- [ ] Invocable definitions register as commands with validated arguments.
- [ ] `{{args}}` inside a `run` string is rejected at parse time.
- [ ] Shell blocks execute argv-only under full permission checks with sandbox inheritance.
- [ ] Shell output is sanitized and labeled before substitution.
- [ ] `shell: true` requires user or managed tier.
- [ ] Tool allowlists are enforced for invoked skills; ambient limits are documented.
- [ ] `/skills validate` catches authoring errors before runtime.
- [ ] `ruff check` and the full pytest suite pass.

## 21. Recommended implementation order

1. **Ship the trust gate first, alone, ahead of everything else in this plan.** Project skills reaching the system prompt unapproved is a live exposure, the fix reuses machinery that already exists in `mcp/manager.py`, and it needs none of the metadata work. Extend it to agent personas in the same change, since they share a parser and a directory convention.
2. **Add metadata and resolution second.** Deterministic precedence with `/skills why` is the foundation everything else builds on, and it is purely additive.
3. **Add the budget third.** It is independent, immediately valuable, and it bounds the cost of any library growth that later phases encourage.
4. **Unify commands fourth**, since it needs metadata and arguments but nothing security-sensitive.
5. **Add shell blocks fifth, and only after the trust gate is proven.** Shipping declarative command execution before untrusted definitions are excluded would be shipping the vulnerability with the feature.
6. **Add tool allowlists last**, advisory first. Document precisely what they do not cover; an allowlist that overpromises is worse than none.
7. Migrate `rules.py` onto the shared resolver as part of step 2 rather than leaving a third system in place — the whole point is one loader, and deferring the migration guarantees it never happens.
