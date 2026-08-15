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
    backend="http://localhost:11434",
    system="...",        # Agent's field is `system`; `system_prompt` is the
    tools=[],            # SubAgentSpec / MantisAgentOptions spelling
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
options = MantisAgentOptions(
    model="qwen2.5:7b",
    tools=[researcher_tool, drafter_tool, reviewer_tool],
)
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

Isolation is a property of the **spec**, not of the wrapper, and
`IsolationMode` is a string literal type — `"asyncio_task"` (the default),
`"subprocess"`, or `"remote"`. There is no `SHARED` member:

```python
from mantis_agent import SubAgentSpec, as_subagent_tool

spec = SubAgentSpec(
    name="researcher",
    system_prompt="Research the topic and report back.",
    model="qwen2.5:7b",
    isolation="asyncio_task",
)
researcher_tool = as_subagent_tool(spec)
```

`as_subagent_tool(spec_or_agent, *, name=None, description=None,
parent_provider=None)` takes no `isolation` argument. Pass
`parent_provider=parent.provider` to share the parent's HTTP pool —
recommended in `asyncio_task` mode.

## Multiple sub-agents in parallel

If the parent emits two sub-agent calls in the same turn, they run
concurrently (assuming `parallel_safe=True`, the default). Each gets
its own task group; results thread back in emission order.

```python
options = MantisAgentOptions(
    tools=[
        as_subagent_tool(spec_a),
        as_subagent_tool(spec_b),
        as_subagent_tool(spec_c),
    ],
)
```

The parent model can fan out: "use researcher + drafter + reviewer in
parallel". The runtime handles the rest.

## Passing prompts and data

Sub-agents accept a single `prompt` argument by default — whatever the
parent passes. To accept structured input, define a custom schema on
the spec:

`SubAgentSpec` has no `input_schema` field — the wrapped tool always exposes a
single `prompt` argument. For structured input, build the `Tool` yourself with
the schema you want and dispatch to the sub-agent inside it:

```python
import json

from mantis_agent import SubAgentSpec, Tool, as_subagent_tool

spec = SubAgentSpec(
    name="researcher",
    system_prompt="Research the topic and report back.",
    model="qwen2.5:7b",
    description="Research a topic at a given depth.",
)
inner = as_subagent_tool(spec)


async def research(topic: str, depth: str = "shallow") -> str:
    return await inner.fn(prompt=json.dumps({"topic": topic, "depth": depth}))


structured = Tool(
    name="researcher",
    description="Research a topic at a given depth.",
    input_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "depth": {"type": "string", "enum": ["shallow", "deep"]},
        },
        "required": ["topic"],
    },
    fn=research,
)
```

## Budgets and limits

Apply caps separately to each sub-agent:

Dollar caps live on a `Budget`, which the spec carries; `max_turns` is a field
in its own right:

```python
from mantis_agent import SubAgentSpec
from mantis_agent.budget import Budget

researcher = SubAgentSpec(
    name="researcher",
    system_prompt="Research.",
    model="qwen2.5:7b",
    max_turns=8,
    budget=Budget(max_usd=0.10),
)
drafter = SubAgentSpec(
    name="drafter",
    system_prompt="Draft.",
    model="qwen2.5:7b",
    max_turns=5,
    budget=Budget(max_usd=0.05),
)
```

`Budget` also takes `max_input_tokens`, `max_output_tokens`,
`max_total_tokens`, and a `fallback_model` to downshift to before the cap is
hit. The parent's own cap rolls up everything: its model calls plus all
sub-agent spend.

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
