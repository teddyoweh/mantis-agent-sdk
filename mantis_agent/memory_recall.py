"""Memory **recall** — surface the right ``~/.mantis-agent/memory/`` topic files
for the current turn. The read side of the memory system (the write side lives
in :mod:`mantis_agent.memory`).

Faithful to Claude Code's ``memdir`` recall (``findRelevantMemories.ts`` +
``memoryScan.ts`` + ``memoryAge.ts``):

1. **Scan** all memory files for retrieval, including older notes and bodies.
   The public ``scan_memories()`` keeps its newest-200 metadata-only default.
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
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .memory import MemoryEntry, list_memory_entries, load_memory_entry

MAX_SCANNED = 200       # recency prefilter cap (CC: MAX_MEMORY_FILES)
DEFAULT_LIMIT = 5       # CC surfaces at most 5 per turn
DEFAULT_MAX_CHARS = 12_000
_CACHE_MAX_ENTRIES = 512
_CACHE_MAX_CHARS = 8_000_000
# Absolute path + nanosecond mtime/ctime + size prevents cross-home/stale hits.
_BODY_CACHE: OrderedDict[tuple[str, int, int, int], MemoryEntry] = OrderedDict()
_cache_chars = 0
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


def scan_memories(*, limit: int | None = MAX_SCANNED) -> list[ScoredMemory]:
    """All memory entries, newest-first, capped at ``MAX_SCANNED`` (score 0).

    Reads only frontmatter. ``limit=None`` disables the compatibility cap;
    nonpositive limits return no entries.
    """
    if limit is not None and limit <= 0:
        return []
    scored = [
        ScoredMemory(e, _mtime(e), 0.0)
        for e in list_memory_entries(frontmatter_only=True)
    ]
    scored.sort(key=lambda s: s.mtime, reverse=True)
    return scored if limit is None else scored[:limit]


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1}


def _keyword_score(query_tokens: set[str], entry: MemoryEntry) -> float:
    """Distinct lexical overlap; metadata outweighs body-only evidence.

    Repeating a term in a long body cannot inflate its relevance.
    """
    if not query_tokens:
        return 0.0
    name_t = _tokens(entry.name)
    desc_t = _tokens(entry.description)
    score = (
        2.0 * len(query_tokens & name_t)
        + 1.0 * len(query_tokens & desc_t)
        + 0.25 * len(query_tokens & _tokens(entry.body))
    )
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
    # A negative limit would slice as "all but the last -n" (and a plain loop
    # would treat it as unbounded), silently surfacing almost everything.
    # Clamp to >=0 so limit <= 0 means "none".
    limit = max(0, limit)
    if limit == 0 or (selector is None and not _tokens(query)):
        return []
    candidates = [
        s for s in scan_memories(limit=None)
        if not (s.entry.path and str(s.entry.path) in already_surfaced)
    ]
    if not candidates or limit == 0:
        return []
    if selector is not None:
        return [_hydrate_body(s) for s in selector(query, candidates)[:limit]]

    candidates = [_hydrate_body(s) for s in candidates]
    qt = _tokens(query)
    scored = [
        ScoredMemory(s.entry, s.mtime, _keyword_score(qt, s.entry))
        for s in candidates
    ]
    hits = [s for s in scored if s.score > 0]
    # Highest score first; mtime breaks ties (newer wins).
    hits.sort(key=lambda s: (s.score, s.mtime), reverse=True)
    return [_hydrate_body(s) for s in hits[:limit]]


def _hydrate_body(scored: ScoredMemory) -> ScoredMemory:
    """Load body with a bounded, stat-aware cache shared across recall turns."""
    global _cache_chars
    if scored.entry.body or scored.entry.path is None:
        return scored
    try:
        path = scored.entry.path
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        full = _BODY_CACHE.get(key)
        if full is not None:
            _BODY_CACHE.move_to_end(key)
        else:
            full = load_memory_entry(scored.entry.slug)
            if full is None:
                return scored
            size = len(full.to_markdown())
            if size <= _CACHE_MAX_CHARS:
                while _BODY_CACHE and (
                    len(_BODY_CACHE) >= _CACHE_MAX_ENTRIES
                    or _cache_chars + size > _CACHE_MAX_CHARS
                ):
                    _, evicted = _BODY_CACHE.popitem(last=False)
                    _cache_chars -= len(evicted.to_markdown())
                _BODY_CACHE[key] = full
                _cache_chars += size
        # MemoryEntry is mutable: don't expose the cache's copy to callers.
        return ScoredMemory(replace(full), stat.st_mtime, scored.score)
    except (OSError, UnicodeError):
        return scored


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


def _excerpt(body: str, query: str, max_chars: int) -> str:
    """Center a bounded excerpt on query evidence, not a blind file prefix."""
    body = body.strip()
    if max_chars <= 0:
        return ""
    if len(body) <= max_chars:
        return body
    # Reserve ellipses at both edges; never emit a partial matching token.
    width = max_chars - 2
    if width <= 0:
        return ""
    qt = _tokens(query)
    matches = [m for m in re.finditer(r"[a-z0-9]+", body, re.IGNORECASE)
               if m.group().lower() in qt and len(m.group()) <= width]
    if qt and not matches and qt & _tokens(body):
        return ""
    start = 0
    if matches:
        # Prefer a window covering the most distinct query terms. Sliding
        # counts keep selection linear even for repetitive, large notes.
        counts: dict[str, int] = {}
        left = 0
        best = 0
        anchor = matches[0]
        for match in matches:
            token = match.group().lower()
            counts[token] = counts.get(token, 0) + 1
            while match.end() - matches[left].start() > width:
                old = matches[left].group().lower()
                counts[old] -= 1
                if not counts[old]:
                    del counts[old]
                left += 1
            if len(counts) > best:
                best = len(counts)
                anchor = matches[left]
                start = max(0, anchor.start() - (width - (match.end() - anchor.start())) // 2)
    end = min(len(body), start + width)
    return ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")


def render_recalled_memory(
    scored: ScoredMemory, *, query: str = "", max_chars: int | None = None,
) -> str:
    """One reminder; optional bound includes the source, caveat and wrapper.

    Return empty when the bound cannot fit the header and a useful excerpt.
    The default preserves the standalone renderer's full-body behavior.
    """
    caveat = _freshness_caveat(scored.mtime)
    path = str(scored.entry.path) if scored.entry.path else scored.entry.slug
    if caveat:
        header = f"{caveat}\n\nMemory: {path}:"
    else:
        header = f"Memory (saved {_age_str(scored.mtime)}): {path}:"
    prefix = f"<system-reminder>\n{header}\n\n"
    suffix = "\n</system-reminder>"
    body = scored.entry.body.strip()
    if max_chars is not None:
        available = max_chars - len(prefix) - len(suffix)
        if available < 0:
            return ""
        excerpt = _excerpt(body, query, available)
        if body and not excerpt:
            return ""
        body = excerpt
    return prefix + body + suffix


def recall_block(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    already_surfaced: frozenset[str] = frozenset(),
    selector: Selector | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, list[str]]:
    """Convenience: find relevant memories for ``query`` and return
    ``(combined_text, surfaced_paths)`` ready to inject as a context reminder.
    ``max_chars`` bounds the entire text, including separators and caveats.
    Only actually rendered entries are marked surfaced; nonpositive bounds
    return no text or paths. ``combined_text`` is "" when nothing fits.
    """
    if max_chars <= 0 or limit <= 0:
        return "", []
    hits = find_relevant_memories(
        query, limit=limit, already_surfaced=already_surfaced, selector=selector
    )
    if not hits:
        return "", []
    blocks: list[str] = []
    paths: list[str] = []
    remaining = max_chars
    for hit in hits:
        separator = 2 if blocks else 0
        block = render_recalled_memory(hit, query=query, max_chars=remaining - separator)
        if not block:
            continue
        blocks.append(block)
        remaining -= len(block) + separator
        if hit.entry.path:
            paths.append(str(hit.entry.path))
    return "\n\n".join(blocks), paths


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_CHARS",
    "MAX_SCANNED",
    "ScoredMemory",
    "Selector",
    "find_relevant_memories",
    "recall_block",
    "render_recalled_memory",
    "scan_memories",
]
