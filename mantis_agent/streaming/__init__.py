"""Streaming subsystem.

Three concerns live here, all of which operate on streams from a provider:

* :class:`StreamingToolExecutor` — dispatch tool calls the moment their
  JSON finalizes mid-stream instead of waiting for ``message_stop``. The
  speed unlock described in ``docs/plan.md`` §5.
* :class:`ToolCallTextParser` — Path B/C state machine that pulls
  ``<tool_call>{...}</tool_call>`` blocks out of a plain-text stream for
  models without native tool-calling.
* :class:`ThinkingParser` — splits inline ``<think>...</think>`` content
  from regular text for reasoning-class models (R1, QwQ, etc.).
"""

from __future__ import annotations

from .executor import CanUseToolFn, StreamingToolExecutor
from .text_tool_parser import ToolCallTextParser
from .thinking_parser import ThinkingParser

__all__ = [
    "CanUseToolFn",
    "StreamingToolExecutor",
    "ThinkingParser",
    "ToolCallTextParser",
]
