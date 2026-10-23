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


def test_a_summary_must_record_actions_already_taken() -> None:
    p = _build_summarization_prompt(_transcript())
    assert "Actions Already Taken" in p and "never repeat" in p.lower()


def _crm_like_transcript(n: int = 60) -> list:
    """An agent emailing customers one by one, each step reading a big record."""
    msgs: list = [UserMessage(content="email every overdue customer once")]
    for i in range(n):
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"g{i}", name="mcp__crm__get_customer",
                                                           input={"customer_id": f"C-{i}"})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"g{i}", content="notes " * 2000)]))
        msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"s{i}", name="mcp__crm__send_email",
                                                           input={"to": f"c{i}@x.example", "subject": "Reminder"})]))
        msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"s{i}", content="sent")]))
    return msgs


def test_every_side_effect_survives_a_prompt_too_big_to_send_whole() -> None:
    # the results are shrunk before anything else; every send survives
    p = _build_summarization_prompt(_crm_like_transcript(), max_chars=60_000)
    assert len(p) <= 60_000
    assert all(f"c{i}@x.example" in p for i in range(60))


def test_the_elided_middle_keeps_its_calls_as_an_action_log() -> None:
    p = _build_summarization_prompt(_crm_like_transcript(200), max_chars=40_000)
    assert len(p) <= 40_000 + 500
    assert "tool calls made in the elided part" in p
    assert "c100@x.example" in p                 # a send from deep in the middle
