# Changelog

All notable changes to `mantis-agent-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and from 1.0.0 on the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The full versioning policy is in [SEMVER.md](SEMVER.md).

## [Unreleased]

### Added — the four things dogfooding said were missing

- **`response_model` — ask for a type, get an instance.** Structured output
  meant hand-writing the provider envelope (`{"type": "json_schema",
  "json_schema": {...}}`), then `json.loads`-ing whatever came back, with every
  field name living in two places. Now::

      @dataclass
      class Invoice:
          vendor: str
          total_usd: float

      options = MantisAgentOptions(model=..., response_model=Invoice)
      ...
      result.parsed        # -> Invoice(vendor='Northwind Traders', ...)

  Accepts dataclasses, `msgspec.Struct`, `TypedDict`, and pydantic models;
  derives the schema (inlined and `additionalProperties: false`, which strict
  mode requires), and decodes onto `parsed`. Two behaviors learned from watching
  small models: a ```` ```json ```` fence is stripped before parsing, and a parse
  failure is a *run* failure — `parsed=None` with a success flag would be the
  same silent trap as everything else fixed this cycle, so the reason lands in
  `errors` with the head of what the model actually said. An explicit
  `response_format` still wins. `SDKResultMessage` gains a `parsed` field, the
  first declared entry in the new `MANTIS_WIRE_EXTENSIONS` list in the parity
  test — extensions are now a decision on the record rather than a silent drift.

- **Errors name where they went.** `ProviderError: Not Found` was the most
  expensive message in the SDK: it's what a bare model name produces when it
  falls through to the openai_compat default and nothing is listening, and it
  said nothing about which door was knocked on. Now `Not Found (404 from
  http://localhost:8000/v1/chat/completions) — port 8000 is the vLLM default …`,
  with hints for the Ollama, llama.cpp and TGI ports. The query string is
  stripped: Gemini carries the API key there, and an error message is exactly
  the string that ends up in a log or a screenshot.

- **`raise_on_error`.** `query()` reports failures on the final message and
  never raises, so the loop everyone writes — print assistant text — prints
  nothing and exits 0 when the backend is down. Opt in and the result is still
  yielded first, then `AgentError` is raised as the iterator finishes. Fixing
  this exposed a companion bug: `_build_result` took an `errors` argument it
  never forwarded, so every failed run on the typed path reported
  `is_error=True` with an empty `errors` list — the detail was collected and
  dropped. `ResultMessage.errors` now carries it.

- **Skills are off by default for library callers.** `skills=None` used to mean
  "discover every `SKILL.md` under `~/.mantis-agent/skills/` and inject the
  matching ones" — into *any* agent, including a library caller's in an
  unrelated directory. A coding agent with a three-tool belt called
  `check-internet` and pinged google.com, and the tool was real: a skill on the
  developer's machine. Whether an agent works should not depend on whose laptop
  it runs on. `None` now means off; `"auto"` is the old behavior and is what the
  `mantis` terminal passes; `"all"` and an explicit list are unchanged.


### Fixed — found by dogfooding the SDK against a local model

Four defects surfaced by building real agents with the package rather than
reading it. Each was invisible from the source and obvious from a run.

