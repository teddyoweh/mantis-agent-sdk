"""Memory — the ``~/.mantis-agent/MEMORY.md`` tree.

Mirrors Claude Code's ``~/.claude/memory/`` system 1:1:

* ``MEMORY.md`` at the root is the index. One line per entry. Loaded into
  the agent's context every session (cap ~150 lines so the overhead is
  bounded).
* Each entry is a markdown file with a YAML frontmatter block carrying:
    name, description, type ∈ {user, feedback, project, reference}
* Once a topic accumulates 3+ entries at the root, it gets *promoted*:
  create ``memory/{topic}/INDEX.md``, move the entries in, replace the
  root pointer with one line pointing at the subdir.
* Sub-INDEX.md uses the same format, allowing arbitrary tree depth.

Entry shape::

    ---
    name: Short title
    description: One-line hook — used to decide relevance later
    type: project
    ---

    Body of the memory. Markdown. Tight. Lead with the rule or fact.

This module is **side-effect free** until you call a function. We do
NOT auto-create the directory on import — explicit ``ensure_memory_dir``
or any save_* function creates it on first use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .paths import ensure_dir, get_memory_dir, get_memory_index

__all__ = [
    "MemoryEntry",
    "ensure_memory_dir",
    "list_memory_entries",
    "load_memory_entry",
    "load_memory_index",
    "save_memory_entry",
    "update_memory_index",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


MemoryType = Literal["user", "feedback", "project", "reference"]


@dataclass(slots=True)
class MemoryEntry:
    """One memory file. ``slug`` is the on-disk filename without ``.md``.

    The body is plain markdown. We don't parse it; the agent just renders
    the raw text into its context when this entry is relevant.
    """

    slug: str
    name: str
    description: str
    type: MemoryType = "project"
    body: str = ""
    path: Path | None = None  # set on load, None on freshly-constructed

    def to_markdown(self) -> str:
        """Render to the canonical on-disk format."""

        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"type: {self.type}\n"
            "---\n"
            "\n"
            f"{self.body.rstrip()}\n"
        )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def ensure_memory_dir() -> Path:
    """Create ``~/.mantis-agent/memory/`` if missing. Returns the path."""
    return ensure_dir(get_memory_dir())


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL
)


def _parse_entry(text: str, *, slug: str, path: Path) -> MemoryEntry:
    """Split the YAML-ish frontmatter from the body. Tolerant of missing
    keys — defaults to ``type='project'`` and empty description.

    We don't import a YAML parser for three keys; a regex over the
    frontmatter block is fast and dependency-free.
    """

    m = _FRONTMATTER_RE.match(text)
    if not m:
        # Body-only file — treat the whole thing as body, slug as name.
        return MemoryEntry(
            slug=slug,
            name=slug.replace("_", " ").replace("-", " ").title(),
            description="",
            type="project",
            body=text,
            path=path,
        )
    front, body = m.groups()
    kv: dict[str, str] = {}
    for line in front.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        kv[k.strip().lower()] = v.strip()
    type_raw = kv.get("type", "project").lower()
    type_val: MemoryType = type_raw if type_raw in ("user", "feedback", "project", "reference") else "project"  # type: ignore[assignment]
    return MemoryEntry(
        slug=slug,
        name=kv.get("name", slug),
        description=kv.get("description", ""),
        type=type_val,
        body=body.strip("\n"),
        path=path,
    )


def load_memory_entry(slug: str) -> MemoryEntry | None:
    """Load one entry by filename slug (without ``.md``). Searches the
    flat ``memory/`` root only — for nested topics, the caller passes
    ``"topic/slug"`` or walks via :func:`list_memory_entries`.
    """

    base = get_memory_dir()
    path = base / f"{slug}.md"
    if not path.exists():
        # Allow nested form: "topic/slug"
        path = base / f"{slug}.md"
        if not path.exists():
            return None
    return _parse_entry(path.read_text(encoding="utf-8"), slug=slug, path=path)


def list_memory_entries(*, recursive: bool = True) -> list[MemoryEntry]:
    """Return every memory entry under ``memory/``.

    Skips ``INDEX.md`` and ``MEMORY.md`` (those are indexes, not entries).
    Returns them in alphabetical order (slug) for stable test output;
    callers can re-sort by `name`/`type`/etc. as needed.
    """

    base = get_memory_dir()
    if not base.exists():
        return []
    out: list[MemoryEntry] = []
    iterator = base.rglob("*.md") if recursive else base.glob("*.md")
    for path in sorted(iterator):
        if path.name in ("INDEX.md", "MEMORY.md"):
            continue
        rel = path.relative_to(base).with_suffix("")
        slug = str(rel).replace("\\", "/")
        try:
            out.append(_parse_entry(path.read_text(encoding="utf-8"), slug=slug, path=path))
        except OSError:
            continue
    return out


def save_memory_entry(entry: MemoryEntry) -> Path:
    """Write the entry to disk, creating the directory if needed.

    Returns the resolved path. If ``entry.slug`` includes a ``/``, nested
    directories are created (e.g. ``stripe/onboarding``).
    """

    ensure_memory_dir()
    path = get_memory_dir() / f"{entry.slug}.md"
    ensure_dir(path.parent)
    path.write_text(entry.to_markdown(), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IndexLine:
    title: str
    path: str  # relative to memory/
    hook: str = ""

    def render(self) -> str:
        if self.hook:
            return f"- [{self.title}]({self.path}) — {self.hook}"
        return f"- [{self.title}]({self.path})"


def load_memory_index() -> str:
    """Return the raw text of ``MEMORY.md``, or an empty string if missing.

    Loaded once per session and injected into the agent's context. We don't
    parse it — the agent reads it as markdown and decides which entries
    are relevant to look up via :func:`load_memory_entry`.
    """

    path = get_memory_index()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def update_memory_index(
    *,
    lines: list[IndexLine] | None = None,
    header: str = "# Memory index\n\nOne-line entries — read the linked file for the full body.\n\n",
) -> Path:
    """Rewrite ``MEMORY.md`` from a list of index lines.

    Convenience for callers that want to programmatically regenerate the
    index after adding/removing entries. Users editing manually are
    expected to keep the file under ~150 lines — we don't enforce a cap
    here, just document it.
    """

    ensure_memory_dir()
    if lines is None:
        # Auto-derive from entries on disk.
        entries = list_memory_entries(recursive=True)
        lines = [
            IndexLine(title=e.name, path=f"memory/{e.slug}.md", hook=e.description)
            for e in entries
        ]
    body = "".join(line.render() + "\n" for line in lines)
    path = get_memory_index()
    path.write_text(header + body, encoding="utf-8")
    return path
