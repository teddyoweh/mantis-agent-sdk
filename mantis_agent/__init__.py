"""mantis-agent-sdk — Claude Code for open-source models.

The semver-stable public API is the set of names exported in ``__all__``.
Anything else (including names imported at the top of this module but not
listed in ``__all__``) is implementation detail and may change between
minor releases. See ``SEMVER.md`` for the full policy.
"""

from .agent import Agent
from .builtin_tools import WebFetch, WebSearch, web_fetch, web_search
from .capabilities import (
    BackendCapability,
    ModelCapability,
    ToolUsePath,
    lookup_model,
    resolve_tool_use_path,
)
from .errors import (
    AgentError,
    AuthError,
    BudgetExceededError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    StreamProtocolError,
    ToolExecutionError,
)
from .events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
)
from .claude_compat import (
    AgentDefinition,
    CLIConnectionError,
    MantisAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookContext as ClaudeHookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionResult as ClaudePermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    Plugin,
    ResultMessage,
    ToolPermissionContext,
    create_sdk_mcp_server,
)
# Pull SystemMessage from claude_compat as the canonical top-level name —
# this is the flat-shape version with .subtype + .data that Claude SDK
# examples use. Our internal SystemMessage (no subtype) is still
# available via `from mantis_agent.types import SystemMessage as
# InternalSystemMessage` if needed.
from .claude_compat import SystemMessage  # type: ignore[assignment]  # overrides earlier import
from .memory import (
    MemoryEntry,
    list_memory_entries,
    load_memory_entry,
    load_memory_index,
    save_memory_entry,
    update_memory_index,
)
from .paths import (
    get_mantis_agent_dir,
    get_memory_dir,
    get_memory_index,
    get_sessions_dir,
    get_session_path,
)
from .settings import (
    KNOWN_SETTING_KEYS,
    SETTING_SOURCES,
    apply_settings_to_options,
    load_setting_source,
    load_settings,
    merge_settings,
    resolve_setting_path,
    save_setting_source,
    update_setting_source,
)
from .query import (
    APIAssistantMessage,
    APIUserMessage,
    SDKAssistantMessage,
    SDKCompactBoundaryMessage,
    SDKMessage,
    SDKPermissionDenial,
    SDKResultMessage,
    SDKStatusMessage,
    SDKSystemMessage,
    SDKUserMessage,
    query,
)
from .system_reminder import (
    build_live_context_block,
    is_system_reminder,
    prepend_user_context,
    render_user_context,
    strip_system_reminders,
    wrap_system_reminder,
)
from .session import (
    Checkpoint,
    InMemorySessionStore,
    Session,
    SessionInfo,
    SessionNotFoundError,
    SessionStore,
    SqliteSessionStore,
    fork_session,
    make_checkpoints,
    resume_session,
)
from .transcripts import (
    JsonlTranscript,
    iter_transcripts,
    read_transcript,
)
from .subagent import (
    IsolationMode,
    SubAgentSpec,
    SubAgentTool,
    WrappedAgentTool,
    as_subagent_tool,
)
from .response_format import (
    ResponseFormatError,
    normalize_response_format,
    translate_response_format,
)
from .tools import Tool, ToolRegistry, tool
from .tracing import (
    InMemoryTracer,
    OTelTracer,
    Span,
    Tracer,
)
from .types import (
    AssistantMessage,
    ContentBlock,
    Message,
    ModelUsage,
    SystemMessage as InternalSystemMessage,  # keep accessible for advanced use
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

# --- public, semver-stable API surface (1.0.0+) -----------------------------
# Everything below is covered by the SemVer guarantee in SEMVER.md.
# Symbols imported above but NOT listed here are implementation detail.
__all__ = [
    # Core
    "Agent",
    "query",
    "tool",
    # Tools
    "Tool",
    "ToolRegistry",
    "WebFetch",
    "WebSearch",
    "web_fetch",
    "web_search",
    # Sub-agents
    "AgentDefinition",
    "IsolationMode",
    "SubAgentSpec",
    "SubAgentTool",
    "WrappedAgentTool",
    "as_subagent_tool",
    # Capabilities / routing
    "BackendCapability",
    "ModelCapability",
    "ToolUsePath",
    "lookup_model",
    "resolve_tool_use_path",
    # Plugins
    "Plugin",
    # Messages — internal flat shapes
    "AssistantMessage",
    "ContentBlock",
    "Message",
    "ModelUsage",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "Usage",
    "UserMessage",
    "SystemMessage",
    # Messages — Claude SDK parity shapes
    "APIAssistantMessage",
    "APIUserMessage",
    "SDKAssistantMessage",
    "SDKCompactBoundaryMessage",
    "SDKMessage",
    "SDKPermissionDenial",
    "SDKResultMessage",
    "SDKStatusMessage",
    "SDKSystemMessage",
    "SDKUserMessage",
    "ResultMessage",
    # Streaming events
    "ContentBlockDelta",
    "ContentBlockStart",
    "ContentBlockStop",
    "InputJsonDelta",
    "MessageDelta",
    "MessageStart",
    "MessageStop",
    "StreamEvent",
    "TextDelta",
    "ThinkingDelta",
    # Claude SDK parity entry points
    "MantisAgentOptions",
    "ClaudeSDKClient",
    "ClaudeSDKError",
    "CLIConnectionError",
    "HookMatcher",
    "HookInput",
    "HookJSONOutput",
    "ClaudeHookContext",
    "ClaudePermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ToolPermissionContext",
    "create_sdk_mcp_server",
    # Sessions
    "Checkpoint",
    "InMemorySessionStore",
    "Session",
    "SessionInfo",
    "SessionNotFoundError",
    "SessionStore",
    "SqliteSessionStore",
    "fork_session",
    "make_checkpoints",
    "resume_session",
    # Structured output
    "ResponseFormatError",
    "normalize_response_format",
    "translate_response_format",
    # Tracing
    "Tracer",
    "Span",
    "InMemoryTracer",
    "OTelTracer",
    # Errors
    "AgentError",
    "AuthError",
    "BudgetExceededError",
    "PermissionDeniedError",
    "ProviderError",
    "RateLimitError",
    "StreamProtocolError",
    "ToolExecutionError",
    # Version
    "__version__",
]


def _detect_version() -> str:
    """Single-source the version from installed package metadata.

    Falls back to a literal so import still works in editable / unbuilt
    checkouts where metadata may be stale. The release workflow always
    builds from pyproject.toml so the published wheel reports the
    correct version through importlib.metadata.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version  # type: ignore[attr-defined]

        return version("mantis-agent-sdk")
    except Exception:  # pragma: no cover - extremely defensive
        return "2.7.0"


__version__ = _detect_version()
