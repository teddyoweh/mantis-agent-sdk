# API reference

The public surface — what you can import from `mantis_agent` and rely on
across minor versions.

## Top-level imports

```python
from mantis_agent import (
    # Core
    query,
    ClaudeSDKClient,
    MantisAgentOptions,
    Agent,

    # Tools
    tool,
    Tool,
    ToolRegistry,
    WebFetch,
    WebSearch,

    # Sub-agents
    SubAgentSpec,
    SubAgentTool,
    WrappedAgentTool,
    as_subagent_tool,
    IsolationMode,

    # MCP
    create_sdk_mcp_server,

    # Permissions / hooks
    HookMatcher,
    HookInput,
    HookJSONOutput,
    ToolPermissionContext,
    PermissionResultAllow,
    PermissionResultDeny,

    # Plugins
    Plugin,

    # Sessions
    Session,
    SessionInfo,
    SessionStore,
    InMemorySessionStore,
    SqliteSessionStore,
    Checkpoint,
    make_checkpoints,
    fork_session,
    resume_session,

    # Messages
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,

    # Capabilities
    BackendCapability,
    ModelCapability,
    ToolUsePath,
    lookup_model,
    resolve_tool_use_path,

    # Errors
    AgentError,
    AuthError,
    BudgetExceededError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    StreamProtocolError,
    ToolExecutionError,
    CLIConnectionError,
    ClaudeSDKError,
)
```

## By topic

- [query / ClaudeSDKClient](client.md) — the two entry points.
- [MantisAgentOptions](options.md) — every option, with defaults.
- [Message types](messages.md) — flat-shape vs. internal shape.
- [Tools](tools.md) — `@tool`, registries, built-ins.
- [Errors](errors.md) — full error hierarchy.
- [Sessions](sessions.md) — `Session`, `Checkpoint`, fork, resume, stores.

## Exported types without a guide of their own

These are in `mantis_agent.__all__` — importable and stable — but they belong
to surfaces documented elsewhere or are read rather than constructed. Listed so
`__all__` and the docs agree, and so you know what a name is when you meet it.

**Workflows** — see [Workflows](../guides/workflows.md).

| Name | What it is |
|---|---|
| `WorkflowDefinition` | A validated workflow template: `name`, `description`, `phases`, `inputs`, `when_to_use`, `briefing`, `model`, `default_agent_type`. |
| `WorkflowRun` | Serializable snapshot of one whole run: `id`, `name`, `phases`, `status`, timings, `log_lines`, `resumed_from`. |
| `AgentRun` | One child-agent execution inside a phase — status, usage, cost, turns, recent activity. |
| `discover_workflow_definitions(cwd=None, *, errors=None)` | Built-ins plus every `workflows/*.md` under the user and project directories. |
| `WorkflowError(code, message)` | Typed control-plane error; `code` is a stable string such as `not_found`. |
| `WorkflowDefinitionError(message, errors=())` | A definition failed to parse or is structurally invalid; `errors` lists the reasons. |

**Sub-agents** — see [Sub-agents](../guides/sub-agents.md).

| Name | What it is |
|---|---|
| `AgentType` | A selectable sub-agent persona for the `task` tool: `name`, `description`, `system_prompt`, `tools`, `model`, `max_steps`, `source`. |

**Content and errors**

| Name | What it is |
|---|---|
| `ImageBlock(source=...)` | Image content in a message. `source` follows the Anthropic shape; adapters translate per provider. |
| `ResponseFormatError` | Raised when `response_format` is malformed or unsupported on the active provider. |

**Tracing** — pass a `Tracer` as the `tracer` option.

| Name | What it is |
|---|---|
| `Tracer` | Protocol for anything the agent loop calls to record spans. Implement it, or use `InMemoryTracer` in tests. |
| `Span` | One unit of work: `name`, ids, `start_ns`/`end_ns`, `attributes`, `status`, `exception`. |

```python
from mantis_agent import InMemoryTracer

tracer = InMemoryTracer()
options = {"model": "mock", "backend": "mock", "tracer": tracer}
# after a run, tracer.spans holds the recorded Span objects
```

## Versioning

Public surface follows semver from 1.0. Until then:

- Names listed in `mantis_agent.__all__` are stable across patch
  versions.
- Anything imported from submodules (`mantis_agent.streaming.*`,
  `mantis_agent.providers.*`) is implementation detail and may move
  freely.
- Settings file schema and JSONL transcript format are stable across
  minor versions even today.
