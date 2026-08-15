# Hooks

Hooks observe — and, on gating events, can veto — the agent lifecycle. Use
[permissions](permissions.md) for user-facing allow/deny prompts; use hooks to
record state, write logs, rewrite a tool's arguments in flight, or block a call
outright by returning `HookResult(block=True)`.

## The shape

`hooks` is a **dict** keyed by event name; each value is a list of
`HookMatcher`s, and each matcher holds the callables to run.

```python
from mantis_agent import HookMatcher, MantisAgentOptions


async def log_tool(ctx):
    # One argument: a HookContext. `ctx.tool` is the Tool, `ctx.input` the
    # arguments the model produced.
    print(f"[{ctx.event}] {ctx.tool.name if ctx.tool else ''} {ctx.input}")
    return None  # None = observe only; return a HookResult to block or mutate.


options = MantisAgentOptions(
    model="qwen2.5:7b",
    hooks={
        "PreToolUse": [HookMatcher(hooks=[log_tool])],
        # `matcher` is an optional tool-name pattern — omit it to see every tool.
        "PostToolUse": [HookMatcher(matcher="Bash", hooks=[log_tool])],
    },
)
```

Several matchers on one event all run, in declaration order, and mutations
chain: each hook sees the previous one's `mutated_input`. A hook that raises is
skipped rather than crashing the loop — unless `MANTIS_HOOKS_FAIL_CLOSED=1`, in
which case a raising hook on a gating event denies the call.

`HookContext` carries `event`, `tool`, `input`, `output` (on `PostToolUse`),
`messages_snapshot` (on lifecycle events), `agent_id`, and an `arbitrary`
extras bag. Each event populates the subset that makes sense for it.

To block or rewrite a call, return a `HookResult`:

```python
from mantis_agent.hooks import HookResult


async def redact_paths(ctx):
    args = dict(ctx.input or {})
    if "/etc/" in str(args.get("path", "")):
        return HookResult(block=True, note="refused: system path")
    return HookResult(mutated_input=args)
```

## Events

`hooks={...}` recognizes **15** event names. Anything else is skipped in
silence, so a plausible-looking name registers nothing:

**Per-tool**

- `PreToolUse` — the model emitted a `tool_use`, before the executor runs it.
- `PostToolUse` — the tool returned, before the result is threaded back.
- `PostToolUseFailure` — the tool raised.

**Per-turn / session**

- `UserPromptSubmit` — a prompt was submitted, before the model sees it.
- `Stop` / `StopFailure` — the loop is about to exit.
- `SessionStart` / `SessionEnd`.
- `PreCompact` / `PostCompact` — around a context compaction.

**Delegation and permissions**

- `SubagentStart` / `SubagentStop`.
- `PermissionRequest` / `PermissionDenied`.
- `Notification`.

Seven of those can **veto** what they wrap — `PreToolUse`, `UserPromptSubmit`,
`Stop`, `SubagentStop`, `PreCompact`, `PermissionRequest`,
`InstructionsLoaded`. On the rest, a returned `block` is ignored and the hook
is observation-only.

The runtime has twelve further slots — `Setup`, `TaskCreated`, `TaskCompleted`,
`Elicitation`, `ElicitationResult`, `ConfigChange`, `FileChanged`,
`CwdChanged`, `InstructionsLoaded`, `WorktreeCreate`, `WorktreeRemove`,
`TeammateIdle` — that the dict form doesn't map. Reach them by building `Hooks`
and handing it to `Agent` directly:

```python
from mantis_agent import Agent
from mantis_agent.hooks import HookMatcher as InternalMatcher, Hooks


async def on_worktree(ctx):
    print("worktree created", ctx.arbitrary)
    return None


agent = Agent(
    model="qwen2.5:7b",
    backend="http://localhost:11434",
    hooks=Hooks(worktree_create=InternalMatcher(hook=on_worktree)),
)
```

Note the two different `HookMatcher`s: `mantis_agent.HookMatcher` is the
Claude-SDK-compatible one (`matcher`, `hooks=[...]`) used in the dict form;
`mantis_agent.hooks.HookMatcher` is the internal one (`hook=`, `matcher=`) that
`Hooks` fields take. `MantisAgentOptions(hooks=Hooks(...))` does *not* work —
it expects the dict.

## What a hook receives and returns

`HookInput` and `HookJSONOutput` are exported for Claude-SDK type parity, but
both are aliases for `dict[str, Any]` — they are not structured types. The real
payload is a `HookContext`, and the real return value is a `HookResult`:

```python
from mantis_agent.hooks import HookResult


async def normalise(ctx):
    if ctx.event == "PreToolUse" and ctx.input and "city" in ctx.input:
        return HookResult(mutated_input={**ctx.input, "city": ctx.input["city"].title()})
    return None
```

| `HookResult` field | Effect |
|---|---|
| `block` | On a vetoing event, cancels the operation — no exception raised. |
| `mutated_input` | On `PreToolUse`, replaces the tool input. |
| `note` | Free-form reason, for logs and observability. |

Returning `None` means "observe only".

## Plugins ship hooks too

A `Plugin` bundles hooks alongside tools and a system-prompt addition. Its
`hooks` field takes the same dict form:

```python
from mantis_agent import HookMatcher, MantisAgentOptions, Plugin


async def log_tool(ctx):
    print(ctx.event, ctx.tool.name if ctx.tool else "")
    return None


logging_plugin = Plugin(
    name="logging",
    hooks={
        "PreToolUse": [HookMatcher(hooks=[log_tool])],
        "PostToolUse": [HookMatcher(hooks=[log_tool])],
    },
)

options = MantisAgentOptions(model="qwen2.5:7b", plugins=[logging_plugin])
```

On a per-event collision, the user's hooks win over a plugin's.

See [Plugins](plugins.md).
