"""Skills — composable knowledge blobs the agent can pull into context.

Skills are *not* tools. A tool is something the agent calls and gets data
back from. A skill is a chunk of instructions / context the agent reads
*before* deciding what to do. Upstream Claude Code keeps these separate for
a good reason: skills are cheaper (no LLM round-trip), they don't pollute
the tool call space, and they can be sorted/filtered/prefetched in ways
tools can't.

Typical lifecycle
-----------------
1. User or platform registers skills via ``SkillRegistry.add``.
2. At turn start, the agent calls ``registry.always_loaded()`` and injects
   those bodies into the system prompt.
3. The agent also calls ``registry.match(user_message)`` to find skills
   relevant to the incoming turn and may inject those too (subject to budget).
4. If the model later signals "I need skill X", the host can look it up by
   name and append on demand.

The matching is intentionally dumb in v0 — substring search over
``name + search_hint``. A real embedding-based ranker is M5 work. The
interface is shaped so a smarter ``match`` can swap in without touching
callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import msgspec


# ---------------------------------------------------------------------------
# Skill struct
# ---------------------------------------------------------------------------


class Skill(msgspec.Struct, frozen=True, omit_defaults=True):
    """A self-contained chunk of agent-facing knowledge.

    ``name`` is the stable identifier — match against it when the agent says
    "load skill X". Keep it short and slug-like.

    ``description`` is one sentence — used in skill *catalogs* the agent
    reads when deciding whether to ask for a skill explicitly.

    ``body`` is the actual content (usually markdown). It gets injected into
    the system prompt, so price accordingly.

    ``search_hint`` is extra text used only by the matcher — synonyms,
    related concepts, example queries. Never shown to the model directly,
    so feel free to keyword-stuff it.

    ``always_load`` — set true for skills that *must* be present every turn
    (e.g. core safety guidelines, organization-wide style guides). The agent
    pays this cost on every request, so reserve it for high-value content.

    ``category`` — optional taxonomy bucket for filtering ("safety",
    "domain/finance", "personality"). Free-form; the SDK doesn't enforce.
    """

    name: str
    description: str
    body: str
    search_hint: str | None = None
    always_load: bool = False
    category: str | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SkillRegistry:
    """Holds the set of skills for an ``Agent``.

    The registry is *not* itself async — operations are pure in-memory data
    massaging. The agent will await summarizer/provider calls around it,
    but the registry itself stays sync for simplicity.
    """

    _by_name: dict[str, Skill] = field(default_factory=dict)
    # Ordered list of always-load skills — preserves insertion order so
    # users can reason about how they'll be concatenated into the prompt.
    _always: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, *skills: Skill) -> None:
        """Register one or more skills.

        Raises ``ValueError`` on duplicate names — silently overwriting
        bites people debugging "why doesn't my skill have the latest body".
        """

        for s in skills:
            if s.name in self._by_name:
                raise ValueError(f"duplicate skill name {s.name!r}")
            self._by_name[s.name] = s
            if s.always_load:
                self._always.append(s.name)

    def remove(self, name: str) -> Skill | None:
        """Remove a skill by name. Returns it for the caller's convenience."""

        skill = self._by_name.pop(name, None)
        if skill is not None and name in self._always:
            self._always.remove(name)
        return skill

    def get(self, name: str) -> Skill | None:
        return self._by_name.get(name)

    # ------------------------------------------------------------------
    # Read paths used by the agent loop
    # ------------------------------------------------------------------

    def always_loaded(self) -> list[Skill]:
        """Skills the agent should inject into the system prompt every turn.

        Ordering follows insertion order.
        """

        return [self._by_name[n] for n in self._always if n in self._by_name]

    def match(self, query: str, limit: int = 5) -> list[Skill]:
        """Return skills whose ``name`` or ``search_hint`` matches ``query``.

        v0 implementation: case-insensitive substring search, scored by
        whether the hit is on the name (high) or the search hint (lower).
        Ties resolve to insertion order.

        ``limit`` caps the number of returned skills; callers should respect
        whatever prompt-budget they have on top of this.
        """

        if not query:
            return []
        q = query.lower()

        scored: list[tuple[int, int, Skill]] = []  # (-score, insertion idx, skill)
        for idx, skill in enumerate(self._by_name.values()):
            score = 0
            name_l = skill.name.lower()
            if q in name_l:
                # Exact name match is most authoritative.
                score += 10
                if name_l == q:
                    score += 5
            if skill.search_hint:
                hint_l = skill.search_hint.lower()
                if q in hint_l:
                    score += 3
            desc_l = skill.description.lower()
            if q in desc_l:
                score += 2
            if score > 0:
                scored.append((-score, idx, skill))

        scored.sort()
        return [s for _, _, s in scored[:limit]]

    def by_category(self, category: str) -> list[Skill]:
        """All skills in a given category — useful for catalog-style listing.

        Returns insertion order; categories are not sorted internally.
        """

        return [s for s in self._by_name.values() if s.category == category]

    # ------------------------------------------------------------------
    # Dunders
    # ------------------------------------------------------------------

    def __bool__(self) -> bool:
        return bool(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterable[Skill]:  # type: ignore[override]
        return iter(self._by_name.values())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def render_skills(skills: list[Skill]) -> str:
    """Concatenate skill bodies into a single block suitable for inclusion in
    a system prompt.

    Each skill is fenced with a small header so the model can tell where one
    ends and the next begins. Empty input returns empty string — callers can
    safely splice the output into a prompt without conditional guards.
    """

    if not skills:
        return ""
    parts: list[str] = []
    for s in skills:
        parts.append(f"<skill name={s.name!r}>")
        if s.description:
            parts.append(s.description)
            parts.append("")
        parts.append(s.body.rstrip())
        parts.append("</skill>")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


__all__ = ["Skill", "SkillRegistry", "render_skills"]
