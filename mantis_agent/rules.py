"""Path-scoped conditional rules — ``.mantis/rules/*.md`` that apply only when a
matching file is in play, so project instructions stay lean.

A rule file may declare ``globs:`` (or ``paths:``) in YAML frontmatter::

    ---
    globs: ["**/*.sql", "db/**"]
    ---
    Use snake_case for all SQL identifiers; never SELECT *.

Such a rule is injected into context ONLY when a file matching one of its globs
is *active* — mentioned in the current turn (an ``@``-mention or a file the agent
just read/edited). A rule with no globs always applies and is left to the normal
project-memory load. This mirrors Claude Code's path-specific instructions.
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

WORKSPACE_DIR = ".mantis"
RULES_SUBDIR = "rules"

# Tool-call argument keys that carry a file path.
_PATH_KEYS = ("path", "file_path", "filename", "notebook_path", "file")
# Require the ``@`` to start a token (not follow a word char) so email
# addresses like ``foo@bar.com`` aren't captured as file mentions.
_MENTION_RE = re.compile(r"(?<![\w@])@([\w./~-]+\.[\w]+)")  # @src/db/schema.sql


def parse_rule_frontmatter(text: str) -> tuple[list[str], str]:
    """Split a rule file into ``(globs, body)``. ``globs`` is empty when the rule
    is unconditional. Supports inline (``globs: ["a","b"]`` / ``globs: a, b``),
    single (``globs: a``), and block-list frontmatter."""
    if not text.startswith("---"):
        return [], text.strip()
    end = text.find("\n---", 3)
    if end == -1:
        return [], text.strip()
    fm = text[3:end].strip()
    body = text[end + 4:].lstrip("\n").strip()

    globs: list[str] = []
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        key, _, val = line.partition(":")
        if key.strip().lower() not in ("globs", "paths", "glob", "path"):
            continue
        val = val.strip()
        if val:  # inline value on the same line
            globs.extend(_split_glob_value(val))
        else:  # block list: subsequent "  - item" lines
            for nxt in lines[i + 1:]:
                m = re.match(r"\s*-\s*(.+)", nxt)
                if not m:
                    break
                globs.extend(_split_glob_value(m.group(1)))
        break
    return [g for g in (g.strip().strip("'\"") for g in globs) if g], body


def _split_glob_value(val: str) -> list[str]:
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        val = val[1:-1]
    return [p.strip().strip("'\"") for p in val.split(",") if p.strip()]


def discover_conditional_rules(cwd: str | Path) -> list[tuple[Path, list[str], str]]:
    """Every ``.mantis/rules/*.md`` WITH globs, walking up from ``cwd``. Returns
    ``(path, globs, body)``. Unconditional rules (no globs) are excluded — they're
    handled by the normal project-memory load."""
    cwd = Path(cwd).expanduser()
    out: list[tuple[Path, list[str], str]] = []
    seen: set[Path] = set()
    cur = cwd.resolve()
    while True:
        rules_dir = cur / WORKSPACE_DIR / RULES_SUBDIR
        if rules_dir.is_dir():
            for f in sorted(rules_dir.glob("*.md")):
                rf = f.resolve()
                if rf in seen:
                    continue
                seen.add(rf)
                try:
                    globs, body = parse_rule_frontmatter(f.read_text("utf-8", "replace"))
                except OSError:
                    continue
                if globs and body:
                    out.append((f, globs, body))
        if cur.parent == cur:
            break
        cur = cur.parent
    return out


def rule_file_has_globs(path: Path) -> bool:
    """True if a rule file declares globs (so the always-on loader can skip it)."""
    try:
        globs, _ = parse_rule_frontmatter(path.read_text("utf-8", "replace"))
    except OSError:
        return False
    return bool(globs)


def active_files_from_messages(messages: list[Any], window: int = 8) -> set[str]:
    """File paths in play in the recent conversation — from tool-call path args
    and ``@``-mentions in user text — over the last ``window`` messages."""
    active: set[str] = set()
    for msg in messages[-window:]:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            active.update(_MENTION_RE.findall(content))
        elif isinstance(content, list):
            for blk in content:
                inp = getattr(blk, "input", None)
                if isinstance(inp, dict):
                    for k in _PATH_KEYS:
                        v = inp.get(k)
                        if isinstance(v, str) and v:
                            active.add(v)
                txt = getattr(blk, "text", None)
                if isinstance(txt, str):
                    active.update(_MENTION_RE.findall(txt))
    return active


def _matches(path: str, glob: str) -> bool:
    p = path.replace("\\", "/")
    g = glob.strip()
    return (
        fnmatch(p, g)
        or fnmatch(os.path.basename(p), g)
        or fnmatch(p, g.replace("**/", "*"))
        or fnmatch(p, "*/" + g)
    )


def select_matching_rules(
    rules: list[tuple[Path, list[str], str]], active_files: set[str]
) -> list[tuple[Path, str]]:
    """Rules whose any glob matches any active file → ``(path, body)``."""
    out: list[tuple[Path, str]] = []
    for path, globs, body in rules:
        if any(_matches(f, g) for f in active_files for g in globs):
            out.append((path, body))
    return out


def render_rules_reminder(bodies: list[str]) -> str:
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    joined = "\n\n".join(bodies)
    return wrap_system_reminder(
        "Path-specific project rules apply to files you're working with now:\n\n"
        + joined
    )
