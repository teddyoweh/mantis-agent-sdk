# Workflows

A **workflow** is a named, multi-phase fan-out of subagents that runs in the
background, streams live progress, persists what it did, and can be resumed.

One mental model runs through the whole feature:

```
workflow  ›  phase  ›  agent
```

…and a workflow is *also* a background job, so it has an id and an observable
status from the moment it starts.

| | |
|---|---|
| `task` | one focused delegation, one report |
| `coordinate` | an ad-hoc decomposition the model invents on the spot |
| **`workflow`** | a **named template** you wrote, re-runnable and resumable |

## The ten things you can do

1. [Run a workflow by name](#running-one) — `/workflows run review target=…`
2. [Multi-phase execution](#the-definition-format) — phases run in order, each rolling up its own status
3. [Parallel fan-out and per-item pipelines](#phase-modes)
4. [Background it](#backgrounding) — returns a run id + job id immediately
5. [Disposable expert subagents](#agent-types) — built-in personas or your own Markdown ones
6. [Watch it live](#the-viewer) — phase rail, per-agent status, tokens, cost, timing
7. [Drill into one agent](#drilling-in) — its prompt, activity, result, error
8. [Watch several at once](#the-viewer) — multiple concurrent runs is the normal case
9. [Control it](#controls) — stop, pause/resume, cancel, skip, retry, save
10. [Inspect and resume history](#history-and-resume) — long after the session ended

---

## Running one

```
/workflows list                             # what's available, and from where
/workflows run review target=mantis_agent/agent.py
/workflows                                  # the live viewer
/workflows history                          # past runs (persisted)
/workflows resume w2h94j                    # replay the unchanged prefix
/workflows export w2h94j                    # dump the run's JSON into the CWD
```

Arguments are `key=value`; anything left over becomes the `objective` input, so
`/workflows run understand the auth flow` does what it looks like.

The model can start one too, with the `workflow` tool — but only when you asked
for orchestration. Workflows spawn many agents and cost far more than a single
`task` call, so the tool description tells the model that scale must be
requested, not inferred. To turn the feature off entirely:

```bash
export MANTIS_AGENT_DISABLE_WORKFLOWS=1
```

## Built-in templates

| name | shape |
|---|---|
| `understand` | three parallel readers over a subsystem → one brief |
| `design` | three independent designs → judge → synthesized recommendation |
| `review` | dimensions in parallel → adversarial verify per dimension → report |
| `research` | multi-modal sweep → deep read per lead → sourced answer |
| `implement` | plan → implement → adversarial verify (`VERDICT: PASS/FAIL/PARTIAL`) |

## The definition format

Definitions are Markdown with frontmatter — the same shape as `agents/*.md` and
`skills/*/SKILL.md` — plus a fenced `json` block holding the phase graph.

````markdown
---
name: review
description: Review a change across dimensions, then verify each finding
when_to_use: on a diff that is about to ship
---

Prose here is the shared **briefing**: it is prepended to every agent's prompt
in this workflow. Children start with no memory of anything, so this is how
house rules travel with them.

```json
{
  "inputs": [
    {"name": "target", "required": true, "description": "what to review"}
  ],
  "phases": [
    {"title": "Review", "mode": "parallel", "detail": "one per dimension",
     "agents": [
       {"label": "correctness", "agent_type": "explore",
        "prompt": "Review {target} for correctness bugs."},
       {"label": "edge-cases", "agent_type": "explore",
        "prompt": "Review {target} for edge cases."}
     ]},
    {"title": "Verify", "mode": "pipeline", "over": "phase:Review",
     "stages": [
       {"label": "refute", "agent_type": "verify",
        "prompt": "Try to refute these findings:\n{item}"}
     ]},
    {"title": "Report", "mode": "sequential",
     "agents": [
       {"label": "report", "prompt": "Write the review:\n{phase:Verify}"}
     ]}
  ]
}
```
````

### Where they live, and who wins

| source | path | precedence |
|---|---|---|
| project | `./.mantis/workflows/*.md` | highest |
| user | `$MANTIS_AGENT_HOME/workflows/*.md` | middle |
| built-in | shipped with the SDK | lowest |

Later sources win **by name**, so a project can override a built-in `review`
with its own. A definition that fails to parse is skipped and named in
`/workflows list` — a broken file never takes down the session.

### Phase modes

| mode | behavior |
|---|---|
| `parallel` | every agent at once, behind a barrier — the phase ends when all are done |
| `sequential` | in order; each agent sees the previous one's output as `{prev}` |
| `pipeline` | one independent chain per item from `over`; no barrier between items |

`over` is either `phase:<Title>` (an **earlier** phase's results, one item per
result) or `input:<name>` (a newline- or comma-separated input).

### Template placeholders

| placeholder | resolves to |
|---|---|
| `{name}` | a declared or supplied input |
| `{phase:Title}` | the joined results of an earlier phase |
| `{item}` | the current item (pipeline only) |
| `{prev}` | the previous stage's / agent's output |
| `{index}` | 1-based position |

Unknown placeholders are left **exactly as written**, so a prompt containing
JSON or code braces survives untouched.

### Agent types

`agent_type` picks the persona: the built-in `explore`, `plan`,
`general-purpose` and `verify`, or any custom one you defined in
`.mantis/agents/<name>.md`. An unknown type falls back to the workflow default
and is logged rather than failing the run.

Each child carves its tool kit from the parent's belt through its persona's
policy, and inherits the parent's permission gate and budget — a write-capable
child prompts you exactly as the parent would. Orchestration tools themselves
(`task`, `coordinate`, `workflow`) are stripped from children, so a fan-out
cannot recursively fan out.

### Safety rails

A definition may declare at most **64 agents**, and a pipeline whose `over`
resolves to more items than that is refused at run time — before anything is
spent.

## Backgrounding

Starting a workflow returns immediately:

```
▶ workflow review · run w2h94j · job #3 · 3 phases
→ /workflows to watch · /jobs for lifecycle · the report lands as a job notification
```

The run id and the job id are two views of one thing: `/jobs` shows lifecycle
(running, cancel, elapsed), `/workflows` shows structure (phases, agents,
tokens). Each names the other. When it finishes, the report is injected as
context so the model learns the outcome on its next turn — and
`job_output(<id>)` reads it early.

## The viewer

`/workflows` with no arguments opens the live overlay:

```
Workflows 1/2 · review (w2h94j) · job #3
◇ Review    2/3    ❯● ◇ correctness: reading agent.py…   ▶ 12s · ↓4.1k tok
✓ Verify    3/3      ● ✓ edge-cases: three findings…       28s · ↑9.2k tok
◇ Report    0/1      ● ◇ refute·1: checking claim 2…     ▶  4s · ↓1.8k tok
↑↓ select · enter/→ inspect · ←/esc back · x stop · p pause/resume · c cancel · k skip · r retry · s save
```

Left is the phase rail (glyph · title · done/total) for the selected run; right
is the agent list. `↑↓` moves across **all** runs, so several concurrent
workflows are one continuous list rather than a special case.

### Drilling in

`Enter` (or `→`) opens one agent:

```
Workflow agent · correctness
workflow: review (w2h94j) · job #3
phase: Review · explore · qwen2.5:7b
status: running · 12s
progress: 2 turns · 5 tools · 4.1k tok · $0.0042
prompt
  Review mantis_agent/agent.py for correctness bugs.
recent activity
  - grep
  - read_file
result
  …
```

`Esc` (or `←`) goes back: detail → list → closed. Closing the overlay never
stops the run.

Only observable facts appear: prompt, tool names, turn counts, visible output.
Hidden model reasoning is never surfaced — the engine does not record it.

## Controls

| key | action | when it applies |
|---|---|---|
| `x` | stop the run | while it is running |
| `p` | pause / resume | pausing gates new agents at their phase boundary; in-flight turns finish |
| `c` | cancel the selected agent | while that agent is running |
| `k` | skip the selected agent | queued (marked cancelled) or running (aborted) |
| `r` | retry the selected agent | once it has finished, errored, or been cancelled |
| `s` | snapshot the run into the durable store | always, live or from history |

An action that does not apply says **why** — "cannot cancel: agent a0 is done",
"review is not live (loaded from history) — only 'save' works here" — instead of
failing silently.

## History and resume

Every run is written to `$MANTIS_AGENT_HOME/workflows/runs/<run-id>.json` when
it ends, including runs that were stopped or that failed. Plain JSON: the run
snapshot plus the definition name and its inputs.

```
/workflows history
/workflows resume w2h94j
```

Resume replays every agent whose **phase, label and prompt digest** are
unchanged — those come back instantly, free, marked `(replayed)` — and re-runs
everything else. Edit a prompt in the definition and that agent, and everything
downstream that depends on it, runs live again.

Input values that look like credentials (`api_key`, `token`, `secret`, …) are
written as `[redacted]`, and a redacted value is never resurrected on resume.

## From Python

```python
from mantis_agent import load_workflow_definition, make_workflow_tool
from mantis_agent.workflow_tool import prepare_workflow_launch

defn = load_workflow_definition("review")

launch = prepare_workflow_launch(
    defn, {"target": "mantis_agent/agent.py"},
    agent_runner=my_runner,       # or omit and let make_workflow_tool build one
    model="qwen2.5:7b",
)
print(launch.run_id)              # exists before anything runs
result = await launch.execute()   # persists the artifact on the way out
```

Or hand the whole thing to a model as a tool:

```python
registry.add(make_workflow_tool(
    model=model, provider=provider, tools=parent_kit,
    permissions=permissions, jobs=job_manager,
    on_run=viewer.register,       # live registration
    on_progress=progress_sink,    # task-tool event shape
))
```

## See also

* [Sub-agents](sub-agents.md) — the personas a workflow's phases run
* [Budget and limits](budget.md) — capping what a fan-out can spend
* [Permissions](permissions.md) — the gate children inherit
