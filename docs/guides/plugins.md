# Plugins

A `Plugin` is a bundle of tools, hooks, and system-prompt text that
attaches to an `Agent` at session start. Use plugins to package reusable
agent capabilities — telemetry, retry logic, domain tools — without
mutating shared state.

## Anatomy

```python
from mantis_agent import Plugin, HookMatcher, tool

@tool
async def log_event(name: str, payload: dict) -> str:
    """Record an analytics event."""
    ...

async def attach_session_id(hook_input, hook_ctx):
    ...

analytics = Plugin(
    name="analytics",
    tools=[log_event],
    system_prompt_addition=(
        "Whenever the user reports a problem, call log_event "
        "with name='problem' and the relevant payload."
    ),
    hooks=[
        HookMatcher(event="PreToolUse", handler=attach_session_id),
    ],
)
```

A plugin has four parts (all optional):

- `name` — used in errors and logs.
- `tools` — a list of `Tool` (functions decorated with `@tool`).
- `system_prompt_addition` — appended to the agent's system prompt.
- `hooks` — `HookMatcher` instances registered for the session.

## Attaching

```python
from mantis_agent import ClaudeAgentOptions

options = ClaudeAgentOptions(
    model="qwen2.5:7b",
    tools=[primary_tool],
    plugins=[analytics, retries, telemetry],
)
```

Plugins merge at session start. The final tool registry is the union
across all plugins plus the top-level `tools`. The final system prompt
is the user's `system_prompt` followed by each plugin's
`system_prompt_addition`, in declaration order. Hooks accumulate across
plugins.

## Idempotency

Attaching the same plugin twice is a no-op — the runtime de-duplicates
by `name`. This makes it safe to attach plugins from multiple sources
(e.g. a base config layer + a per-environment override) without
worrying about double-counting hooks.

## Sharing config

Plugins are plain Python objects — store them in a module and import
where needed:

```python
# my_app/plugins.py
analytics = Plugin(name="analytics", ...)
retries = Plugin(name="retries", ...)
telemetry = Plugin(name="telemetry", ...)
```

```python
# my_app/agents/researcher.py
from my_app.plugins import analytics, retries, telemetry
...
options = ClaudeAgentOptions(..., plugins=[analytics, retries, telemetry])
```

## When to use a plugin vs. a sub-agent

- **Plugin** — extends the *current* agent. Same model, same context,
  shared transcript.
- **Sub-agent** — runs as a child agent invoked via tool. Its own model
  and context.

Pick a plugin when the work doesn't deserve a fresh context. Pick a
sub-agent when the work needs to be opaque to the parent.
