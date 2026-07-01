# Permissions

Permissions sit between the model's request to use a tool and the tool
actually running. You can approve, deny, or **rewrite** tool arguments
before dispatch.

## `can_use_tool`

The single hook for permission decisions:

```python
from mantis_agent import (
    MantisAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

async def can_use_tool(
    tool_name: str,
    tool_input: dict,
    ctx: ToolPermissionContext,
):
    if tool_name == "write_file" and tool_input["path"].startswith("/etc"):
        return PermissionResultDeny(reason="No writes outside the project")
    return PermissionResultAllow()

options = MantisAgentOptions(
    model="qwen2.5:7b",
    tools=[write_file, ...],
    can_use_tool=can_use_tool,
)
```

The callback runs *every* time the model emits a `tool_use` block. It must
return either `PermissionResultAllow` or `PermissionResultDeny`.

## Rewriting arguments

The most useful pattern: let the tool run, but with safer arguments.

```python
async def can_use_tool(tool_name, tool_input, ctx):
    if tool_name == "shell" and "rm -rf" in tool_input["command"]:
        # Strip the dangerous part
        safe = tool_input["command"].replace("rm -rf", "rm -ri")
        return PermissionResultAllow(updated_input={"command": safe})
    return PermissionResultAllow()
```

`updated_input` is passed to the tool *instead of* what the model asked
for. The model sees the tool result of the rewritten call.

## `permission_denials` on the result

Denied calls don't crash the agent. They surface in the final
`ResultMessage`:

```python
async for msg in query(...):
    if msg.type == "result":
        for denial in msg.permission_denials:
            print(f"denied {denial.tool_name}: {denial.reason}")
```

The model also sees the denial as a tool_result with `is_error=True`, so
it can choose to retry differently.

## Default modes

If you don't pass `can_use_tool`, set a default policy via
`permissions.default_mode`:

| Mode | Behaviour |
|---|---|
| `allow` (default) | All tools run without prompting. |
| `ask` | Prompt the user on stdin before running. Useful for CLI tools. |
| `deny` | All tools refused. Useful for testing without side effects. |

```python
options = MantisAgentOptions(
    model="qwen2.5:7b",
    tools=[shell, write_file],
    permissions={"default_mode": "ask"},
)
```

## `allowed_tools` / `disallowed_tools`

Whitelists / blacklists by tool name:

```python
options = MantisAgentOptions(
    tools=[fetch, search, shell, write_file],
    allowed_tools=["fetch", "search"],       # only these run
    # disallowed_tools=["shell"],            # everything except these
)
```

`allowed_tools` and `disallowed_tools` are mutually exclusive — set one,
not both.

## `ToolPermissionContext`

The `ctx` argument to `can_use_tool` carries:

```python
ctx.session_id    # current session id
ctx.turn          # turn number (0-indexed)
ctx.messages      # full transcript up to this point
ctx.signal        # anyio.Event — fired by Agent.cancel()
```

`ctx.signal` is the same event used for [mid-stream
cancellation](streaming.md#mid-stream-cancellation). If you observe it
fired inside `can_use_tool`, return `PermissionResultDeny(reason="cancelled")`
to short-circuit further dispatch.

## Composition with hooks

Permission and hooks are separate concerns:

- **`can_use_tool`** decides whether a single tool call runs.
- **Hooks** observe events (`PreToolUse`, `PostToolUse`, `Stop`, …) and
  can attach side effects, but don't gate dispatch.

See [Hooks](hooks.md) for the full hook event taxonomy.
