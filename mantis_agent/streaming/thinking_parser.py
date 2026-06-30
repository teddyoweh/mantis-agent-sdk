"""Inline thinking-tag splitter for reasoning-class OSS models.

The OSS reasoning-model landscape has fragmented into several tag conventions
that all mean the same thing — "this run of text is model-internal reasoning,
not part of the answer". This parser consolidates them into ONE normalized
event stream:

    ThinkingChunk(text)  — content inside any opening reasoning tag
    TextChunk(text)      — everything outside

Supported tags (by model)
-------------------------

  <think>...</think>          DeepSeek-R1, QwQ, R1-Distill-Qwen/Llama,
                              Marco-o1, OpenThinker, Granite reasoning,
                              SmolLM 3 reasoning, Phi-4 reasoning preview
  <thought>...</thought>       Hermes-Pro / Hermes-3 in reasoning mode
  <reasoning>...</reasoning>   Some Mistral fine-tunes, IBM Granite older
  <thinking>...</thinking>     Some Claude-style fine-tunes (alt syntax)
  <reflection>...</reflection> Reflection-70B class

Pass ``tags=`` to the constructor when a model uses a non-default pair.
``ModelCapability.inline_thinking_tags`` carries the per-model list so the
provider doesn't have to remember.

State machine: ``OUTSIDE`` ↔ ``INSIDE``. While ``OUTSIDE`` we scan for any
of the configured opening tags; while ``INSIDE`` we scan only for the
matching close tag (so a stray ``<thought>`` inside ``<think>`` is treated
as content, never as a nested mode flip).

The parser is *capability-gated* upstream: instantiated only when the model
declares inline thinking, so a non-reasoning Llama 3.1 pays exactly zero.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Union

import msgspec

__all__ = [
    "DEFAULT_THINKING_TAGS",
    "TextChunk",
    "ThinkingChunk",
    "ThinkingParser",
    "ThinkingParserEvent",
]


# ---------------------------------------------------------------------------
# Default tag set
# ---------------------------------------------------------------------------

# (opening_tag, closing_tag) pairs. Order matters only for tie-breaking on
# the same prefix — not relevant for these tags since they all differ
# after ``<``.
DEFAULT_THINKING_TAGS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("<thought>", "</thought>"),
    ("<reasoning>", "</reasoning>"),
    ("<thinking>", "</thinking>"),
    ("<reflection>", "</reflection>"),
)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TextChunk(msgspec.Struct, frozen=True, tag="text", tag_field="type"):
    """A run of plain text emitted outside any reasoning block."""

    text: str


class ThinkingChunk(msgspec.Struct, frozen=True, tag="thinking", tag_field="type"):
    """A run of model-internal reasoning emitted inside a reasoning block."""

    text: str


ThinkingParserEvent = Union[TextChunk, ThinkingChunk]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class ThinkingParser:
    """Streaming splitter that consolidates every supported reasoning-tag
    convention into one normalized ``Thinking``/``Text`` event stream.

    Construction
    ------------
    Default: accepts every tag pair in :data:`DEFAULT_THINKING_TAGS`. Pass
    ``tags=[("<custom>", "</custom>")]`` to restrict, or
    ``tags=ModelCapability.inline_thinking_tags`` to use the per-model list.

    Usage::

        parser = ThinkingParser()
        for delta in stream:
            for event in parser.feed(delta):
                handle(event)
        for event in parser.finalize():
            handle(event)
    """

    __slots__ = ("_inside_close", "_buf", "_open_tags", "_max_open_len", "_holdback")

    def __init__(
        self,
        tags: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        # Normalize input into a frozen pair-list for stable iteration.
        pairs = tuple(tags) if tags else DEFAULT_THINKING_TAGS
        if not pairs:
            raise ValueError("ThinkingParser requires at least one (open, close) pair")
        # Lowercase tags for case-insensitive matching against the stream —
        # OSS reasoning models sometimes capitalize tags.
        self._open_tags: tuple[tuple[str, str], ...] = tuple(
            (o.lower(), c.lower()) for o, c in pairs
        )
        self._max_open_len = max(len(o) for o, _ in self._open_tags)
        all_lens = [len(t) for pair in self._open_tags for t in pair]
        self._holdback = max(all_lens) - 1
        # When None, we're outside any block. When set, we're inside a block
        # whose close tag is this string.
        self._inside_close: str | None = None
        self._buf = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, text: str) -> Iterator[ThinkingParserEvent]:
        """Consume a chunk of text. Yields zero or more parser events."""

        if not text:
            return
        self._buf += text
        yield from self._drain()

    def finalize(self) -> Iterator[ThinkingParserEvent]:
        """Flush trailing buffer. An unterminated reasoning block is emitted
        as a ``ThinkingChunk`` (preserving content beats dropping it)."""

        if self._buf:
            if self._inside_close is not None:
                yield ThinkingChunk(text=self._buf)
            else:
                yield TextChunk(text=self._buf)
            self._buf = ""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _drain(self) -> Iterator[ThinkingParserEvent]:
        while True:
            if self._inside_close is None:
                # OUTSIDE: scan for the earliest opening tag.
                idx, matched_open, matched_close = self._find_earliest_open()
                if idx == -1:
                    # No full opening tag visible. Flush everything except a
                    # potential partial-tag tail (longest prefix of any
                    # opening tag at the buf's end).
                    safe = self._safe_flush_len_any_open()
                    if safe > 0:
                        chunk = self._buf[:safe]
                        self._buf = self._buf[safe:]
                        yield TextChunk(text=chunk)
                    return

                # Found an opening tag at idx. Emit preceding text + flip state.
                if idx > 0:
                    yield TextChunk(text=self._buf[:idx])
                self._buf = self._buf[idx + len(matched_open):]
                self._inside_close = matched_close
                # Loop — body of the block may contain a close tag immediately.
            else:
                # INSIDE: scan only for the configured close tag.
                close = self._inside_close
                idx = self._buf.lower().find(close)
                if idx == -1:
                    safe = _safe_flush_len(self._buf, close, self._holdback)
                    if safe > 0:
                        chunk = self._buf[:safe]
                        self._buf = self._buf[safe:]
                        yield ThinkingChunk(text=chunk)
                    return

                if idx > 0:
                    yield ThinkingChunk(text=self._buf[:idx])
                self._buf = self._buf[idx + len(close):]
                self._inside_close = None
                # Loop — there may be another opening tag in the remainder.

    def _find_earliest_open(self) -> tuple[int, str, str]:
        """Return ``(idx, open_tag, close_tag)`` for the earliest opening
        tag visible in the buffer. Case-insensitive match. ``idx`` is the
        position of the match in the *lowercased* buffer (which is the
        same as in the original buffer since the buffer is otherwise
        unchanged)."""

        haystack = self._buf.lower()
        best_idx = -1
        best_open = ""
        best_close = ""
        for o, c in self._open_tags:
            i = haystack.find(o)
            if i != -1 and (best_idx == -1 or i < best_idx):
                best_idx = i
                best_open = o
                best_close = c
        return best_idx, best_open, best_close

    def _safe_flush_len_any_open(self) -> int:
        """Largest ``n`` such that ``buf[:n]`` does not contain a partial
        opening tag at the tail (i.e. the trailing ``buf[n:]`` could still
        complete into some opening tag with future feeds)."""

        n = len(self._buf)
        haystack = self._buf.lower()
        # Try each opening tag; the smallest safe length wins.
        smallest = n
        for o, _ in self._open_tags:
            max_prefix = min(n, len(o) - 1, self._holdback)
            holdback_here = 0
            for k in range(max_prefix, 0, -1):
                if haystack.endswith(o[:k]):
                    holdback_here = k
                    break
            candidate = n - holdback_here
            if candidate < smallest:
                smallest = candidate
        return max(0, smallest)


# ---------------------------------------------------------------------------
# Helper — partial-suffix detection (case-insensitive)
# ---------------------------------------------------------------------------


def _safe_flush_len(buf: str, tag: str, holdback: int) -> int:
    n = len(buf)
    max_prefix = min(n, len(tag) - 1, holdback)
    haystack = buf.lower()
    for k in range(max_prefix, 0, -1):
        if haystack.endswith(tag[:k]):
            return n - k
    return n
