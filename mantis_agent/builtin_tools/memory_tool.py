"""The ``remember`` tool — the agent's write path into persistent memory.

Its counterpart is recall (``memory_recall.py``, wired into the run loop): what
the agent saves here is surfaced automatically in future sessions when a user
message is relevant to it. One fact per memory; keep the description a sharp
one-liner — that's the signal recall scores against.
"""

from __future__ import annotations

import re

from ..tools import tool


@tool(name="remember", is_read_only=False)
async def remember(name: str, description: str, body: str, type: str = "project") -> str:
    """Save a durable memory that persists across sessions and is recalled when
    relevant. Use for things worth remembering long-term: project conventions,
    user preferences, hard-won gotchas, where things live. NOT for transient
    task state.

    Args:
        name: Short title (e.g. "Redis session cache").
        description: A sharp one-line hook — recall scores relevance against this.
        body: The fact itself, in full.
        type: One of user | feedback | project | reference (default project).
    """
    from ..memory import MemoryEntry, save_memory_entry, update_memory_index

    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60] or "memory"
    t = type if type in ("user", "feedback", "project", "reference") else "project"
    path = save_memory_entry(
        MemoryEntry(slug=slug, name=name, description=description, type=t, body=body)
    )
    update_memory_index()
    return f"Saved memory '{name}' → {path}"
