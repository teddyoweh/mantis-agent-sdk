"""``ask_user_question`` — let the agent ask the user structured multiple-choice
questions mid-task (Claude Code's AskUserQuestion). The agent proposes 1-4
questions, each with 2-4 labelled options; the user picks (or types "Other" for
free text). The chosen answers come back as the tool result so the agent can act
on real preferences instead of guessing.

The interactive prompt is provided by the harness (the ``mantis`` terminal) via
an injected ``asker``. In a headless / library run with no asker, the tool
returns a note rather than blocking.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..tools import Tool, tool

# asker(questions) -> per-question answers. Each answer:
#   {"question": str, "header": str, "answers": list[str]}
Asker = Callable[[list[dict]], Awaitable[list[dict]]]

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "description": "1-4 multiple-choice questions to ask the user.",
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The full question, clear and specific, ending in a question mark.",
                    },
                    "header": {
                        "type": "string",
                        "description": "Very short chip label (≤12 chars), e.g. 'Library', 'Approach'.",
                    },
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "description": "2-4 distinct choices. No 'Other' — it's added automatically.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Concise choice text (1-5 words). If you recommend one, put it first and append ' (Recommended)'.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "What this choice means / its trade-offs.",
                                },
                            },
                            "required": ["label", "description"],
                        },
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "description": "Allow selecting multiple options. Default false.",
                    },
                },
                "required": ["question", "header", "options"],
            },
        }
    },
    "required": ["questions"],
}


def _normalize(questions: Any) -> list[dict]:
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty list")
    out: list[dict] = []
    for i, q in enumerate(questions[:4]):
        if not isinstance(q, dict) or "question" not in q or "options" not in q:
            raise ValueError(f"question #{i + 1} needs 'question' and 'options'")
        opts = [
            {"label": str(o.get("label", "")), "description": str(o.get("description", ""))}
            for o in q["options"]
            if isinstance(o, dict) and o.get("label")
        ]
        if len(opts) < 2:
            raise ValueError(f"question #{i + 1} needs at least 2 options")
        out.append({
            "question": str(q["question"]),
            "header": str(q.get("header", "") or q["question"])[:12],
            "options": opts[:4],
            "multiSelect": bool(q.get("multiSelect", False)),
        })
    return out


def make_ask_user_question(asker: Asker | None) -> Tool:
    """Build the ``ask_user_question`` tool bound to an interactive ``asker``.
    ``None`` → the tool reports that no interactive user is available."""

    @tool(
        name="ask_user_question",
        is_read_only=False,
        is_concurrency_safe=False,
        input_schema=_SCHEMA,
    )
    async def ask_user_question(args: dict) -> str:
        """Ask the user multiple-choice questions to gather preferences, clarify
        ambiguity, or offer a decision — instead of guessing. Provide 1-4
        questions, each with 2-4 labelled options; the user can always pick
        "Other" for free text. If you recommend an option, make it the first and
        append " (Recommended)" to its label. Don't use this for yes/no
        confirmations you could just proceed on."""
        qs = _normalize(args.get("questions"))
        if asker is None:
            return (
                "No interactive user is available to answer (headless run). "
                "Proceed with your best judgment and state the assumption."
            )
        answers = await asker(qs)
        lines: list[str] = []
        for a in answers:
            picked = ", ".join(a.get("answers", [])) or "(skipped)"
            lines.append(f"{a.get('header', 'Q')}: {picked}")
        return "\n".join(lines) if lines else "(no answers)"

    return ask_user_question
