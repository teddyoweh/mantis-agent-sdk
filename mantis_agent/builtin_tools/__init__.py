"""Built-in tools shipped with mantis-agent-sdk.

These are the SDK's equivalent of Claude Code's built-in tools (WebSearch,
WebFetch, etc.) — same names and signatures so a Spawn workflow can swap
``claude_agent_sdk`` for ``mantis_agent`` without code changes.

Importing this module is free; provider-specific HTTP clients are built
lazily on first call.
"""

from __future__ import annotations

from .fs import (
    CODING_TOOLS,
    bash,
    edit_file,
    glob,
    grep,
    ls,
    monitor,
    multi_edit,
    read_file,
    write_file,
)
from .web import WebFetch, WebSearch, aclose_builtin_clients, web_fetch, web_search

__all__ = [
    "CODING_TOOLS",
    "WebFetch",
    "WebSearch",
    "aclose_builtin_clients",
    "bash",
    "edit_file",
    "glob",
    "grep",
    "ls",
    "monitor",
    "multi_edit",
    "read_file",
    "web_fetch",
    "web_search",
    "write_file",
]
