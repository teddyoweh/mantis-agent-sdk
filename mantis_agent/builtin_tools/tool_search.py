"""``tool_search`` — load a tool's schema only when it's actually needed.

Every tool the model *might* call normally costs tokens on *every* request:
name, description, and a full JSON Schema, resent each turn whether or not it
gets used. mantis's own built-in belt is ~4k tokens; three MCP servers can
double or triple that. On a 7B model with an 8k window that's most of the
context gone before the user's question arrives — which is why the terminal
already resorts to a hard-coded "slim belt" for small models.

Deferred tools fix that properly. A deferred tool is advertised by *name*
(plus a one-line summary) and nothing else. When the model wants one, it calls
``tool_search``; the matching schemas come back and stay live for the rest of
the session, so the second call is a normal tool call.

Query forms, matching the shape Claude Code uses so prompts port over:

* ``select:read_file,grep`` — fetch these exact tools by name
* ``notebook jupyter``      — keyword search, best ``max_results`` returned
* ``+slack send``           — require "slack" in the name, rank by the rest
"""

from __future__ import annotations

import json
import re

from ..tools import Tool, ToolRegistry, tool

# How the loaded schemas come back. The model has to be able to call the tool
# from this alone, so it's the same JSON the wire would have carried.
_HEADER = (
    "Loaded {n} tool schema{s}. These are now callable directly — call them "
    "like any other tool, no further search needed.\n\n"
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _score(tool_obj: Tool, terms: list[str]) -> float:
    """Rank a deferred tool against the query's terms.

    Name matches dominate description matches — when someone searches "slack"
    they want ``mcp__slack__send``, not the unrelated tool whose description
    happens to mention Slack.
    """
    name = _norm(tool_obj.name)
    desc = _norm(tool_obj.description)[:600]
    score = 0.0
    for term in terms:
        if not term:
            continue
        if term == name:
            score += 12
        elif term in name.split():
            score += 8
        elif term in name:
            score += 5
        if term in desc:
            score += 1.5
    return score


def search_deferred(registry: ToolRegistry, query: str,
                    max_results: int = 5) -> list[Tool]:
    """The matching deferred tools, best first. Pure — no mutation — so the
    ranking can be tested without a live registry."""
    deferred = registry.deferred_tools()
    if not deferred:
        return []
    q = (query or "").strip()

    # select: exact names, in the order asked for.
    if q.lower().startswith("select:"):
        wanted = [n.strip() for n in q[len("select:"):].split(",") if n.strip()]
        by_name = {t.name: t for t in deferred}
        out: list[Tool] = []
        for name in wanted:
            t = by_name.get(name)
            if t is None:                       # tolerate drifted spellings
                norm = re.sub(r"[^a-z0-9]", "", name.lower())
                t = next((x for x in deferred
                          if re.sub(r"[^a-z0-9]", "", x.name.lower()) == norm), None)
            if t is not None and t not in out:
                out.append(t)
        return out

    terms = _norm(q).split()
    required = [t[1:] for t in q.split() if t.startswith("+") and len(t) > 1]
    required = [_norm(r) for r in required]
    terms = [t for t in terms if t not in required]

    pool = deferred
    for req in required:
        pool = [t for t in pool if req in _norm(t.name)]
    if not terms:                                # only required terms given
        return pool[:max_results]

    ranked = sorted(
        ((t, _score(t, terms)) for t in pool),
        key=lambda pair: (-pair[1], pair[0].name),
    )
    return [t for t, s in ranked if s > 0][:max_results]


def make_tool_search(registry: ToolRegistry) -> Tool:
    """Build the ``tool_search`` tool bound to one registry.

    Stateful by nature — searching *promotes* the tools it finds, so the tool
    has to hold the registry the agent is running from.
    """

    @tool(name="tool_search", is_read_only=True)
    async def tool_search(query: str, max_results: int = 5) -> str:
        """Load the full schemas for deferred tools so you can call them.

        Deferred tools are listed by name in the system prompt but their
        schemas are not loaded, so you cannot call them correctly until you
        fetch them here. Loaded tools stay callable for the rest of the
        session — search once, then call as normal.

        Args:
            query: ``select:name1,name2`` to fetch exact tools by name;
                plain keywords to search (``github issues``); a ``+term``
                prefix to require that term in the tool name (``+slack send``).
            max_results: How many schemas to load (1–20, default 5).
        """
        try:
            n = max(1, min(int(max_results), 20))
        except (TypeError, ValueError):
            n = 5
        found = search_deferred(registry, query, n)
        if not found:
            avail = registry.deferred_index()
            if not avail:
                return ("No deferred tools — every tool is already loaded and "
                        "callable. Just call the one you need.")
            names = ", ".join(name for name, _ in avail[:40])
            return (f"No deferred tool matches {query!r}. Available deferred "
                    f"tools: {names}")
        registry.surface(*[t.name for t in found])
        body = "\n".join(json.dumps(t.to_wire()) for t in found)
        return _HEADER.format(n=len(found), s="" if len(found) == 1 else "s") + body

    return tool_search


def deferred_prompt_section(registry: ToolRegistry, limit: int = 60) -> str:
    """The system-prompt paragraph that advertises deferred tools by name.

    Returns "" when nothing is deferred, so the prompt doesn't grow a section
    about a mechanism this session isn't using.
    """
    index = registry.deferred_index()
    if not index:
        return ""
    shown = index[:limit]
    lines = [f"- {name}: {summary}" if summary else f"- {name}"
             for name, summary in shown]
    more = ""
    if len(index) > limit:
        more = f"\n…and {len(index) - limit} more."
    return (
        "\n\n# Deferred tools\n"
        "These tools exist but their schemas are NOT loaded. You cannot call "
        "them until you load them with `tool_search` (e.g. "
        "`tool_search(query=\"select:<name>\")`, or keywords to search). "
        "Once loaded they stay callable.\n"
        + "\n".join(lines) + more
    )


__all__ = ["deferred_prompt_section", "make_tool_search", "search_deferred"]
