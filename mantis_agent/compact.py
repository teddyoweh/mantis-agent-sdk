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
from typing import Any, Protocol, runtime_checkable

import msgspec

from .hooks import HookContext, HookDispatcher
from .types import (
    AssistantMessage,
    ContentBlock,
    ImageBlock,
    Message,
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
# PostCompact dispatch
# ---------------------------------------------------------------------------


async def _dispatch_post_compact(
    dispatcher: "HookDispatcher | None",
    before: list[Message],
    after: list[Message],
    *,
    trigger: str,
) -> None:
    """Fire the non-blocking ``PostCompact`` hook for a completed compaction.

    Called from exactly one place — the tail of ``SimpleCompactor.compact``,
    after the replacement list is built and immediately before it is returned.
    That is the only moment where "the new message list" exists and is final,
    which is what a ``PostCompact`` hook is for (persist the pre-compaction
    transcript, re-index it, notify a dashboard).

    Deliberately NOT fired for:

    * the no-op paths in ``compact`` (nothing to summarize, summarizer failed
      or returned empty). Those return the input list unchanged — there was no
      compaction, so announcing one would be a lie a hook cannot distinguish
      from the real thing.
    * ``microcompact`` / ``emergency_clear``. Both are synchronous payload
      strippers, not summarizations: no turns are replaced, the list identity
      is preserved, and they run on paths (including post-overflow recovery)
      where awaiting user code would be actively harmful.

    ``PostCompact`` is observability — it is not in ``BLOCKING_EVENTS``, so a
    hook that raises is logged and swallowed by the dispatcher and compaction
    still returns its result. The whole call is guarded by ``has()`` so the
    common no-hook path costs one dict lookup.
    """

    if dispatcher is None or not dispatcher.has("PostCompact"):
        return
    extras: dict[str, Any] = {
        "trigger": trigger,
        "before_count": len(before),
        "after_count": len(after),
    }
    await dispatcher.dispatch(
        "PostCompact",
        HookContext(event="PostCompact", messages_snapshot=after, arbitrary=extras),
    )


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


def _is_tool_result_message(msg: Message) -> bool:
    """True if ``msg`` is a UserMessage carrying any ToolResultBlock — i.e. the
    back-half of a tool round whose tool_use lives in a prior message."""
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return False
    return any(isinstance(b, ToolResultBlock) for b in content)


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
    if isinstance(blk, ImageBlock):
        return _image_token_estimate(blk)
    if isinstance(blk, ToolResultBlock):
        if isinstance(blk.content, str):
            return _estimate_tokens(blk.content)
        if isinstance(blk.content, list):
            return sum(_block_token_estimate(b) for b in blk.content)
        return 0
    return 0


# A remote-URL image we can't measure: assume roughly Anthropic's per-image
# ceiling (~1590 tokens) rather than zero.
_REMOTE_IMAGE_TOKENS = 1600


def _image_token_estimate(blk: ImageBlock) -> int:
    """Estimate an image's context cost from the payload we actually ship.

    Images are the single biggest thing this estimator used to get wrong: a
    2 MB screenshot carries ~2.9M base64 characters, and every provider bills
    for what it receives. Falling through to ``0`` (the old behaviour) meant a
    screenshot-heavy loop — browser automation, repeated ``Read`` of a PNG —
    could push the real prompt past the window while the estimator still read
    "plenty of headroom", so neither micro- nor full compaction ever fired.
    """

    src = blk.source if isinstance(blk.source, dict) else {}
    data = src.get("data")
    if isinstance(data, str):
        return _estimate_tokens(data)
    url = src.get("url")
    if isinstance(url, str):
        # Inline data: URI — the base64 rides in the URL itself.
        return _estimate_tokens(url) if url.startswith("data:") else _REMOTE_IMAGE_TOKENS
    return _REMOTE_IMAGE_TOKENS


_CLEARED_TOOL_RESULT = "[old tool result cleared to save context]"
_CLEARED_IMAGE = "[old image cleared to save context]"


def _strip_heavy_blocks(
    msg: Message, min_chars: int, *, images_only: bool = False
) -> Message | None:
    """Return ``msg`` with oversized payloads replaced by placeholders, or None
    if nothing was heavy enough to clear.

    Handles all three shapes that carry weight: string tool results, STRUCTURED
    tool results (a list of blocks — how image-returning tools like ``Read`` on
    a PNG come back, and previously invisible to microcompaction), and bare
    image blocks. Tool results keep their ``tool_use_id`` so the tool_use pair
    still matches and the provider won't 400."""

    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return None
    new_blocks: list[ContentBlock] = []
    touched = False
    for b in content:
        if isinstance(b, ImageBlock) and _block_token_estimate(b) * 4 > min_chars:
            new_blocks.append(TextBlock(text=_CLEARED_IMAGE))
            touched = True
            continue
        if (
            not images_only
            and isinstance(b, ToolResultBlock)
            and b.content != _CLEARED_TOOL_RESULT
            and isinstance(b.content, (str, list))
            and _block_token_estimate(b) * 4 > min_chars
        ):
            new_blocks.append(msgspec.structs.replace(b, content=_CLEARED_TOOL_RESULT))
            touched = True
            continue
        new_blocks.append(b)
    if not touched:
        return None
    return msgspec.structs.replace(msg, content=new_blocks)


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
    dispatcher:
        Optional ``HookDispatcher``. When set, a compaction that actually
        replaced turns fires the non-blocking ``PostCompact`` hook with the new
        message list. Left ``None`` the compactor is hook-free and behaves
        exactly as before — no import-time or per-call cost.
    trigger:
        Label reported to ``PostCompact`` hooks so they can tell an automatic
        threshold compaction ("auto") from an explicit ``/compact`` ("manual").
    """

    __slots__ = (
        "_summarizer",
        "_threshold",
        "_keep_recent",
        "_preserve_system",
        "_micro_threshold",
        "_micro_keep",
        "_micro_min_chars",
        "_summary_token_budget",
        "_dispatcher",
        "_trigger",
    )

    def __init__(
        self,
        summarizer_fn: SummarizerFn,
        *,
        threshold: float = 0.85,
        keep_recent_turns: int = 8,
        preserve_system: bool = True,
        micro_threshold: float = 0.6,
        micro_keep_tool_results: int = 8,
        micro_min_chars: int = 800,
        summary_token_budget: int = 2048,
        dispatcher: "HookDispatcher | None" = None,
        trigger: str = "auto",
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold}")
        if keep_recent_turns < 1:
            raise ValueError("keep_recent_turns must be >= 1")
        if summary_token_budget < 128:
            raise ValueError("summary_token_budget must be >= 128")
        self._summarizer = summarizer_fn
        self._threshold = threshold
        self._keep_recent = keep_recent_turns
        self._preserve_system = preserve_system
        # Microcompaction: cheaper first line. When context passes
        # ``micro_threshold`` of the window, clear the *content* of tool results
        # older than the last ``micro_keep_tool_results`` (only ones larger than
        # ``micro_min_chars``) — no summarizer call. Defers full compaction.
        self._micro_threshold = micro_threshold
        self._micro_keep = max(1, micro_keep_tool_results)
        self._micro_min_chars = micro_min_chars
        # Dead-end guard: weak local models sometimes summarize by echoing the
        # transcript. Bound the replacement text so compaction always creates
        # headroom instead of burning the limited compaction retry budget.
        self._summary_token_budget = summary_token_budget
        # Hook delivery for PostCompact. Stored as the dispatcher itself (not a
        # resolved callable) so a dispatcher whose ``Hooks`` are swapped after
        # construction is still honoured on the next compaction.
        self._dispatcher = dispatcher
        self._trigger = trigger

    @staticmethod
    def _used(messages: list[Message], usage: Usage) -> int:
        reported = usage.input_tokens + usage.output_tokens if usage else 0
        estimate = sum(_message_token_estimate(m) for m in messages)
        return max(reported, estimate)

    async def should_compact(
        self, messages: list[Message], usage: Usage, ctx_window: int
    ) -> bool:
        # Prefer reported usage if we have it — that's the model's truth.
        # Otherwise estimate from message contents. The model's reported
        # input_tokens undercounts the NEXT prompt (it predates the tool results
        # we just appended), so take the larger of reported and our estimate so
        # we trigger before the next call overflows, not after.
        if ctx_window <= 0:
            return False
        return self._used(messages, usage) >= self._threshold * ctx_window

    def should_microcompact(
        self, messages: list[Message], usage: Usage, ctx_window: int
    ) -> bool:
        if ctx_window <= 0:
            return False
        return self._used(messages, usage) >= self._micro_threshold * ctx_window

    def microcompact(self, messages: list[Message]) -> bool:
        """Clear the payload of tool results older than the last ``micro_keep``
        (only those over ``micro_min_chars``), in place. Keeps the block + its
        tool_use_id so pairing is untouched. Returns True if anything changed.
        Cheap (no model call), idempotent."""

        tr_idx = [i for i, m in enumerate(messages) if _is_tool_result_message(m)]
        if len(tr_idx) <= self._micro_keep:
            return False
        cutoff = tr_idx[-self._micro_keep]
        changed = False
        for i in tr_idx[: -self._micro_keep]:
            stripped = _strip_heavy_blocks(messages[i], self._micro_min_chars)
            if stripped is not None:
                messages[i] = stripped
                changed = True
        # Bare images — pasted attachments, screenshots handed straight to the
        # model — live OUTSIDE tool results, so the sweep above never saw them
        # even though they are routinely the heaviest thing in the transcript.
        # Clear the ones older than the keep-window too.
        for i in range(cutoff):
            if _is_tool_result_message(messages[i]):
                continue
            stripped = _strip_heavy_blocks(messages[i], self._micro_min_chars, images_only=True)
            if stripped is not None:
                messages[i] = stripped
                changed = True
        return changed

    def emergency_clear(self, messages: list[Message], *, keep_last: int = 1) -> bool:
        """Last resort after a real context-overflow error: clear heavy payloads
        everywhere EXCEPT the last ``keep_last`` tool results, in place.

        ``microcompact`` and ``compact`` both deliberately protect the recent
        window — which is exactly where a sudden 2 MB screenshot lands. When the
        provider has already rejected the prompt, that protection is what wedges
        the session: every retry re-sends the same oversized turn. This drops the
        recent window's payloads too, so the run can continue in a degraded but
        living state rather than dying on an unrecoverable transcript."""

        tr_idx = [i for i, m in enumerate(messages) if _is_tool_result_message(m)]
        keep = {*tr_idx[-keep_last:]} if keep_last > 0 else set()
        changed = False
        for i in range(len(messages)):
            if i in keep:
                continue
            stripped = _strip_heavy_blocks(messages[i], self._micro_min_chars)
            if stripped is not None:
                messages[i] = stripped
                changed = True
        return changed

    async def compact(self, messages: list[Message]) -> list[Message]:
        """Summarize older messages into a single replacement message, keeping
        the last ``keep_recent_turns`` verbatim. Returns the input UNCHANGED if
        there's nothing to compact, the summarizer fails, or it comes back
        empty — so a bad summary can never destroy live context.

        The replacement is a plain ``UserMessage`` (not a bespoke boundary
        type) so it serializes through providers, ``query()``, and session
        save/load exactly like any other message — no special-casing needed
        downstream.
        """

        if not messages:
            return messages

        # Preserve a leading anchor outside the boundary: an explicit
        # SystemMessage, or the synthetic ``isMeta`` user-context/memory head
        # the agent pins at index 0. Rolling either into a summary would lose
        # the persona / project memory and (for the isMeta head) confuse the
        # "do we already have a context message?" check on the next run.
        # Detect the head by ROLE, not isinstance: the public, SDK-shaped
        # ``SystemMessage`` (from claude_compat, carrying a ``subtype``) is a
        # *different class* from ``types.SystemMessage``, so an isinstance check
        # silently misses real system messages. ``role == "system"`` catches
        # both; ``isMeta`` catches the synthetic user-context head.
        head: list[Message] = []
        body_start = 0
        first = messages[0]
        if (self._preserve_system and getattr(first, "role", None) == "system") or getattr(
            first, "isMeta", False
        ):
            head = [first]
            body_start = 1

        body = messages[body_start:]
        if len(body) <= self._keep_recent:
            # Nothing meaningful to summarize.
            return messages

        # Preserve the ORIGINAL request verbatim: keep the first real (non-meta)
        # user message outside the summary so the agent never loses its goal to a
        # weak/lossy summarizer. Everything AFTER it up to the keep-window is what
        # gets summarized. (Claude Code pins the original request the same way.)
        anchor: list[Message] = []
        sum_start = 0
        first_content = getattr(body[0], "content", None)
        # Anchor the original request whether its content is a plain string or a
        # list of text blocks (multimodal / block-shaped UserMessage). A blanket
        # str-only check silently drops the goal anchor for block-form inputs and
        # folds the original request into the lossy summary.
        anchorable_content = isinstance(first_content, str) or (
            isinstance(first_content, list)
            and bool(first_content)
            and all(isinstance(b, TextBlock) for b in first_content)
        )
        if (
            getattr(body[0], "role", None) == "user"
            and not getattr(body[0], "isMeta", False)
            and anchorable_content
        ):
            anchor = [body[0]]
            sum_start = 1

        # Tool-pair-aware split: choose keep-last-K, then move the boundary so it
        # never lands *between* an assistant tool_use and its following
        # tool_result — that would orphan the tool_result and 400 the next
        # provider call. Prefer sliding FORWARD (fold the whole round into the
        # summary). But if the keep-window tail is nothing but tool_results, the
        # forward slide runs off the end; rather than bail and never make
        # headroom, slide BACKWARD to the start of that round so the pair rides
        # along in ``recent``. Either way ``recent`` never begins on a
        # tool_result and no pair is split.
        split = len(body) - self._keep_recent
        fwd = split
        while fwd < len(body) and _is_tool_result_message(body[fwd]):
            fwd += 1
        if fwd < len(body):
            split = fwd
        else:
            while split > 0 and _is_tool_result_message(body[split]):
                split -= 1
        if split <= 0 or split >= len(body):
            # Degenerate (nothing to summarize, or the whole window is one
            # unbreakable tool round): leave history untouched.
            return messages

        to_summarize = body[sum_start:split]
        recent = body[split:]
        if not to_summarize:
            # The anchor + keep-window already covers everything — nothing between
            # them to compress, so leave history untouched.
            return messages

        prompt = _build_summarization_prompt(to_summarize)
        try:
            summary = await self._summarizer(prompt)
        except Exception:  # noqa: BLE001 — summarizer failed: keep full context
            return messages
        if not summary or not summary.strip():
            return messages  # empty summary: never replace real turns with nothing
        summary = _trim_summary_to_token_budget(summary.strip(), self._summary_token_budget)

        summary_msg = UserMessage(
            content=(
                "[Earlier conversation compacted to save context. "
                f"{len(to_summarize)} messages summarized below. "
                f"Summary capped at ~{self._summary_token_budget} tokens to keep context headroom.]\n\n"
                f"{summary}"
            )
        )
        out = [*head, *anchor, summary_msg, *recent]
        # The line cap above is the dead-end guard for echoing summarizers: it
        # bounds the replacement before it can consume the context window again.
        #
        # PostCompact fires here and nowhere else: this is the single return
        # that represents a real compaction. Every other exit above hands back
        # the caller's own list untouched. See ``_dispatch_post_compact``.
        await _dispatch_post_compact(
            self._dispatcher, messages, out, trigger=self._trigger
        )
        return out


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _trim_summary_to_token_budget(summary: str, token_budget: int) -> str:
    """Bound a lossy compaction summary to a coarse token budget.

    Compaction is a safety mechanism, so its replacement message must be
    smaller than the transcript slice it replaces. Preserve whole lines where
    possible and add an explicit truncation marker rather than silently cutting
    through the text.
    """
    if _estimate_tokens(summary) <= token_budget:
        return summary
    char_budget = max(1, token_budget * 4)
    marker = "\n\n[summary truncated to preserve context headroom]"
    available = max(1, char_budget - len(marker))
    cut = summary[:available]
    line_cut = cut.rfind("\n")
    if line_cut >= available // 2:
        cut = cut[:line_cut]
    return cut.rstrip() + marker


_SUMMARIZER_INSTRUCTIONS = """\
You are compressing an agent CODING conversation so a future turn can resume it
with NO loss of technical fidelity. Another agent will read ONLY your summary to
continue the work — so preserve exact file paths, function/symbol names, key code
snippets, error messages, and the precise next action. Losing a path or an error
means the resumed turn redoes or breaks work.

Produce the summary under these EXACT section headers, in order:

1. Primary Request and Intent — what the user asked for and any explicit
   constraints. Capture every distinct request, not just the latest.
2. Key Technical Concepts — languages, frameworks, tools, and patterns in play.
3. Files and Code Sections — EVERY file created, edited, or examined, each with
   its full path, why it matters, and the important code (short snippets of the
   exact lines added/changed). This is the most important section — be specific.
4. Errors and Fixes — each error encountered and how it was resolved, plus any
   user feedback on the fix.
5. Problem Solving — what has been solved and the approach taken.
6. Pending Tasks — everything still to do, stated explicitly.
7. Current Work — precisely what was being done right before this summary, with
   file paths and code.
8. Next Step — the single next action to take, quoting the relevant task/request.
   If there is genuinely no next step, say so.

Be exhaustive on technical detail, terse on prose. Do NOT invent anything not in
the transcript. Quote exact identifiers and paths. Begin with:
"Summary of prior conversation:".
"""


async def run_manual_compaction(
    messages: list[Message],
    summarizer_fn: "SummarizerFn",
    *,
    focus: str = "",
    keep_recent: int = 4,
    dispatcher: "HookDispatcher | None" = None,
) -> tuple[list[Message], str]:
    """Compact ``messages`` on demand (the ``/compact`` command). Keeps the last
    ``keep_recent`` turns verbatim and summarizes the rest with ``summarizer_fn``;
    an optional ``focus`` hint is appended to the summarizer prompt so the user
    can steer what the summary preserves. Returns ``(new_messages, note)`` and
    leaves the input untouched when there's nothing to compact.

    ``dispatcher`` rides along to the internal compactor so a user-triggered
    compaction fires ``PostCompact`` exactly like an automatic one, tagged
    ``trigger="manual"``."""
    fn = summarizer_fn
    if focus.strip():
        async def fn(prompt: str, _s=summarizer_fn, _f=focus.strip()) -> str:  # noqa: A001
            return await _s(f"{prompt}\n\nFocus your summary especially on: {_f}")

    comp = SimpleCompactor(
        fn,
        keep_recent_turns=max(1, keep_recent),
        dispatcher=dispatcher,
        trigger="manual",
    )
    snapshot = list(messages)
    before = len(snapshot)
    out = await comp.compact(snapshot)
    # Detect real compaction by identity: compact() returns the SAME list it was
    # handed on every no-op path and a freshly built list only when it actually
    # summarized. A length check misfires when exactly one message is folded into
    # the summary (len unchanged) and would report "nothing to compact" despite a
    # completed — and paid-for — summarizer call.
    if out is not snapshot:
        return out, f"compacted {before} → {len(out)} messages"
    return list(messages), "nothing to compact yet — the conversation is still short"


# Bound what we feed the summarizer. A transcript large enough to need
# compaction can easily exceed the window it must be summarized *in* — sending
# it whole makes the summarizer call itself overflow, ``compact`` swallows the
# exception, and compaction silently no-ops exactly when it's needed most.
_MAX_RENDER_CHARS = 4_000
_MAX_PROMPT_CHARS = 240_000
_ELISION = "\n[... transcript middle elided — too large to summarize whole ...]\n"


def _truncate_middle(text: str, limit: int) -> str:
    """Keep the head and tail of ``text``, drop the middle. Intent lives at the
    start of a message and the outcome at the end; the bulk in between (a file
    dump, a page of HTML) is what we can afford to lose."""

    if len(text) <= limit:
        return text
    half = max(1, (limit - len(_ELISION)) // 2)
    return text[:half] + _ELISION + text[-half:]


def _build_summarization_prompt(messages: list[Message]) -> str:
    """Stitch messages into a textual prompt the summarizer model can chew on,
    bounded so the summarization call can't overflow the window itself."""

    rendered = [_truncate_middle(_render_message(m), _MAX_RENDER_CHARS) for m in messages]
    total = sum(len(r) for r in rendered) + len(_SUMMARIZER_INSTRUCTIONS)
    if total > _MAX_PROMPT_CHARS:
        # Keep the oldest turns (where the goal was set) and the newest (where
        # the current state lives); elide the middle.
        budget = _MAX_PROMPT_CHARS // 2
        head: list[str] = []
        used = 0
        for r in rendered:
            if used + len(r) > budget:
                break
            head.append(r)
            used += len(r)
        tail: list[str] = []
        used = 0
        for r in reversed(rendered[len(head):]):
            if used + len(r) > budget:
                break
            tail.append(r)
            used += len(r)
        rendered = [*head, _ELISION, *reversed(tail)]
    parts: list[str] = [_SUMMARIZER_INSTRUCTIONS, "\n--- Transcript ---\n", *rendered]
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
    if isinstance(blk, ImageBlock):
        # Never render the base64 — the whole point is to keep it out of the
        # summarizer prompt. The model only needs to know an image was here.
        return "(image)"
    if isinstance(blk, ToolResultBlock):
        if isinstance(blk.content, str):
            body = blk.content
        elif isinstance(blk.content, list):
            body = " ".join(c for c in (_render_block(b) for b in blk.content) if c)
        else:
            body = "<structured>"
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
