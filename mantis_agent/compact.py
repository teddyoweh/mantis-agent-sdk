"""Compaction — keep the context window from blowing up.

The basic idea, lifted from upstream and well-trodden in production agents:
when the conversation gets long enough to threaten the context window,
summarize the *older* turns with a cheaper / same model and replace them
with a single boundary marker. The marker says "you used to know X, Y, Z";
the recent turns stay verbatim so tool calls and immediate context aren't
lost.

This module ships:

* ``Compactor`` protocol — two methods, ``should_compact`` and ``compact``.
* ``CompactBoundaryMessage`` — a system-message variant that records what
  was summarized away and how many messages it replaced.
* ``SimpleCompactor`` — the v0 default. Heuristic-driven, single-call
  summarizer. Plug in your own ``MapReduceCompactor`` later by implementing
  the protocol.

Token counting
--------------
Real tokenizer integration is deferred to M5 (optional ``tiktoken`` /
``tokenizers`` dep). For v0 we use a coarse ``len(text) // 4`` heuristic.
That's wrong for code-heavy turns by ~30% and for non-English by more, but
it's *consistently* wrong, which is enough to drive the 85% threshold
decision. The cost of being slightly off is one early or one late compaction
— neither breaks correctness.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

import msgspec

from .types import (
    AssistantMessage,
    ContentBlock,
    Message,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
)

# A function that takes a single big prompt string and returns a summary.
# Wired in by the agent — usually a thin wrapper around ``provider.stream``
# that drains text and joins it.
SummarizerFn = Callable[[str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Boundary marker
# ---------------------------------------------------------------------------


class CompactBoundaryMessage(msgspec.Struct, omit_defaults=True):
    """Replaces a run of compacted messages.

    Serializes through the same Message channel as any other message; the
    ``role`` field is ``"system"`` so it slots in next to a SystemMessage and
    providers send it as a system-style turn (or fold it into the system
    prompt — adapter's call).

    ``compacted_count`` is the number of original messages this replaces, so
    a future debugger or fork operation can locate the original turns by
    index if they were saved separately.
    """

    summary: str
    compacted_count: int
    role: str = "system"
    # Marker so consumers can distinguish from a plain SystemMessage even if
    # they only see content/role.
    boundary: bool = True


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Compactor(Protocol):
    """Pluggable compaction strategy."""

    async def should_compact(
        self, messages: list[Message], usage: Usage, ctx_window: int
    ) -> bool: ...

    async def compact(self, messages: list[Message]) -> list[Message]: ...


# ---------------------------------------------------------------------------
# Token heuristic
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Coarse heuristic — ``len(text) // 4``. Replace with a real tokenizer
    in M5 when we ship the optional tokenizer extra.

    Empirically ~within 25% of GPT-4 / Claude tokenizers for prose, way off
    for code (it undercounts) but consistent enough to drive a threshold.
    """

    return len(text) // 4


def _message_token_estimate(msg: Message) -> int:
    """Sum of textual content under a message. Tool calls + tool results count
    by their stringified payload — usage tracks the truth, but this is the
    fallback when no Usage is attached (e.g. resumed sessions)."""

    if isinstance(msg, CompactBoundaryMessage):
        return _estimate_tokens(msg.summary) + 16  # small boundary overhead
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return _estimate_tokens(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for blk in content:
        total += _block_token_estimate(blk)
    return total


def _block_token_estimate(blk: ContentBlock) -> int:
    if isinstance(blk, TextBlock):
        return _estimate_tokens(blk.text)
    if isinstance(blk, ThinkingBlock):
        return _estimate_tokens(blk.thinking)
    if isinstance(blk, ToolUseBlock):
        # Approximate the JSON-serialized input length.
        return _estimate_tokens(repr(blk.input)) + _estimate_tokens(blk.name) + 8
    if isinstance(blk, ToolResultBlock):
        if isinstance(blk.content, str):
            return _estimate_tokens(blk.content)
        if isinstance(blk.content, list):
            return sum(_block_token_estimate(b) for b in blk.content)
        return 0
    return 0


# ---------------------------------------------------------------------------
# SimpleCompactor
# ---------------------------------------------------------------------------


class SimpleCompactor:
    """Default v0 strategy: threshold-on-tokens, keep-last-K-turns, single
    summarizer call.

    Parameters
    ----------
    summarizer_fn:
        Async callable that takes a prompt string and returns the summary
        text. The agent provides this; we don't import anything provider-y
        here so this stays decoupled.
    threshold:
        Fraction of the context window at which we trigger compaction.
        Default 0.85, matching the spec.
    keep_recent_turns:
        Number of *most recent* turns to retain verbatim. A "turn" here is
        any message — user, assistant, tool result — so K=8 keeps roughly
        four conversational exchanges depending on tool use density.
    system_message_floor:
        Whether to preserve a leading ``SystemMessage`` outside the boundary.
        Keep this on — the system prompt anchors the agent's persona, and
        rolling it into a summary loses fidelity.
    """

    __slots__ = (
        "_summarizer",
        "_threshold",
        "_keep_recent",
        "_preserve_system",
    )

    def __init__(
        self,
        summarizer_fn: SummarizerFn,
        *,
        threshold: float = 0.85,
        keep_recent_turns: int = 8,
        preserve_system: bool = True,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold}")
        if keep_recent_turns < 1:
            raise ValueError("keep_recent_turns must be >= 1")
        self._summarizer = summarizer_fn
        self._threshold = threshold
        self._keep_recent = keep_recent_turns
        self._preserve_system = preserve_system

    async def should_compact(
        self, messages: list[Message], usage: Usage, ctx_window: int
    ) -> bool:
        # Prefer reported usage if we have it — that's the model's truth.
        # Otherwise estimate from message contents.
        if ctx_window <= 0:
            return False
        reported = usage.input_tokens + usage.output_tokens if usage else 0
        if reported > 0:
            used = reported
        else:
            used = sum(_message_token_estimate(m) for m in messages)
        return used >= self._threshold * ctx_window

    async def compact(self, messages: list[Message]) -> list[Message]:
        """Summarize older messages into a CompactBoundaryMessage, keep the
        last ``keep_recent_turns`` verbatim.

        If there's nothing to compact (already short enough), returns the
        input unchanged.
        """

        if not messages:
            return messages

        # Identify a leading system message we want to preserve.
        head: list[Message] = []
        body_start = 0
        if (
            self._preserve_system
            and messages
            and isinstance(messages[0], SystemMessage)
        ):
            head = [messages[0]]
            body_start = 1

        body = messages[body_start:]
        if len(body) <= self._keep_recent:
            # Nothing meaningful to summarize.
            return messages

        to_summarize = body[: -self._keep_recent]
        recent = body[-self._keep_recent :]

        prompt = _build_summarization_prompt(to_summarize)
        summary = await self._summarizer(prompt)

        boundary = CompactBoundaryMessage(
            summary=summary,
            compacted_count=len(to_summarize),
        )

        out: list[Message] = []
        out.extend(head)
        out.append(boundary)  # type: ignore[arg-type]
        out.extend(recent)
        return out


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SUMMARIZER_INSTRUCTIONS = """\
You are compressing an agent conversation so a future turn can continue it.
Produce a tight, faithful summary in 200-400 words covering:

1. The user's goal(s) and any constraints they've stated.
2. Decisions made so far and *why* — the reasoning matters more than the verdict.
3. Tools that have been called and what they returned (key findings, not raw output).
4. Open questions, blockers, and what the agent was about to do next.

Write in past tense. Do not invent details. Do not use bullet headers — flowing
prose is denser. Begin with: "Summary of prior conversation:".
"""


def _build_summarization_prompt(messages: list[Message]) -> str:
    """Stitch messages into a textual prompt the summarizer model can chew on."""

    parts: list[str] = [_SUMMARIZER_INSTRUCTIONS, "\n--- Transcript ---\n"]
    for msg in messages:
        parts.append(_render_message(msg))
    return "\n".join(parts)


def _render_message(msg: Message) -> str:
    if isinstance(msg, CompactBoundaryMessage):
        return f"[previous summary] {msg.summary}"
    role = getattr(msg, "role", "?")
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return f"[{role}] {content}"
    if isinstance(content, list):
        chunks: list[str] = []
        for blk in content:
            chunks.append(_render_block(blk))
        return f"[{role}] " + " ".join(c for c in chunks if c)
    return f"[{role}]"


def _render_block(blk: ContentBlock) -> str:
    if isinstance(blk, TextBlock):
        return blk.text
    if isinstance(blk, ThinkingBlock):
        # Keep thinking traces in the summary input — they're often the only
        # record of why the agent did what it did.
        return f"(thinking: {blk.thinking})"
    if isinstance(blk, ToolUseBlock):
        return f"(tool_use {blk.name}={blk.input!r})"
    if isinstance(blk, ToolResultBlock):
        body = blk.content if isinstance(blk.content, str) else "<structured>"
        tag = "tool_error" if blk.is_error else "tool_result"
        return f"({tag} {blk.tool_use_id}: {body})"
    return ""


# Re-export message type so callers can ``from compact import UserMessage``-style
# without touching ``types``.
__all__ = [
    "AssistantMessage",
    "CompactBoundaryMessage",
    "Compactor",
    "SimpleCompactor",
    "SummarizerFn",
    "UserMessage",
]
