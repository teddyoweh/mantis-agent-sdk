# `ClaudeAgentOptions`

Every option you can pass to `query()` or `ClaudeSDKClient`. Accepted as
a dataclass or a plain `dict`.

```python
from mantis_agent import ClaudeAgentOptions, HookMatcher, Plugin

options = ClaudeAgentOptions(
    model="qwen2.5:7b",
    backend=None,                   # auto-route from model
    base_url=None,
    api_key=None,
    tools=[get_weather],
    system_prompt="You are helpful.",
    max_turns=20,
    max_tokens=4096,
    temperature=0.7,
    max_usd=1.0,
    permissions={"default_mode": "allow"},
    can_use_tool=None,
    hooks=[],
    plugins=[],
    mcp_servers=[],
    agents=[],                      # sub-agents
    setting_sources=None,
    allowed_tools=None,
    disallowed_tools=None,
    cwd=None,
    session_id=None,
    persist=True,
    stderr=None,
)
```

## Core

| Key | Type | Default | Purpose |
|---|---|---|---|
| `model` | `str` | (env) | Model name. Auto-routes to a backend. |
| `backend` | `str \| None` | None | Force a backend (`ollama`, `openai_compat`, …). |
| `base_url` | `str \| None` | None | Base URL for HTTP backends. |
| `api_key` | `str \| None` | None | API key for HTTP backends. |
| `system_prompt` | `str` | `""` | Sent as the system message. |
| `max_turns` | `int` | `20` | Hard ceiling on assistant turns. |
| `max_tokens` | `int` | `4096` | Per-call max output tokens. |
| `temperature` | `float` | `0.7` | Sampling temperature. |
| `max_usd` | `float \| None` | None | Session cost cap → `BudgetExceededError`. |

## Tools

| Key | Type | Default |
|---|---|---|
| `tools` | `list[Tool]` | `[]` |
| `allowed_tools` | `list[str] \| None` | None |
| `disallowed_tools` | `list[str] \| None` | None |

`allowed_tools` and `disallowed_tools` are mutually exclusive.

## Permissions

| Key | Type | Default | Purpose |
|---|---|---|---|
| `permissions` | `dict` | `{"default_mode": "allow"}` | Default policy. |
| `can_use_tool` | callable \| None | None | Per-call permission decision. |

See [Permissions](../guides/permissions.md).

## Hooks and plugins

| Key | Type |
|---|---|
| `hooks` | `list[HookMatcher]` |
| `plugins` | `list[Plugin]` |

See [Hooks](../guides/hooks.md), [Plugins](../guides/plugins.md).

## MCP and sub-agents

| Key | Type |
|---|---|
| `mcp_servers` | `list[McpServerConfig \| InProcessServer]` |
| `agents` | `list[SubAgentSpec]` |
| `sampling_handler` | `"auto" \| callable \| None` |

See [MCP servers](../guides/mcp.md), [Sub-agents](../guides/sub-agents.md).

## Settings and persistence

| Key | Type | Default | Purpose |
|---|---|---|---|
| `setting_sources` | `list[str] \| None` | None | JSON files to load and merge. |
| `cwd` | `str \| None` | `os.getcwd()` | Working directory the agent reports. |
| `session_id` | `str \| None` | (generated) | Reuse this id to resume. |
| `persist` | `bool \| str` | `True` | `True` → `~/.mantis-agent/sessions/`, `False` → in-memory, `"path"` → write there. |

## Observability

| Key | Type | Default |
|---|---|---|
| `stderr` | callable \| None | None |
| `extra_headers` | `dict` | `{}` |

`stderr` is invoked with each stream chunk as it arrives — useful for
debug logging. See [Streaming → Stderr callback](../guides/streaming.md#stderr-callback).

## Capability override

| Key | Type | Purpose |
|---|---|---|
| `tool_use_path` | `"native_tools" \| "xml_prompt_engineered" \| "grammar_constrained_json" \| None` | Force a tool-use strategy. |
| `pricing_override` | `dict \| None` | Override the pricing-table entry for this model. |
| `compact_threshold` | `float` | Fraction of context window before compaction fires. Default `0.85`. |

## `dict` form

Every option above is also accepted via a plain `dict`:

```python
async for msg in query(prompt="hi", options={
    "model": "qwen2.5:7b",
    "max_turns": 10,
    "tools": [get_weather],
}):
    ...
```

The two forms are interchangeable — `dict` is converted via
`ClaudeAgentOptions(**d)` internally.
