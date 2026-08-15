# `MantisAgentOptions`

Every option you can pass to `query()` or `ClaudeSDKClient`. Accepted as
a dataclass or a plain `dict`.

```python
from mantis_agent import MantisAgentOptions, HookMatcher, Plugin

options = MantisAgentOptions(
    model="qwen2.5:7b",
    backend=None,                   # auto-routed from the model name when None
    base_url=None,                  # alias for backend; passing both raises
    api_key=None,                   # None = discover from env; "" = no auth
    tools=[get_weather],
    system_prompt="You are helpful.",
    max_turns=20,
    max_tokens=4096,
    temperature=0.7,
    max_budget_usd=1.0,             # the dict-options spelling is max_usd
    permission_mode="default",      # "default" | "auto" | "bypass"
    can_use_tool=None,              # async callback returning a PermissionResult
    allowed_tools=None,
    disallowed_tools=None,
    hooks={},                       # {event: [HookMatcher(...)]} — a dict, not a list
    plugins=[],
    mcp_servers={},                 # {name: config} — a dict, not a list
    agents={},                      # {name: AgentDefinition} — sub-agents, a dict
    effort=None,                    # "minimal".."max" | "none"
    thinking=None,
    max_thinking_tokens=None,
    fallback_model=None,
    response_format=None,
    response_model=None,            # a dataclass/Struct/TypedDict/pydantic model
    raise_on_error=False,           # raise instead of failing quietly
    skills=None,                    # None = off; "auto" = match per turn; "all"
    setting_sources=None,           # ["user", "project", "local"] — names, not paths
    cwd=None,
    session_id=None,
    persist=True,
    include_memory=True,
    stderr=None,
    env=None,
    extra={},                       # escape hatch for adapter-specific keys
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
`MantisAgentOptions(**d)` internally.
