"""``load_skill`` — the progressive-disclosure read side. The skills *catalog*
(name + one-line description) is injected into context each turn; when a task
matches one, the agent calls this tool to pull the full instructions on demand,
so N skills cost N one-liners rather than N full documents.
"""

from __future__ import annotations

from ..tools import tool


@tool(name="load_skill", is_read_only=True)
async def load_skill(name: str) -> str:
    """Load the full instructions for a skill shown in the skills catalog. Call
    this when the current task matches a skill's description — then follow the
    instructions it returns.

    Args:
        name: The skill's name, exactly as listed in the catalog.
    """
    from ..skills import discover_skills, load_skill_body

    body = load_skill_body(name)
    if body is not None:
        return body
    available = ", ".join(s.name for s in discover_skills()) or "none"
    return f"no skill named {name!r} (available: {available})"
