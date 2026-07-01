"""Structured compaction summary (T1.7): the summarizer prompt uses Claude's
multi-section format and carries file paths / errors as raw material, so a
resumed coding turn keeps its technical fidelity."""

from __future__ import annotations

import anyio

from mantis_agent.compact import SimpleCompactor, _build_summarization_prompt
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

_SECTIONS = [
    "Primary Request and Intent",
    "Key Technical Concepts",
    "Files and Code Sections",
    "Errors and Fixes",
    "Problem Solving",
    "Pending Tasks",
    "Current Work",
    "Next Step",
]


def _transcript() -> list:
    return [
        UserMessage(content="add retry logic to the fetcher"),
        AssistantMessage(content=[ToolUseBlock(
            id="c1", name="edit_file",
            input={"path": "src/net/fetcher.py", "old_string": "a", "new_string": "b"})]),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="c1", content="Updated src/net/fetcher.py · +3 -1")]),
        AssistantMessage(content=[TextBlock(text="TimeoutError was raised; added a backoff.")]),
    ]


def test_prompt_has_all_sections() -> None:
    p = _build_summarization_prompt(_transcript())
    for h in _SECTIONS:
        assert h in p, f"missing section: {h}"


def test_prompt_is_not_the_old_prose_format() -> None:
    p = _build_summarization_prompt(_transcript())
    assert "200-400 words" not in p
    assert "flowing\nprose" not in p and "flowing prose" not in p


def test_prompt_carries_file_paths_and_errors() -> None:
    p = _build_summarization_prompt(_transcript())
    assert "src/net/fetcher.py" in p        # exact path preserved as material
    assert "TimeoutError" in p               # error text preserved as material


def test_prompt_demands_fidelity() -> None:
    p = _build_summarization_prompt(_transcript())
    low = p.lower()
    assert "file path" in low and "next" in low
    assert "do not invent" in low            # anti-hallucination instruction


def test_compaction_still_produces_boundary() -> None:
    # End-to-end: the structured prompt still drives a normal compaction.
    seen = {}

    async def summarizer(prompt: str) -> str:
        seen["prompt"] = prompt
        return "Summary of prior conversation: edited src/net/fetcher.py; next: test it."

    c = SimpleCompactor(summarizer, keep_recent_turns=1)
    msgs = _transcript() * 3
    out = anyio.run(lambda: c.compact(list(msgs)))
    assert len(out) < len(msgs)                       # actually compacted
    assert all(h in seen["prompt"] for h in _SECTIONS)  # structured prompt was used
