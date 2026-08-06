"""Trust gate for project-level skills — the ``.mcp.json`` gate, for prompts.

A skill under ``<cwd>/.mantis/skills/<slug>/SKILL.md`` is *repository* data: it
arrives with a `git clone`, nobody reviews it, and :func:`discover_skills` feeds
its frontmatter — and, once loaded, its whole body — straight into the SYSTEM
PROMPT. That makes a project skill a strictly higher-value injection target than
a project MCP server: a server still has to get past the permission prompt
before it does anything, while a skill body is simply *believed*.

``mcp/manager.py`` already fails closed on the same threat (see the
``project_mcp_is_trusted`` block there), so this module mirrors that design
deliberately rather than inventing a second one:

* trust is recorded in a JSON map under the mantis agent dir, keyed by the
  absolute project skills directory;
* the value is a CONTENT digest, so editing — or adding, or removing — a skill
  file re-arms the gate and the user is asked again;
* ``MANTIS_SKILLS_TRUST_PROJECT`` is the automation/CI opt-out, matching
  ``MANTIS_MCP_TRUST_PROJECT``.

User-level skills (``~/.mantis-agent/skills``) are never gated — they're the
user's own files, same as the user-level ``mcp.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover — import cycle: skills.py imports us back
    from .skills import Skill

__all__ = [
    "PROJECT_SOURCE",
    "USER_SOURCE",
    "filter_untrusted_project_skills",
    "project_skills_dir",
    "project_skills_digest",
    "project_skills_trusted",
    "skill_file_hash",
    "trust_project_skills",
    "withheld_project_skills",
]

# Mirrors mcp/manager.py's ``_TRUST_ENV``.
_TRUST_ENV = "MANTIS_SKILLS_TRUST_PROJECT"

#: Values of ``Skill.source``. Only ``PROJECT_SOURCE`` is gated.
USER_SOURCE = "user"
PROJECT_SOURCE = "project"


def _skills_trust_path() -> Path:
    from .paths import get_mantis_agent_dir  # noqa: PLC0415

    return get_mantis_agent_dir() / "skills_trust.json"


def project_skills_dir(cwd: str | Path | None = None) -> Path:
    """Absolute ``<cwd>/.mantis/skills`` — the untrusted layer, and the key the
    trust store is written under (resolved, so ``.``/symlinks can't produce two
    different records for one directory)."""
    from .skills import SKILLS_SUBDIR  # noqa: PLC0415

    base = Path(cwd) if cwd is not None else Path.cwd()
    d = base / ".mantis" / SKILLS_SUBDIR
    try:
        return d.resolve()
    except OSError:  # pragma: no cover — unresolvable path; the raw one still keys fine
        return d.absolute()


def _project_skill_files(cwd: str | Path | None = None) -> list[Path]:
    """Every project ``SKILL.md``, in a stable order (the digest depends on it)."""
    d = project_skills_dir(cwd)
    try:
        if not d.is_dir():
            return []
        return sorted(d.glob("*/SKILL.md"))
    except OSError:
        return []


def skill_file_hash(path: str | Path) -> str | None:
    """SHA-256 of one skill file, or ``None`` if it can't be read.

    The counterpart of ``mcp/manager.py::_file_hash``; public here because the
    unit of trust is a *directory of files*, so callers (and the digest below)
    need the per-file value."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def project_skills_digest(cwd: str | Path | None = None) -> str | None:
    """One digest over every project skill, or ``None`` when there are none.

    Covers each file's path *and* content, so all three ways a repo can change
    what lands in the system prompt — editing a body, adding a skill, deleting
    one — produce a different digest and re-arm the gate."""
    files = _project_skill_files(cwd)
    if not files:
        return None
    root = project_skills_dir(cwd)
    h = hashlib.sha256()
    for f in files:
        fh = skill_file_hash(f)
        if fh is None:
            # Unreadable now — record the path anyway. If it becomes readable
            # later the digest changes, which is the fail-closed direction.
            fh = "-"
        try:
            rel = f.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover — glob() results are always under root
            rel = f.name
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(fh.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def project_skills_trusted(cwd: str | Path | None = None) -> bool:
    """True if the project skills at ``cwd`` are trusted (or there are none).

    Honors the ``MANTIS_SKILLS_TRUST_PROJECT`` opt-out env for automation/CI."""
    if os.environ.get(_TRUST_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    digest = project_skills_digest(cwd)
    if digest is None:
        return True  # no project skills → nothing untrusted to gate
    try:
        store = json.loads(_skills_trust_path().read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(store, dict) and store.get(str(project_skills_dir(cwd))) == digest


def trust_project_skills(cwd: str | Path | None = None) -> bool:
    """Record trust for the CURRENT content of ``<cwd>/.mantis/skills``.

    Returns False if there are no project skills to trust (so a UI can say
    "nothing to trust here" instead of silently writing a useless record)."""
    digest = project_skills_digest(cwd)
    if digest is None:
        return False
    path = _skills_trust_path()
    try:
        store = json.loads(path.read_text("utf-8"))
        if not isinstance(store, dict):
            store = {}
    except (OSError, ValueError):
        store = {}
    store[str(project_skills_dir(cwd))] = digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2), "utf-8")
    return True


def filter_untrusted_project_skills(
    skills: Sequence[Skill], cwd: str | Path | None = None
) -> tuple[list[Skill], list[Skill]]:
    """Split ``skills`` into ``(kept, withheld)`` by the project trust gate.

    Project-tier skills (``Skill.source == "project"``) are withheld until the
    user trusts that exact set of files; everything else passes through. When
    the project is trusted — or ships no skills at all — nothing is withheld."""
    if project_skills_trusted(cwd):
        return list(skills), []
    kept: list[Skill] = []
    withheld: list[Skill] = []
    for s in skills:
        (withheld if getattr(s, "source", USER_SOURCE) == PROJECT_SOURCE else kept).append(s)
    return kept, withheld


def withheld_project_skills(cwd: str | Path | None = None) -> list[Skill]:
    """The project skills currently being withheld, so the UI can tell the user
    what a ``trust`` would let in. Empty when the project is trusted.

    Parses the same files :func:`~mantis_agent.skills.discover_skills` would,
    but deliberately does NOT go through it — discovery drops these on purpose,
    and this is the one place that needs to look at them anyway."""
    from .skills import Skill, _parse_skill_md  # noqa: PLC0415

    if project_skills_trusted(cwd):
        return []
    out: list[Skill] = []
    for md in _project_skill_files(cwd):
        try:
            meta, body = _parse_skill_md(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        name = (meta.get("name") or md.parent.name).strip()
        if not name:
            continue
        out.append(
            Skill(
                name=name,
                description=meta.get("description", ""),
                body=body,
                category=meta.get("category"),
                source=PROJECT_SOURCE,
            )
        )
    return out
