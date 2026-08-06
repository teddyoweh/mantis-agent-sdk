"""Compact one-line previews of a tool call and what it returned.

The live-agent inspector used to render a subagent's activity as the bare tool
name::

    recent #1
      - 0s ago · tool read_file
      - 0s ago · tool read_file
      - 0s ago · tool grep

Five identical lines that say nothing: not which file, not which pattern, not
whether anything came back. What you actually want while watching an agent
work is the *shape* of what it is doing::

    recent #1
      - 0s ago · Search "def _build_payload" → 12 matches
      - 2s ago · Read mantis_agent/tui.py → 4157 lines

``TOOL_VERBS`` lives here rather than in ``tui`` so the SDK-level subagent
wrapper can label a call the same way the transcript does, without importing
the terminal.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "TOOL_VERBS",
    "tool_arg_preview",
    "tool_call_preview",
    "tool_result_preview",
]

# name -> (display verb, argument keys in priority order). The first key that
# carries a value becomes the target shown next to the verb.
TOOL_VERBS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bash": ("Run", ("command",)),
    "read_file": ("Read", ("path", "file_path")),
    "write_file": ("Write", ("path", "file_path")),
    "edit_file": ("Edit", ("path", "file_path")),
    "multi_edit": ("Edit", ("path", "file_path")),
    "ls": ("List", ("path",)),
    "glob": ("Find", ("pattern", "path")),
    "grep": ("Search", ("pattern", "query")),
    "web_search": ("Search web", ("query",)),
    "web_fetch": ("Fetch", ("url",)),
    "todo_write": ("Plan", ()),
    "task": ("Delegate", ("description", "prompt")),
    "lsp": ("Look up", ("symbol",)),
    "notebook_edit": ("Edit cell", ("path", "file_path")),
    "remember": ("Remember", ("name",)),
    "load_skill": ("Load skill", ("name", "skill")),
    "ask_user_question": ("Ask", ()),
    "exit_plan_mode": ("Present plan", ()),
    "bash_output": ("Check output", ("bash_id", "id")),
    "bash_kill": ("Kill", ("bash_id",)),
    "monitor": ("Wait for", ("until_pattern", "path", "port", "bash_id")),
    "watch": ("Watch", ("description", "command")),
    "watch_stop": ("Stop watch", ("job_id",)),
    "pair": ("Confer with", ("peer",)),
    "consult_advisor": ("Consult", ("question",)),
}

_FALLBACK_KEYS = ("path", "file_path", "command", "query", "pattern", "url", "name")

# Tools whose output is a list of things worth counting rather than reading.
_COUNT_UNITS = {
    "grep": "match",
    "glob": "file",
    "ls": "entry",
    "web_search": "result",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())     # collapse newlines/runs of spaces
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _count(n: int, unit: str) -> str:
    """``3 matches`` and ``0 entries`` — not ``3 matchs`` / ``0 entrys``."""
    if n == 1:
        return f"1 {unit}"
    if unit.endswith("y") and len(unit) > 1 and unit[-2] not in "aeiou":
        return f"{n} {unit[:-1]}ies"
    if unit.endswith(("ch", "sh", "s", "x", "z")):
        return f"{n} {unit}es"
    return f"{n} {unit}s"


def tool_arg_preview(name: str, args: dict[str, Any] | None, limit: int = 48) -> str:
    """The salient argument of a call — the file, the pattern, the command."""
    if not args:
        return ""
    _verb, keys = TOOL_VERBS.get(name, (name, _FALLBACK_KEYS))
    for key in (*keys, *_FALLBACK_KEYS):
        val = args.get(key)
        if val:
            return _clip(val if isinstance(val, str) else json.dumps(val), limit)
    return ""


def tool_result_preview(name: str, result: Any, limit: int = 40) -> str:
    """What came back, in a few words.

    Deliberately a SHAPE, not the content: watching an agent you want to know
    that a grep found 12 things or that a read returned 4157 lines, not to have
    the panel flooded with the payload itself.
    """
    if result is None:
        return ""
    if isinstance(result, bool):
        return "ok" if result else "failed"
    if isinstance(result, (list, tuple)):
        return _count(len(result), _COUNT_UNITS.get(name, "item"))
    if isinstance(result, dict):
        return _clip(json.dumps(result), limit)
    if not isinstance(result, str):
        return _clip(str(result), limit)

    text = result.strip()
    if not text:
        return "empty"
    # An error is the single most useful thing to surface, so say so plainly
    # rather than letting it read as an ordinary result.
    low = text[:80].lower()
    if low.startswith(("error", "traceback", "failed", "no such file",
                       "permission denied")):
        return _clip(text, limit)
    lines = text.count("\n") + 1
    unit = _COUNT_UNITS.get(name)
    if unit is not None:
        return _count(lines, unit)
    if lines > 1:
        return f"{lines} lines"
    return _clip(text, limit)


def tool_call_preview(
    name: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    *,
    done: bool = False,
    limit: int = 48,
) -> str:
    """One line for a tool call: ``Search "pattern" → 12 matches``.

    Before the call returns (``done=False``) the arrow is omitted, so a slow
    grep reads as in-flight rather than as having returned nothing.
    """
    verb, _keys = TOOL_VERBS.get(name, (name, _FALLBACK_KEYS))
    target = tool_arg_preview(name, args, limit=limit)
    head = f"{verb} {target}".rstrip()
    if not done:
        return head
    tail = tool_result_preview(name, result)
    return f"{head} → {tail}" if tail else head
