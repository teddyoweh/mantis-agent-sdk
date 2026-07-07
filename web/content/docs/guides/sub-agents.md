# Sub-agents

A **sub-agent** is an agent exposed to the parent as a regular tool.
The parent calls it like any other tool; the sub-agent runs to
completion (with its own model, system prompt, and tools) and returns
its final answer as the tool result.

Use sub-agents when the work decomposes into specialised slices —
"research", "draft", "review" — that benefit from their own context.

## Defining a sub-agent

Two flavours.

### `SubAgentSpec` — declarative

```python
from mantis_agent import SubAgentSpec, as_subagent_tool

researcher = SubAgentSpec(
    name="researcher",
    description="Research a topic and produce a fact-checked summary.",
    model="qwen2.5:7b",
    system_prompt="You are a careful researcher. Cite sources.",
    tools=[web_search, web_fetch],
    max_turns=15,
)

researcher_tool = as_subagent_tool(researcher)
```

### Existing `Agent` — imperative

If you already have an `Agent` instance you want to expose:

```python
from mantis_agent import Agent, as_subagent_tool

researcher = Agent(
    model="qwen2.5:7b",
    system_prompt="...",
    tools=[...],
)
researcher_tool = as_subagent_tool(
    researcher,
    name="researcher",
    description="Research a topic and produce a fact-checked summary.",
)
```

Under the hood this wraps the agent in a `WrappedAgentTool`. Same JSON
schema as `SubAgentTool`.

## Calling from a parent

Add the wrapped sub-agent to the parent's `tools` list:

```python
options = {
    "model": "qwen2.5:7b",
    "tools": [researcher_tool, drafter_tool, reviewer_tool],
}
```

The model invokes them like any other tool. The sub-agent runs in
isolation and returns its final assistant text.

## Isolation

Sub-agents have their own:

- Transcript (separate JSONL file under `~/.mantis-agent/sessions/`)
- System prompt
- Tool registry
- Permission policy
- Budget cap

Set `isolation=IsolationMode.SHARED` to share session and transcript
with the parent (the sub-agent's turns interleave into the parent's
transcript). Use this when the sub-agent should *append* to the
parent's context, not branch off.

```python
from mantis_agent import IsolationMode

researcher_tool = as_subagent_tool(
    researcher,
    isolation=IsolationMode.SHARED,
)
```

## Multiple sub-agents in parallel

If the parent emits two sub-agent calls in the same turn, they run
concurrently (assuming `parallel_safe=True`, the default). Each gets
its own task group; results thread back in emission order.

```python
options = {
    "tools": [
        as_subagent_tool(spec_a),
        as_subagent_tool(spec_b),
        as_subagent_tool(spec_c),
    ],
}
```

The parent model can fan out: "use researcher + drafter + reviewer in
parallel". The runtime handles the rest.

## Passing prompts and data

Sub-agents accept a single `prompt` argument by default — whatever the
parent passes. To accept structured input, define a custom schema on
the spec:

```python
researcher = SubAgentSpec(
    name="researcher",
    description="...",
    input_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "depth": {"type": "string", "enum": ["shallow", "deep"]},
        },
        "required": ["topic"],
    },
)
```

The wrapped tool exposes that schema to the parent; the sub-agent gets
the parsed input as a dict.

## Budgets and limits

Apply caps separately to each sub-agent:

```python
researcher = SubAgentSpec(..., max_usd=0.10, max_turns=8)
drafter = SubAgentSpec(..., max_usd=0.05, max_turns=5)
```

The parent's `max_usd` rolls up everything (its own model calls plus
all sub-agent spend).

## Patterns

Three common shapes:

1. **Specialist pool.** Parent decides which expert to consult based on
   the user's question. Each sub-agent has a narrow domain.
2. **Pipeline.** Parent calls researcher → drafter → reviewer in order,
   threading outputs through.
3. **Fan-out.** Parent emits N sub-agent calls in parallel to compare
   outputs, then picks the best.

See `mantis_agent/examples/multi_agent_research.py` for a worked
example of all three.

## Agent types (the `task` tool)

The `mantis` terminal ships one delegation tool — `task` — with selectable
**agent types** (Claude Code's `subagent_type`):

| type | tools | steps | for |
|---|---|---|---|
| `explore` | read-only | 20 | find code/facts, report with `file:line` |
| `plan` | read-only | 25 | read the code → step-by-step implementation plan |
| `general-purpose` | full belt | 100 | autonomous multi-step execution |

Parallel fan-out works: the model can launch several `task` calls in one
message and they run concurrently. Subagents inherit the parent's
**permission gate** — a write-capable child prompts you exactly like the
parent would.

### User-defined agents

Drop a markdown file in `~/.mantis-agent/agents/<name>.md` (user-wide) or
`./.mantis/agents/<name>.md` (project — wins on collision, can override the
built-ins):

```markdown
---
name: code-reviewer
description: Reviews a diff for bugs and style problems.
tools: read_file, grep, glob    # or "all" / "read-only" (default "all")
model: gpt-5.4-mini             # optional; default inherits the parent
max_steps: 30
---
You are a meticulous code reviewer...
```

`/agents` lists everything discovered. In the SDK, `discover_agent_types()`
returns the same list, and `make_task_tool(..., agent_types=...)` builds the
tool.

### SDK: `options.agents`

Claude-SDK-style agent definitions register as delegatable tools named after
the agent:

```python
from mantis_agent import AgentDefinition, MantisAgentOptions

options = MantisAgentOptions(
    model="qwen2.5:7b",
    agents={"reviewer": AgentDefinition(
        description="Reviews diffs for bugs.",
        prompt="You are a reviewer.",
        tools=["read_file", "grep"],       # narrows the child's kit
    )},
)
```

## Twins (the `pair` tool)

`task` is fire-and-forget; **`pair` is a conversation**. Each named peer — a
*twin* — is a persistent same-model agent that remembers your whole dialogue
with it: propose → get pushback → revise → converge.

- `pair(message, peer="skeptic", persona="stress-test every proposal")` —
  named peers are independent minds with separate memories
- Twins carry the **read-only** kit, so their pushback cites real
  `file:line`, and they can't race the parent on writes
- `reset=true` wipes one peer

In the terminal, `/twin skeptic: <message>` lets **you** join the exact same
conversation the model has been having (state is shared, survives model
switches). SDK: `make_pair_tool(model=..., tools=..., conversations=...)`.
