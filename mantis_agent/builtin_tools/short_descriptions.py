"""Compact descriptions for the heaviest built-in tools.

Sent instead of the full docstring-derived description when the wire tool list
is built compact (``ToolRegistry.to_wire(compact=True)``) — a small context
window, or a model on the prompt-engineered tool path where every schema char
is prompt text. Parameter docs stay in each tool's schema either way, so these
only need to say what the tool is for and the one rule a model most often
breaks with it.

Kept here, keyed by tool name, rather than as ``description_short=`` on each
``@tool`` so the tool modules stay untouched; :func:`apply_short_descriptions`
attaches them at package import.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..tools import Tool

SHORT_DESCRIPTIONS: dict[str, str] = {
    "bash": (
        "Run a shell command non-interactively (bash -lc, cwd = working dir) and "
        "return stdout+stderr. Prefer read_file/edit_file/write_file/glob/grep "
        "for files and search. Set run_in_background=True for servers/watchers, "
        "then read with bash_output. No interactive prompts/editors: use stdin "
        "or -y; don't background what you need now."
    ),
    "bash_output": "Read a background shell's output so far and whether it has exited.",
    "bash_kill": "Kill a background shell (and its children) started with run_in_background.",
    "monitor": (
        "Block until a background shell prints until_pattern or exits, a path "
        "appears/changes, or a localhost port opens — instead of sleep+check loops."
    ),
    "read_file": (
        "Read a file (text comes back with line-number prefixes that are NOT "
        "file content). Use offset/limit for long files."
    ),
    "write_file": (
        "Create or fully overwrite a file. Prefer edit_file for changes to "
        "existing files; read an existing file before overwriting it."
    ),
    "edit_file": (
        "Replace an exact, unique substring in a file (read it first; no "
        "line-number prefixes in old_string)."
    ),
    "multi_edit": (
        "Apply several exact-substring edits to one file, in order. Read the "
        "file first; exact text without line-number prefixes; all-or-nothing."
    ),
    "ls": "List a directory (directories first).",
    "glob": "Find files by glob pattern (e.g. **/*.py), newest first.",
    "grep": (
        "Search file contents by regex (ripgrep). Set fixed_strings=True for "
        "literal code with regex metacharacters."
    ),
    "sleep": (
        "Wait a fixed number of seconds for something external to progress; "
        "don't poll-loop — prefer monitor."
    ),
    "web_fetch": "Fetch a URL's readable text (truncated to ~5000 chars).",
    "web_search": "Search the web; returns TITLE — URL — SNIPPET lines.",
}


def apply_short_descriptions(tools: Iterable[Tool]) -> None:
    """Attach :data:`SHORT_DESCRIPTIONS` to ``tools`` that don't already carry
    a ``description_short`` of their own.

    Each short is paired with the description it summarizes (``_short_for``):
    a copy whose description is later customised — ``dataclasses.replace(
    bash, description=...)`` — sends that custom description in compact mode
    rather than this now-stale summary."""

    for t in tools:
        if not t.description_short and t.name in SHORT_DESCRIPTIONS:
            t.description_short = SHORT_DESCRIPTIONS[t.name]
            t._short_for = (t.description, t.description_short)