- **`can_use_tool` was fail-open.** A policy written exactly as the permissions
  guide and the Claude Agent SDK document it — `if tool_name ==
  "delete_account": return PermissionResultDeny(...)` — let the deletion
  through, for two independent reasons. The resolver passed the whole `Tool`
  object where a name string is promised, so every comparison was false and the
  policy fell through to allow; and on the dict-options path neither
  `can_use_tool` nor `permission_mode` built a `PermissionContext` at all, so the
  agent ran with `permissions=None` and nothing was gated — `"bypass"` looked
  like it worked only because *everything* was ungated. User callbacks now
  receive the documented name string (translated at the boundary, so the
  terminal's own policy keeps the `Tool` it needs), and both option paths build a
  real context. A guardrail that looks configured and isn't is worse than no
  guardrail.

- **`cwd` didn't scope the built-in tools.** It was reported to the model in its
  env context block ("Working directory: /x") while `write_file` and `bash`
  resolved relative paths against the *host process's* directory. The model
  emitted relative paths in good faith, its files landed outside the intended
  tree, and its own follow-up `ls` disagreed with its own writes. The same
  fizzbuzz-and-test prompt on the same 7B model went from **zero files created
  and a confused model** to a correct, passing result once the tools resolved
  against `cwd` — the "small model is dumb" failure was the SDK lying about where
  it was. `Agent` gained a `cwd` field, both option paths forward it, relative
  paths resolve against it, and `bash` starts there. Unset keeps the process cwd,
  so existing callers are unaffected.

- **`msg.type` existed on only one message shape.** The dict path yields objects
  with `.type`; the typed path yielded objects with only `.role`, so the
  documented `if msg.type == "assistant":` loop raised `AttributeError` the
  moment you switched option shapes. The flat messages now expose `type` as a
  property (not a field, so encoded JSON is unchanged) and the same loop works on
  both.

- **`SubAgentSpec` had a model but no destination.** `as_subagent_tool(spec)`
  minted a child that fell back to the openai_compat default (`localhost:8000`)
  and raised `ProviderError: Not Found`. Invoked through a parent model, that
  surfaced as the child politely reporting it "couldn't find" the answer — a
  connection failure disguised as a result. The spec now takes `backend` and
  `api_key`; `parent_provider=` remains the better choice when the child shares
  the parent's backend.

- **Live Ollama tests failed instead of skipping** when the daemon was up but the
  default tag wasn't pulled, reporting `ProviderError: model "llama3.2:3b" not
  found` — which reads as a code regression rather than a missing download. The
  guard now checks the tag list and skips with the `ollama pull` command to run.

### Added


- **`api_key` and `base_url` are real options.** Both doc trees documented them
  for a long time while no code path read either one, and the failure was silent
  in the worst way: unknown option keys flow into `Agent.extra`, so a reader who
  copied the documented snippet got an agent pointing at the default URL with no
  auth — a 401 or a connection refused, far from the cause. On the typed path
  `MantisAgentOptions(api_key=…)` raised `TypeError` outright. Both now exist on
  `Agent`, `MantisAgentOptions`, and the dict form, and reach the provider.

  `base_url` is an accepted alias for `backend` (the name every
  OpenAI-compatible SDK uses); passing both with *different* values raises
  rather than silently preferring one, because quietly picking a URL the caller
  can see they didn't ask for is the failure mode the alias exists to end.
  `api_key` has three meanings: a string is used verbatim, `None` keeps the
  environment discovery chain, and `""` sends no auth at all (for backends that
  authenticate with their own headers). An explicit key beats the environment on
  every adapter that has somewhere to put it, Anthropic's `x-api-key` included.

  Provider construction also stopped losing kwargs: it previously fell back to
  no-kwarg construction on any `TypeError`, so one unsupported argument could
  drop a real `base_url` and leave the adapter on its own default. Kwargs the
  adapter doesn't accept are now filtered individually.

- **`fallback_model` works from options.** `Agent` has always implemented the
  retry-a-failed-turn-on-a-second-model path, and both guides described
  configuring it through options — but neither options path forwarded the key.
  The typed path filed it under `extra`; the dict path never listed it. Nothing
  read either, so a documented resilience feature was off for every caller who
  didn't construct `Agent` by hand.

- **`docs/guides/how-it-works.md` — the page you'd otherwise read source for.**
  Unknown option keys are silent by design (they flow to `Agent.extra` so
  adapters can take provider-specific knobs), which means a typo, a key from
  another SDK, and a key that used to exist all behave identically: accepted,
  inert. That rule, the two option shapes and their two message shapes, the
  precedence chain across code/settings/env, the knobs reachable only on
  `Agent`, and an honest table of the eleven Claude-SDK-parity fields that
  accept a value and do nothing — all in one place, each entry verified by
  probing behavior rather than by reading a docstring.

- **`scripts/check_doc_coverage.py` — a gate for what's *missing*.** The snippet
  checker answers "is what we wrote true?"; this answers "is any of it written
  down?" It walks `__all__`, `MantisAgentOptions` fields, the environment
  variables the package really reads, behavior-changing settings keys, and
  mapped hook events, and fails on anything mentioned in neither doc tree.
  Env names come from the AST — literals and resolved module constants, so
  `f"MANTIS_SUBAGENT_{name}"` can't invent a variable and
  `os.environ.get(_FAIL_CLOSED_ENV)` isn't missed. Deliberate omissions live in
  an `ALLOWLIST` with a stated reason, putting that decision on the record.
  Starting point was 89% of exports, 77% of option fields and 47% of env vars
  documented; all five surfaces are now at 100%.

- **`scripts/check_doc_snippets.py` — CI gate against doc drift.** A wrong
  example is worse than a missing one, and nothing in the suite could tell the
  difference. The checker extracts every fenced snippet from both doc trees and
  asks the *live package* about each claim rather than comparing against a
  hand-maintained list that would rot the same way the prose did: it puts a key
  in an options dict and looks at whether the key does anything or lands in
  `extra`; feeds one event to the hook converter and sees whether a slot gets
  populated; hands `apply_settings_to_options` a single settings key and diffs
  the result. Seven rules — `import`, `kwarg`, `option`, `settings`,
  `hook_event`, `attr`, `shape`, `env`, `syntax` — wired into pytest via
  `tests/test_docs_snippets.py`, with a test that plants one failure per rule so
  the check can't silently stop working.

### Fixed

- **66 doc snippets that did not work.** Found by the checker above and fixed
  across `docs/` and `web/content/docs/`. The substantive ones: `query()`'s two
  option shapes were conflated (typed options yield `msg.content` and auto-route
  from the model name; a plain dict yields `msg.message.content` and does not
  infer a backend at all, defaulting a bare model name to vLLM's port);
  `hooks=` is a dict keyed by event name, not a list of `HookMatcher(event=…)`,
  hooks receive a single `HookContext`, and only 15 event names are mapped —
  the rest were invented and would be silently dropped; `ModelCapability` never
  had `tool_use_path` or `supports_thinking`; `setting_sources` takes the names
  `"user"`/`"project"`/`"local"`, not file paths; `MANTIS_AGENT_BACKEND` never
  existed; `register_pricing`, `pricing_override`, `compact_threshold`,
  `include_thinking`, and `memory_entries` never existed; `PRICING_TABLE` is
  keyed by `(provider, model_id)`; `Tool` takes `fn=`, not `handler=`;
  `ToolExecutionError` is raised *by* the runtime and has no `fatal=` flag;
  `SdkServer` is in-process and has no `serve_stdio()`; `Agent` takes `system=`
  and works in message lists, not prompt strings. A `match` statement in the
  streaming guide also broke on the package's own Python 3.9 floor.

- **Stale `__version__` fallback.** `_detect_version()` still returned
  `"2.61.0"` when package metadata is unreadable, one release behind
  `pyproject.toml`.

## [2.62.0] - 2026-08-06

### Added

- **`mantis update` — self-update that knows how you installed it.** Upgrading
  meant remembering whether this box used `uv tool`, `pipx` or pip, and running
  the wrong one produces an "upgrade" that silently changes nothing. The command
  detects the install mode from the interpreter's location and runs the matching
  updater. `--check` reports the available version without installing and exits
  `1` when an upgrade is waiting, so it composes into a shell condition.

  Two details that come from the failure modes rather than the happy path: a
  uv-created venv contains no `pip`, so `python -m pip` would die with "No module
  named pip" — mantis detects that and installs through `uv` into the same
  interpreter instead. And an **editable install is never touched**: that's a
  working checkout, potentially with uncommitted changes, so the command prints
  the `git pull` you'd want and exits rather than running anything. Version
  comparison is a self-contained PEP 440 subset (`packaging` is not a
  dependency) that gets 2.61 > 2.9 right — a string compare does not, and would
  report "up to date" forever once the minor hit double digits. The in-terminal
  `/update` now shares this detection so the two can't disagree. Subcommands are
  also listed in `mantis --help`, which previously advertised none of them.

- **Named workflows — declarative multi-agent orchestration, end to end.** The
  workflow engine shipped in 2.59.0 could run phases, fan out and pipeline, but
  nothing ever reached it: `/workflows` rendered an empty list because no code
  path registered a run. This closes that loop and adds the layer that was
  missing above it — a **named definition** you can invoke, watch, control,
  persist and resume.

  A definition is Markdown with frontmatter plus a fenced `json` phase graph —
  the same file shape as `agents/*.md` and `skills/*/SKILL.md` — discovered from
  `./.mantis/workflows/*.md` (project) and `$MANTIS_AGENT_HOME/workflows/*.md`
  (user), with project > user > built-in precedence by name. Data, not code:
  Python has no sandbox worth trusting, so where Claude Code evaluates a
  model-authored script, this walks a validated phase list. Phases run
  `parallel` (barrier), `sequential` (each agent sees `{prev}`), or `pipeline`
  (one independent chain per item from an earlier phase or an input, no
  barrier). Prompts template over `{input}`, `{phase:Title}`, `{item}`,
  `{prev}`, `{index}`; unknown placeholders are left literal so prompts
  containing JSON survive. Validation reports every problem at once, a broken
  file is skipped rather than fatal, and a definition (or a runtime fan-out)
  over 64 agents is refused before anything is spent.

  Five built-ins ship as examples worth using: `understand` (parallel readers →
  brief), `design` (independent approaches → judge → recommendation), `review`
  (dimensions → adversarial verify per dimension → report), `research`
  (multi-modal sweep → deep read → sourced answer), `implement` (plan →
  implement → verify).

- **`workflow` tool + `/workflows run`.** Both go through one launch path, so a
  model-started run and a user-started run are the same object. It backgrounds
  through the existing `JobManager` and returns a run id **and** a job id
  immediately; `/jobs` shows lifecycle, `/workflows` shows structure, and each
  names the other. The tool description is explicit that workflows spawn many
  agents and are only for orchestration the user actually asked for;
  `MANTIS_AGENT_DISABLE_WORKFLOWS=1` turns it into a clear refusal. Children
  inherit the parent's permission gate and budget, and orchestration tools
  (`task`, `coordinate`, `workflow`) are now stripped from subagents so a
  fan-out cannot recursively fan out.

- **Durable run history + resume.** Every run — including one that was stopped
  or failed — is written to `$MANTIS_AGENT_HOME/workflows/runs/<id>.json`.
  `/workflows history` lists past runs; `/workflows resume <run-id>` replays
  every agent whose phase, label and prompt digest are unchanged (instant,
  free, marked `replayed`) and re-runs the rest. Cache identity is
  content-addressed, so editing a prompt correctly invalidates that agent and
  everything downstream. Inputs whose names look like credentials are stored as
  `[redacted]` and never resurrected on resume.

- **`/workflows` is a real viewer.** Multiple concurrent runs are one navigable
  list; the header names the selected run, its position, its job and whether
  it's paused. Controls that were implemented but unreachable are now bound and
  documented in the footer: stop, pause/resume, **cancel agent**, **skip
  agent**, **retry agent**, save. Every action routes through one eligibility
  check that returns a sentence either way, so a key pressed on a finished run
  explains itself instead of doing nothing. Drill-down now shows the agent's
  prompt, its full recent activity, model/type/status, timing, usage and cost,
  and its result or error — with its workflow and job named. Never hidden
  reasoning: the engine records tool names and visible text only. The empty
  state teaches the command that starts a run and lists what's available.

### Changed

- `coordinate` runs now register with `/workflows` too (new `on_run` hook), so
  the live viewer covers both orchestration tools.
- `AgentRun` gained serialized `prompt` and `replayed` fields; `WorkflowRun`
  gained `definition`, `job_id` and `resumed_from`. `Job` gained `workflow_id`.
- `Workflow.agent()` accepts `cached=` — register an agent as already-complete
  from a stored result. This is the resume path.
- Workflow run ids now carry a process-wide sequence as well as a clock slice.
  Two workflows created in the same millisecond previously shared an id, which
  would have collided in the viewer, the job link and the on-disk artifact.

### Fixed

- **Claude subscription tokens can reach Opus, Sonnet and Fable again — not just
  Haiku.** With an OAuth token (`sk-ant-oat…`), every premium model answered
  `429 rate_limit_error {"message": "Error"}` while Haiku returned 200. The
  opaque body reads like a spent quota and is not one: Anthropic grants a
  subscription token the premium models only on requests carrying the Claude
  Code identity, and mantis never sent it. The passthrough provider now leads
  with that identity as a standalone first `system` block (folding the string
  into the caller's own system text does *not* satisfy the check — it has to be
  its own block). API keys and gateway Bearer tokens are untouched, and the
  prompt-cache breakpoint moved to the last block so the cached prefix still
  covers everything.

  Two things fell out of the same code path. A custom `anthropic_beta` was
  *replacing* the `oauth-2025-04-20` header rather than appending to it, which
  would 401 an otherwise valid token; betas now merge. And `mantis setup`'s
  credential probe sent neither the beta header nor the identity block, so it
  validated Opus as `credential OK (Error)` — a pass for a request shape mantis
  doesn't send. It now mirrors the real provider and genuinely verifies.

  The `/update`-adjacent 429 hint was also wrong as a result of this bug: it told
  people the model "isn't available on a Claude subscription token" and to switch
  to Haiku. With the request fixed, a 429 here really is a spent usage window,
  and the hint says so.

### Public API

- Added (MINOR): `AgentRun`, `Phase`, `Workflow`, `WorkflowRun`,
  `WorkflowError`, `WorkflowDefinition`, `WorkflowDefinitionError`,
  `discover_workflow_definitions`, `load_workflow_definition`,
  `make_workflow_tool`.

## [2.61.0] - 2026-08-01

### Changed

- Support and continuously test Python 3.9 through 3.14, with compatibility
  backports for exception groups, modern annotations, asyncio timeouts, dataclass
  slots, and newer built-in APIs.

### Added

- **Model-authored workflow scripts.** The `workflow` tool now takes a
  `script` as well as a `name`: the model writes the orchestration itself
  against the engine's own API — `agent()`, `parallel()`, `pipeline()`,
  `phase()`, `log()`, `args`, `budget` — instead of choosing between a fixed
  fan-out (`coordinate`) and a definition a human wrote down.

  This is what makes the rest of the engine reachable. A declarative definition
  is data, and data cannot express *loop until two consecutive rounds find
  nothing new* or *scale the fan-out to the budget that's left* — the shapes
  that make an orchestration engine worth having. Telling: the `parallel`-inside-
  `pipeline` deadlock fixed above survived because nothing model-facing could
  reach that pattern.

  Scripts run through an AST allowlist: loops, conditionals, comprehensions,
  f-strings, `try`/`except`, `def` and `lambda` are available; imports, dunder
  and `_`-prefixed attribute access, `eval`/`exec`/`open`/`getattr` and the
  filesystem are not. Agents a script spawns inherit the parent's tools,
  permissions and budget unchanged, so authoring orchestration never widens what
  runs inside it, and the concurrency cap still applies. This is a guardrail,
  not a security boundary — the author is the session's own model, which can
  usually already run shell commands.

  Scripts are validated before anything spawns, so a rejected one costs nothing,
  and the AST is wrapped in an async function rather than re-indented into a
  string so runtime errors carry the line number from the model's own source.
  `meta` is read as a literal up front, so the run is named and its phase rail
  drawn from the moment it starts.

  This reverses an explicit design decision recorded in `workflow_tool`'s
  docstring ("model-authored code is not something you want `exec()`-ed inside
  the user's process… everything that matters about a workflow is data, not
  control flow"). The docstring now records the reversal and what was kept of
  the original reasoning.
- **Advisor — pair a stronger model to consult at decision points.** Most
  turns of a long task are routine; a handful decide whether it works.
  `mantis --advisor opus`, `/advisor opus`, `advisorModel` in settings or
  `MANTIS_ADVISOR` pairs a second model, and the agent calls `consult_advisor`
  before committing to an approach, on a repeated failure, and before calling a
  hard task done. The advisor reads the live conversation (the tool holds the
  session's own message list, not a copy) and returns judgement — it gets no
  tools, so it can't race the main agent over the same files. `/advisor` shows
  the pairing, `/advisor off` clears it, `/status` carries a line, and each
  consult prints `⤴ consulting <model>` so spend on a second model is never
  invisible. A consult that fails comes back as "proceed on your own judgement"
  instead of taking the session down.

  The advisor **resolves its own provider, base URL and key**, independently of
  whatever the session is running — so a local model can escalate to a hosted
  one (Qwen on your box, three decisions an hour to Opus). Aliases work the way
  `/model` accepts them (`opus`, `sonnet`); an id the catalog doesn't know falls
  back to the session's backend rather than failing. Available headless too,
  where it matters most: nobody is watching a `-p` run, so "check this before
  you commit to it" is the only review it gets. Off by default, and off for
  small local models.

### Fixed

- **A tool call cut off by the output cap was reported as bad JSON syntax,
  which sent the model into a retry loop.** Writing a large file can push the
  `content` argument past `max_tokens` (8192 by default), leaving the arguments
  JSON ending mid-string. Mantis answered with "not valid JSON — re-issue a
  well-formed object": the wrong diagnosis and the opposite of the fix. The
  model's syntax was fine, so it faithfully re-emitted the same oversized call
  and truncated at the identical point, burning a full generation per attempt
  until the anti-runaway guard finally tripped. The stream already carried
  `stop_reason="max_tokens"` and nothing consulted it.

  Truncation is now told apart from malformation structurally — a cut-off
  payload is a well-formed *prefix* (structures still open, or ending inside a
  string), where a genuine syntax error is balanced but wrong — and the model is
  told it hit the output limit, given the actual `max_tokens` value, and told to
  split the write. Balanced-but-wrong payloads still get the original message,
  which is correct for them.
- **"No failures found." was reported as `VERDICT: FAIL`.** When a verifier
  didn't end with the exact `VERDICT: X` contract line, `_parse_verdict` scanned
  the *whole* report for a bare substring and tested `FAIL` first — so the most
  natural way to report success inverted, and any long report that mentioned a
  failure mode in passing came back as a failure. `coordinate` hands that
  verdict to the parent model as the result of the run. The fallback now reads
  only the final line and only accepts a standalone uppercase token; a verifier
  that stated no verdict is reported as `VERDICT: NOT STATED` with its report
  attached, rather than being assigned one it never gave.
- **Workflow deadlock: any nested fan-out hung forever.** The concurrency
  limiter was acquired by `parallel()` around each whole thunk and by
  `pipeline()` around each whole stage. `anyio.CapacityLimiter` is not
  reentrant, so a stage or thunk that itself fanned out held a slot while
  waiting on children that could never acquire one. That made the engine's two
  canonical patterns — `parallel` inside a `pipeline` stage (fan out, then
  verify each item's findings) and `parallel` inside `parallel` (a judge panel)
  — deadlock as soon as the outer fan-out reached the cap. It passed every
  existing test because they are all flat or run with a cap above their
  fan-out, and since the cap is `min(16, cpu-2)` whether a given workflow hung
  depended on the machine it ran on.

  The limiter is now held around the child agent run itself and nowhere else,
  so orchestration wrappers own no slots and nesting is safe at any depth. The
  bound is unchanged in meaning and still exact — measured peak concurrent
  agents equals the cap for flat, nested and three-deep shapes alike. A
  `stop()` with a full queue now drains promptly (agents waiting on the limiter
  re-check for cancellation on acquire rather than each running a full turn).
- **`Attempted to exit cancel scope in a different task` killed the event
  loop.** `Agent.run_iter` is an async generator that deliberately holds the
  streaming tool executor's anyio task group open *across* its yields — that's
  what lets the UI render "tool running…" while tools drain. The cost is that
  the generator must be finalized by the task consuming it. Every consumer
  abandoned it instead (`break`, an exception, or Esc cancelling the turn), so
  teardown fell to the event loop's async-generator shutdown hook, which runs
  in a **different task**, and anyio raised straight into the loop — taking down
  the whole session rather than just the turn. Interrupting a turn with tools in
  flight was enough to trigger it.

  All five consumers (`query`, `compat_query`, `subagent`, and both terminals)
  now close the stream in their own `finally` via a new shielded
  `agent.aclose_stream()` — shielded because the usual trigger *is*
  cancellation, and an unshielded await in an already-cancelled task re-raises
  before cleanup can run.
- **A broken pygments took down any reply containing a code block.** The
  markdown code-block renderer constructed a `rich.Syntax` lazily, so the lexer
  wasn't resolved until rich was already inside its console loop — where a
  raising renderer kills the entire message. Seen in the wild as
  `error: No module named 'pygments.lexers.special'` from a partially-installed
  pygments. The lexer is now resolved eagerly inside a guard and falls back to
  plain text.
- **`error: unsupported message type: CompactBoundaryMessage` — a compaction
  permanently bricked the session.** `CompactBoundaryMessage` is not a wire
  type and no provider's encoder knew it, so the first request after an
  auto-compact raised on *every* backend (`TypeError` on openai_compat and
  ollama, `ProviderError` on anthropic). The boundary stays in the history, so
  every retry hit it again and the conversation could not be continued at all.
  A shared `normalize_messages()` in `providers/base.py` now folds a boundary
  into a `SystemMessage` carrying its `[previous summary]` — the placement the
  type's own docstring anticipated — before any encoder sees it.
- **`error: Unknown parameter: 'max_thinking_tokens'` on every reasoning
  request.** 2.59.0's adaptive thinking wrote `max_thinking_tokens` into the
  OpenAI-compat payload, but that is the Claude SDK's option name — Chat
  Completions has no per-request thinking budget, and OpenAI 400s on any
  unrecognized field. The budget is now dropped and `reasoning_effort` carries
  the intent, which is the only knob the endpoint actually has. The tests that
  should have caught it asserted `_build_payload` against itself, so they
  locked the invented field in instead.
- **SDK control keys leaked onto the Anthropic and Ollama wires.** Both
  providers shallow-merged `extra` onto the payload with no filter, so
  `max_thinking_tokens`, `verbosity`, `reasoning_mode`, `reasoning_context` —
  and `allowed_tools` / `disallowed_tools`, which are permission decisions
  mantis enforces locally — were sent to the vendor. On Anthropic that 400s the
  request outright. There is now one shared `PROVIDER_CONTROL_KEYS` set in
  `providers/base.py`: providers translate these into their native knob
  (Anthropic's `thinking` block, OpenAI's `reasoning_effort`, Ollama's `think`)
  and drop the alias. Opaque vendor parameters still pass through.
- **A non-empty `extra` silently disabled reasoning** in openai_compat: a local
  named `thinking` shadowed the `thinking=` parameter, so `extra={"verbosity":
  …}` with no `"thinking"` key erased the universal config before it was read.
- `verbosity` is now sent only to GPT-5 ids, where it is a real field; it was
  going to gpt-4o and OSS servers that reject it.
- Switching to a small local model with `/model` left a stale advisor pairing
  showing in `/status` after the tool had already been dropped from the belt.
- **Every image paste wrote a duplicate line into the transcript.** Attaching
  already reports itself in the input — the `[Image #N]` chip lands in the line
  and the indicator above the prompt counts what's staged (with the
  can't-see-images warning) — so the extra `attached [Image #1] — sends with
  your next message` in the scrollback was pure noise, once per paste. Ctrl+V
  and `/paste` are now silent on success and speak only on failure.

### Development

- **`scripts/test-matrix.sh`** runs the suite across every Python version CI
  covers, each in its own environment under `.venvs/`. The obvious one-liner —
  `for py in 3.10 3.11 …; do uv run --python "$py" …; done` — rebuilds the
  *project* environment (`.venv/`) in place on each iteration, so it silently
  replaces your dev venv and breaks anything else using it mid-run. The
  resulting import errors look exactly like test failures, which makes the
  matrix untrustworthy in both directions.
- **`pyyaml` added to the `dev` extra.** `tests/test_docs_site.py` documents its
  structural layer as running without the docs extra, but that layer parses
  `mkdocs.yml` and so needs a YAML parser — which nothing installed. Three tests
  errored on a plain `pip install -e ".[dev]"`, i.e. on CI and for every
  contributor. The fallback also now `importorskip`s, so a minimal install
  skips rather than reporting phantom failures.

### Changed

- **The live-agent inspector shows what an agent is doing, not just which tool
  it called.** The feed rendered the bare tool name, so watching three explore
  agents gave you five identical lines of `tool grep` / `tool read_file` — no
  pattern, no file, no result, nothing to tell one call from the next. Each
  call is now one line carrying the salient argument and a shape-only summary
  of what came back:

  ```
  recent #1
    - 0s ago · Search def _build_payload → 12 matches
    - 2s ago · Read mantis_agent/tui.py → 4157 lines
    - 5s ago · Run pytest -q tests/test_advisor.py → 24 passed in 0.08s
    - 9s ago · Read gone.py → FileNotFoundError: gone.py
  ```

  A call in flight shows the argument without the arrow (so a slow grep reads
  as running, not as having returned nothing) and the same line is updated in
  place when it returns — one line per call, not a start line and a finish
  line. Errors are surfaced verbatim instead of being counted as "1 line".
  Result summaries are deliberately shapes, not payloads, so the panel can't be
  flooded by a large read. `TOOL_VERBS` moved to a new `tool_preview` module so
  the SDK-level subagent wrapper labels a call exactly the way the transcript
  does; `mantis_agent.tui.TOOL_VERBS` still resolves.
- Event lines in the inspector measured their own prefix as a hardcoded 10
  columns, under-counting it and wrapping in a narrow pane.
- **⌘V now attaches a copied image file.** A terminal can only deliver text, so
  raw screenshot bytes genuinely cannot ride in on ⌘V — but a file copied in
  Finder pastes as its POSIX path, which is the common "paste my screenshot"
  flow. The bracketed-paste handler recognizes a path (or `file://` URL) to a
  real file and attaches it exactly as Ctrl+V would, instead of dropping a bare
  path string in the buffer. Ordinary pasted text is untouched. The clipboard
  hint now reads `⌘v / ctrl+v to attach` for a copied file, and stays
  `ctrl+v to paste` for raw image bytes, where ⌘V truly cannot work.

## [2.60.0] - 2026-08-01

### Added

- **Image attachment is discoverable.** Ctrl+V has staged images for a while,
  but nothing ever said so — and on macOS ⌘V physically cannot carry an image
  into a terminal, so anyone with a screenshot copied had no way to find out
  the app would take it. A line above the prompt now offers
  `Image in clipboard · ctrl+v to paste` whenever the clipboard holds one, and
  once attached it becomes `◫ 1 image attached · sends with your next message`
  (with a `⚠ <model> can't see images` tail when the current model is
  text-only). Polled off the event loop in a worker thread, repainting only on
  change; set `MANTIS_NO_CLIPBOARD_HINT=1` to switch it off.

- **`/paste`** — stages the clipboard attachment without Ctrl+V (plenty of
  terminals bind it to their own paste), and `/paste <path>` attaches a file
  directly.

- **Dragged image paths attach from mid-sentence.** "what's wrong with
  /tmp/shot.png here?" now sends the image; previously only a message that was
  *nothing but* a path attached anything, so the model answered blind.
  Deliberately images-only — "read /etc/hosts" still means read it with a tool.

- `ImageBlock` is exported from the package root, alongside the other content
  block types.


- **`watch` — streaming background monitors.** The push counterpart to
  `bash(run_in_background=True)`: a long-running script whose every stdout line
  arrives in the conversation as a notification, so the agent learns about a
  failing test, a new PR comment, or a file change without remembering to go
  look. Ports Claude Code's Monitor tool, including the non-obvious parts —
  lines printed within 200ms coalesce into one notification (a traceback stays
  one message); stream events deliberately carry **no** terminal status, so a
  progress ping can never be mistaken for the job closing; stdout is the event
  stream while stderr goes to a log file without notifying; a watch that emits
  too fast is stopped rather than allowed to flood the context; and
  `persistent=true` opts out of the timeout for session-length watches. Stop one
  with `watch_stop(job_id=N)` or `/jobs kill N`. Named `watch` because mantis's
  existing `monitor` already covers the *wait for one condition* case that
  Claude's Monitor docs send elsewhere — the two are complementary.
  `JobManager` grew an `on_stream` callback and `Job.stream_count` to carry it;
  `/jobs` and `/job` show watches with a distinct `◈` glyph and an event count.

- **Click-to-switch models on the dashboard.** The `mantis serve` Models page is
  no longer read-only — every model chip (in provider cards and in the new browse
  list) is clickable and switches the current model via `POST /api/use`, wiring
  the provider's backend so routing stays correct. Recent-model chips are
  clickable too. Clicking a locked provider's model reveals + focuses that
  provider's key field.
- **Unified "any model" search (both surfaces).** A single fuzzy search across
  every model / provider / self-host in one flat, scannable list — in the TUI
  model picker (type to collapse the grouped view into a ranked "matches" list
  tagged by provider) and on the dashboard (a "Browse all models" search box over
  all providers).
- **Self-host from inside the TUI picker.** A pinned `＋ self-host / custom
  endpoint…` row (and a `self-host` tab) in `/models` pre-fills `/connect` so
  bringing your own OpenAI-compatible endpoint never means leaving the picker.
- **Recent models in the TUI picker.** An `↻ recent` group at the top of
  `/models` for one-keystroke re-selection.
- **Open-weight models are actually free now — one-keystroke in-TUI pull.**
  The picker's "open" tab no longer dead-ends in a provider key lock: a model
  already installed in Ollama switches instantly (`● local · free`); a
  pullable one shows `↓ pull <tag>` and **Enter downloads it right there** —
  streamed progress from Ollama's `/api/pull` rendered as a live progress bar
  under the prompt (`↓ pulling qwen3 ████░░ 68% · 185 MB / 271 MB · esc
  cancels`), prompt stays usable, auto-switches when done, esc cancels
  (re-pull resumes). No key, no shelling out, no leaving the TUI. Models
  without a curated tag offer the `/connect` self-host path. Actionable
  errors (too-old Ollama → update hint; unknown tag → library pointer).
  Curated model→Ollama-tag map (gpt-oss, deepseek, qwen3, kimi-k2, llama,
  mistral, gemma, phi…); `/pull <tag>` also works as a command in both TUIs.

- **Deferred tool schemas (`tool_search`).** Every tool costs tokens on every
  request — mantis's own belt is ~4.2k, and a single 26-tool MCP server adds
  ~5.2k more, on every turn, whether or not the model touches it. Past a dozen
  tools the MCP ones are now **deferred**: listed by name in the system prompt,
  with their schemas loaded on demand by a new `tool_search` tool
  (`select:name`, keyword search, or `+term` to require a term in the name).
  That 26-tool server drops from ~5,200 tokens per request to ~790 — about
  175k tokens saved over a 40-turn session, and the difference between "MCP
  works" and "MCP works on a 7B model". Deferring hides the schema, it never
  disables the tool: a blind call still executes. `/status` reports how many
  are deferred; `{"toolSearch": {"mode": "off"|"always", "threshold": 12}}`
  overrides the policy.
- **OS-level sandboxing for shell commands.** `--godmode`, `/goal`, `mantis -p`
  in CI and scheduled runs all exist so nobody has to watch — which is exactly
  where "we'll ask the user" stops being a safety story. `/sandbox on` (or
  `--sandbox`, or `{"sandbox": {"enabled": true}}`) wraps every shell command
  in the OS's own sandbox: **Seatbelt** on macOS, **bubblewrap** on Linux.
  Writes are confined to the project plus temp; the rest of the disk stays
  readable but read-only, and `--sandbox-no-network` cuts the network too.
  These are kernel-level refusals, not prompts — the tests prove a write to
  `$HOME` fails and the file never appears. Off by default (silently shrinking
  what a shell can do would be its own surprise), `failIfUnavailable` refuses
  to run rather than falling back to unconfined, and the setting rides on the
  environment so it reaches subagents and background shells.
- **`/cron` — scheduled runs that outlive the session.** `/loop` and `/watch`
  die when you close the terminal; these don't. `/cron every 30m triage new
  failures`, or from the shell `mantis cron add "daily 09:00" "summarize
  yesterday's commits"`, plus `list` / `logs` / `run` / `pause` / `remove`.
  Schedules read as `every 30m`, `daily 09:00`, `mon 09:00`, or a 5-field cron
  expression. **`mantis cron install`** registers a one-minute tick with
  launchd or a systemd user timer, so jobs fire with nothing open. Each run
  goes through the same headless path as `mantis -p`, in its own directory,
  with a per-run log — and is **sandboxed by default**. Nothing fires on
  import: only an explicit `tick` or `daemon` runs a job.
- **`mantis -p` — headless print mode.** One prompt, the answer, exit:
  `mantis -p "fix the failing test"`. The prompt comes from the argument, from
  `-`, or from piped stdin. Unlike `mantis-agent run`, it resolves the model
  the way an interactive session does — the one you last used, with its
  provider key and backend already wired — so scripts stop repeating
  `--model`, and it carries the terminal's full tool belt.
  `--output-format text|json|stream-json` (`--json` shorthand): text prints the
  reply, json prints one result object (`--verbose` → the whole message array),
  stream-json emits NDJSON — a `system`/`init` event, every assistant/user
  message including tool calls and results, then the final `result` — and
  requires `--verbose`, matching Claude Code's rule. Exit code is 1 exactly
  when the result is an error; stdout carries only the result so a pipeline can
  parse it, with everything else on stderr. Also `--allowed-tools` /
  `--disallowed-tools` (both `--allowedTools` spellings), `--append-system-prompt`,
  `--session-id`, and `--godmode` for unattended runs. Piping into `head` exits
  quietly instead of printing a broken-pipe traceback.
- **Headless runs share the terminal's sessions.** A `mantis -p` run is
  recorded in the same store `mantis --resume` reads, so a CI run shows up in
  your session picker and you can pick up where a script left off:
  `--resume <id>` reloads that conversation's turns (the model doesn't redo
  work it already did), `--continue` grabs the most recent session in this
  directory, and `--session-id` pins one for correlating runs. Under the hood
  `query()` gained an `options["messages"]` seed for prior history — it's
  threaded into the loop but not re-emitted on the stream, since a resuming
  consumer already has those turns.
- **`mantis serve` redesigned as an instrument panel.** The dashboard was a
  tidy admin page; it's now built out of the product's own materials. A left
  rail replaces the tab strip and always shows what the agent is wired to
  (model, how it's reached, providers / servers / skills / sessions), with
  `1`–`6` jumping between pages. Monospace is the display face — this
  product's proper nouns are `mcp.json` and `claude-opus-5` — and every page
  opens with a **signal path**: a live wiring diagram (`agent ──▶ 4 servers
  ──▶ 3 local ──▶ 1 withheld`) whose nodes carry real state. No hairline
  borders anywhere: surfaces are layered instead. Everything enumerable —
  servers, skills, providers — is the same expand-in-place row.
- **MCP page: an inspector that proves itself.** Rows expand into the server's
  real configuration (command, args, env keys, url, headers, extra fields, and
  the raw JSON entry as it sits on disk) with credentials masked and a
  **Reveal secrets** toggle. **Test connection** runs an actual handshake and
  lists the tools that came back, with latency. **Edit** opens the entry as
  JSON. Adding takes a whole `{"mcpServers": …}` blob, one entry object, a
  command, or a URL in a single field. An untrusted project `.mcp.json` gets a
  banner with one-click **Trust this file**.
- **Models page: a comparison and a setup flow, not a list.** Every model now
  shows its context window and whether it supports native tools, reasoning
  effort, or visible thinking — read from the SDK's own capability table —
  with filter chips (all / ready to use / needs a key), `/` to focus the
  filter, and arrow-key selection. **Test this route** proves the current
  wiring end to end. Providers became a setup task with a progress bar
  (`2 of 12 connected`), each vendor's real logo (inlined in the wheel — a
  local dashboard shouldn't tell twelve CDNs which providers you're looking
  at, and the page has to work offline), a one-click **Add key** that opens
  the field, and a per-provider reachability check. A provider that already
  has a key shows the masked key and where it came from, with **Replace** as
  an opt-in rather than a paste box in your face.
- **Activity page rebuilt.** Six overlapping panels became one composition:
  the **trace** (26 weeks of daily volume as a raw envelope under a 7-day mean,
  peak annotated, drawn on load), a read-out strip, a **punchcard** of
  weekday × hour (the joint distribution, so "Sunday night" is finally
  answerable), a tool-mix spectrum, and a project ledger.
- **Skills page** gained search and a real editor — name, description,
  category, always-load, body — writing the same `SKILL.md` the agent reads.
- New endpoints behind all of this: `GET /api/mcp/entry`,
  `POST /api/mcp/paste|test|trust`, `POST /api/model/test`, plus a
  `punchcard` and per-model capability info in the existing payloads.
- **`/mcp` is a full MCP inspector.** The fullscreen view went from a status
  list to a two-level browser: a clean table of every configured server (status
  dot, transport, origin, live tool count / error text), and — on Enter — a
  detail card showing that server's *actual* configuration: command, args,
  env keys, url, headers, any extra fields, the connected tool names, warnings,
  errors, which file defines it, and the raw JSON entry as it sits on disk
  (scrollable when it's long). Credentials — env values, header values,
  `?apiKey=` URL params, `--api-key` argv values — are masked by default;
  `s` reveals them for the current view.
- **`/mcp` takes JSON.** `a` opens one paste field that accepts whatever's on
  your clipboard: a full `{"mcpServers": {…}}` blob (adds all of them at once),
  a bare `{name: entry}` map, a single entry object, `claude mcp add-json`'s
  named shape, a shell command, or a URL — comments and trailing commas
  tolerated. `e` edits the selected server's entry as JSON, prefilled from
  disk. `x` removes, `t` trusts a project `.mcp.json`, `r` reconnects
  everything. Parse failures explain themselves instead of saying "couldn't
  parse that". Writes still go only to `~/.mantis-agent/mcp.json`; project- and
  settings-defined servers are read-only and say which file to edit.

### Changed

- **Working status moved above the input.** The spinner line (`✻ Mulling… (34s
  · esc to interrupt)`), the retry note, and the live task checklist now render
  *above* the prompt (with a blank line of breathing room), like Claude Code —
  the reply streams in right over them and the input stays anchored at the
  bottom. The footer below the prompt now always shows mode · model · knobs,
  even mid-turn. Transcript spacing switched to leading separators — same
  rhythm mid-turn, but a turn now ends tight against the input rule (one
  blank, not two).
- **Model picker + Models page visual refresh** ("any model, any provider, any
  self-host"). TUI picker: redrawn as a framed panel — title/current model in
  the top border, nav hints + a live scroll indicator ("12 more ↓") in the
  bottom border, width-capped table-aligned rows with a right-aligned "how it
  runs" column (`● local · free` / `↓ pull <tag>` / `self-host` / `needs key`
  / context window / `● now`), a clamped tab bar that collapses overflow into
  "+N →", and single-cell glyphs throughout (no emoji width drift). Dashboard:
  a hero banner, the self-host form presented as a first-class card, and
  clearer hosting state — all within the existing design system.

### Fixed

- **The macOS clipboard probe no longer decodes the image.** It ran
  `the clipboard as «class PNGf»`, converting the entire screenshot just to
  answer yes/no (~130ms); `clipboard info for «class PNGf»` reports the flavor
  without decoding (~50ms), which is what makes polling it affordable.

- **More vision models recognized.** The paste-time warning missed Grok, Llama
  4, InternVL, GLM-4V, Phi-4-multimodal, o1 and gpt-4-turbo, so attaching to a
  perfectly capable model claimed it couldn't see.

- **Images no longer blow past auto-compaction.** The token estimator scored
  every `ImageBlock` as **zero**, so a screenshot-heavy run — browser
  automation, repeated `Read` of a PNG — could carry millions of base64
  characters while the estimator still reported plenty of headroom. Neither
  microcompaction nor full compaction ever fired, and the turn died on a
  provider context-overflow error. Images are now estimated from the payload
  actually shipped (base64 length, or a ~1600-token floor for remote URLs).

- **Structured tool results are compactable.** Microcompaction only cleared
  tool results whose content was a *string*, so the list-shaped results that
  image-returning tools produce were skipped no matter how old or how large.
  It now clears structured results and bare `ImageBlock`s too, keeping
  `tool_use_id` intact so the tool pair still matches.

- **An overflowed session can recover instead of wedging.** Both compaction
  paths deliberately protect the recent window — exactly where a sudden
  oversized turn lands — so after a real overflow the retry re-sent the same
  rejected prompt, and every following message (including a manual `/compact`)
  hit the same wall. Emergency compaction now escalates to
  `SimpleCompactor.emergency_clear`, dropping heavy payloads in the recent
  window as well, keeping only the newest tool result. A degraded run beats a
  dead one.

- **The summarizer prompt can't overflow on its own.** A transcript large
  enough to need compaction could exceed the window it had to be summarized
  *in*; the call failed, `compact` swallowed the exception, and compaction
  silently no-opped precisely when it mattered. The prompt is now bounded per
  message and in total, eliding the middle, and never renders base64.


- **Dashboard details that were quietly wrong.** Primary buttons rendered
  near-black text on the dark-green light-mode accent (unreadable); the
  "when you work" dial showed `-1:00` as the peak hour with no data; the
  Activity page drew a full grid of empty charts on a fresh install instead of
  saying there's nothing recorded yet; a modal survived a tab switch; and
  absolute home paths (`/Users/you/…`) are shortened to `~/…` so a screenshot
  of the page doesn't broadcast your account name.
- **One broken MCP server no longer takes down the ones behind it.** A server
  that died around the handshake (a command that exits instantly, a URL with
  nothing listening) tore down its client's task group — whose host task was
  the connect loop itself — so the cancellation sailed past `except Exception`
  and silently aborted every server queued after it: no tools, no errors, no
  status rows, and `/mcp` stuck on "connecting…". Root cause was
  `MCPClient.close()` unwinding its scopes in the wrong order (transport before
  its own task group), leaving the caller with a live cancelled scope that
  cancelled everything it did next. Close order is fixed, `connect_all()`
  isolates per-server failures for real, and `MCPManager.aclose()` is safe
  against two teardowns overlapping.
- **Quitting with MCP servers configured no longer prints a traceback.** A
  server that dies during (or after) the handshake cancels the client's task
  group, and that cancellation escaped teardown as an `asyncio.CancelledError`
  splattered over the shell — with a non-zero exit code. The stdio transport
  now shields its stdin close like it already shielded process waits, and
  `MCPManager.stop()` is bounded (15s, 5s on an in-session reload) and total:
  it always closes what connected, and never raises.
- **A wrong MCP URL fails in seconds, not a minute.** The MCP HTTP/SSE
  transports were using the model-API HTTP client (10s connect × four
  backed-off retries), so one unreachable server stalled startup — and any
  `/mcp` mutation behind it — for ~40s. They now connect fast and fail fast
  (5s connect, no retries), keeping the generous read budget a live streaming
  session needs.

## [2.59.0] - 2026-07-14

### Added

- **Ambitious harness (persistence + completion contract).** The core loop
  (`Agent.run_iter`) no longer stops the instant a turn has no tool call. A
  gated completion contract keeps working while there's real unfinished work
  (open todos or an unmet token/USD *target*), under a diminishing-returns
  guard and a hard continuation cap — with a terse "keep working, don't
  summarize" nudge. `persist=True` by default (opt out with `persist=False`); a
  plain `query()` that returns a final answer with no todos behaves exactly as
  before. New `Budget` spend *floors* (`target_usd`/`target_total_tokens`) plus
  `runway()`/`target_unmet()` two-sided signals.
- **Adaptive thinking + effort.** Providers accept a `thinking` config and
  translate it per backend (OpenAI `reasoning_effort`, Anthropic thinking
  budget, Ollama best-effort), gated by a new `supports_reasoning_effort`
  capability. `effort` option (low|medium|high|max); an "ultrathink"/"think
  harder" keyword escalates reasoning for a turn; thinking bumps after repeated
  failure.
- **Coordinator tool.** `mantis_agent.coordinator.make_coordinate_tool` wires
  the workflow engine into a model-facing `coordinate` tool — decompose an
  objective into plan → parallel workers → synthesis → adversarial verify,
  streamed live into `/workflows`.
- **`/workflows` engine + viewer** (fullscreen overlay + classic modal): a
  deterministic orchestration engine (`workflow.py`: agent/parallel/pipeline/
  phase, per-agent budget, stop/pause/resume/skip/retry) with a live viewer.
- **Live subagent inspector.** ↓ from the prompt steps into running subagents;
  Enter drills into a focused detail view, ← goes back, Esc closes.
- **AGI-level `/agi` + `/goal`.** Budget-aware continuation, robust completion
  markers, stagnation guard, independent evidence-cited verify, durable
  frontier, recall-at-kickoff, pause-not-kill.
- **Sharper exploration.** Rewritten `explore`/`plan` personas, a new
  adversarial `verify` persona (`VERDICT: PASS|FAIL|PARTIAL`), light context
  instead of blind exploration, and a "# Delegating to subagents" section in
  the main system prompt.

### Fixed

- Large security + correctness hardening: swarm now runs under the parent
  permission context; permission-callback rewrites are re-validated (fail
  closed); SSRF guard on `web_fetch`; MCP transport fixes (SSE handshake, HTTP
  per-request errors, string ids, session DELETE); capabilities table
  (qwen3/deepseek-r1/llama.cpp); cache-token cost double-count; Optional/Union
  tool-schema derivation; notebook read-before-write; compaction tool-pair
  boundary; settings precedence; plus ~120 further audit fixes.
- Lint now blocks CI (removed `|| true`); classic `run()` missing `anyio`
  import (would `NameError` on `MANTIS_CLASSIC=1`).

## [2.58.0] - 2026-07-07

### Added

- **Background jobs.** `task(run_in_background=true)` detaches any subagent as
  a job: returns the id instantly, keeps showing in the live progress block,
  and on completion the user gets a notification while the RESULT is injected
  into context so the model knows next turn. `job_output(job_id, wait)` checks
  in; `/jobs` lists and kills; leftovers die with the session. Engine:
  `mantis_agent.jobs.JobManager` (spawn/wait/cancel, 60-min backstop,
  exactly-once completion events, pre-start-cancel race handled).

## [2.57.0] - 2026-07-07

### Added

- **`/swarm N <task>`** — N parallel general-purpose agents in isolated git
  worktrees; a judge ranks the diffs and applies only the winner's patch.
  Engine (`mantis_agent.swarm.run_swarm`) takes injectable runner/judge.
- **Live subagent progress** — in-flight `task`/swarm runs render under the
  spinner (`⎿ ◇ explore · 6 tools · 42s`).
- **Crash recovery** — unclean exits are detected; the next launch offers the
  crashed session's `/resume` line.
- **Vision guard** — Ctrl+V into a text-only model warns immediately instead
  of silently attaching an invisible image.
- **PTY end-to-end tests** — an expect-style harness drives the real
  fullscreen binary (slash commands, pickers, `!` prefix, crash reboot).
- **Docs** — sub-agents, MCP, and terminal guides now cover agent types,
  twins, `.mcp.json`, autonomy commands, sessions/rewind, small-model mode.

## [2.56.0] - 2026-07-07

### Added

- **Subagent types** for the `task` tool — `explore`, `plan`, `general-purpose`,
  plus user-defined agents from `~/.mantis-agent/agents/*.md` / `.mantis/agents/*.md`
  (frontmatter: name/description/tools/model/max_steps). Parent permissions are
  inherited; parallel fan-out is preserved. `/agents` lists them.
- **`pair` — twins.** Persistent same-model peers the agent converses with across
  turns (`peer=`, `persona=`, `reset`). Grounded in read tools. `/twin` lets the
  USER join the same conversations.
- **Full MCP integration.** `.mcp.json` / `~/.mantis-agent/mcp.json` /
  `settings.mcpServers` discovery, `MCPManager` lifecycle with per-request
  timeouts, `mcp__server__tool` namespacing, `/mcp` status. External stdio/sse/http
  servers now also work through the SDK's `options.mcp_servers` (previously
  dropped), and `options.agents` AgentDefinitions register as delegatable tools.
- **`monitor` tool** — wait for a background shell pattern/exit, a file, or a port
  in one call.
- **Autonomy:** `/goal` autopilot (plan → execute → adversarial verify → reflect,
  cycle-capped, esc stops), `/watch` sentinel (agent wakes when a command starts
  failing), `/loop` intervals.
- **Terminal parity wave:** `!` bash prefix, `#` memory notes, custom slash
  commands with `$ARGUMENTS`, skills as slash commands, persistent input history,
  message queueing while a turn runs, esc-esc rewind picker, file-state rewind
  (write tools checkpoint; `/rewind` restores code), session auto-titles + terminal
  tab titles, next-prompt ghost suggestions, resume picker with replay + time-ago,
  Ctrl+V image paste in the fullscreen UI, `/status` `/cost` `/doctor`
  `/permissions` `/mcp` `/skills` `/update` `/release-notes`, `--resume` CLI.
- **Small-model mode:** 7B-class local models get a slim 10-tool belt, compact
  system prompt, and stable prompt prefix (Ollama KV-cache reuse — follow-up
  turns drop from ~19s to ~1s).

### Fixed

- MCP stdio transport crashed on spawn (`anyio.subprocess` does not exist);
  `MCPTool.to_mantis_agent_tool` passed an invalid kwarg — the stdio path had
  never been exercised.
- Streaming executor parameter cache was keyed by `id(fn)` — a GC'd tool
  closure's recycled address could silently DROP a new tool's arguments.
- Retry noise: transport + model-level retries now surface as one in-place
  spinner note in the TUI (headless keeps WARNING logs); slash-command errors
  can no longer escape as raw tracebacks; offline DNS errors get a clear hint.
- Unknown-tool errors now name the closest real tool and tell the model it may
  answer in text (small models invent tool names).
- `/model` resolves numbers / ids / provider names / tier words / fuzzy
  fragments and never persists an unresolvable string; live-model caches and
  the saved model store reject implausible ids and self-heal.
- MCP client task-group lifetimes confined to one task (`MCPManager.start/stop`)
  — teardown from async generators / other tasks no longer misnests cancel scopes.

## [1.8.1] - 2026-06-30

### Added

- **`mantis` full-screen: a live, navigable slash-command menu.** Typing `/`
  now shows a real layout window (not a fragile completion float) listing the
  matching commands with descriptions — arrow ↑/↓ to select, Tab/Enter to fill.
- **`/models` is a selectable model picker.** It lists only *chat* models from
  the active backend (embeddings, tts, whisper, moderation, and legacy models
  are filtered out), navigable with the arrow keys; Enter switches and rebuilds
  the agent so the change takes effect immediately, and persists the choice.
  `/model <partial>` filters as you type.

### Fixed

- `/model <id>` in the full-screen UI now rebuilds the live agent (previously it
  set the model string but the running agent kept the old model).

## [Unreleased]

### Added

- **Built-in tracing — span tree of every agent run, no required dependency.**
  New `Agent(tracer=...)` field accepts any object satisfying the new
  `Tracer` protocol. The agent loop emits four span kinds:
  - `agent.run` (one per `Agent.run()` / `run_iter()` call) carrying
    `agent.model`, `agent.turns`, and aggregate token / cost totals.
  - `agent.turn` (one per turn) with `turn.stop_reason`,
    `turn.input_tokens`, `turn.output_tokens`, `turn.tool_uses`.
  - `llm.call` (one per provider stream) with `llm.input_tokens`,
    `llm.output_tokens`, `llm.cache_read_tokens`,
    `llm.cache_creation_tokens`, `llm.stop_reason`, `llm.first_token_ms`.
  - `tool.call` (one per dispatched tool) with `tool.name`, `tool.id`,
    `tool.input.keys` (sorted KEYS only — never values, so PII can't
    leak into observability backends), `tool.is_error`,
    `tool.result.len`.

  Ships two implementations:
  - `InMemoryTracer` — zero-dep, records to a list; `tracer.tree()`
    reconstructs the span forest, `tracer.summary()` aggregates by
    span name, `tracer.write_jsonl(path)` exports to disk.
  - `OTelTracer` — lazy-imports `opentelemetry.trace`. Plugs into any
    existing OpenTelemetry pipeline (Datadog, Honeycomb, Tempo,
    Jaeger, ...). Raises a clear `ImportError` at construction if
    `opentelemetry-api` isn't installed.

  Span ids are 16-char hex (OTel SpanID width); trace ids are 32-char
  hex (OTel TraceID width) so exporters can use them verbatim. When
  `Agent.tracer is None` the loop pays zero overhead — every span call
  site is gated by a single `if tracer is None`. Tool-input keys are
  recorded but **values are never copied into spans** — opinionated
  privacy default that matches what production teams ship to SaaS
  observability backends. Four new public exports: `Tracer`, `Span`,
  `InMemoryTracer`, `OTelTracer`. Example:
  `python -m mantis_agent.examples.with_tracing`.

- **Structured output via `response_format`.** New `Agent(response_format=...)`
  and `ClaudeAgentOptions(response_format=...)` fields accept the OpenAI
  `response_format` shape — `{"type": "json_object"}` for free-form JSON, or
  `{"type": "json_schema", "json_schema": {"name": ..., "schema": ..., "strict": ...}}`
  for schema-constrained output. The agent layer translates per backend:
  OpenAI-compat / Modal / llama.cpp pass the envelope through verbatim,
  Ollama maps to its native top-level `format` field, TGI maps to
  `parameters.grammar`. `anthropic_passthrough` raises a loud
  `ResponseFormatError` (real Anthropic API has no `response_format`).
  Three new public exports: `ResponseFormatError`,
  `normalize_response_format`, `translate_response_format`.

## [2.55.0] — 2026-07-01

### Fixed

- **Credentials and backend URLs are whitespace-stripped.** A trailing newline on
  an API key or token — which `.env` files and copy-paste routinely add — poisons
  the `Authorization` header and produces a confusing 401. Keys, `ANTHROPIC_AUTH_TOKEN`,
  and the backend URL are now `.strip()`ed at every entry point (`catalog.set_key`
  / `api_key_for`, the Anthropic-passthrough provider, and the TUI constructor).

### Added

- **`ls` shows file sizes and a count summary.** Each file entry now includes a
  human-readable size (`config.json (2 B)`, `data.csv (4.1 KB)`) and the listing
  is headed by a `(N dirs, M files)` count, so the model can gauge a file's size
  before deciding whether to read it whole or page with `offset`/`limit`.
  Directories are still listed first, marked with a trailing `/`.

## [2.54.0] — 2026-06-30

### Added

- **`bash_kill` tool** — terminate a background shell (Claude's KillShell). The
  agent could start (`bash run_in_background=True`) and read (`bash_output`) a
  long-running background process, but had no way to STOP one it started — only
  session-end cleanup did. `bash_kill(bash_id)` terminates it (whole process
  group, so forked children die too) and deregisters it; reports if the id is
  unknown or already exited. Completes the background-shell lifecycle:
  start → read → kill.

## [2.53.0] — 2026-06-30

### Changed

- **`bash_output` is now incremental.** It returned the ENTIRE accumulated log on
  every call — so an agent polling a long-running background process (a dev server,
  a slow build) re-injected all prior output into context each time, wasting the
  window and confusing the model with repeats. It now returns only the output
  written SINCE the last read (tracking a byte offset per shell, Claude's
  BashOutput behavior), with a `(no new output)` note when nothing changed. The
  running/exited status is still shown each call.

## [2.52.0] — 2026-06-30

### Added

- **`grep(fixed_strings=True)` — literal search** (Claude Grep parity). Models
  constantly search for code containing regex metacharacters — `config.get("x")`,
  `arr[0]`, `a|b` — where the `.`/`(`/`[` match wrongly (or make the pattern an
  invalid regex that errors). `fixed_strings=True` treats the pattern as a literal
  string (`rg -F`, or `re.escape` on the Python fallback), so it matches exactly.
  Default remains regex.

## [2.51.0] — 2026-06-30

### Added

- **Context-overflow auto-recovery.** When a model rejects a prompt as too long
  (`context_length_exceeded`, "maximum context length…", "prompt is too long"),
  the agent now emergency-compacts — clears old tool-result bodies (no model call)
  AND summarizes older turns — and retries the request ONCE, instead of failing the
  turn. A safety net for when auto-compaction didn't fire in time (a sudden huge
  input, or a model whose real window is smaller than advertised). Retries only
  once (if it still overflows, it errors with the `/compact` hint from 2.50), and
  needs no config beyond the compactor that's on by default. New
  `_is_context_overflow` / `Agent._emergency_compact`.

## [2.50.0] — 2026-06-30

### Added

- **Actionable hints for three more common errors.** When a turn fails, the error
  line now suggests a fix for: **context-length-exceeded** ("the conversation is
  too long — /compact to shrink it, or /clear to start fresh"), a model that
  **doesn't support tool calling** ("/models to pick a tool-capable model"), and
  **out-of-memory** on local models ("pick a smaller / more-quantized model").
  These join the existing auth / rate-limit / model-not-found / connection hints.
  Ordered so a "tools not supported" message isn't mis-hinted as "model not
  available."

## [2.49.0] — 2026-06-30

### Added

- **`mantis --continue` (`-c`)** — resume your most recent conversation on launch
  instead of starting fresh, picking up exactly where you left off (Claude's
  `--continue`). Loads the newest session's messages and continues writing to the
  same on-disk session (so it keeps growing, not forking). Prints a one-line
  "continuing: <first prompt>" confirmation; with no past conversation it starts
  fresh with a note. Builds on the full-screen persistence added in 2.48. New
  `MantisTUI.resume_most_recent`.

## [2.48.0] — 2026-06-30

### Fixed

- **The default (full-screen) TUI now persists conversations.** It never created a
  session or saved turns to disk — so `/resume`, `/branch`, and `/rewind` (wired in
  2.15.1) had nothing to work with there; only the classic REPL fallback did. The
  full-screen path now starts an on-disk session at launch and appends each turn
  (best-effort, meta/context messages skipped, failed turns not saved), so past
  conversations actually show up in the `/resume` picker and can be branched.

## [2.47.0] — 2026-06-30

### Changed

- **`max_tokens` now defaults to the model's full output budget.** The old default
  of 1024 tokens (~100 lines) silently truncated a large file write or edit
  mid-output — a frequent, confusing failure. When the caller leaves the default,
  the agent now uses the model's advertised `max_output_tokens` (e.g. 4096),
  capped at 8192 so it stays sane, and capped DOWN for small-output models. An
  explicitly-set `max_tokens` (any non-default value, higher or lower) is always
  respected.

## [2.46.0] — 2026-06-30

### Added

- **Esc clears a half-typed input line when idle.** Previously Esc did nothing if
  you'd typed a message but not sent it — now it clears the line (the standard
  REPL expectation), while every existing Esc behavior is preserved by precedence:
  cancel an inline key entry, close the model picker, deny a permission prompt,
  cancel/skip a question, or interrupt a running reply — those all still win over
  clearing. The precedence is now an explicit, tested `tui.esc_action` decision
  function instead of a nested if-ladder.

## [2.45.0] — 2026-06-30

### Added

- **`edit_file`/`multi_edit` auto-fix copied line numbers.** `read_file` prints
  each line as `  42\tcode` — and models constantly copy that numbered output
  straight into an edit's `old_string`, which then never matches the real file. On
  a miss, the edit tools now strip the `<num>\t` prefixes and retry; if the
  stripped form matches, the edit proceeds. So the single most common edit failure
  on OSS models self-corrects instead of erroring. Normal edits are unaffected;
  a genuinely-absent string still errors (with the existing "closest line" hint).

## [2.44.0] — 2026-06-30

### Fixed

- **`@`-mentioning a binary/image file no longer dumps garbage into context.**
  `@screenshot.png`, `@archive.zip`, `@model.bin` used to read the file's bytes and
  decode them as UTF-8 — injecting a wall of replacement-character garbage that
  wasted context and confused the model. Such files are now detected (by extension
  or a NUL-byte sniff) and noted instead: `[pic.png is a binary/image file — not
  inlined; read it with read_file (images render inline on vision models)]`. Text
  files — including ones with unusual extensions — are still inlined normally.

## [2.43.0] — 2026-06-30

### Added

- **Salvage Llama-style `<function=NAME>{json}</function>` tool calls.** When a
  model emits a tool call as text instead of using the structured channel (common
  on OSS models), mantis recovers it. It already handled JSON objects and shell
  fences; now it also parses the `<function=name>…</function>` /
  `<function_call name="…">…</function_call>` shapes Llama-family models produce.
  Salvaged names go through the tool-name resolver too, so `<function=Read>` maps
  to `read_file`. Recovers a call that would otherwise be lost as prose.

## [2.42.0] — 2026-06-30

### Fixed

- **`todo_write` maps status synonyms instead of dropping them to `pending`.** A
  model that marked an item `done`, `finished`, `complete`, `doing`, `in-progress`,
  `todo`, `blocked`, etc. had it silently normalized to `pending` — so a *finished*
  task showed as *not started*, misreporting progress to the user. Statuses are now
  mapped to the canonical `pending`/`in_progress`/`completed` via a synonym table
  (case/format-insensitive); a genuinely unknown value still defaults to `pending`.

## [2.41.0] — 2026-06-30

### Added

- **`sleep` tool** (parity roadmap T2). A bounded, interruptible wait for agents
  that must pause for external progress — a deploy to roll out, a CI run to
  advance, a background process (`bash_output`) to produce more output — before
  checking again. `sleep(seconds)` clamps to 0–600s, holds no shell, and respects
  cancellation, so it's safe for waits longer than a `bash` `sleep` (which the
  120s command timeout would kill). Registered in the coding tool belt.

## [2.40.0] — 2026-06-30

### Added

- **"Did you mean?" for wrong file paths.** When `read_file`/`edit_file`/
  `multi_edit` are given a path that doesn't exist but a close-name file DOES in
  the same directory, the error now suggests it — `no such file: config.jsonn.
  Did you mean .../config.json?` — so a model that guessed a slightly-wrong path
  self-corrects in one step instead of flailing. Uses `difflib` on the directory
  listing; a genuinely-missing file or bad directory still gets a plain error (no
  false suggestions). Mirrors the existing edit-miss hint. New `_path_suggestion`.

## [2.39.0] — 2026-06-30

### Fixed

- **`/help` no longer drifts.** It was a hardcoded list that had fallen out of
  date — `/compact`, `/init`, `/learn`, `/resume`, `/branch`, `/rewind`, and
  `/vim` were all missing. `/help` is now generated from the registered
  `SLASH_COMMANDS` (with categories: model · session · project · review · editor),
  so every command — including any added later — is listed automatically with its
  real description. A test asserts full coverage so it can't drift again.

## [2.38.0] — 2026-06-30

### Added

- **Schema-driven tool-argument coercion.** Models pass typed args as strings
  constantly — `head_limit="10"`, `replace_all="true"`, `timeout="30"`. The
  executor now coerces each argument to the type its `input_schema` declares
  before calling: `"10"`→`10` (integer), `"0.5"`→`0.5` (number),
  `"true"/"yes"/"1"`→`True` / `"false"/"no"/"0"`→`False` (boolean), and a
  JSON-string array/object into the real structure. Best-effort — an
  uncoercible value is left untouched, correct types are a no-op. Runs right
  before the extra-arg filter (2.36), so loose model output is repaired end to
  end. New `_coerce_to_schema`.

## [2.37.0] — 2026-06-30

### Added

- **Tool-name resolution tolerates Claude-name / case drift.** Many OSS models
  learned Claude Code's capitalized tool names and emit `Read`, `Bash`, `Edit`,
  `Grep`, `str_replace`, etc. — which don't match mantis's `read_file`, `bash`,
  `edit_file`, `grep`. Tool dispatch now resolves a call by: exact match →
  case/underscore-insensitive match → a Claude-Code-name alias table. So those
  calls just work instead of failing as "unknown tool" and burning a turn.
  `ToolRegistry.get()` stays exact (internal checks rely on it); the new
  `resolve()` does the fuzzy matching, wired into the executor + agent dispatch.

## [2.36.0] — 2026-06-30

### Added

- **Tool calls tolerate hallucinated extra arguments.** Small/local models
  routinely add an argument a tool doesn't declare (e.g. `read_file(path=…,
  recursive=true)`), which used to `TypeError` the call and burn a whole turn on
  the error+retry. The executor now drops arguments the tool's function won't
  accept before invoking it, so the call succeeds with the valid args. Tools that
  take `**kwargs` (explicit-schema tools) are passed through untouched, clean
  calls are unaffected, and a *misspelled required* arg still errors clearly
  (it's dropped, not invented). Signature lookups are cached. New
  `_filter_tool_input`.

## [2.35.0] — 2026-06-30

### Fixed

- **Background shells no longer outlive the session.** Processes started with
  `bash(run_in_background=True)` (dev servers, watchers, long builds) were tracked
  but never cleaned up — so they kept running after `mantis` exited, holding ports
  and leaking resources. `Agent.aclose()` (via `aclose_builtin_clients`) now
  terminates every still-running background shell, killing the whole detached
  process group so forked children die too. New `terminate_background_shells`
  (idempotent, best-effort).

## [2.34.0] — 2026-06-30

### Changed

- **Compaction now preserves the original task verbatim.** The first real user
  message (the original request) used to be rolled into the summary — so if the
  summarizer (often a weak/local model) captured it poorly, the agent could lose
  sight of its goal after a long session. It's now pinned OUTSIDE the summary,
  kept word-for-word between the context head and the summary, so the objective
  survives compaction regardless of summary quality (matching Claude Code). Only
  the turns AFTER it (up to the keep-window) are summarized.

## [2.33.0] — 2026-06-30

### Fixed

- **The compaction summarizer now retries transient failures too.** The turn loop
  got transient-error retry in 2.23, but the summarizer call that runs during
  auto-compaction went straight to the provider with no retry — so a single rate
  limit / 5xx / connection blip while compacting could kill the whole run (right
  when the context was full and compaction was most needed). It now retries
  transients with the same backoff (`max_retries`, honoring `Retry-After`); auth
  and other non-transient errors still fail fast.

## [2.32.0] — 2026-06-30

### Changed

- **Clearer permission prompts for file edits.** The Allow/Deny prompt used to
  show a raw `edit_file(path='...', old_string='...')` repr — hard to review at a
  glance. File-editing tools now get a path-focused change summary:
  `edit src/app.py:  "def old():" → "def new():"`, `write cfg.json (3 lines)`,
  `edit m.py (2 changes)`, `edit notebook n.ipynb (cell 4)`. Long strings are
  whitespace-collapsed and capped so the prompt stays a readable one-liner. Bash
  prompts (with their danger warnings) are unchanged.

## [2.31.0] — 2026-06-30

### Added

- **`PreCompact` hook is now dispatched.** It fires just before the agent
  summarizes (compacts) old history — a lossy step — so integrators can snapshot
  or persist the full transcript before it's compressed, or return `block=True` to
  skip the built-in compaction and handle it themselves. Defined-but-dead before;
  now wired into the run loop's compaction path. Follows `UserPromptSubmit` (2.28)
  in bringing the hook system past tool-only events into the run lifecycle.

## [2.30.0] — 2026-06-30

### Changed

- **`web_fetch` returns markdown, not flat text.** The default (non-Exa) extractor
  now preserves the structure a model can actually navigate — headings (`#`),
  links (`[text](url)`), and list items (`- `) — instead of collapsing everything
  into a wall of text, matching Claude's WebFetch. Still stdlib-only (no
  BeautifulSoup), still drops script/style/head and decodes entities; non-HTML
  bodies are returned verbatim as before. New `_html_to_markdown` (the old
  `_html_to_text` name remains as an alias).

## [2.29.0] — 2026-06-30

### Added

- **Budget wrap-up.** A run approaching a configured budget (USD / tokens / turns)
  now gets the same coherent ending as a turn-limited one (2.25): once it's within
  ~75% of the cap it's nudged (once) to stop starting new work and summarize what
  it did, what's left, and the next step — BEFORE the hard cap raises
  `BudgetExceededError` mid-task. The wrap-up reminder wording is now
  limit-aware ("turn limit" vs "budget limit"). Runs with no budget configured are
  unaffected.

## [2.28.0] — 2026-06-30

### Added

- **`UserPromptSubmit` hook is now dispatched.** The event was defined but never
  fired. It now runs once as each user turn begins, before any model call, and a
  hook can either **inject extra context** (its `note`, wrapped as a
  system-reminder — for dynamic per-turn context) or **block the prompt entirely**
  (`block=True` — a guardrail). Hook errors are swallowed (never crash the run),
  and with no hook configured it's a no-op.
- The hook dispatcher now **propagates notes from non-blocking hooks** (previously
  a `note` was only returned on a block), which is what makes UserPromptSubmit
  context-injection work; other events are unaffected.

## [2.27.1] — 2026-06-30

### Fixed

- **Read-before-write guard no longer blocks writing an empty file.** After
  creating a file another way — `bash("touch config.json")`, `> file`, an empty
  scaffold — then calling `write_file` on it, the guard (2.3) wrongly demanded a
  read first, even though a 0-byte file has no unseen content to clobber. Empty
  files now write freely; non-empty unread files are still protected.

## [2.27.0] — 2026-06-30

### Added

- **Live cost in the footer.** The pinned-input footer's usage indicator now
  appends the running session cost — `12k/32k 38% · $0.03` — so API users see
  spend accrue in real time, not only when they open `/context`. The cost tail is
  shown only when non-zero, so local/free models keep the clean `12k/32k 38%`
  without a `$0.00` distraction. New pure `tui.format_ctx_status` (token fill
  colours by threshold: grey → yellow ≥75% → red ≥90%).

## [2.26.0] — 2026-06-30

### Added

- **Session cost readout in `/context`.** The agent tracked spend internally but
  never showed it. `/context` now reports the cumulative USD cost of the session,
  summed per turn from each turn's usage against the model's pricing (each API
  call re-bills the full prompt, so cost accumulates by turn, not token totals).
  Local / self-hosted models correctly show `$0.00 (local / no API cost)`; unknown
  models are skipped. New pure `budget.estimate_cost` and `tui.format_cost`.

## [2.25.0] — 2026-06-30

### Added

- **Final-turn wrap-up.** When a run reaches its turn limit (`max_steps`), the
  agent used to just stop — often on a dangling tool result with no answer,
  leaving the user hanging mid-task. Now a one-shot reminder is injected on the
  last allowed step telling the model to stop starting new work and instead give
  a concise summary of what it did, what's left, and the next step — so a
  turn-limited run ends coherently. Only fires when actually approaching the limit
  (normal runs that finish early are unaffected); skipped for degenerate
  single-step runs. New `_final_turn_reminder`.

## [2.24.0] — 2026-06-30

### Changed

- **Rate-limit retries honor the server's `Retry-After`.** The transient-retry
  backoff (2.23) now waits the exact time a 429 response asks for via its
  `Retry-After` header (already parsed into `RateLimitError.retry_after_s`),
  instead of guessing with exponential backoff — so a throttled retry actually
  succeeds instead of hitting the limit again too soon. Capped at 60s so a hostile
  or huge value can't hang the agent; falls back to exponential backoff when no
  header is present. New `_retry_delay` helper.

## [2.23.0] — 2026-06-30

### Added

- **Transient-error retry with backoff.** A model call that fails BEFORE any
  output with a transient error — rate limit (429), 5xx / overloaded (500/502/
  503/504/529), or a transport blip (connection reset, read timeout) — is now
  retried up to `max_retries` times (default 2) with exponential backoff
  (0.5s → 1s → …, capped) instead of killing the turn on a single throttle. Auth
  failures and other 4xx are never retried (they won't self-heal); a failure
  after streaming has begun still propagates (can't retry partial output); model
  fallback still kicks in after retries are exhausted. New `Agent.max_retries`
  and `_is_transient`.

## [2.22.0] — 2026-06-30

### Added

- **`mantis run -` reads the prompt from stdin.** Pipe a file or generated spec
  straight into the agent — `cat feature.md | mantis run --tools --yes -` — instead
  of cramming it into a shell argument. When the prompt is `-` it's read from
  stdin (stripped); an empty result errors clearly. Rounds out the automation
  surface (`--tools`, `--yes`, `--json`, stdin).

## [2.21.0] — 2026-06-30

### Added

- **`mantis run --json` (`--output-format json`)** — structured result output for
  scripting/CI. Instead of streaming the reply as text, `run` prints one JSON
  object with `result` (the final answer), `is_error`, `num_turns`,
  `total_cost_usd`, `usage` (input/output tokens), `session_id`, and more —
  matching Claude's `-p --output-format json` shape so a script can parse the
  outcome. Exit code reflects `is_error`. Completes the automation trio with
  `--tools` (2.19) and `--yes` (2.20).

## [2.20.0] — 2026-06-30

### Added

- **`mantis run --dangerously-skip-permissions` (alias `--yes`)** — full autonomy
  for trusted automation. `--tools` in a headless run refuses dangerous shell
  commands (there's no human to approve them), which blocked real CI use. This
  flag sets `permission_mode=bypass` so every tool runs without asking, including
  dangerous shell. Off by default; the safe headless behavior (auto-run
  non-dangerous, refuse dangerous) is unchanged unless you opt in.

## [2.19.0] — 2026-06-30

### Added

- **`mantis run --tools` — scriptable one-shot agent.** The one-shot `run` (and
  `chat`) command was chat-only: `mantis run "fix foo.py"` couldn't read or edit
  anything. The new `--tools` flag gives it the full coding kit (read/write/edit/
  bash/grep/glob/ls/lsp/web), so a single headless command can actually do the
  work — `mantis run --tools --model … "run the tests and summarize failures"` —
  for CI/automation (Claude's `-p` use case). Non-dangerous tools run without a
  prompt in this headless mode; dangerous shell commands are still refused.

## [2.18.0] — 2026-06-30

### Changed

- **`glob` skips dependency/VCS/build junk by default.** A broad `**/*.py` (or
  any recursive glob) used to return every match inside `.venv`, `node_modules`,
  `.git`, `__pycache__`, `dist`, `target`, etc. — drowning the real files and
  blowing the 200-match cap on vendored noise. It now filters those directories,
  matching ripgrep's gitignore-aware behavior (grep was already clean). An
  explicit glob INTO such a dir (`node_modules/**/*.js`) or a `path` inside one is
  still honored.

## [2.17.0] — 2026-06-30

### Added

- **`@`-mentions now support directories.** Mentioning `@src/` (or `@src`) injects
  a listing of that directory's contents (subdirectories marked with `/`) so the
  agent sees the structure immediately — the counterpart to file mentions
  injecting file contents (2.11). The mention matcher no longer requires a file
  extension, so directory and extension-less paths resolve too; `@words` that
  aren't real paths (`@teammate`, emails) are still ignored.

## [2.16.0] — 2026-06-30

### Added

- **`bash` now has a persistent working directory.** Each foreground command
  starts where the previous one left off, so `cd sub` followed by a later `ls`
  (in a separate `bash` call) behaves like a real shell instead of resetting to
  the launch directory every time — the Claude Code behavior, and a fix for a
  constant papercut on multi-step shell work. Implemented by carrying the final
  `$PWD` between calls via a marker that's stripped from output; exit codes are
  preserved, a vanished tracked directory falls back gracefully, and background
  commands inherit the tracked cwd too.

## [2.15.1] — 2026-06-30

### Fixed

- **`/resume`, `/branch`, `/rewind` now work in the default (full-screen) TUI.**
  They were implemented and advertised in the slash menu, but the full-screen
  dispatcher never wired them — so typing `/resume` fell through and was sent to
  the model as the literal text "/resume" instead of resuming a session. Now they
  run their `MantisTUI` handlers inside `in_terminal` so the output scrolls above
  the pinned prompt like every other command.

## [2.15.0] — 2026-06-30

### Changed

- **"Allow for session" now actually sticks for edits.** It was keyed on the
  exact tool input, so every `edit_file`/`write_file` — which always has a
  different old_string/new_string/content — re-prompted anyway, making the option
  useless for the highest-friction case. Edit/write/notebook tools are now keyed
  by the FILE PATH: approve editing `foo.py` once and further edits to `foo.py`
  this session don't re-prompt (`bar.py` still asks). `bash` and other tools stay
  scoped to the exact call, as before.

## [2.14.0] — 2026-06-30

### Changed

- **Malformed-history self-healing is now library-wide, not just the TUI.**
  `run_iter` closes any unanswered `tool_use` at the very start of a run, so a
  history left dangling by ANY path — a cancelled `Agent.run()`, a session saved
  mid-tool then resumed, or a hand-built message list — produces a well-formed
  first request instead of a provider error. `close_open_tool_calls` is now
  position-aware: it inserts the synthetic `tool_result` immediately after the
  assistant that opened it (correctly slotting BETWEEN the tool_use and a
  following user message), and augments a partially-answered result message.
  Idempotent.

## [2.13.0] — 2026-06-30

### Changed

- **Interrupting a turn now keeps the work.** Pressing Esc/Ctrl-C mid-reply used
  to discard the ENTIRE turn — your message and everything the agent had already
  done (files read, tools run) vanished. Now the completed work is kept; only the
  tool calls left unanswered by the interrupt are closed with a synthetic
  `[interrupted by user]` result, so the history stays well-formed and you can
  continue or redirect from where it stopped (the Claude Code behavior). New
  `agent.close_open_tool_calls`.

## [2.12.0] — 2026-06-30

### Added

- **`/learn` command** — memory consolidation. Have the agent review the current
  session and save the DURABLE facts worth keeping (your preferences and
  conventions, project gotchas, where things live, decisions + rationale) to
  persistent memory via the `remember` tool — the manual, on-demand form of
  auto-memory. `/learn` reviews everything; `/learn <focus>` steers it. Prompt is
  guarded against saving transient task state or duplicating existing memories.
  Recalled automatically in future sessions.

## [2.11.0] — 2026-06-30

### Added

- **`@`-file-mentions now inject file contents.** Previously `@`-mentions only
  autocompleted the path; the agent saw the literal `@foo.py` and had to do a
  separate `read_file` (or miss it). Now when you send a message mentioning
  `@path` files that exist, their current contents are injected inline (as an
  isMeta system-reminder, so your visible message stays clean) — the model has
  them immediately, no extra round-trip. Files too large to inline get a note
  pointing at `read_file`; non-file `@words` are ignored; duplicates deduped. New
  `tui.resolve_file_mentions` / `render_mention_block`.

## [2.10.1] — 2026-06-30

### Fixed

- **Friendly labels for recently-added tools.** `task`, `lsp`, `notebook_edit`,
  `remember`, `load_skill`, `ask_user_question`, `exit_plan_mode`, and
  `bash_output` were rendering in the terminal as a bare tool name with no target.
  They now show a human verb + target — e.g. `⚒ Delegate find the auth bug`,
  `⚒ Look up render_diff`, `⚒ Remember cache TTL` — matching the built-in tools.

## [2.10.0] — 2026-06-30

### Added

- **`task` tool — subagent delegation** in the terminal (Claude Code's Task
  primitive). The agent can now hand a focused, multi-step investigation to a
  fresh read-only subagent that runs to completion and returns just its findings —
  keeping the main context clean (no dozens of intermediate file dumps). The
  subagent shares the parent's model/provider but gets only a read-only kit
  (read_file, grep, glob, ls, lsp, web) — it cannot edit, run shell, recurse into
  another `task`, or prompt the user, so delegation is safe and unsupervised. Runs
  concurrently for parallel exploration. New `subagent.make_task_tool` (the
  underlying `SubAgentTool`/`as_subagent_tool` machinery already existed; this
  wires a general-purpose read-only variant into the `mantis` agent).

## [2.9.0] — 2026-06-30

### Added

- **`/init` command** (parity roadmap T1.2, completing it). Bootstraps a project's
  `MANTIS.md` — `/init` expands into a canned prompt that has the agent explore the
  codebase (ls/glob/grep/read) and write a tight `MANTIS.md` with the build/lint/
  test/run commands, high-level architecture, key conventions, and gotchas. That
  file then auto-loads into context every future session (the load-bearing half,
  already shipped). Improves an existing `MANTIS.md` rather than clobbering it. New
  `tui.INIT_PROMPT` / `expand_slash_prompt`.

## [2.8.0] — 2026-06-30

### Added

- **Path-scoped conditional rules.** A `.mantis/rules/*.md` file may now declare
  `globs:` (or `paths:`) in frontmatter, and is injected into context ONLY when a
  matching file is active in the conversation — an `@`-mention or a file the agent
  just read/edited. So a SQL style rule rides only SQL work, a Go rule only Go
  work, keeping project instructions lean instead of spending context on rules
  that don't apply. Rules with no globs stay unconditional (loaded always, as
  before). Deduped per session. New `mantis_agent.rules` module. Mirrors Claude
  Code's path-specific instructions.

## [2.7.0] — 2026-06-30

### Added

- **`/compact` command.** Compress the conversation on demand instead of waiting
  for auto-compaction — frees context before a big next step. Takes an optional
  focus hint (`/compact the auth refactor`) that steers what the summary
  preserves. Keeps the last few turns verbatim, summarizes the rest with the
  current model, and reports the before→after message count. Short conversations
  are a no-op. New `compact.run_manual_compaction` helper.

## [2.6.0] — 2026-06-30

### Added

- **Inline image rendering** in the terminal (iTerm2 / WezTerm). When the agent
  reads an image with multimodal `read_file`, the `mantis` terminal now *shows*
  it inline (via the iTerm2 `OSC 1337;File=` protocol, with tmux passthrough)
  plus a `[media, size]` note — the visual counterpart to the model being able to
  see it (1.28). Terminals without support just get the note, so nothing breaks.
  New `mantis_agent.inline_image` module (`iterm2_image_escape`,
  `supports_inline_images`, `image_block_to_inline`).

## [2.5.0] — 2026-06-30

### Changed

- **Structured compaction summaries** (parity roadmap T1.7). When a long coding
  session auto-compacts, the summarizer now produces Claude's multi-section
  format — Primary Request · Key Technical Concepts · Files and Code Sections
  (with exact paths + snippets) · Errors and Fixes · Problem Solving · Pending
  Tasks · Current Work · Next Step — instead of 200–400 words of prose. This
  preserves file paths, symbol names, error messages, and the precise next action
  across a resumed turn, so the agent doesn't redo or break work after a
  compaction. The transcript fed to the summarizer already carries tool inputs
  (file paths) and errors as raw material.

## [2.4.1] — 2026-06-30

### Fixed

- **Diff word-highlighting no longer lights up every line on a re-indent.** The
  colored diff paired the i-th removed line with the i-th added line by position,
  so wrapping a block in `try:`/`except` (or any re-indent) shifted every line and
  word-diffed unrelated pairs — nearly every character showed as "changed." Now
  removed↔added lines are aligned by their stripped content (SequenceMatcher):
  lines that only moved/re-indented match as unchanged and get no char emphasis;
  only genuinely modified lines are word-diffed against their real counterpart. A
  one-char edit still highlights exactly one char. New `_compute_word_emphasis`.

## [2.4.0] — 2026-06-30

### Added

- **Refusal recovery.** When the model ends a turn with a bare, no-tool-call
  refusal ("I'm sorry, but I can't complete that request") — the spurious
  over-refusals small/aligned models emit on perfectly legitimate local work
  (listing processes/ports, reading your own files, running builds) — the agent
  now nudges it ONCE with a reminder that it's operating in the user's own
  authorized environment and re-prompts, instead of dead-ending the task. Capped
  at one retry per run, so a genuinely harmful request is simply refused again
  and stops. New `Agent.recover_refusals` flag (default True; set False to opt
  out). New `_looks_like_refusal` detector (length-capped + precise, so a long
  answer or an "I can't find that file" isn't misread).

## [2.3.0] — 2026-06-30

### Added

- **Read-before-write guard** (Claude Code's readFileState). `write_file` now
  refuses to clobber an existing file the tools haven't *seen* this session, or
  one that changed on disk since it was read — so unseen or newer content is
  never silently destroyed by a blind overwrite. The tools (`read_file`,
  `write_file`, `edit_file`, `multi_edit`) track each file's mtime; new files and
  read-then-write / write-then-overwrite flows pass freely, and the error tells
  the model to read first (recoverable in one step).

## [2.2.0] — 2026-06-30

### Fixed

- **`web_fetch` no longer depends on BeautifulSoup.** Its default (non-Exa) path
  called `bs4` — not a dependency — so a plain `web_fetch(url)` returned raw HTML
  (tags, `<script>`, CSS) as the model's "readable text". Rewritten with a
  stdlib HTML→text extractor (drops script/style/head, block-closes → newlines,
  strips tags, unescapes entities, collapses whitespace). Non-HTML bodies (JSON,
  plain text, markdown, source) are now returned verbatim by content-type instead
  of being tag-stripped. Same dependency-free treatment `web_search` got in 1.25.

## [2.1.0] — 2026-06-30

### Added

- **`lsp` is now multi-language.** Goto-definition and the `symbols` outline
  work across JavaScript, TypeScript, Go, Rust, Java, Ruby, and C/C++ (in
  addition to Python's precise ast path) via targeted declaration-syntax regex —
  so a function call or a control-flow brace is never mistaken for a definition
  the way plain grep would. TS interfaces/types/enums, Go/Rust types, Ruby
  modules, etc. are recognized with their kind. References stay Python-only
  (ast-precise).

## [2.0.0] — 2026-06-30

### Changed (BREAKING)

- **`ClaudeAgentOptions` is renamed to `MantisAgentOptions`.** The options class
  is now natively mantis-branded across the whole codebase, docs, and examples.
  `ClaudeAgentOptions` is **removed** (no alias) — update imports to
  `from mantis_agent import MantisAgentOptions`. All other drop-in symbols
  (`query`, `tool`, `AssistantMessage`, …) are unchanged.

### Added

- **Session-resume context freshness.** `Session.load` (and `resume_session`)
  now drops the synthetic `isMeta` context/reminder messages (env + git + memory
  head, recall, todo) by default so a resumed session RE-DERIVES current context
  instead of replaying a stale snapshot from when it was created. New
  `strip_context_messages` helper; pass `fresh_context=False` to keep the frozen
  head.

## [1.36.0] — 2026-06-30

### Added

- **Thinking-block rendering in the terminal** (parity roadmap T2 polish).
  Reasoning models (DeepSeek-R1, QwQ, API extended-thinking) emit a thinking
  block; previously the terminal dropped it entirely. Now it's shown dimmed above
  the answer under a `✻ thinking` header, capped at 12 lines (with a `… (N more
  lines)` note) so a long chain-of-thought doesn't bury the reply. New pure
  `_thinking_lines` helper.

## [1.35.0] — 2026-06-30

### Added

- **`lsp` gained a `symbols` operation** — a file/project outline: classes with
  their methods (indented) plus top-level functions, each with a line number.
  `lsp(operation="symbols", path=...)`, with an optional `symbol` substring to
  filter a large tree. The fast "show me the structure of this file / where's
  everything" view, ast-based (async methods and nested scopes handled). New
  `find_symbols` helper.

## [1.34.0] — 2026-06-30

### Added

- **`lsp` tool — semantic code navigation** (parity roadmap T1.8). Goto-definition
  and find-references for Python, done the mantis way: via the stdlib `ast` module
  instead of an external language server, so it has zero dependencies and works
  out of the box. Unlike grep it distinguishes a *definition* (function / class /
  method / module-level assignment) from a *mention*, resolves attribute accesses
  (`x.method`), and skips names in comments/strings. `lsp(operation="definition"
  | "references", symbol=..., path=...)`. Wired into the `mantis` tool belt. New
  `find_definitions` / `find_references` helpers.

## [1.33.0] — 2026-06-30

### Added

- **`/memory`** (parity roadmap T2). Open your instruction-memory files in
  `$EDITOR` to curate what the agent knows: `/memory` (project `MANTIS.md`),
  `/memory agents` (`AGENTS.md`), `/memory user` (user-level `MANTIS.md`).
  Creates the file with a template if missing and rebuilds the context head so
  edits apply on the next turn. New pure `resolve_memory_target` helper.

## [1.32.0] — 2026-06-30

### Added

- **`notebook_edit` tool** (parity roadmap T2). Edit a Jupyter notebook cell:
  `replace` (default), `insert` (a new code/markdown cell before an index), or
  `delete`, addressed by 0-based `cell_number`. Replacing a code cell clears its
  now-stale outputs and execution count; writes nbformat-style JSON back. Pairs
  with notebook reading (1.31.0) to complete notebook support.

## [1.31.0] — 2026-06-30

### Added

- **Notebook (`.ipynb`) reading** (parity roadmap T2). `read_file` on a Jupyter
  notebook now renders readable cells — markdown, code, and text outputs (stream,
  execute_result, and errors as `EName: value`; image outputs noted) — instead of
  dumping raw JSON. Falls back to plain text if the file isn't valid notebook
  JSON. New `_render_notebook` helper.

## [1.30.0] — 2026-06-30

### Added

- **`/diff`** (parity roadmap T2). Review every change the agent made this
  session in one view — runs `git diff HEAD` and renders each file with the same
  full-width syntax-highlighted, word-level-highlighted diff renderer used inline,
  plus a list of new (untracked) files. New pure `split_git_diff()` parser (splits
  `git diff` output into per-file hunks, stripping git headers). Notes when the
  directory isn't a git repo.

## [1.29.0] — 2026-06-30

### Added

- **Microcompaction** (parity roadmap T2). A cheap first line of context defense
  that runs before full compaction: once the window passes 60%, the bodies of
  tool results older than the last 8 (only those over ~800 chars) are cleared to
  `[old tool result cleared to save context]` — no summarizer call. It keeps the
  blocks and their `tool_use_id` intact (pairing untouched) and is idempotent, so
  a long chain of `read`/`grep`/`bash` dumps you've already acted on stops
  bloating the window, deferring the expensive summarizing compaction (which
  still fires at 85% as the fallback). New `SimpleCompactor.should_microcompact`
  / `microcompact`.

## [1.28.0] — 2026-06-30

### Added

- **Multimodal `read_file`** (parity roadmap T2). Reading an image
  (png/jpg/gif/webp/bmp) now returns it as an image the model can actually see —
  on vision-capable backends — instead of dumping mojibake; PDFs and other
  binaries get a helpful note. Under the hood, the tool executor now passes a
  tool that returns an `ImageBlock`/`TextBlock` (or a list of them) straight
  through as the tool-result content instead of stringifying it, so any tool can
  return rich content. (Anthropic serializes images in tool results; other
  backends vary by model.)

## [1.27.0] — 2026-06-30

### Added

- **MCP resources + prompts** (parity roadmap T2). The MCP client gained
  `list_resources()` / `read_resource(uri)` (the readable blobs a server exposes)
  and `list_prompts()` / `get_prompt(name, arguments)` (reusable named prompt
  templates, rendered to `[role] text`). Both list calls are paginated; binary
  resource parts are noted rather than dumped as base64. New `MCPResource` /
  `MCPPrompt` types. Previously the client only spoke `tools/list` + `tools/call`.

## [1.26.0] — 2026-06-30

### Added

- **Model fallback** (parity roadmap T2). `Agent(fallback_model=...)` — if the
  primary model call fails *before producing any output* (overload,
  model-not-found, connection drop), the turn is retried once on the fallback
  model (same provider/backend), so a transient outage doesn't kill the run. A
  failure *after* tokens have streamed is re-raised (no unsafe partial-output
  retry); the fallback is one-shot per run (no retry loop). In the terminal, set
  `MANTIS_AGENT_FALLBACK_MODEL`.

## [1.25.0] — 2026-06-30

### Fixed

- **Web search works out of the box again.** The keyless DuckDuckGo fallback
  required `beautifulsoup4` (not a dependency), so `web_search` returned an error
  unless you set an API key. It's now dependency-free (stdlib HTML parsing),
  unwraps DuckDuckGo's `/l/?uddg=` redirector to real URLs, and falls back to the
  `lite` endpoint when the html one is empty. Set `EXA_API_KEY` / `BRAVE_API_KEY`
  / `TAVILY_API_KEY` for higher-quality results as before.

### Added

- **Todo re-injection** (parity roadmap T2). When an `Agent` is given a live
  `todos` list (the one `todo_write` mutates), the current state is re-injected
  as a `<system-reminder>` at the top of each turn — refreshed, not accumulated —
  so the model keeps its plan in view over a long task. Wired in the terminal.

### Changed

- The terminal input prompt is now `❯` (was `›`).

## [1.24.0] — 2026-06-30

### Added

- **`/export` and `/copy`** (parity roadmap T2). `/export [path]` saves the
  conversation to a shareable markdown file (default `mantis-conversation.md`);
  `/copy` copies the last assistant reply to the system clipboard (pbcopy /
  wl-copy / xclip / clip). New pure `render_transcript()` helper and
  `clipboard.copy_to_clipboard()`.

## [1.23.0] — 2026-06-30

### Added

- **Hooks: multiple hooks per event + tool-name matchers** (parity roadmap T2).
  A hook field now accepts a list of callables and/or `HookMatcher(hook=fn,
  matcher="Bash")` — the dispatcher runs every *matching* hook in order (fnmatch
  against the tool name; non-tool events always run), chains input mutations, and
  short-circuits on the first block. Backward compatible: a bare callable still
  works. The `claude_compat` SDK-shaped `HookMatcher(matcher=..., hooks=[...])`
  now works end to end with real matcher semantics (previously only the first
  callable per event was honored).

## [1.22.0] — 2026-06-30

### Added

- **Word-level diff highlighting** (parity roadmap T2). On a modified line the
  diff renderer now brightens just the characters that actually changed
  (Claude's `diffAddedWord` / `diffRemovedWord` green/red), so a one-character
  edit lights up one character instead of the whole line reading as changed.
  Lines are paired within each change block and char-diffed; a wholesale rewrite
  skips the emphasis (the row colour already tells that story). New pure
  `_word_diff_spans` helper.

## [1.21.0] — 2026-06-30

### Added

- **Skills are now live in the product** (parity roadmap T1.3). The SKILL.md
  progressive-disclosure system was built but dead — now it works end to end.
  Drop a skill at `~/.mantis-agent/skills/<slug>/SKILL.md` (or
  `./.mantis/skills/...` per project) with `name`/`description` frontmatter and a
  markdown body. Each session injects only the **catalog** (name + one-line
  description) into context; when a task matches, the agent calls the new
  **`load_skill`** tool to pull the full instructions on demand — so N skills
  cost N one-liners, not N documents. `skills.discover_skills` /
  `render_skill_catalog` / `load_skill_body`.

## [1.20.0] — 2026-06-30

### Added

- **`bash(run_in_background=True)` + the `bash_output` tool** (parity roadmap
  T1.4 complete). Long-running commands — a dev server, a file watcher, a slow
  build — can now run detached: bash returns a background id immediately instead
  of blocking or timing out, streams stdout+stderr to a temp log, and
  `bash_output(bash_id=...)` reads the accumulated output plus whether it's still
  running or has exited (with its code). Processes start in their own session so
  they survive independently.

## [1.19.0] — 2026-06-30

### Added

- **Vim editing mode + external editor in the terminal** (parity roadmap T2).
  Toggle vim keybindings on the input line with **`/vim`** (or start with
  `MANTIS_VIM=1`). Press **Ctrl-X Ctrl-E** to compose a long or multi-line prompt
  in `$EDITOR` — the classic shell ergonomic. Both are near-free wins for anyone
  who lives in the terminal.

## [1.18.0] — 2026-06-30

### Added

- **Prompt caching for the Anthropic backend** (parity roadmap T0.4 — Tier 0
  complete). The passthrough now sets `cache_control: ephemeral` breakpoints on
  the system prompt and the last message, so Anthropic reads the stable prefix
  (system + conversation-so-far) from cache instead of re-billing it every turn
  — a large cost/latency win on multi-turn sessions. On by default; the provider
  already tallied `cache_read`/`cache_creation` tokens, now it actually requests
  the cache. Set `cache_prompts=False` on the provider to opt out.

## [1.17.0] — 2026-06-30

### Added

- **Plan-mode approval handoff** (parity roadmap T1.6). Plan mode already gated
  mutations read-only; now there's the missing present-plan → approve → execute
  flow. In plan mode the agent researches, then calls the new **`exit_plan_mode`**
  tool with its plan; the terminal renders it and asks you to approve via the
  same picker as AskUserQuestion. On approval plan mode is lifted so the agent
  can start editing; otherwise it stays read-only and revises. The plan-mode
  denial message now points the model at `exit_plan_mode`.

## [1.16.0] — 2026-06-30

### Added

- **Tool-result truncation backstop** (parity roadmap T0.3). A single huge tool
  result — `cat`-ing a big file, a noisy build log, an MCP tool dumping JSON —
  can no longer blow the whole context window in one turn. The executor caps each
  result (tool-aware: reads/shell/web-fetch get more room than a generic tool),
  keeping head + tail and eliding the middle with a note that says how much was
  dropped and to narrow the query. Default 30k chars; override with
  `MANTIS_AGENT_MAX_TOOL_RESULT`.

## [1.15.0] — 2026-06-30

### Added

- **`grep` gained real search modes** (parity roadmap T1.4). New args:
  `output_mode` (`content` / `files_with_matches` / `count`), `context_lines`
  (show lines around each match), `file_type` (restrict to a language like `py`,
  `rust`, `js` — maps to `rg --type` and to extensions in the Python fallback),
  `head_limit` (cap output; replaces the hardcoded 50-match limit), and
  `multiline` (patterns spanning line boundaries). Both the ripgrep path and the
  dependency-free Python fallback honor every mode. (`glob` already sorted results
  by mtime, newest-first.)

## [1.14.0] — 2026-06-30

### Added

- **Context-window awareness in the terminal** (parity roadmap T1.5). The footer
  now shows a live fill indicator (`12k/32k 38%`, coloured green→yellow→red as it
  fills) so you can see how full the window is at a glance. A new **`/context`**
  command renders a bar plus an estimated split across system prompt, memory/env
  context head, and conversation. New `context_breakdown()` helper.

## [1.13.0] — 2026-06-30

### Added

- **`@`-file-mentions in the terminal** (parity roadmap T1.1). Type `@` anywhere
  in the prompt to fuzzy-find a file under the cwd and drop its path in — no more
  pasting paths by hand. The completer ranks basename-prefix matches first, skips
  VCS/build dirs and dotfiles, and is bounded so it stays snappy on big repos.
  Navigate with ↑/↓, accept with Tab/Enter.

## [1.12.0] — 2026-06-30

### Added

- **`ask_user_question` tool — the agent can ask *you* structured multiple-choice
  questions mid-task** (Claude Code's AskUserQuestion). It proposes 1-4 questions,
  each with 2-4 labelled options (label + description); you pick with number keys
  or arrows, toggle multiple with space when `multiSelect` is set, or choose
  "Other" to type free text. Rendered as an in-pane picker in the full-screen
  terminal (Future-bridged like the permission prompt), with a numbered fallback
  in the classic REPL and a graceful no-op when headless. The chosen answers come
  back as the tool result, so the agent acts on real preferences instead of
  guessing. Wired into the `mantis` tool belt.

## [1.11.0] — 2026-06-30

### Added

- **`mantis --dangerously-skip-permissions` (alias `mantis --godmode`)** starts
  the terminal in engine-level bypass: every tool runs with no confirmation
  prompt — including dangerous shell commands (`rm -rf`, `curl|sh`, `sudo`),
  which are otherwise always gated. Sets the permission context's `mode=bypass`
  so the whole permission pipeline short-circuits to Allow, and prints a red
  warning banner on start. For trusted, unattended runs where you accept all
  risk.

## [1.10.0] — 2026-06-30

### Added

- **Memory recall is now wired into the run loop — the agent surfaces the *right*
  memories.** Before each turn it scores the `~/.mantis-agent/memory/` topic
  files against the latest user message (keyword-overlap, fully offline) and
  injects the top matches as an isMeta `<system-reminder>`, deduped across the
  session, with a staleness caveat for notes older than a day. Previously the
  whole `MEMORY.md` index was dumped regardless of relevance; the rich
  `memory_recall.py` engine was dead code. New `Agent.include_recall` (default
  True; disabled by `MANTIS_AGENT_NO_CONTEXT=1`).
- **A `remember` tool** gives the agent a write path into persistent memory — it
  can save durable facts (project conventions, preferences, gotchas) that recall
  then surfaces automatically in future sessions. Wired into the `mantis`
  terminal's tool belt. Read + write now form a closed loop.

## [1.9.1] — 2026-06-30

### Changed

- **The `mantis` system prompt is rebuilt to Claude-Code quality.** It keeps
  mantis's local-model tuning (act immediately, call tools instead of describing,
  never refuse a normal engineering task) and adds the engineering discipline a
  real coding agent needs: read before you change, make the smallest diff (no
  speculative abstractions / over-engineering / needless comments), prefer
  editing over creating files, diagnose failures before switching tactics, and
  **verify before reporting done + report outcomes faithfully**. New "Acting with
  care" section (confirm destructive / shared-state actions; approval once ≠
  approval always; no `--no-verify` shortcuts) and output conventions
  (`file_path:line_number`, lead with the answer, no emojis unless asked). The
  static environment line is dropped — the richer `<env>` context head (1.9.0)
  covers it.

## [1.9.0] — 2026-06-30

### Added

- **The agent is now oriented in your repo** (parity roadmap T0.5). Every
  session injects an `<env>` + git snapshot into the context head: working
  directory, platform, OS version, today's date, a shallow directory listing,
  and git branch / main branch / user / status / recent commits (Claude Code's
  format, incl. the "snapshot in time" disclaimer and 2k status truncation).
  Built once and memoized (`Agent._env_context`) so the prompt-cache prefix
  stays stable across turns; rides in the same isMeta head compaction preserves.
  New `include_env` field (default True); `MANTIS_AGENT_NO_CONTEXT=1` disables
  all context injection. New `system_reminder.build_env_context_block` /
  `build_git_context` / `render_environment_context`.
- **`AGENTS.md` is now auto-loaded** alongside `MANTIS.md` in the project-memory
  cwd-walk (same tier/precedence) — a project's existing AGENTS.md is picked up
  with no config.

## [1.8.1] — 2026-06-30

### Security

- **Dangerous shell commands can no longer skip the permission prompt.** A bash
  command flagged by the danger classifier (`rm -rf`, `curl|sh`, `sudo`, raw
  disk writes, …) now always requires live confirmation — a broad `allow` rule,
  `acceptEdits`, or the mode default can't auto-run it. With no interactive
  approver (library / headless), such a command is denied rather than run. Only
  an explicit `deny` rule or `bypass` mode overrides it.

## [1.8.0] — 2026-06-30

### Added

- **Interactive permission prompts — the terminal no longer runs bash/write/edit
  unconfirmed** (parity roadmap T0.2). `default` mode now *asks* before every
  mutating tool (Allow once / Allow for session / Deny), rendered as an in-pane
  prompt in the full-screen app (resolved by a keypress, no nested prompt).
  `accept edits on` auto-approves file edits but still asks for bash; `plan mode`
  still denies mutations; `bypass` and read-only tools never prompt. A bash
  danger classifier annotates the prompt (`rm -rf`, `curl|sh`, `sudo`, …).
  `settings.json` `permissions.allow/deny/ask` rules are now loaded and enforced.

### Changed

- `check_permission` resolves `Ask` through a new `PermissionContext.asker`
  callback with per-`(tool, input)` "allow for session" memory; `PermissionMode`
  gains `acceptEdits`. Library/headless callers without an asker keep the old
  non-blocking behavior, so nothing hangs. New `classify_bash_command`.

## [1.7.0] — 2026-06-30

### Added

- **Auto-compaction is now wired into the agent loop** (parity roadmap T0.1).
  When a conversation approaches the model's context window, `Agent` summarizes
  older turns at a safe boundary and continues — so long sessions no longer grow
  until the provider 413s. On by default (`auto_compact=True`); pass
  `auto_compact=False` or a custom `compactor=` to override. The summary is a
  plain `UserMessage` (serializes through providers/`query()`/sessions), the
  split is tool-pair-aware (never orphans a `tool_use`), the summarizer call is
  billed through the budget tracker, and a per-run cap guards against a
  non-converging summary. Covered by `tests/test_compaction.py`.

### Fixed

- `SimpleCompactor` now detects a leading system message by `role` rather than
  `isinstance`, so the SDK-shaped `SystemMessage` (from `claude_compat`) is
  correctly preserved outside the compaction boundary.

## [1.6.0] — 2026-06-30

### Added

- **`mantis setup` — a real first-run experience.** Detects your machine
  (RAM / Apple Silicon / NVIDIA VRAM) and recommends the best *coding* model
  that fits, from a curated coding-first catalog (Qwen2.5-Coder 0.5B→32B plus
  DeepSeek-R1 for code reasoning). Pick from the list, take the ★ recommendation,
  or `--auto`; it installs Ollama if missing, pulls the model, and sets it as
  your default so `mantis` opens straight into a working agent.
  `mantis setup --list` prints the catalog; `mantis setup --model <tag>` pulls
  a specific one. (The older `mantis-agent setup-local` still works.)

## [1.5.1] — 2026-06-30

### Docs

- **README polished end to end** — sharper intro hook (terminal + library, one
  install), the terminal section rewritten as natural prose, and the stale
  pre-1.0 "acceptance test"/test-count copy refreshed (831 tests, 3.11–3.13).

## [1.5.0] — 2026-06-30

### Changed

- **`pip install mantis-agent-sdk` now ships the terminal out of the box** — no
  `[cli]` extra needed. `prompt_toolkit` and `rich` moved into core
  dependencies (both lazy-imported, so the stdlib-only `mantis-agent`
  diagnostics CLI keeps its snappy cold start). `[cli]` is kept as a no-op for
  back-compat.

## [1.4.1] — 2026-06-30

### Changed

- **Diff colors now match Claude Code exactly** — bright green/red gutter
  markers + line numbers (`rgb(105,219,124)` / `rgb(255,168,180)`, Claude's
  `diffAdded`/`diffRemoved`) over the dark-blend row fill, with the dimmed
  variants as the fallback fg.

## [1.4.0] — 2026-06-30

### Changed

- **Syntax-highlighted diffs (sexier than the line-numbered blocks).** Diff rows
  now render the code with full syntax highlighting *on top of* the full-width
  green/red background — `def` keywords, identifiers, types, strings all
  colored inside the added/removed rows (language detected from the file
  extension). Plus a Claude-Code-style `<file>  +N -M` summary line.

## [1.3.3] — 2026-06-30

### Changed

- **Diffs now render like Claude Code** — full-width dark-green/dark-red
  background rows for additions/deletions (not just colored text), with a
  line-number gutter (additions show new-file numbers, deletions show
  old-file numbers) and dim context lines.
- Fixed remaining `Text` styles that used `ansi*` names (rendered white): the
  tool-result branch, error lines, and the todo checklist now use valid rich
  colors.

## [1.3.2] — 2026-06-30

### Fixed

- **Diffs now render in color.** `Text(style="ansigreen"/"ansired"/…)` silently
  produced white text (rich's `Text` doesn't accept the `ansi*` color names that
  its markup parser does) — so edit diffs showed `+`/`-` with no green/red.
  Converted all `Text` styles to valid rich names (`green`/`red`/`bright_black`).
  Also: `multi_edit` now returns a unified diff like `edit_file`/`write_file`, so
  multi-edit operations render colored diffs too.

## [1.3.1] — 2026-06-30

### Docs

- **README documents the `mantis` terminal** — install (`[cli]` extra), the
  full-screen agent TUI, edit diffs, tool calls, clipboard paste, slash
  commands, keys, and configuration env vars — alongside the existing library
  (API) docs.

## [1.3.0] — 2026-06-30

### Added

- **Clipboard paste (Ctrl+V) in the terminal** — paste a copied image, or a
  copied file path, straight into the prompt as an attachment. New
  `mantis_agent.clipboard` module (macOS/Linux/Windows) with image + file
  detection; wired into the TUI input.

This release also bundles all the interactive-terminal work from 1.1.x–1.2.x:
the `mantis` Claude-Code-style terminal — praying-mantis mascot, Markdown +
syntax-highlighted code, line-numbered edit diffs, friendly tool-call headers,
the animated thinking spinner, dark slash-command menu, and full-screen mode
(input pinned to the bottom, always visible while the agent works).

## [1.2.2] — 2026-06-30

### Fixed

- **Blank line between your message and the reply** in full-screen mode. Switched
  to a trailing-blank spacing model (each block emits its own separator; tool
  calls emit none so their result hugs) so the gap is reliable.
- **Ctrl+C now quits when idle** (and still interrupts a running reply).

## [1.2.1] — 2026-06-30

### Fixed

- **Consistent spacing in full-screen mode.** One blank line between blocks
  (user message, assistant text, tool call) with tool results hugging their
  call — fixing assistant text that was cramped right under a tool result and
  the doubled gap before the next prompt.

## [1.2.0] — 2026-06-30

### Added

- **Full-screen mode — the input is pinned to the bottom and always visible,
  even while the agent is working.** `mantis` now runs as a `prompt_toolkit`
  app whose bottom region (rule · input · rule · footer) stays fixed while the
  conversation scrolls above it (the Claude Code layout). The thinking spinner
  lives in the footer; Esc / Ctrl+C interrupts a running reply, Ctrl+D quits.
  All existing rich rendering (banner, markdown, diffs, tool calls) is reused.
  Set `MANTIS_CLASSIC=1` to force the classic scrolling REPL; full-screen also
  auto-falls-back to it if it can't start.

## [1.1.28] — 2026-06-30

### Changed

- **Input frame uses a solid rule** (`─`) again instead of the dashed `┄`.

## [1.1.27] — 2026-06-30

### Changed

- **Removed the "? for shortcuts" footer hint** (default mode shows no footer
  text), and dropped the toolbar's reverse/white background so the footer is
  plain text on the terminal background.

## [1.1.26] — 2026-06-30

### Fixed

- **Bottom rule hugs the input at launch.** The input is padded toward the
  bottom of the screen so its framing rules + footer hug it, instead of the
  toolbar floating to the screen floor with a gap. Safe with `erase_when_done`
  (the frame is wiped and `› message` echoed in place, so the first message
  scrolls naturally instead of being buried).

## [1.1.25] — 2026-06-30

### Fixed

- **Bottom rule now hugs the input, and the Enter flicker is gone.** Dropped
  `reserve_space_for_menu` from 8 to 0: it had inserted 8 blank rows between the
  input and the bottom rule/footer (rule floated far below the input), and that
  large reserved region repainted on submit (the ~1s flicker). Now both dashed
  rules sit directly above and below the input.

## [1.1.24] — 2026-06-30

### Changed

- **Input frame uses a dashed rule** (`┄`) instead of a solid line.

## [1.1.23] — 2026-06-30

### Fixed

- **Input rules now frame only the live input**, not every past message. The
  framed prompt (top rule + input + bottom rule + footer) is erased on submit
  (`erase_when_done`) and the submitted line is echoed as a clean `› message`,
  so scrollback has no stray rules.

## [1.1.22] — 2026-06-30

### Changed

- **Input is framed with horizontal rules** above and below it (the toolbar
  draws the lower rule), matching Claude Code's prompt framing.

## [1.1.21] — 2026-06-30

### Fixed

- **Removed the grey highlight box around inline `code`/filenames.** rich's
  default markdown code style uses a reverse/background that read like a
  stray text selection; inline code and code blocks now render as plain green
  text.

## [1.1.20] — 2026-06-30

### Added

- **Real diffs for edits.** `edit_file` and `write_file` now return a compact
  unified diff, and the TUI renders it as a line-numbered green/red diff block
  under the tool call (additions green, deletions red, context dim) — like
  Claude Code's edit view.

## [1.1.19] — 2026-06-30

### Fixed

- **Hotfix:** 1.1.18's wheel was built mid-edit and shipped `builtin_tools/fs.py`
  without its `import re`, so the package failed to import. Rebuilt with the
  import in place.

## [1.1.18] — 2026-06-30

### Fixed

- **Doubled spacing between messages.** Both the render and the pre-spinner
  step were emitting blank lines, so blocks were separated by two blank lines
  (and bullets could orphan above code). Now the single pre-spinner blank is
  the only separator and content lands on the spinner's cleared line — exactly
  one blank line between blocks, call+result still hugged.

## [1.1.17] — 2026-06-30

### Changed

- **Tool call and its result are hugged together** (no blank/spinner gap
  between `⚒ write foo.py` and its `└ …` result). Spacing is kept above the
  call and below the result group.

## [1.1.16] — 2026-06-30

### Changed

- **Tighter Markdown rendering.** Code blocks no longer carry rich's large
  vertical padding / grey box — replies are compact (one blank line around
  code instead of three).
- **Spinner spacing.** The thinking spinner now gets a blank line above it
  after tool results too (not just at turn start), so it isn't cramped.

## [1.1.15] — 2026-06-30

### Changed

- **Blank line between a reply and the next prompt** so turns don't jam together.
- **`/clear` now blanks the screen** (clears scrollback) and redraws the banner —
  a clean fresh start — instead of just printing "(history cleared)".

## [1.1.14] — 2026-06-30

### Changed

- **Tool calls render Claude-Code-style.** Instead of `⚒ read(path=...)` and a
  raw output dump, tool calls now show a friendly verb + target
  (`⚒ Read foo.py`, `⚒ Edit foo.py`, `⚒ Run date +%H:%M`, `⚒ Search "pat"`)
  with the result hanging off a `└` branch and overflow capped.

## [1.1.13] — 2026-06-30

### Fixed

- **Your messages no longer vanish after sending.** The launch bottom-padding
  pushed the prompt below a wall of blank lines, so the first message scrolled
  up into that emptiness and looked gone. Removed the padding: the banner sits
  at the top, the input right beneath it, and the conversation flows downward
  with every message visible.

## [1.1.12] — 2026-06-30

### Changed

- **Breathing room above tool calls and the loading spinner.** Tool-call lines
  (`⚒ grep(...)`) and the thinking spinner now get a blank line above them
  instead of being cramped against the previous output.

## [1.1.11] — 2026-06-30

### Fixed

- **Mascot no longer clipped, and the input is back at the bottom.** The banner
  height is now *measured* at the real terminal width (handling wrapping on
  narrow windows) instead of estimated, and `mantis` clears the screen +
  scrollback before drawing — so the banner sits fully at the top and the
  input is padded to the bottom row at any size. Fixes the case where a narrow
  window scrolled the mascot's head/antennae off the top.

## [1.1.10] — 2026-06-30

### Changed

- **Slash-command menu restyled** to a dark panel with a description column
  and a bright-green selected row — replacing prompt_toolkit's default
  white-background menu. Each command (`/help`, `/model`, `/clear`, `/cwd`,
  `/exit`, `/quit`) now shows a one-line description.

## [1.1.9] — 2026-06-30

### Fixed

- **Banner no longer scrolls off the top into a huge empty void.** The old
  bottom-padding overflowed on tall/narrow windows (wrapped banner text made
  the line math under-count), pushing the mascot off-screen and stranding the
  prompt at the bottom. `mantis` now clears to a fresh screen, prints the
  banner at the top, and puts the input right beneath it — robust at any
  terminal size.

## [1.1.8] — 2026-06-30

### Changed

- **Thinking spinner is now mantis green** (was coral) to match the mascot.

## [1.1.7] — 2026-06-30

### Changed

- **Assistant replies are rendered as Markdown** (code blocks with syntax
  highlighting, bold/italics, lists, tables) instead of raw text — using an
  ANSI colour theme so it looks right in Terminal.app. No more literal
  ```` ``` ```` fences in the output.

## [1.1.6] — 2026-06-30

### Added

- **Animated "thinking" status line** while the model works: a pulsing star,
  a random whimsical gerund, and a live elapsed timer — e.g.
  `✻ Undulating… (34s)` — rendered on a transient row that clears itself the
  instant output arrives. The input has no border/separator lines around it.

## [1.1.5] — 2026-06-30

### Changed

- **Input is pinned to the bottom of the terminal on launch** (Claude-Code
  style): the banner stays at the top and the prompt is pushed down to the
  bottom row, instead of sitting right under the banner with a large empty
  gap below. After the first turn, output scrolls naturally.

## [1.1.4] — 2026-06-30

### Changed

- **Mascot reworked so it reads as a mantis, not a lizard.** The body now
  rears up steeply (instead of lying horizontal) and the raptorial forelegs
  are drawn bold and folded in front — the posture + arms are what
  distinguish a praying mantis from a generic green creature. Slimmer
  abdomen and thin legs.

## [1.1.3] — 2026-06-30

### Fixed

- **`mantis` now auto-selects an installed model instead of dying on a missing
  default.** On startup it probes the backend (Ollama `/api/tags`, else
  OpenAI-compat `/v1/models`); if the configured model isn't installed it picks
  the closest one that is (same base family → any chat model → first available)
  and notes the swap. When nothing is installed or the backend is unreachable
  it prints an actionable hint (`ollama serve` / `ollama pull <model>`). The
  per-turn "model not found" error now also suggests the exact pull command.

## [1.1.3] — 2026-06-30

### Changed

- **Mascot redrawn to match a real praying mantis.** Reared-up alert stance,
  facing right: abdomen low-left, prothorax rearing up to a triangular head
  with a compound eye and long antennae, raptorial forelegs folded in the
  "praying" pose, standing on bent legs. Smaller footprint (7 rows), with a
  pale highlight ridge and a paler folded forearm for depth.

## [1.1.2] — 2026-06-30

### Changed

- **Redrew the `mantis` mascot as a side-profile praying mantis.** The
  front-facing sprite read as a face; the new mascot is a pixel *bitmap*
  rasterized with half-blocks (2× vertical resolution, two-color cells) —
  triangular head with a compound eye, swept antennae, the raptorial
  forelegs folded in the "praying" pose, an arched body, and three legs.

## [1.1.1] — 2026-06-30

### Changed

- **`mantis` banner mascot is now a praying mantis.** Replaced the reused
  placeholder sprite with a purpose-drawn 5-row pixel praying mantis
  (antennae, triangular head, two compound eyes, folded raptorial forelegs).

## [1.1.0] — 2026-06-30

### Added

- **`mantis` — an interactive, Claude-Code-style agent terminal.** Run
  `mantis` in any directory for a banner (pixel mascot + version + model +
  cwd), a bordered input with a rotating `Try "…"` placeholder, a mode
  footer cycled with `shift+tab`, slash commands (`/help`, `/model`,
  `/clear`, `/cwd`, `/exit`), and token-level streaming from any configured
  backend. Configuration reads the standard `MANTIS_AGENT_MODEL`,
  `MANTIS_AGENT_BASE_URL`, and `MANTIS_AGENT_API_KEY` env vars.
  - New module `mantis_agent.tui` and a new `mantis` console entry point.
  - New `[cli]` optional extra (`prompt_toolkit`, `rich`) keeps the core
    SDK dependency-light; the stdlib-only `mantis-agent` CLI is unchanged.
    Install with `pip install 'mantis-agent-sdk[cli]'`.

## [1.0.0] — 2026-05-17

First stable release. The public API — the set of names in
`mantis_agent.__all__` — is now covered by the SemVer guarantee
documented in [SEMVER.md](SEMVER.md).

### Added

- **Locked public API surface.** `mantis_agent.__all__` is now the
  single source of truth for what is covered by SemVer. A new test
  (`tests/test_public_api_surface.py`) snapshots the set and fails on
  unintentional drift.
- **`__version__` from package metadata.** `mantis_agent.__version__`
  now reads from `importlib.metadata.version("mantis-agent-sdk")` when the
  package is installed, so it always tracks `pyproject.toml`.
- **`SEMVER.md`** — the versioning policy.
- **`RELEASING.md`** — the release runbook.
- **`.github/workflows/release.yml`** — tag-driven PyPI publish using
  trusted publishing (OIDC). No long-lived API token required.
- **`.github/workflows/test.yml`** — CI matrix on Python 3.11, 3.12, 3.13.
- Expanded `__all__` to include every Claude SDK parity symbol that was
  previously imported at the top of `mantis_agent` but only
  conventionally public: `ClaudeAgentOptions`, `ClaudeSDKClient`,
  `ClaudeSDKError`, `CLIConnectionError`, `AgentDefinition`,
  `HookMatcher`, `HookInput`, `HookJSONOutput`, `ClaudeHookContext`,
  `ClaudePermissionResult`, `PermissionResultAllow`,
  `PermissionResultDeny`, `ToolPermissionContext`, `ResultMessage`,
  `IsolationMode`, `create_sdk_mcp_server`.
- PyPI metadata: `readme`, `authors`, `keywords`, `classifiers`,
  `project.urls`, and explicit hatchling `wheel`/`sdist` targets so the
  built sdist contains tests, docs, and policy files.

### Highlights of the road to 1.0

The pre-1.0 series shipped the building blocks the 1.0 surface relies on.
A non-exhaustive summary:

- **Multi-model**: Ollama (native + auto-routing), OpenAI-compat
  (vLLM, Together, Fireworks, Groq, OpenRouter, Cerebras), llama.cpp,
  TGI, OpenAI native (`gpt-*`, `o1`/`o3`/`o4`), Gemini OpenAI-compat,
  Modal serverless adapter, `anthropic_passthrough` for parity testing.
- **Tool use**: three paths (native, prompt-engineered `<tool_call>`,
  grammar-constrained JSON), capability-table-driven selection across
  30+ models, parallel dispatch, mid-stream dispatch, mid-stream
  cancellation via `ToolPermissionContext.signal`.
- **Streaming**: full `ContentBlockStart`/`Delta`/`Stop` plus
  `MessageStart`/`Delta`/`Stop` event surface; tools fire on
  `ContentBlockStop`, not after `MessageStop`.
- **Thinking**: inline `<think>` for DeepSeek-R1, QwQ, Marco-o1, R1-distill;
  out-of-band thinking blocks for the DeepSeek API; `ThinkingBlock`
  in `AssistantMessage.content`.
- **MCP**: stdio / sse / http transports, in-process server via
  `create_sdk_mcp_server`, elicitation, sampling.
- **Sessions**: JSONL transcript persistence, `~/.mantis-agent/` layout,
  fork + resume from arbitrary checkpoint, memory entries + index,
  `<system-reminder>` and `isMeta` injection, auto-compaction.
- **Budget**: per-model pricing, `max_usd` ceiling →
  `BudgetExceededError`, `total_cost_usd` and `modelUsage` on
  `ResultMessage`, `max_turns` ceiling.
- **Local setup**: `mantis-agent setup-local` for Ollama (Linux/macOS/Windows)
  and `mantis-agent setup-local-llamacpp` for llama.cpp.
- **Examples**: 16 verified examples across ≥3 backends, including
  `quickstart`, `ollama_local`, `with_thinking`, `tools_option`,
  `mcp_calculator`, `system_prompt`, `fireworks_hosted`,
  `vllm_self_hosted`, `multi_agent_research`.
- **Docs site**: mkdocs-material at `docs/`.

[Unreleased]: https://github.com/teddyoweh/mantis-agent-sdk/compare/v2.62.0...HEAD
[2.62.0]: https://github.com/teddyoweh/mantis-agent-sdk/releases/tag/v2.62.0
[1.0.0]: https://github.com/teddyoweh/mantis-agent-sdk/releases/tag/v1.0.0
