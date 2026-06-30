"""Built-in tools shipped with mantis-agent-sdk.

These are the SDK's equivalent of Claude Code's built-in tools (WebSearch,
WebFetch, etc.) — same names and signatures so a Spawn workflow can swap
``claude_agent_sdk`` for ``mantis_agent`` without code changes.

Importing this module is free; provider-specific HTTP clients are built
lazily on first call.
"""

from __future__ import annotations

from .web import WebFetch, WebSearch, aclose_builtin_clients, web_fetch, web_search

__all__ = [
    "WebFetch",
    "WebSearch",
    "aclose_builtin_clients",
    "web_fetch",
    "web_search",
]
