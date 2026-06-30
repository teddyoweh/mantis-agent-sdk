# API reference

The public surface — what you can import from `mantis_agent` and rely on
across minor versions.

## Top-level imports

```python
from mantis_agent import (
    # Core
    query,
    ClaudeSDKClient,
    ClaudeAgentOptions,
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
- [ClaudeAgentOptions](options.md) — every option, with defaults.
- [Message types](messages.md) — flat-shape vs. internal shape.
- [Tools](tools.md) — `@tool`, registries, built-ins.
- [Errors](errors.md) — full error hierarchy.
- [Sessions](sessions.md) — `Session`, `Checkpoint`, fork, resume, stores.

## Versioning

Public surface follows semver from 1.0. Until then:

- Names listed in `mantis_agent.__all__` are stable across patch
  versions.
- Anything imported from submodules (`mantis_agent.streaming.*`,
  `mantis_agent.providers.*`) is implementation detail and may move
  freely.
- Settings file schema and JSONL transcript format are stable across
  minor versions even today.
