"""Memory **recall** — surface the right ``~/.mantis-agent/memory/`` topic files
for the current turn. The read side of the memory system (the write side lives
in :mod:`mantis_agent.memory`).

Faithful to Claude Code's ``memdir`` recall (``findRelevantMemories.ts`` +
``memoryScan.ts`` + ``memoryAge.ts``):

1. **Scan** every memory file, reading only its frontmatter, and **mtime-sort
   newest-first, capped at 200** — recency is a prefilter, not a weight.
2. **Select up to 5** relevant to the query. Claude Code asks a cheap model;
   we default to a dependency-free keyword-overlap scorer (so recall works
   fully offline) and accept an optional ``selector`` callable to plug an LLM
   in for parity.
3. **Inject** each selected file wrapped in a ``<system-reminder>`` with an
   age/freshness header — and a staleness CAVEAT for anything older than a day
   ("point-in-time observation… verify against current code") so the model
   doesn't treat a stale note as live fact.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .memory import MemoryEntry, list_memory_entries

MAX_SCANNED = 200       # recency prefilter cap (CC: MAX_MEMORY_FILES)
DEFAULT_LIMIT = 5       # CC surfaces at most 5 per turn
_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are be it this that how do i my me "
    "you your we our can what when where which while at by from as if then".split()
)


@dataclass(frozen=True)
class ScoredMemory:
    entry: MemoryEntry
    mtime: float
    score: float


def _mtime(entry: MemoryEntry) -> float:
    try:
        return entry.path.stat().st_mtime if entry.path else 0.0
    except OSError:
        return 0.0


def scan_memories() -> list[ScoredMemory]:
    """All memory entries, newest-first, capped at ``MAX_SCANNED`` (score 0)."""
    scored = [ScoredMemory(e, _mtime(e), 0.0) for e in list_memory_entries()]
    scored.sort(key=lambda s: s.mtime, reverse=True)
    return scored[:MAX_SCANNED]


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1}


def _keyword_score(query_tokens: set[str], entry: MemoryEntry) -> float:
    """Overlap of query tokens with the memory's name + description + type.
    Description matches weigh more (it's the relevance signal CC's selector
    reads); name matches a bit more still."""
    if not query_tokens:
        return 0.0
    name_t = _tokens(entry.name)
    desc_t = _tokens(entry.description)
    score = 2.0 * len(query_tokens & name_t) + 1.0 * len(query_tokens & desc_t)
    if entry.type in query_tokens:
        score += 0.5
    return score


# A selector takes (query, candidates) and returns the chosen subset, in order.
Selector = Callable[[str, list[ScoredMemory]], list[ScoredMemory]]


def find_relevant_memories(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    already_surfaced: frozenset[str] = frozenset(),
    selector: Selector | None = None,
) -> list[ScoredMemory]:
    """Pick up to ``limit`` memories relevant to ``query``.

    ``already_surfaced`` is a set of absolute paths to skip (so a long session
    doesn't re-surface the same notes). Pass ``selector`` to override the
    built-in keyword scorer with an LLM-backed chooser.
    """
    candidates = [
        s for s in scan_memories()
        if not (s.entry.path and str(s.entry.path) in already_surfaced)
    ]
    if not candidates:
        return []
    if selector is not None:
        return selector(query, candidates)[:limit]

    qt = _tokens(query)
    scored = [
        ScoredMemory(s.entry, s.mtime, _keyword_score(qt, s.entry))
        for s in candidates
    ]
    hits = [s for s in scored if s.score > 0]
    # Highest score first; mtime breaks ties (newer wins).
    hits.sort(key=lambda s: (s.score, s.mtime), reverse=True)
    return hits[:limit]


# ---------------------------------------------------------------------------
# Freshness + injection
# ---------------------------------------------------------------------------


def _age_days(mtime: float) -> int:
    if not mtime:
        return 0
    return max(0, int((datetime.now(timezone.utc).timestamp() - mtime) // 86_400))


def _age_str(mtime: float) -> str:
    d = _age_days(mtime)
    return "today" if d == 0 else ("yesterday" if d == 1 else f"{d} days ago")


def _freshness_caveat(mtime: float) -> str:
    """Staleness warning for memories older than a day (port of
    ``memoryFreshnessNote``); empty for fresh ones."""
    d = _age_days(mtime)
    if d <= 1:
        return ""
    return (
        f"This memory is {d} days old. Memories are point-in-time observations, "
        f"not live state — claims about code behavior or file:line citations may "
        f"be outdated. Verify against current code before asserting as fact."
    )


def render_recalled_memory(scored: ScoredMemory) -> str:
    """One memory as a ``<system-reminder>`` block with an age/freshness header."""
    caveat = _freshness_caveat(scored.mtime)
    path = str(scored.entry.path) if scored.entry.path else scored.entry.slug
    if caveat:
        header = f"{caveat}\n\nMemory: {path}:"
    else:
        header = f"Memory (saved {_age_str(scored.mtime)}): {path}:"
    return f"<system-reminder>\n{header}\n\n{scored.entry.body.strip()}\n</system-reminder>"


def recall_block(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    already_surfaced: frozenset[str] = frozenset(),
    selector: Selector | None = None,
) -> tuple[str, list[str]]:
    """Convenience: find relevant memories for ``query`` and return
    ``(combined_text, surfaced_paths)`` ready to inject as a context reminder.
    ``combined_text`` is "" when nothing is relevant."""
    hits = find_relevant_memories(
        query, limit=limit, already_surfaced=already_surfaced, selector=selector
    )
    if not hits:
        return "", []
    blocks = [render_recalled_memory(h) for h in hits]
    paths = [str(h.entry.path) for h in hits if h.entry.path]
    return "\n\n".join(blocks), paths


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_SCANNED",
    "ScoredMemory",
    "Selector",
    "find_relevant_memories",
    "recall_block",
    "render_recalled_memory",
    "scan_memories",
]
