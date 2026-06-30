"""Typed exceptions.

Everything user-facing inherits from ``AgentError``. Provider-specific errors
get wrapped in ``ProviderError`` with the original payload preserved on
``.raw`` so callers can introspect without parsing strings.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base for every error this SDK raises."""


class ProviderError(AgentError):
    """A provider API returned an error response."""

    __slots__ = ("status_code", "raw")

    def __init__(self, message: str, *, status_code: int | None = None, raw: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.raw = raw


class RateLimitError(ProviderError):
    """HTTP 429. Carries ``retry_after_s`` when the provider returned a hint."""

    __slots__ = ("retry_after_s",)

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        retry_after_s: float | None = None,
        raw: Any = None,
    ):
        super().__init__(message, status_code=status_code, raw=raw)
        self.retry_after_s = retry_after_s


class AuthError(ProviderError):
    """HTTP 401/403."""


class ToolExecutionError(AgentError):
    """A tool raised during dispatch. ``tool_name`` and ``tool_use_id`` let the
    agent loop format a proper ``tool_result`` block with ``is_error=True``."""

    __slots__ = ("tool_name", "tool_use_id", "cause")

    def __init__(self, tool_name: str, tool_use_id: str, cause: BaseException):
        super().__init__(f"tool {tool_name!r} raised: {cause!r}")
        self.tool_name = tool_name
        self.tool_use_id = tool_use_id
        self.cause = cause


class StreamProtocolError(AgentError):
    """The provider's stream broke framing (bad SSE, malformed JSON, etc.)."""


class BudgetExceededError(AgentError):
    """A configured budget (turns, tokens, USD) was exceeded."""

    __slots__ = ("kind", "limit", "used")

    def __init__(self, kind: str, limit: float, used: float):
        super().__init__(f"budget {kind} exceeded: used {used} >= limit {limit}")
        self.kind = kind
        self.limit = limit
        self.used = used


class PermissionDeniedError(AgentError):
    """``canUseTool`` denied a tool call, or a rule blocked it.

    The agent loop catches this and turns it into a ``tool_result``
    block with ``is_error=True``; it's a hard error only when raised
    outside the dispatch context.
    """

    __slots__ = ("tool_name", "tool_use_id", "reason")

    def __init__(self, tool_name: str, tool_use_id: str, reason: str):
        super().__init__(f"permission denied for tool {tool_name!r}: {reason}")
        self.tool_name = tool_name
        self.tool_use_id = tool_use_id
        self.reason = reason


class CapabilityError(AgentError):
    """A required capability isn't available on the (model, backend) pair."""


class TemplateError(AgentError):
    """Chat template rendering failed (missing variables, bad template, ...)."""
