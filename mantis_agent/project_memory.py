"""``MANTIS.md`` — the layered instruction-memory hierarchy, a faithful port of
Claude Code's ``CLAUDE.md`` system (see ``utils/claudemd.ts``) with mantis
naming.

Four tiers are discovered and concatenated into the system prompt, lowest to
highest precedence (later tiers appear later, so they win on conflict):

1. **Managed** — ``/etc/mantis-agent/MANTIS.md``: org-wide policy for all users.
2. **User** — ``~/.mantis-agent/MANTIS.md``: your private instructions, every
   project. (The separate auto-memory ``MEMORY.md`` index is handled by
   :mod:`mantis_agent.memory`; this is the human-authored always-on file.)
3. **Project** — walking UP from the cwd to the filesystem root, each directory
   is checked for ``MANTIS.md``, ``.mantis/MANTIS.md`` and ``.mantis/rules/*.md``.
   These are checked into the repo and shared with the team.
4. **Local** — ``MANTIS.local.md`` (gitignored): private per-project notes.

Any file may pull in others with ``@path`` imports (``@./notes.md``,
``@~/shared.md``, ``@/abs/path.md``) — resolved relative to the importing file,
recursive up to ``MAX_IMPORT_DEPTH``, deduplicated, and skipped inside fenced
code blocks. Imports are emitted right after their parent.

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .paths import get_mantis_agent_dir

# File / directory names — the mantis analogues of CLAUDE.md / .claude.
PROJECT_FILE = "MANTIS.md"
# AGENTS.md — the cross-tool convention (Claude Code, others). Loaded natively
# at the same tier/precedence as MANTIS.md so a project's existing AGENTS.md is
# picked up with no config.
AGENTS_FILE = "AGENTS.md"
LOCAL_FILE = "MANTIS.local.md"
WORKSPACE_DIR = ".mantis"
RULES_SUBDIR = "rules"
MANAGED_PATH = Path("/etc/mantis-agent") / PROJECT_FILE

MAX_IMPORT_DEPTH = 5
_MAX_FILE_BYTES = 100_000  # don't slurp a giant file into the prompt

# Tiers whose files are authored by the machine owner / admin and are NOT
# delivered via an untrusted repo clone. Only these may import absolute or
# home-relative (``@/abs`` / ``@~/...``) paths. Project/local tier files ship in
# (or alongside) the repo, so allowing them to import ``@~/.ssh/id_rsa`` would
# let a cloned repo exfiltrate arbitrary files into the model prompt.
_TRUSTED_IMPORT_TIERS = frozenset({"user", "managed"})

# ``@path`` — leading boundary, then a path that allows ``\ ``-escaped spaces.
_IMPORT_RE = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")


@dataclass(frozen=True)
class MemoryFile:
    path: Path
    tier: str  # 'managed' | 'user' | 'project' | 'local'
    content: str
    parent: Path | None = None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _walk_up(start: Path) -> list[Path]:
    """Directories from ``start`` up to the filesystem root (inclusive)."""
    dirs: list[Path] = []
    cur = start.resolve()
    while True:
        dirs.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return dirs


def _project_candidates(cwd: Path) -> list[Path]:
    """Project-memory files, ordered ROOT→cwd so the most-specific (deepest)
    file is emitted last and therefore wins."""
    out: list[Path] = []
    for d in reversed(_walk_up(cwd)):  # root first, cwd last
        out.append(d / PROJECT_FILE)
        out.append(d / AGENTS_FILE)  # cross-tool convention, same tier
        out.append(d / WORKSPACE_DIR / PROJECT_FILE)
        rules = d / WORKSPACE_DIR / RULES_SUBDIR
        if rules.is_dir():
            from .rules import rule_file_has_globs  # noqa: PLC0415

            # Only UNCONDITIONAL rules load always; path-scoped ones (with globs)
            # are injected per-turn when a matching file is active (see rules.py).
            out.extend(f for f in sorted(rules.glob("*.md")) if not rule_file_has_globs(f))
    return out


def _local_candidates(cwd: Path) -> list[Path]:
    return [d / LOCAL_FILE for d in reversed(_walk_up(cwd))]


# ---------------------------------------------------------------------------
# @import expansion
# ---------------------------------------------------------------------------


def _within(path: Path, base_dir: Path) -> bool:
    """True if ``path`` resolves inside ``base_dir`` (blocks ``../`` escapes)."""
    try:
        path.resolve().relative_to(base_dir.resolve())
        return True
    except (ValueError, OSError):
        return False


def _expand_path(raw: str, base_dir: Path, tier: str) -> Path | None:
    raw = raw.split("#", 1)[0].replace("\\ ", " ").strip()
    if not raw:
        return None
    trusted = tier in _TRUSTED_IMPORT_TIERS
    if raw.startswith("~/"):
        # Home-relative import: only owner/admin tiers. A repo-shipped file that
        # could pull in ~/.ssh/id_rsa is an exfiltration vector — reject it.
        return Path(raw).expanduser() if trusted else None
    if raw.startswith("/"):
        # Absolute import: same restriction as home-relative.
        return Path(raw) if trusted else None
    if raw.startswith("./") or raw.startswith("../"):
        p = (base_dir / raw).resolve()
        if not trusted and not _within(p, base_dir):
            return None  # untrusted tier may not escape its own directory tree
        return p
    # Bare relative path (e.g. "notes/foo.md") — must look path-like.
    if re.match(r"^[a-zA-Z0-9._-]", raw):
        p = (base_dir / raw).resolve()
        if not trusted and not _within(p, base_dir):
            return None
        return p
    return None


def _extract_imports(content: str, base_dir: Path, tier: str) -> list[Path]:
    """Resolve ``@path`` imports in ``content``, skipping fenced code blocks."""
    out: list[Path] = []
    in_fence = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _IMPORT_RE.finditer(line):
            p = _expand_path(m.group(1), base_dir, tier)
            if p is not None:
                out.append(p)
    return out


def _read(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > _MAX_FILE_BYTES:
            # Over the cap: read only the first _MAX_FILE_BYTES BYTES rather than
            # slurping a potentially huge (e.g. multi-GB) file into RAM just to
            # slice it. Byte-bounded read matches the byte-measured st_size guard;
            # "replace" absorbs a multibyte char split at the boundary.
            with path.open("rb") as fh:
                return fh.read(_MAX_FILE_BYTES).decode("utf-8", "replace")
        return path.read_text("utf-8", "replace")
    except OSError:
        return None


def _process_file(
    path: Path,
    tier: str,
    seen: set[str],
    out: list[MemoryFile],
    *,
    depth: int = 0,
    parent: Path | None = None,
) -> None:
    """Read ``path`` (if present & unseen), append it, then recurse into its
    ``@import``s. Depth-limited and deduped (port of ``processMemoryFile``)."""
    key = str(path.resolve()) if path.exists() else str(path)
    if key in seen or depth >= MAX_IMPORT_DEPTH:
        return
    content = _read(path)
    if content is None or not content.strip():
        return
    seen.add(key)
    out.append(MemoryFile(path=path, tier=tier, content=content.strip(), parent=parent))
    for imp in _extract_imports(content, path.parent, tier):
        _process_file(imp, tier, seen, out, depth=depth + 1, parent=path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_memory_files(cwd: str | Path | None = None) -> list[MemoryFile]:
    """Discover + load every memory file (with imports expanded), in precedence
    order: managed → user → project (root→cwd) → local."""
    base = Path(cwd).resolve() if cwd else Path.cwd()
    seen: set[str] = set()
    out: list[MemoryFile] = []

    _process_file(MANAGED_PATH, "managed", seen, out)
    _process_file(get_mantis_agent_dir() / PROJECT_FILE, "user", seen, out)
    for p in _project_candidates(base):
        _process_file(p, "project", seen, out)
    for p in _local_candidates(base):
        _process_file(p, "local", seen, out)
    return out


_TIER_LABEL = {
    "managed": "organization policy",
    "user": "your personal instructions, all projects",
    "project": "project instructions (checked into the repo)",
    "local": "private project notes",
}


def render_memory_prompt(cwd: str | Path | None = None) -> str:
    """Build the labeled memory block for the system prompt, or "" if no memory
    files exist. Each file is shown with its source path + tier so the model
    knows where an instruction came from and how authoritative it is."""
    files = load_memory_files(cwd)
    if not files:
        return ""
    home = Path.home()

    def short(p: Path) -> str:
        try:
            return "~/" + str(p.relative_to(home))
        except ValueError:
            return str(p)

    parts = [
        "The following instruction-memory files are in effect. Treat them as "
        "standing instructions from the user; more-specific (later) files win on "
        "conflict."
    ]
    for f in files:
        origin = f"imported by {short(f.parent)}" if f.parent else _TIER_LABEL.get(f.tier, f.tier)
        parts.append(f"\n# {short(f.path)}  ({origin})\n{f.content}")
    return "\n".join(parts)


__all__ = [
    "LOCAL_FILE",
    "MANAGED_PATH",
    "MAX_IMPORT_DEPTH",
    "MemoryFile",
    "PROJECT_FILE",
    "WORKSPACE_DIR",
    "load_memory_files",
    "render_memory_prompt",
]
